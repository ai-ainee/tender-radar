"""Public CPPP discovery: RSS, official HTML latest-tender links, indexed fallback.

Each result is a *candidate*, not an independently verified current bid.
No CAPTCHA bypass, fabricated tender URLs, or assertion of verified dates/buyers.
"""
from __future__ import annotations
import os
import re
import urllib.request
import xml.etree.ElementTree as ET
from urllib.parse import urljoin, urlparse, parse_qs
from bs4 import BeautifulSoup
from common import SearchEngine, log

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
CPPP_RSS = "https://eprocure.gov.in/cppp/rssdldtlist"
CPPP_ALT = "https://eprocure.gov.in/cppp/latestactivetendersnew/cpppdata"
OFFICIAL_LISTINGS = (
    ("central_eproc", "https://eprocure.gov.in/eprocure/app"),
    ("central_epublish", "https://eprocure.gov.in/epublish/app"),
    ("central_etenders", "https://etenders.gov.in/eprocure/app"),
)
OFFICIAL_HOSTS = frozenset(("eprocure.gov.in", "etenders.gov.in"))


def _http(url, timeout=14, headers=None):
    try:
        req = urllib.request.Request(url, headers=headers or {
            "User-Agent": UA,
            "Accept": "text/html,application/xml,application/atom+xml,text/xml,*/*;q=0.5",
        })
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read(1_500_000).decode("utf-8", errors="replace")
    except Exception as exc:
        log.info("[cppp] public endpoint unavailable %s: %s", url[:95], str(exc)[:110])
        return ""


def _is_feed(body):
    return bool(re.search(r"<(?:rss|feed)(?:\s|>)|<item(?:\s|>)|<entry(?:\s|>)", body or "", re.I))


def _try_direct():
    for url in (CPPP_RSS, CPPP_ALT):
        body = _http(url)
        if _is_feed(body):
            return body, "direct_feed"
    return "", ""


def _try_jina():
    token = os.getenv("JINA_API_KEY", "").strip()
    headers = {"User-Agent": UA}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    # Reader often produces Markdown rather than raw XML. Reject non-XML safely.
    for url in (CPPP_RSS, CPPP_ALT):
        body = _http("https://r.jina.ai/" + url, timeout=25, headers=headers)
        if _is_feed(body):
            return body, "jina_feed"
    return "", ""


def _try_scraperapi():
    from urllib.parse import urlencode
    keys = [x.strip() for x in os.getenv("SCRAPERAPI_API_KEYS", "").split(",") if x.strip()]
    for key in keys[:2]:
        url = "https://api.scraperapi.com?" + urlencode({
            "api_key": key, "url": CPPP_RSS, "render": "false"})
        body = _http(url, timeout=25)
        if _is_feed(body):
            return body, "scraperapi_feed"
    return "", ""


def _matches(title, snippet, keywords):
    hay = (str(title or "") + " " + str(snippet or "")).casefold()
    return any(str(k).casefold().strip() in hay for k in keywords if str(k).strip())


def _site_host(url):
    host = (urlparse(url).hostname or "").lower().removeprefix("www.")
    return host in OFFICIAL_HOSTS


def _detail_url(url):
    if not url or not url.startswith("https://") or not _site_host(url):
        return False
    u = urlparse(url)
    q = {k.casefold(): [v.casefold() for v in vs] for k,vs in parse_qs(u.query).items()}
    p = u.path.casefold()
    page = " ".join(q.get("page", []))
    # Filter out generic search/home/login links. Only actual detail references.
    if any(x in page for x in ("tenderdetails", "tenderdetail", "tendersfullview")):
        return bool(q.get("tnid") or q.get("tenderid") or q.get("bidid"))
    if "tenderid" in q or "tnid" in q:
        return True
    return bool(re.search(r"/(?:tendersfullview|tenderdetail)/[^/]{6,}$", p))


def _meta(method, portal, official_link=False):
    return {
        "internal_source": "cppp",
        "portal": portal,
        "verification_status": "official_listing_link" if official_link else "unverified_index",
        "official_source_verified": bool(official_link),
        "source_fetched": bool(official_link),
        "active_tender_verified": False,
        "buyer_verified": False,
        "current_deadline_verified": False,
        "specification_verified": False,
        "provisional_tender": True,
        "requires_corrigendum_check": True,
        "review_required": True,
        "relevance_keyword_match": True,
        "snippet_only": True,
        "fetch_method": method,
    }


def _tag_text(node, local_name):
    for child in node.iter():
        if str(child.tag).split("}")[-1].lower() == local_name.lower():
            return "".join(child.itertext()).strip()
    return ""


def _parse_feed(body, keywords, max_results):
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        return []
    out = []
    for item in root.iter():
        typ = str(item.tag).split("}")[-1].lower()
        if typ not in ("item", "entry"):
            continue
        title = _tag_text(item, "title")
        desc = _tag_text(item, "description") or _tag_text(item, "summary")
        url = _tag_text(item, "link")
        if not url:
            for ln in item.iter():
                if str(ln.tag).split("}")[-1].lower() == "link":
                    url = ln.attrib.get("href", "").strip()
                    if url:
                        break
        if not title or not url or not _matches(title, desc, keywords):
            continue
        if urlparse(url).scheme != "https" or not urlparse(url).hostname:
            continue
        # Feeds can include external links. Preserve their provenance, but
        # never assert that a third-party link is an official tender detail.
        official = _detail_url(url)
        out.append({
            "url": url, "title": title[:300],
            "snippet": ("UNVERIFIED CPPP FEED ENTRY. Buyer, current deadline and "
                        "active status require official verification. " +
                        BeautifulSoup(desc, "html.parser").get_text(" ", strip=True))[:1000],
            "date": _tag_text(item, "pubDate") or _tag_text(item, "published"),
            "metadata": _meta("rss_feed", "cppp_feed", official),
        })
        if len(out) >= max_results:
            break
    return out


def _parse_html_list(body, base_url, portal, keywords, max_results):
    """Extract *actual anchored bid detail URLs* from CPPP latest-tenders HTML.

    Portal homepages contain many non-tender links; never turn title-only text
    into fabricated detail links. Dates in table cells remain unverified.
    """
    soup = BeautifulSoup(body or "", "html.parser")
    out, seen = [], set()
    for a in soup.find_all("a", href=True):
        url = urljoin(base_url, a["href"].strip())
        if not _detail_url(url) or url in seen:
            continue
        parent = a.find_parent("tr")
        title = a.get_text(" ", strip=True)
        sibling = parent.get_text(" ", strip=True) if parent else title
        if len(title) < 6:
            title = sibling
        if not _matches(title, sibling, keywords):
            continue
        seen.add(url)
        out.append({
            "url": url, "title": title[:300],
            "snippet": ("UNVERIFIED CPPP OFFICIAL LISTING LINK. Check latest "
                        "bid status, buyer, deadline and corrigenda. " + sibling)[:1000],
            "metadata": _meta("official_html_listing", portal, True),
        })
        if len(out) >= max_results:
            break
    return out


def _index_fallback(keywords, max_results=100, searcher=None, queries_per_run=3):
    """Official-domain indexed details when live portal and feeds are blocked.

    This uses an explicit site: search; common.SearchEngine's legacy cleaning
    strips site: unless preserve_operators=True. Do not widen to random domains.
    """
    searcher = searcher or SearchEngine()
    clean_terms = [str(x).replace('"','').strip() for x in keywords if str(x).strip()]
    expression = " OR ".join('"' + t + '"' for t in clean_terms[:4])
    out, seen = [], set()
    for host, portal in (("eprocure.gov.in", "cppp_central"),
                         ("etenders.gov.in", "cppp_etenders"))[:max(0, queries_per_run)]:
        query = f"site:{host} ({expression}) (tender OR procurement OR bid)"
        try:
            items = searcher.search(query, country="india", pages=1, recent=False,
                                    preserve_operators=True)
        except Exception as exc:
            log.warning("[cppp-index] %s: %s", host, str(exc)[:110])
            continue
        count = 0
        for item in items or []:
            if not isinstance(item, dict):
                continue
            url = str(item.get("link") or item.get("url") or "").strip()
            title = str(item.get("title") or "")
            snippet = str(item.get("snippet") or "")
            if not _detail_url(url) or url in seen or not _matches(title, snippet, keywords):
                continue
            seen.add(url); count += 1
            out.append({
                "url": url, "title": title[:300],
                "snippet": ("UNVERIFIED CPPP SEARCH-INDEX REFERENCE; source page and "
                            "current buyer/deadline/status not read. " + snippet)[:1000],
                "metadata": _meta("official_domain_index", portal, False),
            })
            if len(out) >= max_results:
                break
        log.info("[cppp-index] %s %d provisional details from %d indexed hits",
                 host, count, len(items or []))
        if len(out) >= max_results:
            break
    return out


def fetch_cppp(keywords, max_results=100, searcher=None, http_get=None):
    """Collect relevant official CPPP bid *references*, bounded by max_results."""
    if not keywords or max_results <= 0:
        return []
    fetch = http_get or _http
    out, seen = [], set()
    def add(items):
        for item in items:
            url = item.get("url")
            if url and url not in seen and len(out) < max_results:
                seen.add(url)
                out.append(item)

    for portal, listing in OFFICIAL_LISTINGS:
        body = fetch(listing)
        if body:
            candidates = _parse_html_list(body, listing, portal, keywords, max_results)
            add(candidates)
            log.info("[cppp-html] %s: %d matching detail(s)", portal, len(candidates))
        if len(out) >= max_results:
            break
    # Historically used RSS endpoints are still attempted, but not assumed valid.
    if len(out) < max_results:
        body, method = _try_direct()
        if not body:
            body, method = _try_jina()
        if not body:
            body, method = _try_scraperapi()
        if body:
            add(_parse_feed(body, keywords, max_results))
    # Always augment from indexed official detail pages; lists only show a small
    # latest-tender window, so relying on HTML alone misses relevant older bids.
    if len(out) < max_results:
        add(_index_fallback(keywords, max_results - len(out), searcher=searcher))
    log.info("[cppp] %d provisional candidate(s); official current status NOT verified",len(out))
    return out
