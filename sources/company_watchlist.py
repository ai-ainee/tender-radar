"""sources/company_watchlist.py — direct crawl of known buyer URLs.

For each URL in the watchlist:
  - Fetches the page
  - Extracts anchors whose text matches procurement keywords
  - Filters to same-host links
  - Returns one dict per matching anchor

Accepts both:
  • Full URLs with a path: https://www.nhai.gov.in/tenders
  • Bare domains: nhai.gov.in  (auto-tries /tenders, /procurement, ...)
"""
import re
from urllib.parse import urljoin, urlparse

from common import fetch_page, log

# Anchor text patterns that indicate a procurement-signal link
SIGNAL_LINK_RE = re.compile(
    r"\b(tender|rfp|rfq|eoi|procurement|expression of interest|"
    r"bid|invitation to bid|supply|purchase|acquisition|"
    r"empanelment|vendor registration|request for quotation)\b",
    re.I,
)
ANCHOR_RE = re.compile(
    r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>(.*?)</a>',
    re.I | re.S,
)

# Common procurement page paths for bare-domain watchlist entries
PROCUREMENT_PATHS = (
    "/tenders", "/tender", "/procurement", "/rfp", "/rfq",
    "/eoi", "/empanelment", "/vendor-registration", "/vendors",
)


def _clean_anchor(html):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html or "")).strip()


def _extract_signal_links(html, base_url, host, max_per_site=5):
    out = []
    seen = set()
    for m in ANCHOR_RE.finditer(html):
        href = (m.group(1) or "").strip()
        text = _clean_anchor(m.group(2))
        if not href or not text or len(text) < 8:
            continue
        if not SIGNAL_LINK_RE.search(text):
            continue
        full = urljoin(base_url, href)
        if urlparse(full).netloc != host:
            continue
        if full in seen:
            continue
        seen.add(full)
        out.append({"url": full, "title": text[:300]})
        if len(out) >= max_per_site:
            break
    return out


def _crawl_entry(entry, max_per_site):
    entry = str(entry or "").strip()
    if not entry:
        return []
    if not entry.startswith("http"):
        entry = "https://" + entry

    parsed = urlparse(entry)
    has_path = bool(parsed.path and parsed.path.strip("/"))
    host = parsed.netloc.lower().replace("www.", "")
    company = host.split(".")[0].title()

    urls_to_try = [entry] if has_path else [
        entry.rstrip("/") + p for p in PROCUREMENT_PATHS
    ]

    for url in urls_to_try:
        try:
            html = fetch_page(url, max_chars=30000)
        except Exception as e:
            log.debug(f"[watchlist] fetch failed {url[:60]}: {str(e)[:80]}")
            continue
        if not html or len(html) < 500:
            continue
        hits = _extract_signal_links(html, url, host, max_per_site)
        if hits:
            return [{"url": h["url"], "title": h["title"],
                     "snippet": h["title"], "company": company}
                    for h in hits]
        # If no hits from this path, try the next (for bare domains)
        if has_path:
            return []

    return []


def fetch_company_watchlist(urls, max_per_site=5):
    """Return list of {url, title, snippet, company} from watchlist URLs."""
    if not urls:
        return []

    out = []
    for entry in urls:
        try:
            results = _crawl_entry(entry, max_per_site)
            if results:
                log.info(f"[watchlist] {entry[:60]}: {len(results)} signal(s)")
            out.extend(results)
        except Exception as e:
            log.warning(f"[watchlist] {entry[:60]}: ERROR {str(e)[:100]}")
    return out
