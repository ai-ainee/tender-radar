import json
import logging
from playwright.sync_api import sync_playwright
from common import _html_text, is_safe_url

logger = logging.getLogger("portal_crawler")

def crawl_all_pages(start_url, cookie_json_str="", max_pages=3):
    """
    Crawls up to `max_pages` of a portal in a headless Chromium instance.
    Injects session cookies directly from the Google Sheet settings.
    Handles page numbers ('1', '2', '3'), 'Next' links, and ASP.NET __doPostBack.
    """
    captured_html = []
    if not is_safe_url(start_url):
        logger.warning(f"Blocked unsafe portal URL: {start_url}")
        return captured_html

    with sync_playwright() as p:
        # Launch headless Chromium with standard desktop viewport
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"]
        )
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
        )

        # 1. Inject cookies retrieved from Google Sheets Settings (bad entries are skipped, not fatal)
        if cookie_json_str and cookie_json_str.strip():
            try:
                raw_cookies = json.loads(cookie_json_str)
            except Exception as e:
                logger.error(f"Error parsing session cookies: {e}")
                raw_cookies = []
            added = 0
            for c in raw_cookies if isinstance(raw_cookies, list) else []:
                try:
                    if not c.get("name") or not c.get("domain"):
                        continue
                    ck = {"name": c["name"], "value": str(c.get("value", "")),
                          "domain": c["domain"], "path": c.get("path", "/")}
                    exp = c.get("expires", c.get("expirationDate"))
                    if isinstance(exp, (int, float)) and exp > 0: ck["expires"] = exp
                    for k in ("secure", "httpOnly"):
                        if isinstance(c.get(k), bool): ck[k] = c[k]
                    ss = str(c.get("sameSite", "")).capitalize()
                    if ss in ("Strict", "Lax", "None"): ck["sameSite"] = ss
                    context.add_cookies([ck])
                    added += 1
                except Exception as e:
                    logger.warning(f"Skipped bad cookie {c.get('name') if isinstance(c, dict) else '?'}: {e}")
            logger.info(f"Injected {added} session cookie(s)")

        page = context.new_page()

        try:
            page.goto(start_url, wait_until="networkidle", timeout=45000)
        except Exception as e:
            logger.error(f"Failed to load {start_url}: {e}")
            browser.close()
            return captured_html

        current_page_num = 1

        while current_page_num <= max_pages:
            raw_html = page.content()
            cleaned_page = _html_text(raw_html.encode("utf-8", errors="ignore"), "utf-8", 6000)
            if cleaned_page:
                captured_html.append(cleaned_page)

            if current_page_num >= max_pages:
                break

            target_next_page = current_page_num + 1
            advanced = False

            # Strategy A: Numbered Page Buttons ('2', '3', '4')
            page_btn = page.locator(
                f"//a[normalize-space(text())='{target_next_page}'] | "
                f"//input[@value='{target_next_page}'] | "
                f"//button[normalize-space(text())='{target_next_page}'] | "
                f"//span[contains(@class, 'page') and normalize-space(text())='{target_next_page}']"
            ).first

            if page_btn.count() > 0 and page_btn.is_visible():
                try:
                    page_btn.click()
                    page.wait_for_load_state("networkidle")
                    page.wait_for_timeout(2500)
                    advanced = True
                except Exception:
                    advanced = False

            # Strategy B: 'Next' / '>' Text or ARIA controls
            if not advanced:
                next_btn = page.locator(
                    "//a[contains(translate(text(), 'NEXT', 'next'), 'next') or contains(text(), '>') or contains(text(), '»')] | "
                    "//input[contains(translate(@value, 'NEXT', 'next'), 'next')] | "
                    "//button[contains(translate(text(), 'NEXT', 'next'), 'next') or contains(text(), '>') or contains(text(), '»')] | "
                    "//*[@aria-label='Next' or @aria-label='Next page' or @title='Next']"
                ).first

                if next_btn.count() > 0 and next_btn.is_visible():
                    try:
                        next_btn.click()
                        page.wait_for_load_state("networkidle")
                        page.wait_for_timeout(2500)
                        advanced = True
                    except Exception:
                        advanced = False

            # Strategy C: ASP.NET __doPostBack targets
            if not advanced:
                postback_elem = page.locator(
                    f"//*[contains(@href, '__doPostBack') and contains(@href, 'Page${target_next_page}')]"
                ).first

                if postback_elem.count() > 0:
                    try:
                        postback_elem.click()
                        page.wait_for_load_state("networkidle")
                        page.wait_for_timeout(2500)
                        advanced = True
                    except Exception:
                        advanced = False

            if not advanced:
                break

            current_page_num += 1

        browser.close()

    return captured_html
