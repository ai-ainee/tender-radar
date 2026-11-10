"""Public GeM BidPlus listing collector (Step 23 diagnostic/navigation fix).

Only reads publicly accessible bid listing pages. Does not bypass access
controls, CAPTCHAs, or GeM login. Returns [] on unavailable sources.

Contract: fetch_gem(keywords, max_results=80) -> [{url,title,snippet}, ...]
"""
import re
from urllib.parse import urljoin, urlparse

from common import log

# The -data endpoint is a search-data endpoint, not a standalone HTML page.
GEM_URLS = (
    "https://bidplus.gem.gov.in/all-bids",
    "https://bidplus.gem.gov.in/bidlists",
)

BID_LINK = re.compile(r"/(?:showbidDocument/\d+|bidding/bid/[^?#]+)", re.I)
GEM_HOST = "bidplus.gem.gov.in"
BLOCK_INDICATORS = (
    "access denied", "captcha", "verify you are human", "request blocked",
    "attention required", "unusual traffic",
)


def _normalise_bid_link(href):
    if not href:
        return None
    url = urljoin("https://bidplus.gem.gov.in/", href.strip())
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname != GEM_HOST:
        return None
    if not BID_LINK.search(parsed.path):
        return None
    return url.split("#", 1)[0]


def _extract_items(page, keywords, max_results):
    # Text from the enclosing bid record is preferable to a button saying
    # "View Document"; searching the button text alone yields false negatives.
    raw = page.evaluate("""() => Array.from(document.querySelectorAll('a[href]'))
        .filter(a => /showbidDocument\\/\\d+|\\/bidding\\/bid\\//i.test(a.href))
        .map(a => {
            let node = a.closest('tr, .bid_card, .card, .bid-list, li') || a.parentElement || a;
            return {href: a.href, text: (node.innerText || a.innerText || '').trim().replace(/\\s+/g, ' ')};
        })""")
    terms = [str(k).casefold().strip() for k in keywords if str(k).strip()]
    output, seen = [], set()
    for item in raw or []:
        url = _normalise_bid_link(item.get("href", ""))
        title = str(item.get("text", "")).strip()
        if not url or url in seen or not title:
            continue
        seen.add(url)
        if terms and not any(term in title.casefold() for term in terms):
            continue
        output.append({"url": url, "title": title[:300], "snippet": title[:500]})
        if len(output) >= max_results:
            break
    return output


def fetch_gem(keywords, max_results=80):
    """Fetch public GeM bid listings without guessing bid IDs or fabricating leads."""
    terms = [str(k).strip() for k in (keywords or []) if str(k).strip()]
    if not terms or max_results <= 0:
        return []
    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
    except ImportError:
        log.warning("[gem] playwright missing; public listing not checked")
        return []

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"]
            )
            try:
                context = browser.new_context(viewport={"width": 1365, "height": 900})
                page = context.new_page()
                for address in GEM_URLS:
                    response = None
                    try:
                        # Avoid networkidle: the GeM page may continue polling.
                        response = page.goto(address, wait_until="domcontentloaded", timeout=25000)
                    except PWTimeout:
                        log.warning("[gem] navigation timed out: %s", address)
                        # Playwright may have loaded usable DOM before timing out.
                    except Exception as exc:
                        log.warning("[gem] navigation failed %s: %s", address, str(exc)[:160])
                        continue

                    status = response.status if response is not None else "unknown"
                    log.info("[gem] page=%s status=%s final_url=%s", address, status, page.url)
                    if isinstance(status, int) and status in (401, 403, 429, 503):
                        log.warning("[gem] access restricted (HTTP %s); not bypassing", status)
                        continue
                    try:
                        sample = page.locator("body").inner_text(timeout=5000)[:2000].casefold()
                    except Exception:
                        sample = ""
                    if any(marker in sample for marker in BLOCK_INDICATORS):
                        log.warning("[gem] access challenge detected on %s; skipping", address)
                        continue
                    try:
                        page.locator("a[href*='showbidDocument'], a[href*='/bidding/bid/']").first.wait_for(timeout=8000)
                    except PWTimeout:
                        log.warning("[gem] no bid links in rendered DOM at %s (dynamic listing or changed markup)", address)
                        continue
                    items = _extract_items(page, terms, max_results)
                    log.info("[gem] page yielded %d matching public bid(s)", len(items))
                    if items:
                        return items
                log.warning("[gem] no matching bids from accessible public listings; see per-URL diagnostics")
                return []
            finally:
                browser.close()
    except Exception as exc:
        log.warning("[gem] collection failed: %s", str(exc)[:200])
        return []
