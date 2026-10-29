"""sources/gem.py — GeM (Government e-Marketplace) via Playwright.

GeM blocks all plain-HTTP requests from datacenter IPs (WAF returns 403
or a JS challenge). Playwright executes the JS challenge automatically and
works from GitHub Actions runners.

Requires: playwright (in requirements.txt) + chromium binary
(workflow: `python -m playwright install chromium --with-deps`).

Bid URL pattern: https://bidplus.gem.gov.in/showbidDocument/<id>
Also captured: https://bidplus.gem.gov.in/bidding/bid/... URLs.
"""
import re
from urllib.parse import quote_plus

from common import log

GEM_URLS = (
    "https://bidplus.gem.gov.in/all-bids-data",
    "https://bidplus.gem.gov.in/all-bids",
)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")


def fetch_gem(keywords, max_results=80):
    """Return list of {url, title, snippet} from GeM bid list.

    Returns [] on: playwright missing, page load timeout, no cards found,
    or any exception. Never raises.
    """
    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
    except ImportError:
        log.warning("[gem] playwright not installed — skipping (pip install playwright)")
        return []

    if not keywords:
        return []

    kw_lower = [str(k).lower().strip() for k in keywords if k]
    out = []
    seen = set()

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-setuid-sandbox",
                      "--disable-dev-shm-usage"],
            )
            ctx = browser.new_context(
                user_agent=UA,
                viewport={"width": 1440, "height": 900},
            )
            page = ctx.new_page()

            loaded = False
            for gem_url in GEM_URLS:
                try:
                    page.goto(gem_url, wait_until="networkidle", timeout=45000)
                    loaded = True
                    break
                except PWTimeout:
                    log.debug(f"[gem] timeout loading {gem_url}")
                    continue
                except Exception as e:
                    log.debug(f"[gem] load failed {gem_url}: {str(e)[:100]}")
                    continue

            if not loaded:
                log.warning("[gem] all URLs failed to load")
                browser.close()
                return []

            # Wait for bid cards. Multiple known selectors.
            got_cards = False
            for sel in (
                "a[href*='showbidDocument']",
                "a[href*='/bidding/bid/']",
                ".bid_card", ".card",
                "table tr td a",
            ):
                try:
                    page.wait_for_selector(sel, timeout=10000)
                    got_cards = True
                    break
                except PWTimeout:
                    continue
            if not got_cards:
                log.warning("[gem] no bid cards found — site may have restructured")
                browser.close()
                return []

            # Extract all anchors with bid-like hrefs
            items = page.evaluate("""
                () => {
                    const out = [];
                    const seen = new Set();
                    const hrefs = document.querySelectorAll(
                        "a[href*='showbidDocument'], a[href*='/bidding/bid/']"
                    );
                    hrefs.forEach(a => {
                        const href = a.getAttribute('href') || '';
                        const text = (a.textContent || '').trim();
                        if (href && text.length >= 10 && !seen.has(href)) {
                            seen.add(href);
                            out.push({href, text});
                        }
                    });
                    return out;
                }
            """)

            for item in items:
                href = (item.get("href") or "").strip()
                title = (item.get("text") or "").strip()
                if not href or not title:
                    continue
                if href.startswith("http"):
                    url = href
                elif href.startswith("/"):
                    url = "https://bidplus.gem.gov.in" + href
                else:
                    url = "https://bidplus.gem.gov.in/" + href
                if url in seen:
                    continue
                seen.add(url)
                if kw_lower and not any(k in title.lower() for k in kw_lower):
                    continue
                out.append({
                    "url": url,
                    "title": title[:300],
                    "snippet": title[:500],
                })
                if len(out) >= max_results:
                    break

            browser.close()

    except Exception as e:
        log.warning(f"[gem] unexpected error: {str(e)[:150]}")
        return []

    log.info(f"[gem] {len(out)} bid(s) matching keywords")
    return out
