import argparse
import copy
import json
import os
import re
import time
from pathlib import Path

from dotenv import load_dotenv, find_dotenv
from playwright.sync_api import (
    sync_playwright,
    Error as PlaywrightError,
    TimeoutError as PlaywrightTimeoutError,
)


# ==========================================================
# ENV HELPERS
# ==========================================================

def load_env_file() -> Path:
    env_path = find_dotenv(usecwd=True)

    if env_path:
        load_dotenv(env_path)
        print(f"[+] Loaded .env from: {env_path}")
        return Path(env_path).resolve().parent

    load_dotenv()
    print("[!] No .env found. Using current working directory.")
    return Path.cwd()


def resolve_cookie_path_from_env(env_dir: Path) -> Path:
    value = os.getenv("COOKIES_TXT") or os.getenv("COOKIES_FILE")

    if not value:
        raise RuntimeError("Missing COOKIES_TXT in .env")

    raw = value.strip().strip('"').strip("'")
    p = Path(raw)

    candidates = []

    if p.is_absolute():
        candidates.append(p)
    else:
        candidates.append(env_dir / p)
        candidates.append(Path.cwd() / p)
        candidates.append(Path(__file__).resolve().parent / p)

    for candidate in candidates:
        if candidate.exists():
            return candidate.resolve()

    tried = "\n".join(f"- {x.resolve()}" for x in candidates)

    raise FileNotFoundError(
        f"Cookie file not found from COOKIES_TXT={value}\n\n"
        f"Tried:\n{tried}"
    )


# ==========================================================
# COMMON HELPERS
# ==========================================================

def parse_bool_text(value) -> bool:
    return str(value).strip().upper() in {"TRUE", "YES", "1", "ON"}


def get_any(d: dict, *keys, default=None):
    lower = {str(k).lower(): v for k, v in d.items()}

    for key in keys:
        if key in d:
            return d[key]

        key_lower = str(key).lower()

        if key_lower in lower:
            return lower[key_lower]

    return default


def is_facebook_domain(domain: str) -> bool:
    d = domain.lower().lstrip(".")
    return d == "facebook.com" or d.endswith(".facebook.com")


def clean_domain(domain: str | None, url: str | None = None, include_subdomains: bool = False) -> str | None:
    if not domain and url:
        try:
            from urllib.parse import urlparse
            domain = urlparse(url).hostname
        except Exception:
            domain = None

    if not domain:
        return None

    domain = str(domain).strip()

    if domain.startswith("#HttpOnly_"):
        domain = domain.replace("#HttpOnly_", "", 1)

    domain = domain.replace("https://", "").replace("http://", "")
    domain = domain.split("/")[0]
    domain = domain.split(":")[0]
    domain = domain.strip().lower()

    if not domain:
        return None

    domain_without_dot = domain.lstrip(".")

    if not is_facebook_domain(domain_without_dot):
        return None

    if include_subdomains and not domain.startswith("."):
        domain = "." + domain

    return domain


def cookie_is_expired(expires_value) -> bool:
    if expires_value in {None, "", -1, "-1"}:
        return False

    try:
        expires_int = int(float(expires_value))

        if expires_int <= 0:
            return False

        return expires_int < int(time.time())

    except Exception:
        return False


# ==========================================================
# JSON COOKIE PARSER
# ==========================================================

def find_cookie_objects(obj) -> list[dict]:
    """
    Recursively finds cookie objects inside JSON.

    Supports:
    - Cookie-Editor exports
    - EditThisCookie exports
    - Playwright storage_state exports
    - nested JSON formats
    """
    found = []

    if isinstance(obj, dict):
        keys = {str(k).lower() for k in obj.keys()}

        if "name" in keys and "value" in keys:
            found.append(obj)

        for value in obj.values():
            found.extend(find_cookie_objects(value))

    elif isinstance(obj, list):
        for item in obj:
            found.extend(find_cookie_objects(item))

    return found


def build_cookie_from_json(raw: dict) -> dict | None:
    name = get_any(raw, "name")

    if not name:
        return None

    name = str(name).strip()

    if not name:
        return None

    value = get_any(raw, "value", default="")

    domain = get_any(raw, "domain", "host")
    url = get_any(raw, "url")

    host_only = bool(get_any(raw, "hostOnly", "host_only", default=False))
    include_subdomains = not host_only

    domain = clean_domain(
        domain=domain,
        url=url,
        include_subdomains=include_subdomains,
    )

    if not domain:
        return None

    expires = get_any(
        raw,
        "expires",
        "expirationDate",
        "expiration",
        "expiry",
        default=None,
    )

    if cookie_is_expired(expires):
        return None

    path = get_any(raw, "path", default="/")

    if not path:
        path = "/"

    path = str(path)

    if not path.startswith("/"):
        path = "/"

    secure = bool(get_any(raw, "secure", default=True))
    http_only = bool(get_any(raw, "httpOnly", "http_only", default=False))

    return {
        "name": name,
        "value": "" if value is None else str(value),
        "domain": domain,
        "path": path,
        "secure": secure,
        "httpOnly": http_only,
    }


def load_json_cookies(path: Path) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8", errors="ignore"))

    raw_cookies = find_cookie_objects(data)

    print(f"[+] Raw cookie objects found in JSON: {len(raw_cookies)}")

    cookies = []
    skipped = 0

    for raw in raw_cookies:
        cookie = build_cookie_from_json(raw)

        if cookie:
            cookies.append(cookie)
        else:
            skipped += 1

    print(f"[+] Valid Facebook JSON cookies: {len(cookies)}")
    print(f"[+] Skipped JSON cookies: {skipped}")

    return cookies


# ==========================================================
# NETSCAPE cookies.txt PARSER
# ==========================================================

def build_cookie_from_netscape_line(line: str) -> dict | None:
    http_only = False

    if line.startswith("#HttpOnly_"):
        http_only = True
        line = line.replace("#HttpOnly_", "", 1)
    elif line.startswith("#"):
        return None

    parts = line.split("\t")

    if len(parts) < 7:
        parts = re.split(r"\s+", line, maxsplit=6)

    if len(parts) < 7:
        return None

    domain_raw, include_subdomains_raw, path_raw, secure_raw, expires_raw, name_raw, value_raw = parts[:7]

    include_subdomains = parse_bool_text(include_subdomains_raw)

    domain = clean_domain(
        domain=domain_raw,
        include_subdomains=include_subdomains,
    )

    if not domain:
        return None

    if cookie_is_expired(expires_raw):
        return None

    name = name_raw.strip()

    if not name:
        return None

    path = path_raw.strip() or "/"

    if not path.startswith("/"):
        path = "/"

    return {
        "name": name,
        "value": value_raw,
        "domain": domain,
        "path": path,
        "secure": parse_bool_text(secure_raw),
        "httpOnly": http_only,
    }


def load_netscape_cookies(path: Path) -> list[dict]:
    cookies = []
    skipped = 0

    with path.open("r", encoding="utf-8", errors="ignore") as f:
        for raw_line in f:
            line = raw_line.strip()

            if not line:
                continue

            cookie = build_cookie_from_netscape_line(line)

            if cookie:
                cookies.append(cookie)
            else:
                skipped += 1

    print(f"[+] Valid Facebook Netscape cookies: {len(cookies)}")
    print(f"[+] Skipped Netscape lines: {skipped}")

    return cookies


# ==========================================================
# AUTO-DETECT COOKIE FILE FORMAT
# ==========================================================

def load_cookie_file(path: Path) -> list[dict]:
    if not path.exists():
        raise FileNotFoundError(f"Cookie file not found: {path}")

    start = path.read_text(encoding="utf-8", errors="ignore").lstrip()[:10]

    if start.startswith("[") or start.startswith("{"):
        print("[+] Detected JSON cookie file")
        cookies = load_json_cookies(path)
    else:
        print("[+] Detected Netscape cookies.txt file")
        cookies = load_netscape_cookies(path)

    names = sorted({c["name"] for c in cookies})

    print("\n[+] Cookie names loaded:")
    print(names)

    if "c_user" not in names or "xs" not in names:
        print("\n[!] WARNING:")
        print("[!] c_user and/or xs missing.")
        print("[!] Facebook login probably will not work.\n")
    else:
        print("\n[+] c_user and xs exist in loaded cookies.\n")

    return cookies


# ==========================================================
# PLAYWRIGHT COOKIE INJECTION
# ==========================================================

def cookie_domain_variants(cookie: dict) -> list[dict]:
    variants = [cookie]

    domain = cookie.get("domain", "")

    if domain.startswith("."):
        no_dot = copy.deepcopy(cookie)
        no_dot["domain"] = domain.lstrip(".")
        variants.append(no_dot)
    else:
        with_dot = copy.deepcopy(cookie)
        with_dot["domain"] = "." + domain
        variants.append(with_dot)

    return variants


def add_cookies_safely(context, cookies: list[dict]) -> None:
    if not cookies:
        raise RuntimeError("No cookies to add.")

    try:
        context.add_cookies(cookies)
        print(f"[+] Added cookies in batch: {len(cookies)}")
        return
    except PlaywrightError as e:
        print("[!] Batch cookie add failed. Trying one by one...")
        print(f"[!] Reason: {str(e).splitlines()[0]}")

    added = 0
    failed = []

    for cookie in cookies:
        ok = False

        for variant in cookie_domain_variants(cookie):
            try:
                context.add_cookies([variant])
                added += 1
                ok = True
                break
            except PlaywrightError:
                continue

        if not ok:
            failed.append(
                {
                    "name": cookie.get("name"),
                    "domain": cookie.get("domain"),
                }
            )

    print(f"[+] Added cookies one by one: {added}")
    print(f"[+] Failed cookies: {len(failed)}")

    if failed:
        print("[!] Failed cookies:")
        for item in failed:
            print(f"    - {item['name']} | {item['domain']}")

    if added == 0:
        raise RuntimeError("Could not add any cookie to Chromium.")


def debug_context_cookies(context) -> None:
    urls = [
        "https://facebook.com/",
        "https://www.facebook.com/",
        "https://m.facebook.com/",
    ]

    all_names = set()

    for url in urls:
        try:
            cookies = context.cookies(url)
            names = {c["name"] for c in cookies}
            all_names.update(names)

            print(f"[+] Cookies visible to {url}:")
            print(sorted(names))

        except Exception as e:
            print(f"[!] Could not read cookies for {url}: {e}")

    print("\n[+] Combined visible cookie names:")
    print(sorted(all_names))

    if "c_user" in all_names:
        print("[+] c_user found inside Chromium context")
    else:
        print("[!] c_user NOT found inside Chromium context")

    if "xs" in all_names:
        print("[+] xs found inside Chromium context")
    else:
        print("[!] xs NOT found inside Chromium context")

    print()


# ==========================================================
# FACEBOOK LOGIN CHECK
# ==========================================================

def check_login_form(page) -> bool:
    try:
        email_count = page.locator('input[name="email"]').count()
        password_count = page.locator('input[name="pass"]').count()

        return email_count > 0 or password_count > 0

    except Exception:
        return False


# ==========================================================
# MAIN
# ==========================================================

def main() -> None:
    env_dir = load_env_file()

    parser = argparse.ArgumentParser(
        description="Test Facebook cookies JSON/txt integration with Chromium"
    )

    parser.add_argument(
        "--cookies",
        default=None,
        help="Optional override for cookie file path. If not provided, COOKIES_TXT from .env is used.",
    )

    parser.add_argument(
        "--url",
        default=None,
        help="URL to open after cookies are injected. If not provided, FACEBOOK_TEST_URL or Facebook homepage is used.",
    )

    parser.add_argument(
        "--headless",
        action="store_true",
        help="Run Chromium in headless mode",
    )

    parser.add_argument(
        "--close",
        action="store_true",
        help="Close Chromium automatically after test",
    )

    args = parser.parse_args()

    if args.cookies:
        cookies_path = Path(args.cookies).resolve()
    else:
        cookies_path = resolve_cookie_path_from_env(env_dir)

    test_url = (
        args.url
        or os.getenv("FACEBOOK_TEST_URL")
        or os.getenv("FACEBOOK_POST_URL")
        or "https://www.facebook.com/"
    )

    print(f"[+] Cookies file: {cookies_path}")
    print(f"[+] Test URL: {test_url}")
    print(f"[+] Headless: {args.headless}")

    cookies = load_cookie_file(cookies_path)

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=args.headless,
            slow_mo=100,
        )

        context = browser.new_context(
            viewport={"width": 1366, "height": 900},
            locale="en-US",
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
        )

        add_cookies_safely(context, cookies)

        print()
        debug_context_cookies(context)

        page = context.new_page()

        print(f"[+] Opening: {test_url}")
        page.goto(test_url, wait_until="domcontentloaded", timeout=60000)

        try:
            page.wait_for_load_state("networkidle", timeout=15000)
        except PlaywrightTimeoutError:
            pass

        page.wait_for_timeout(3000)

        if check_login_form(page):
            print("[!] Facebook login form is visible.")
            print("[!] Cookies were injected, but Facebook did NOT accept them as a logged-in session.")
        else:
            print("[+] Login form is not visible.")
            print("[+] Cookie integration looks good.")

        if not args.close and not args.headless:
            input("\nPress ENTER to close Chromium...")

        context.close()
        browser.close()


if __name__ == "__main__":
    main()