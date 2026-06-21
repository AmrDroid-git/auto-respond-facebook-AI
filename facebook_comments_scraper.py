import json
import os
import time
from pathlib import Path

from dotenv import load_dotenv, find_dotenv
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError, Error as PlaywrightError


def load_env() -> None:
    env_path = find_dotenv(usecwd=True)

    if env_path:
        load_dotenv(env_path)
        print(f"[+] Loaded .env from: {env_path}")
    else:
        load_dotenv()
        print("[!] No .env file found. Using environment variables only.")


def env_required(name: str) -> str:
    value = os.getenv(name)

    if not value:
        raise RuntimeError(f"Missing env variable: {name}")

    return value


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)

    if value is None:
        return default

    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def env_int(name: str, default: int) -> int:
    value = os.getenv(name)

    if value is None:
        return default

    try:
        return int(value)
    except ValueError:
        return default


def click_exact_visible_text(page, labels: list[str]) -> str | None:
    """
    Finds the smallest visible element whose first text line equals one of labels,
    then clicks its center with the real mouse.
    This works better on Facebook dropdowns than el.click().
    """
    result = page.evaluate(
        """
        (labels) => {
            const wanted = labels.map(x => x.toLowerCase());

            function visible(el) {
                const r = el.getBoundingClientRect();
                const s = window.getComputedStyle(el);

                return r.width > 0 &&
                       r.height > 0 &&
                       s.display !== "none" &&
                       s.visibility !== "hidden";
            }

            const nodes = Array.from(document.querySelectorAll("div, span, a, button"));

            let best = null;

            for (const el of nodes) {
                if (!visible(el)) continue;

                const raw = (
                    el.innerText ||
                    el.textContent ||
                    el.getAttribute("aria-label") ||
                    ""
                ).trim();

                if (!raw) continue;

                const firstLine = raw.split("\\n")[0].trim();
                const normalized = firstLine.toLowerCase();

                if (!wanted.includes(normalized)) continue;

                const r = el.getBoundingClientRect();
                const area = r.width * r.height;

                if (area <= 0) continue;
                if (area > 100000) continue;

                if (!best || area < best.area) {
                    best = {
                        text: firstLine,
                        x: r.left + r.width / 2,
                        y: r.top + r.height / 2,
                        area
                    };
                }
            }

            return best;
        }
        """,
        labels,
    )

    if not result:
        return None

    page.mouse.click(result["x"], result["y"])
    return result["text"]


def click_contains_visible_text(page, patterns: list[str]) -> int:
    """
    Clicks visible elements containing one of the given text patterns.
    Used for View more comments / replies / See more.
    """
    clicked_items = page.evaluate(
        """
        (patterns) => {
            const wanted = patterns.map(x => x.toLowerCase());

            function visible(el) {
                const r = el.getBoundingClientRect();
                const s = window.getComputedStyle(el);

                return r.width > 0 &&
                       r.height > 0 &&
                       s.display !== "none" &&
                       s.visibility !== "hidden";
            }

            const nodes = Array.from(document.querySelectorAll(
                "div[role='button'], span[role='button'], a[role='button'], button, a, div, span"
            ));

            const clicked = [];

            for (const el of nodes) {
                if (!visible(el)) continue;

                const raw = (
                    el.innerText ||
                    el.textContent ||
                    el.getAttribute("aria-label") ||
                    ""
                ).trim();

                if (!raw) continue;

                const text = raw.toLowerCase();

                if (!wanted.some(w => text.includes(w))) continue;

                const r = el.getBoundingClientRect();

                if (r.width * r.height > 120000) continue;

                try {
                    const clickable = el.closest("[role='button'], [role='menuitem'], button, a") || el;
                    clickable.click();
                    clicked.push(raw);
                } catch (e) {}
            }

            return clicked;
        }
        """,
        patterns,
    )

    return len(clicked_items or [])


def select_all_comments(page) -> None:
    print("[+] Trying to select All comments...")

    opened = click_exact_visible_text(
        page,
        [
            "Most relevant",
            "Newest",
            "Les plus pertinents",
            "Plus récents",
            "Plus récent",
            "الأكثر ملاءمة",
            "الأحدث",
        ],
    )

    if opened:
        print(f"[+] Opened comments menu: {opened}")
        page.wait_for_timeout(800)
    else:
        print("[!] Could not open comments menu. Maybe it is not visible yet.")

    selected = click_exact_visible_text(
        page,
        [
            "All comments",
            "Tous les commentaires",
            "كل التعليقات",
        ],
    )

    if selected:
        print(f"[+] Selected comments filter: {selected}")
        page.wait_for_timeout(1500)
        return

    # fallback: contains search
    fallback_clicked = click_contains_visible_text(
        page,
        [
            "all comments",
            "tous les commentaires",
            "كل التعليقات",
        ],
    )

    if fallback_clicked:
        print("[+] Selected All comments using fallback click.")
        page.wait_for_timeout(1500)
    else:
        print("[!] Could not select All comments automatically.")


def click_more_comments_and_replies(page) -> int:
    return click_contains_visible_text(
        page,
        [
            "view more comments",
            "view previous comments",
            "show more comments",
            "more comments",
            "previous comments",
            "view replies",
            "view more replies",
            "see more",
            "afficher plus de commentaires",
            "voir plus de commentaires",
            "commentaires précédents",
            "voir les réponses",
            "voir plus de réponses",
            "voir plus",
            "عرض المزيد",
            "عرض الردود",
        ],
    )


def scroll_inside_post(page) -> None:
    viewport = page.viewport_size or {"width": 1366, "height": 900}

    x = viewport["width"] * 0.55
    y = viewport["height"] * 0.75

    page.mouse.move(x, y)
    page.mouse.wheel(0, 1700)


def extract_comments(page) -> list[dict]:
    """
    Works for logged-out Facebook public post view.

    Main strategy:
    - Facebook comments usually appear in grey rounded bubbles.
    - The bubble text is usually:
        Person Name
        Comment text
    """
    return page.evaluate(
        """
        () => {
            const results = [];
            const seen = new Set();

            function visible(el) {
                const r = el.getBoundingClientRect();
                const s = window.getComputedStyle(el);

                return r.width > 0 &&
                       r.height > 0 &&
                       s.display !== "none" &&
                       s.visibility !== "hidden";
            }

            function cleanLines(text) {
                return text
                    .split("\\n")
                    .map(x => x.trim())
                    .filter(Boolean);
            }

            function isUiLine(x) {
                return /^(Like|Reply|Share|Comment|Send|Follow|Following|Author|Top fan|Edited|See more|Hide|Report|Translate|Facebook|Most relevant|Newest|All comments|Log in|Forgot Account\\?|J’aime|Répondre|Partager|Voir plus|Traduire|أعجبني|رد|مشاركة)$/i.test(x);
            }

            function isTimeLine(x) {
                return /^(\\d+\\s*)?(s|sec|m|min|h|hr|hrs|d|day|days|w|week|weeks|mo|month|months|y|yr|year|years|j|sem|mn)$/i.test(x);
            }

            function isReactionLine(x) {
                return /^[0-9,.Kk\\s]+[\\u{1F300}-\\u{1FAFF}\\u{2600}-\\u{27BF}\\s]*$/u.test(x);
            }

            function badPersonName(x) {
                if (!x) return true;
                if (x.length > 80) return true;
                if (/comments?|shares?|reactions?|facebook|log in|password|email/i.test(x)) return true;
                if (isUiLine(x)) return true;
                return false;
            }

            function addComment(person, comment) {
                person = person.replace(/\\s+/g, " ").trim();
                comment = comment.replace(/\\s+/g, " ").trim();

                if (badPersonName(person)) return;
                if (!comment || comment.length < 2) return;

                const key = person + "::" + comment;

                if (seen.has(key)) return;
                seen.add(key);

                results.push({
                    personwhocommented: person,
                    comment: comment
                });
            }

            // Strategy 1: grey rounded comment bubbles
            const allDivs = Array.from(document.querySelectorAll("div"));

            for (const el of allDivs) {
                if (!visible(el)) continue;

                const r = el.getBoundingClientRect();
                const s = window.getComputedStyle(el);

                const text = (el.innerText || "").trim();
                if (!text) continue;

                if (r.width < 50 || r.width > 700) continue;
                if (r.height < 25 || r.height > 350) continue;

                const bg = s.backgroundColor || "";
                const radius = parseFloat(s.borderRadius || "0");

                const looksLikeBubble =
                    radius >= 8 &&
                    bg !== "rgba(0, 0, 0, 0)" &&
                    bg !== "transparent" &&
                    bg !== "rgb(255, 255, 255)";

                if (!looksLikeBubble) continue;

                const lines = cleanLines(text);

                if (lines.length < 2) continue;

                const person = lines[0];

                const commentParts = [];

                for (const line of lines.slice(1)) {
                    if (isUiLine(line)) continue;
                    if (isTimeLine(line)) continue;
                    if (isReactionLine(line)) continue;
                    if (/^\\d+\\s*(like|likes|reply|replies)$/i.test(line)) continue;

                    commentParts.push(line);
                }

                addComment(person, commentParts.join(" "));
            }

            // Strategy 2: role article fallback
            const articles = Array.from(document.querySelectorAll("[role='article']"));

            for (const article of articles) {
                if (!visible(article)) continue;

                const text = (article.innerText || "").trim();
                if (!text) continue;

                const lines = cleanLines(text);
                if (lines.length < 2) continue;

                const person = lines[0];

                const commentParts = [];

                for (const line of lines.slice(1)) {
                    if (isUiLine(line)) continue;
                    if (isTimeLine(line)) continue;
                    if (isReactionLine(line)) continue;
                    if (/^\\d+\\s*(like|likes|reply|replies)$/i.test(line)) continue;

                    commentParts.push(line);
                }

                addComment(person, commentParts.join(" "));
            }

            return results;
        }
        """
    )


def main() -> None:
    load_env()

    post_url = env_required("FACEBOOK_POST_URL")
    output_json = os.getenv("OUTPUT_JSON", "facebook_comments.json")

    headless = env_bool("HEADLESS", False)
    max_scrolls = env_int("MAX_SCROLLS", 40)
    scroll_wait_ms = env_int("SCROLL_WAIT_MS", 1200)

    print(f"[+] Post URL: {post_url}")
    print(f"[+] Output JSON: {output_json}")
    print(f"[+] Headless: {headless}")

    all_comments = []
    seen = set()

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=headless,
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

        page = context.new_page()

        print("[+] Opening Facebook post without cookies...")
        page.goto(post_url, wait_until="domcontentloaded", timeout=60000)

        try:
            page.wait_for_load_state("networkidle", timeout=15000)
        except PlaywrightTimeoutError:
            pass

        page.wait_for_timeout(2500)

        select_all_comments(page)

        for i in range(max_scrolls):
            try:
                clicked = click_more_comments_and_replies(page)

                scroll_inside_post(page)
                time.sleep(scroll_wait_ms / 1000)

                comments = extract_comments(page)

            except PlaywrightError as e:
                print(f"[!] Playwright error: {str(e).splitlines()[0]}")
                break

            added = 0

            for c in comments:
                person = c["personwhocommented"].strip()
                comment = c["comment"].strip()

                key = person + "::" + comment

                if key in seen:
                    continue

                seen.add(key)

                all_comments.append(
                    {
                        "personwhocommented": person,
                        "comment": comment,
                    }
                )

                added += 1

            print(
                f"[+] Cycle {i + 1}/{max_scrolls} | "
                f"clicked={clicked} | new={added} | total={len(all_comments)}"
            )

        output_path = Path(output_json)
        output_path.write_text(
            json.dumps(all_comments, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        print(f"[+] Saved {len(all_comments)} comments to: {output_path.resolve()}")

        context.close()
        browser.close()


if __name__ == "__main__":
    main()