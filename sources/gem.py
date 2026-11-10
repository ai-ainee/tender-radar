"""sources/gem.py — Public GeM bid collector with search-index fallback.

Step 36: preserve explicit source/provenance markers and reject ambiguous bid IDs.
Do not treat search excerpts, initial bid documents, or dates as proof of
current bid status; amendments/extensions must be checked on GeM.

Step 27C: GeM rejects connections from some hosted CI runners even if the site
opens in a user's browser. Try GeM's public HTML pages first. When unavailable,
use the shared SearchEngine (Serper, Brave, DDGS) to discover indexed GeM bid
references on public web pages. Fallback records are provisional, NOT verified
GeM bid status. No guessed GeM document URLs, logins, CAPTCHAs or proxy bypass.

Contract: fetch_gem(keywords, max_results=80) -> [{url,title,snippet,metadata}, ...].
"""

import re
from datetime import datetime
from urllib.parse import urljoin, urlparse

from common import log, SearchEngine

GEM_URLS = (
    "https://bidplus.gem.gov.in/all-bids",
    "https://bidplus.gem.gov.in/bidlists",
)
GEM_HOST = "bidplus.gem.gov.in"
BID_LINK = re.compile(r"/(?:showbidDocument/\d+|bidding/bid/[^?#]+)", re.I)
BID_NUMBER = re.compile(r"\bGEM\s*/\s*(20\d{2})\s*/\s*B\s*/\s*(\d{5,9})\b", re.I)
BLOCK_INDICATORS = (
    "access denied", "captcha", "verify you are human", "request blocked",
    "attention required", "unusual traffic",
)
LANDING_SEGMENTS = frozenset((
    "search", "search-results", "category", "categories", "topic", "topics",
    "latest-tenders", "all-tenders", "product", "products", "tag", "tags",
    "jobs", "tenders", "tender", "home", "indiaproducts",
))


def _normalise_bid_link(href):
    if not href:
        return None
    url = urljoin("https://bidplus.gem.gov.in/", str(href).strip())
    parts = urlparse(url)
    if parts.scheme != "https" or parts.hostname != GEM_HOST:
        return None
    if not BID_LINK.search(parts.path):
        return None
    return url.split("#", 1)[0]


def _extract_items(page, keywords, max_results):
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
        output.append({
            "url": url,
            "title": title[:300],
            "snippet": title[:500],
            # This link was seen on an official listing, but no current
            # deadline, corrigendum, purchasing buyer, or active status
            # has been verified by this collector.
            "metadata": {
                "verification_status": "official_listing_link",
                "fetch_method": "official_listing",
                "active_tender_verified": False,
                "current_deadline_verified": False,
                "buyer_verified": False,
                "specification_verified": False,
                "requires_corrigendum_check": True,
            },
        })
        if len(output) >= max_results:
            break
    return output


def _official_listing(keywords, max_results):
    """Read public listing if accessible; on failure let indexed fallback run."""
    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
    except ImportError:
        log.warning("[gem] playwright missing; cannot check official listing")
        return []

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage"],
            )
            try:
                page = browser.new_context(
                    viewport={"width": 1365, "height": 900}
                ).new_page()
                output = []
                seen_urls = set()

                for address in GEM_URLS:
                    try:
                        response = page.goto(
                            address, wait_until="domcontentloaded", timeout=25000
                        )
                        status = response.status if response is not None else None
                        log.info("[gem] listing=%s status=%s", address, status)

                        if status in (401, 403, 429, 503):
                            log.warning("[gem] HTTP %s; not bypassing access restriction", status)
                            continue

                        body = page.locator("body").inner_text(timeout=5000).casefold()
                        if any(flag in body[:2000] for flag in BLOCK_INDICATORS):
                            log.warning("[gem] access challenge detected: %s", address)
                            continue

                        page.locator(
                            "a[href*='showbidDocument'], "
                            "a[href*='/bidding/bid/']"
                        ).first.wait_for(timeout=8000)
                        for item in _extract_items(
                            page, keywords, max_results - len(output)
                        ):
                            if item["url"] not in seen_urls:
                                seen_urls.add(item["url"])
                                output.append(item)
                        if len(output) >= max_results or output:
                            break
                    except PWTimeout:
                        log.warning("[gem] page timed out or no bid links: %s", address)
                        continue
                    except Exception as exc:
                        log.warning(
                            "[gem] navigation/extraction failed %s: %s",
                            address, str(exc)[:160],
                        )
                        continue
                return output
            finally:
                browser.close()
    except Exception as exc:
        log.warning("[gem] official listing collection failed: %s", str(exc)[:160])
        return []


def _candidate_url(url):
    """Accept a specific public tender page; reject category/search landing pages."""
    if not isinstance(url, str):
        return False
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        return False
    if parsed.hostname == GEM_HOST:
        return bool(_normalise_bid_link(url))
    if parsed.hostname.endswith(".gem.gov.in") or parsed.hostname == "gem.gov.in":
        return False  # GeM HTML listing URLs are not bid references
    parts = [p.lower() for p in parsed.path.split("/") if p]
    if not parts or parts[-1] in LANDING_SEGMENTS or len(parsed.path) < 14:
        return False
    if any(part in ("search", "topic", "category", "tag", "product", "job-search", "indiaproducts") for part in parts[:-1]):
        return False
    if parts[-1].endswith(("-tenders", "-jobs", "-bids")):
        return False
    return True


def _indexed_fallback(keywords, max_results):
    """Discover individually indexed bid notices via the shared search engine.

    SearchEngine uses configured Serper keys, tries other configured providers,
    and falls back to DuckDuckGo if paid API requests fail. All matches remain
    unverified until the official GeM source confirms them.
    """
    terms = [str(t).strip() for t in keywords if str(t).strip()]
    terms = list(dict.fromkeys(t for t in terms if 1 < len(t) <= 100))[:6]
    if not terms:
        return []

    out, seen_bids = [], set()
    ambiguous_count = 0
    current_year = datetime.now().year
    searcher = SearchEngine()
    for term in terms:
        clean_term = term.replace('"', '').strip()
        # Avoid an overly restrictive month filter: the reference may be
        # indexed after the date it appeared on GeM. Never imply it is active.
        query = f'GEM/{current_year}/B {clean_term} tender'
        log.info("[gem-index] finding individually indexed bid references for %r", term[:40])
        try:
            organic = searcher.search(query, country="india", pages=1, recent=False)
        except Exception as exc:
            log.warning("[gem-index] public-search failure for %r: %s", term[:40], str(exc)[:120])
            continue
        log.info("[gem-index] %d search result(s) returned for %r", len(organic or []), term[:40])
        for item in organic or []:
            if not isinstance(item, dict):
                continue
            url = str(item.get("link") or "").strip()
            title = str(item.get("title") or "").strip()
            snippet = str(item.get("snippet") or "").strip()
            hay = f"{title} {snippet}"
            if not _candidate_url(url):
                continue
            if not any(t.casefold() in hay.casefold() for t in terms):
                continue

            # A search snippet can mention several different bids. Prefer
            # a single bid reference in the title; otherwise require exactly
            # one reference in the snippet. Never assign a random bid ID.
            title_refs = list(dict.fromkeys(
                f"GEM/{m.group(1)}/B/{m.group(2)}"
                for m in BID_NUMBER.finditer(title)
            ))
            snippet_refs = list(dict.fromkeys(
                f"GEM/{m.group(1)}/B/{m.group(2)}"
                for m in BID_NUMBER.finditer(snippet)
            ))
            candidate_refs = title_refs if title_refs else snippet_refs
            if len(candidate_refs) != 1:
                if len(candidate_refs) > 1:
                    ambiguous_count += 1
                continue
            bid_id = candidate_refs[0]
            if bid_id in seen_bids:
                continue
            seen_bids.add(bid_id)
            out.append({
                "url": url,  # Real indexed page, never a fabricated GeM URL.
                "title": f"{bid_id} — {title}"[:300],
                "snippet": (
                    "UNVERIFIED SEARCH-INDEX DISCOVERY. Search result only; "
                    "buyer, product specification, current deadline and bid "
                    "status are NOT verified. Original PDF dates can change "
                    "through GeM extensions and corrigenda. Check the latest "
                    "official GeM listing and all corrigenda before qualifying "
                    f"this as an active purchase. Reference: {bid_id}. {snippet}"
                )[:1000],
                "metadata": {
                    "verification_status": "unverified_index",
                    "provisional": True,
                    "fetch_method": "search_index",
                    "indexed_bid_reference": bid_id,
                    "source_index_domain": (urlparse(url).hostname or "").lower(),
                    "matched_target": term,
                    "active_tender_verified": False,
                    "current_deadline_verified": False,
                    "buyer_verified": False,
                    "specification_verified": False,
                    "requires_corrigendum_check": True,
                },
            })
            if len(out) >= max_results:
                break
        if len(out) >= max_results:
            break
    if ambiguous_count:
        log.info("[gem-index] %d ambiguous search result(s) skipped (multiple bid IDs)", ambiguous_count)
    log.info("[gem-index] %d provisional individual bid reference(s), NOT verified GeM tenders", len(out))
    return out


def fetch_gem(keywords, max_results=80):
    """Try official GeM first; fallback searches indexed public bid references."""
    terms = [str(t).strip() for t in (keywords or []) if str(t).strip()]
    if not terms or max_results <= 0:
        return []
    official = _official_listing(terms, max_results)
    if official:
        log.info("[gem] %d official HTML bid(s) matched", len(official))
        return official
    log.warning("[gem] official HTML results unavailable or empty; using indexed search fallback")
    provisional = _indexed_fallback(terms, max_results)
    log.info("[gem] %d provisional indexed reference(s) returned; official source still unverified", len(provisional))
    return provisional
