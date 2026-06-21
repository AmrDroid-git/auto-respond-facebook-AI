import os
import re
import json
import time
import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Any, List, Optional

from dotenv import load_dotenv
from playwright.sync_api import sync_playwright


# =========================
# ENV
# =========================

load_dotenv()


def env_str(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in ["1", "true", "yes", "y", "on"]


def env_int(name: str, default: int = 0) -> int:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    try:
        return int(value.strip())
    except ValueError:
        return default


COOKIES_FILE = env_str("COOKIES_TXT", "cookie.json")
FACEBOOK_POST_URL = env_str("FACEBOOK_POST_URL")
OUTPUT_JSON = env_str("OUTPUT_JSON", "facebook_comments.json")

HEADLESS = env_bool("HEADLESS", False)
MAX_SCROLLS = env_int("MAX_SCROLLS", 30)
SCROLL_WAIT_MS = env_int("SCROLL_WAIT_MS", 1500)
USE_MOBILE_FACEBOOK = env_bool("USE_MOBILE_FACEBOOK", False)

FORCE_ALL_COMMENTS = env_bool("FORCE_ALL_COMMENTS", True)
COMMENT_LIMIT = env_int("COMMENT_LIMIT", 0)
BROWSER_SLOW_MO_MS = env_int("BROWSER_SLOW_MO_MS", 0)

REPLY_TEXT = env_str("REPLY_TEXT", "thanks bro")

# Optional: put your page/profile names here to avoid replying to yourself.
# Example in .env:
# SELF_AUTHOR_NAMES=مستهلك الفوردز,Amr Slama
SELF_AUTHOR_NAMES = [
    x.strip().lower()
    for x in env_str("SELF_AUTHOR_NAMES", "").split(",")
    if x.strip()
]


# =========================
# UTILS
# =========================

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_text(value: str) -> str:
    return " ".join(value.strip().lower().split())


def make_comment_key(author: str, text: str) -> str:
    base = f"{author.strip()}|{text.strip()}"
    return hashlib.sha256(base.encode("utf-8")).hexdigest()[:24]


def convert_to_mobile_url(url: str) -> str:
    if "facebook.com" not in url:
        return url

    url = url.replace("https://www.facebook.com", "https://m.facebook.com")
    url = url.replace("http://www.facebook.com", "https://m.facebook.com")
    url = url.replace("https://facebook.com", "https://m.facebook.com")
    url = url.replace("http://facebook.com", "https://m.facebook.com")

    return url


def safe_inner_text(locator, timeout: int = 1500) -> str:
    try:
        return locator.inner_text(timeout=timeout).strip()
    except Exception:
        return ""


def safe_get_attr(locator, attr: str) -> str:
    try:
        value = locator.get_attribute(attr)
        return value.strip() if value else ""
    except Exception:
        return ""


# =========================
# COOKIES
# =========================

def normalize_same_site(value: Optional[str]) -> Optional[str]:
    if not value:
        return None

    v = value.strip().lower()

    if v in ["lax", "samesitelax"]:
        return "Lax"

    if v in ["strict", "samesitestrict"]:
        return "Strict"

    if v in ["none", "no_restriction", "no_restrictions", "samesitenone"]:
        return "None"

    return None


def load_facebook_cookies(cookie_path: str) -> List[Dict[str, Any]]:
    path = Path(cookie_path)

    if not path.exists():
        raise FileNotFoundError(f"Cookie file not found: {path.resolve()}")

    raw = path.read_text(encoding="utf-8").strip()

    if not raw:
        raise ValueError("Cookie file is empty.")

    data = json.loads(raw)

    if isinstance(data, dict):
        if "cookies" in data and isinstance(data["cookies"], list):
            cookies_raw = data["cookies"]
        else:
            cookies_raw = [data]
    elif isinstance(data, list):
        cookies_raw = data
    else:
        raise ValueError("Unsupported cookie JSON format.")

    cookies = []

    for c in cookies_raw:
        if not isinstance(c, dict):
            continue

        name = c.get("name")
        value = c.get("value")

        if not name or value is None:
            continue

        domain = c.get("domain") or ".facebook.com"
        path_value = c.get("path") or "/"

        cookie = {
            "name": str(name),
            "value": str(value),
            "domain": str(domain),
            "path": str(path_value),
        }

        expires = (
            c.get("expires")
            or c.get("expirationDate")
            or c.get("expiry")
            or c.get("expiration")
        )

        if expires:
            try:
                expires_float = float(expires)
                if expires_float > 0:
                    cookie["expires"] = expires_float
            except Exception:
                pass

        same_site = normalize_same_site(c.get("sameSite"))
        if same_site:
            cookie["sameSite"] = same_site

        if "secure" in c:
            cookie["secure"] = bool(c.get("secure"))

        if "httpOnly" in c:
            cookie["httpOnly"] = bool(c.get("httpOnly"))

        cookies.append(cookie)

    cookie_names = [c["name"] for c in cookies]

    print(f"[+] Loaded {len(cookies)} cookies.")
    print(f"[+] Cookie names: {cookie_names}")

    if "c_user" not in cookie_names or "xs" not in cookie_names:
        print("[!] WARNING: cookie.json does not contain both c_user and xs.")
        print("[!] Login may fail.")

    return cookies


# =========================
# LOG
# =========================

def load_log(path: str) -> Dict[str, Any]:
    p = Path(path)

    if not p.exists():
        return {
            "post_url": FACEBOOK_POST_URL,
            "created_at": now_iso(),
            "updated_at": now_iso(),
            "replied": {},
        }

    try:
        data = json.loads(p.read_text(encoding="utf-8"))

        if "replied" not in data or not isinstance(data["replied"], dict):
            data["replied"] = {}

        return data

    except Exception:
        return {
            "post_url": FACEBOOK_POST_URL,
            "created_at": now_iso(),
            "updated_at": now_iso(),
            "replied": {},
        }


def save_log(path: str, data: Dict[str, Any]) -> None:
    data["updated_at"] = now_iso()

    Path(path).write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


# =========================
# FACEBOOK COMMENT HELPERS
# =========================

def extract_author(article) -> str:
    aria = safe_get_attr(article, "aria-label")

    patterns = [
        r"Comment by\s+(.+)",
        r"Commentaire de\s+(.+)",
        r"تعليق بواسطة\s+(.+)",
        r"تعليق من\s+(.+)",
    ]

    for pattern in patterns:
        match = re.search(pattern, aria, re.IGNORECASE)
        if match:
            author = match.group(1).strip()
            author = re.split(r"\s+(?:at|à|le|·|\d)", author)[0].strip()
            if author:
                return author

    links = article.locator("a[role='link'], a[href*='facebook.com']")
    count = min(links.count(), 10)

    ignored = {
        "like",
        "reply",
        "share",
        "hide",
        "j’aime",
        "répondre",
        "partager",
        "masquer",
        "see more",
        "voir plus",
    }

    for i in range(count):
        link = links.nth(i)

        try:
            if not link.is_visible(timeout=500):
                continue
        except Exception:
            continue

        text = safe_inner_text(link, timeout=700)
        cleaned = " ".join(text.split()).strip()

        if not cleaned:
            continue

        if cleaned.lower() in ignored:
            continue

        if len(cleaned) > 80:
            continue

        return cleaned

    return ""


def clean_comment_text(text: str) -> str:
    ignored_exact = {
        "like",
        "reply",
        "share",
        "hide",
        "j’aime",
        "répondre",
        "partager",
        "masquer",
        "see more",
        "voir plus",
        "modified",
    }

    lines = []

    for line in text.splitlines():
        cleaned = " ".join(line.split()).strip()

        if not cleaned:
            continue

        if cleaned.lower() in ignored_exact:
            continue

        lines.append(cleaned)

    return "\n".join(lines).strip()


def is_self_author(author: str) -> bool:
    author_norm = normalize_text(author)

    for self_name in SELF_AUTHOR_NAMES:
        if author_norm == normalize_text(self_name):
            return True

    return False


def is_probably_comment(article_text: str, author: str) -> bool:
    if not article_text or not author:
        return False

    if is_self_author(author):
        return False

    text_lower = article_text.lower()

    # Avoid replying to our own already-created replies.
    if REPLY_TEXT.lower() in text_lower:
        return False

    bad_phrases = [
        "write a comment",
        "comment as",
        "add a comment",
        "répondre en tant que",
        "écrire un commentaire",
        "most relevant",
        "top comments",
        "all comments",
        "tous les commentaires",
    ]

    if any(phrase in text_lower for phrase in bad_phrases):
        return False

    return True


def get_comment_articles(page):
    selectors = [
        "div[role='article'][aria-label*='Comment']",
        "div[role='article'][aria-label*='Commentaire']",
        "div[role='article']",
    ]

    for selector in selectors:
        locator = page.locator(selector)

        try:
            if locator.count() > 0:
                return locator
        except Exception:
            continue

    return page.locator("div[role='article']")


def find_reply_button(article):
    reply_texts = [
        "Reply",
        "Répondre",
        "رد",
    ]

    for text in reply_texts:
        locator = article.get_by_text(text, exact=True)

        try:
            count = min(locator.count(), 10)
        except Exception:
            continue

        for i in range(count):
            item = locator.nth(i)

            try:
                if item.is_visible(timeout=500):
                    return item
            except Exception:
                continue

    return None


# =========================
# IMPORTANT FIXED PART
# =========================

def mark_existing_editors(page) -> None:
    """
    Mark all text editors before clicking Reply.
    After clicking Reply, the new reply editor should be focused.
    """
    try:
        page.evaluate(
            """
            () => {
                document.querySelectorAll('[contenteditable="true"]').forEach((el) => {
                    el.setAttribute('data-bot-editor-before-reply', '1');
                });
            }
            """
        )
    except Exception:
        pass


def get_active_contenteditable(page):
    """
    After clicking Facebook Reply, the correct reply box is usually focused.
    This avoids typing into the main comment box.
    """
    try:
        handle = page.evaluate_handle(
            """
            () => {
                const active = document.activeElement;

                if (!active) {
                    return null;
                }

                if (active.matches && active.matches('[contenteditable="true"]')) {
                    return active;
                }

                if (active.closest) {
                    return active.closest('[contenteditable="true"]');
                }

                return null;
            }
            """
        )

        element = handle.as_element()

        if element:
            return element

    except Exception:
        pass

    return None


def find_new_reply_editor(page, article):
    """
    First: use focused editor.
    Second: search only for a new editor created after clicking Reply.
    """
    active_editor = get_active_contenteditable(page)

    if active_editor:
        return active_editor, True

    selectors = [
        '[contenteditable="true"]:not([data-bot-editor-before-reply="1"])',
        'div[role="textbox"][contenteditable="true"]:not([data-bot-editor-before-reply="1"])',
    ]

    scopes = [article, page]

    for scope in scopes:
        for selector in selectors:
            locator = scope.locator(selector)

            try:
                count = locator.count()
            except Exception:
                continue

            for i in range(count - 1, -1, -1):
                item = locator.nth(i)

                try:
                    if item.is_visible(timeout=700):
                        return item, False
                except Exception:
                    continue

    return None, False


def get_editor_text(editor) -> str:
    try:
        return editor.evaluate(
            """
            (el) => {
                return (el.innerText || el.textContent || '').trim();
            }
            """
        )
    except Exception:
        return ""


def editor_is_focused(editor) -> bool:
    try:
        return bool(
            editor.evaluate(
                """
                (el) => {
                    return el === document.activeElement || el.contains(document.activeElement);
                }
                """
            )
        )
    except Exception:
        return False


def submit_reply(page, article, author: str) -> bool:
    reply_button = find_reply_button(article)

    if reply_button is None:
        return False

    try:
        mark_existing_editors(page)

        reply_button.click(timeout=2000)
        time.sleep(1)

        editor, was_focused = find_new_reply_editor(page, article)

        if editor is None:
            print(f"[!] Could not find focused reply box for {author}")
            return False

        # Do not click again if Facebook already focused the correct reply box.
        # Clicking again can break the default selected mention.
        if not was_focused and not editor_is_focused(editor):
            editor.click(timeout=1500)
            time.sleep(0.3)

        existing_text = get_editor_text(editor)

        # Facebook often inserts/selects the person's name automatically.
        # We press End to keep that default mention, then type only " thanks bro".
        if existing_text:
            page.keyboard.press("End")
            time.sleep(0.2)
            page.keyboard.type(f" {REPLY_TEXT}", delay=35)
        else:
            page.keyboard.type(REPLY_TEXT, delay=35)

        time.sleep(0.5)

        # In Facebook comment/reply boxes, Enter sends the reply.
        page.keyboard.press("Enter")
        time.sleep(2)

        return True

    except Exception as e:
        print(f"[!] Reply failed for {author}: {e}")
        return False


# =========================
# EXPAND COMMENTS
# =========================

def click_first_visible_text(page, texts: List[str], timeout: int = 700) -> bool:
    for text in texts:
        locators = [
            page.get_by_text(text, exact=True),
            page.get_by_text(text, exact=False),
        ]

        for locator in locators:
            try:
                count = min(locator.count(), 10)
            except Exception:
                continue

            for i in range(count):
                item = locator.nth(i)

                try:
                    if item.is_visible(timeout=timeout):
                        item.click(timeout=timeout)
                        return True
                except Exception:
                    continue

    return False


def choose_all_comments(page) -> None:
    print("[+] Trying to switch comment filter to All comments...")

    filter_texts = [
        "Most relevant",
        "Top comments",
        "Newest",
        "Oldest",
        "Les plus pertinents",
        "Plus pertinents",
        "Tous les commentaires",
    ]

    all_comments_texts = [
        "All comments",
        "Tous les commentaires",
        "Tous",
    ]

    clicked_filter = click_first_visible_text(page, filter_texts, timeout=1000)

    if clicked_filter:
        time.sleep(1)
        clicked_all = click_first_visible_text(page, all_comments_texts, timeout=1500)

        if clicked_all:
            print("[+] Selected All comments.")
        else:
            print("[!] Could not select All comments. Continuing anyway.")
    else:
        print("[!] Comment filter not found. Continuing anyway.")


def expand_comments(page) -> None:
    expand_texts = [
        "View more comments",
        "View previous comments",
        "See more comments",
        "View more replies",
        "See more replies",
        "View more",
        "See more",
        "Afficher plus de commentaires",
        "Voir plus de commentaires",
        "Voir les commentaires précédents",
        "Voir plus de réponses",
        "Afficher plus de réponses",
        "Voir plus",
        "عرض المزيد من التعليقات",
        "عرض المزيد من الردود",
        "عرض المزيد",
    ]

    clicked_any = True
    rounds = 0

    while clicked_any and rounds < 4:
        clicked_any = False
        rounds += 1

        for text in expand_texts:
            locator = page.get_by_text(text, exact=False)

            try:
                count = min(locator.count(), 20)
            except Exception:
                continue

            for i in range(count):
                item = locator.nth(i)

                try:
                    if item.is_visible(timeout=400):
                        item.click(timeout=700)
                        clicked_any = True
                        time.sleep(0.35)
                except Exception:
                    continue


# =========================
# PROCESS COMMENTS
# =========================

def process_visible_comments(page, log: Dict[str, Any]) -> int:
    articles = get_comment_articles(page)

    try:
        total = articles.count()
    except Exception:
        total = 0

    print(f"[+] Visible article/comment blocks: {total}")

    replied_now = 0

    for i in range(total):
        if COMMENT_LIMIT > 0 and len(log["replied"]) >= COMMENT_LIMIT:
            print("[+] COMMENT_LIMIT reached.")
            return replied_now

        article = articles.nth(i)

        try:
            if not article.is_visible(timeout=700):
                continue
        except Exception:
            continue

        raw_text = safe_inner_text(article, timeout=1500)
        comment_text = clean_comment_text(raw_text)
        author = extract_author(article)

        if not is_probably_comment(comment_text, author):
            continue

        comment_key = make_comment_key(author, comment_text)

        if comment_key in log["replied"]:
            continue

        print(f"[+] Replying to: {author}")

        success = submit_reply(page, article, author)

        if success:
            log["replied"][comment_key] = {
                "author": author,
                "reply_text_typed": REPLY_TEXT,
                "note": "Facebook default reply mention was kept; script typed only the reply text.",
                "comment_snippet": comment_text[:500],
                "replied_at": now_iso(),
            }

            save_log(OUTPUT_JSON, log)

            replied_now += 1
            print(f"[+] Replied to {author}")

            time.sleep(1.5)
        else:
            print(f"[!] Could not reply to {author}. Skipping.")

    return replied_now


# =========================
# MAIN
# =========================

def main() -> None:
    if not FACEBOOK_POST_URL:
        raise ValueError("FACEBOOK_POST_URL is missing in .env")

    post_url = FACEBOOK_POST_URL

    if USE_MOBILE_FACEBOOK:
        post_url = convert_to_mobile_url(post_url)

    cookies = load_facebook_cookies(COOKIES_FILE)
    log = load_log(OUTPUT_JSON)

    print("[+] Starting browser...")
    print(f"[+] Post URL: {post_url}")
    print(f"[+] Headless: {HEADLESS}")
    print(f"[+] Reply text: {REPLY_TEXT}")

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=HEADLESS,
            slow_mo=BROWSER_SLOW_MO_MS,
        )

        context = browser.new_context(
            viewport={"width": 1366, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            locale="en-US",
        )

        context.add_cookies(cookies)

        page = context.new_page()

        print("[+] Opening Facebook post...")
        page.goto(post_url, wait_until="domcontentloaded", timeout=60000)
        time.sleep(5)

        if "login" in page.url.lower():
            print("[!] Facebook redirected to login.")
            print("[!] Cookies are probably invalid or expired.")
            browser.close()
            return

        if FORCE_ALL_COMMENTS:
            choose_all_comments(page)
            time.sleep(1)

        total_replied = 0

        for scroll_index in range(MAX_SCROLLS):
            print(f"[+] Scroll round {scroll_index + 1}/{MAX_SCROLLS}")

            expand_comments(page)

            replied_now = process_visible_comments(page, log)
            total_replied += replied_now

            if COMMENT_LIMIT > 0 and len(log["replied"]) >= COMMENT_LIMIT:
                break

            page.mouse.wheel(0, 1800)
            time.sleep(SCROLL_WAIT_MS / 1000)

        print("[+] Final pass...")
        expand_comments(page)
        total_replied += process_visible_comments(page, log)

        save_log(OUTPUT_JSON, log)

        print("====================================")
        print("[+] Done.")
        print(f"[+] New replies in this run: {total_replied}")
        print(f"[+] Total replied in log: {len(log['replied'])}")
        print(f"[+] Log file: {OUTPUT_JSON}")
        print("====================================")

        browser.close()


if __name__ == "__main__":
    main()