import json
import logging
from playwright.sync_api import sync_playwright

logger = logging.getLogger("portal_crawler")

def crawl_all_pages(start_url, cookie_json_str="", max_pages=3):
    """
    Crawls up to `max_pages` of a portal in a headless Chromium instance.
    Injects session cookies directly from the Google Sheet settings.
    Handles page numbers ('1', '2', '3'), 'Next' links, and ASP.NET __doPostBack.
    """
    captured_html = []

    with sync_playwright() as p:
        # Launch headless Chromium with standard desktop viewport
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"]
        )
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
        )

        # 1. Inject cookies retrieved from Google Sheets Settings
        if cookie_json_str and cookie_json_str.strip():
            try:
                raw_cookies = json.loads(cookie_json_str)
                formatted_cookies = []
                for c in raw_cookies:
                    cookie_dict = {
                        "name": c["name"],
                        "value": c["value"],
                        "domain": c.get("domain", ""),
                        "path": c.get("path", "/")
                    }
                    formatted_cookies.append(cookie_dict)
                context.add_cookies(formatted_cookies)
            except Exception as e:
                logger.error(f"Error parsing session cookies in GitHub Action: {e}")

        page = context.new_page()

        try:
            page.goto(start_url, wait_until="networkidle", timeout=45000)
        except Exception as e:
            logger.error(f"Failed to load {start_url}: {e}")
            browser.close()
            return captured_html

        current_page_num = 1

        while current_page_num <= max_pages:
            captured_html.append(page.content())

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
