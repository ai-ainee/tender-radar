"""Public corporate procurement and early buying-signal discovery (Step 52).

A general-purpose collector driven by Settings, not fixed brands/industries.
Retrieves only publicly indexed evidence; nothing in this module verifies an
active RFQ, actual buyer, tender deadline, purchase, or named decision-maker.
No authentication, CAPTCHA bypass, invented URLs or scraped private contacts.

feed/search result schema: {url,title,snippet,date,category,metadata}.
"""
from __future__ import annotations

import os
import re
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date
from html import unescape
from urllib.parse import urlparse

from common import SearchEngine, env_int, log

BSE_PUBLIC_RSS = "https://www.bseindia.com/data/xml/announcements.xml"

# Each track has an independent query and an evidence marker. A search match is
# a *lead to review*, never proof that a target-product purchase will occur.
@dataclass(frozen=True)
class CorporateTrack:
    name: str
    category: str
    query: str
    evidence: tuple[str, ...]

TRACKS = (
    CorporateTrack("direct_rfq", "tender", '({portfolio}) ("request for quotation" OR RFQ OR RFP) {geo}', ("rfq", "rfp", "quotation", "proposal")),
    CorporateTrack("direct_purchase", "tender", '({portfolio}) ("purchase order" OR "purchase requirement" OR "procurement notice") {geo}', ("purchase", "procurement", "requirement")),
    CorporateTrack("corporate_tenders", "tender", '({portfolio}) (tender OR bid OR "invitation to bid") private company {geo}', ("tender", "bid", "procurement")),
    CorporateTrack("vendor_empanelment", "tender", '({portfolio}) ("vendor registration" OR empanelment OR "supplier onboarding") {geo}', ("vendor", "supplier", "empanelment", "prequalification")),
    CorporateTrack("direct_renewal", "tender", '({portfolio}) (renewal OR replacement OR upgrade) (license OR equipment OR contract OR procurement) {geo}', ("renewal", "replacement", "upgrade", "procurement", "contract")),
    CorporateTrack("facility_expansion", "expansion", '({context}) ("new factory" OR "new plant" OR "capacity expansion") {geo}', ("plant", "factory", "capacity", "expansion")),
    CorporateTrack("new_projects", "expansion", '({context}) ("new project" OR "project launch" OR "new facility") {geo}', ("project", "facility", "launch")),
    CorporateTrack("capex", "filing", '({context}) (capex OR "capital expenditure" OR investment) company {geo}', ("capex", "investment", "expenditure")),
    CorporateTrack("contract_awards", "expansion", '({context}) ("contract awarded" OR "order received" OR "work order") {geo}', ("order", "contract", "awarded")),
    CorporateTrack("hiring", "hiring", '({portfolio}) (hiring OR vacancy OR "job opening") company {geo}', ("hiring", "job", "vacancy", "recruit")),
    CorporateTrack("tech_adoption", "news", '({portfolio}) (implemented OR adopted OR migration OR transformation) {geo}', ("implemented", "adopted", "migration", "transformation")),
    CorporateTrack("supplier_qualification", "tender", '({portfolio}) ("prequalification" OR "supplier invitation" OR "registration of vendors") {geo}', ("prequalification", "supplier", "vendor")),
    CorporateTrack("partnerships", "news", '({context}) ("strategic partnership" OR "joint venture" OR "new alliance") {geo}', ("partnership", "venture", "alliance")),
    CorporateTrack("leases_new_offices", "expansion", '({context}) ("office expansion" OR "new office" OR "industrial land allotment") {geo}', ("office", "land", "facility", "expansion")),
    CorporateTrack("new_budgets", "filing", '({context}) ("approved investment" OR "board approved" OR "capital allocation") {geo}', ("approved", "investment", "allocation")),
)

DIRECT_TRACKS = frozenset(t.name for t in TRACKS if t.category == "tender")
SIGNAL_TERMS = frozenset(word for t in TRACKS for word in t.evidence)


def _terms(values, limit=10):
    if isinstance(values, str):
        values = re.split(r"[,;\n]", values)
    result = []
    for value in values or []:
        clean = " ".join(str(value).split()).strip('"\'')
        if not 2 <= len(clean) <= 90 or clean.casefold() in {r.casefold() for r in result}:
            continue
        result.append(clean)
        if len(result) >= limit:
            break
    return result


def _watchlist_sites(entries):
    """Explicit user watchlist only: no guessed company website URLs."""
    out = []
    seen = set()
    for item in _terms(entries, 120):
        candidate = item if "://" in item else "https://" + item
        p = urlparse(candidate)
        host = (p.hostname or "").lower()
        if ("." not in host or " " in host or not re.fullmatch(r"[a-z0-9.-]+", host)
                or host.startswith(".") or host.endswith(".")):
            continue
        try:
            port = p.port
        except ValueError:
            continue
        if port not in (None, 443, 80):
            continue
        if host not in seen:
            out.append(host)
            seen.add(host)
    return out


def _watchlist_names(entries):
    """Watchlist company names without DNS-like syntax."""
    return [x for x in _terms(entries, 120) if not "." in x and "://" not in x]


def _expr(items, max_items=4):
    items = _terms(items, max_items)
    return " OR ".join('"' + x.replace('"', '') + '"' if " " in x else x for x in items)


def _geo(settings):
    countries = _terms(settings.get("countries") or ["India"], 2)
    return " ".join(countries[:2]) or "India"


def plan_queries(settings, max_queries=24, on=None):
    """Capped, rotating plan. Every track appears in a typical 24-query run.

    Company names and watchlist domains rotate over days; a query budget does
    not falsely imply that every company is inspected during each run.
    """
    on = on or date.today()
    budget = max(1, min(int(max_queries), 60))
    products = _terms(settings.get("targets") or [], 15)
    industries = _terms(settings.get("industries") or [], 12)
    if not products and not industries:
        return []
    # Rotation prevents targets beyond the first 4 from being starved.
    off = on.toordinal() % len(products) if products else 0
    ordered = products[off:] + products[:off]
    portfolio = _expr(ordered)
    context = _expr(industries or ordered)
    geo = _geo(settings)
    plans = []
    # Allocate a minimum one slot to each evidence category before extra company
    # searches; no product-specific source switches.
    for track in TRACKS:
        if len(plans) >= budget:
            break
        if track.name in DIRECT_TRACKS and not portfolio:
            continue
        query = track.query.format(portfolio=portfolio, context=context, geo=geo)
        plans.append({"track": track.name, "category": track.category,
                      "query": query, "site_host": "", "scope": "broad"})
    sites = _watchlist_sites(settings.get("company_watchlist") or [])
    names = _watchlist_names(settings.get("company_watchlist") or [])
    site_off = on.toordinal() % len(sites) if sites else 0
    name_off = on.toordinal() % len(names) if names else 0
    # Explicit sites: site restriction is preserved by SearchEngine opt-in.
    for host in sites[site_off:] + sites[:site_off]:
        if len(plans) >= budget:
            break
        plans.append({"track": "watchlist_procurement", "category": "tender",
                      "query": f'site:{host} (RFQ OR RFP OR tender OR procurement OR "supplier registration") {portfolio or context}',
                      "site_host": host, "scope": "company_site"})
    for name in names[name_off:] + names[:name_off]:
        if len(plans) >= budget:
            break
        plans.append({"track": "watchlist_company_signal", "category": "expansion",
                      "query": f'"{name}" (expansion OR procurement OR capex OR "contract awarded" OR hiring) {geo}',
                      "site_host": "", "scope": "watchlist_name"})
    return plans


def _host_matches(url, expected):
    try:
        actual = (urlparse(url).hostname or "").lower()
        return bool(actual and (actual == expected or actual.endswith("." + expected)))
    except ValueError:
        return False


def _valid_public_url(url):
    if not isinstance(url, str) or len(url) > 1800:
        return False
    p = urlparse(url)
    if p.scheme not in ("https", "http") or not p.hostname or p.username or p.password:
        return False
    h = p.hostname.lower()
    if h in ("localhost", "127.0.0.1", "0.0.0.0", "::1") or h.endswith(".local"):
        return False
    return True


def _match_term(hay, terms):
    for term in terms:
        if term.casefold() in hay:
            return term
    return ""


def _has_evidence(hay, track):
    if track == "watchlist_procurement":
        return any(x in hay for x in ("rfq", "rfp", "tender", "procurement", "supplier", "vendor", "quotation"))
    if track == "watchlist_company_signal":
        return any(x in hay for x in ("expansion", "procurement", "capex", "contract", "hiring", "award"))
    item = next((t for t in TRACKS if t.name == track), None)
    return bool(item and any(k in hay for k in item.evidence))


def _classify_watchlist(track, hay):
    if track != "watchlist_company_signal":
        return "tender" if track == "watchlist_procurement" else ""
    if any(x in hay for x in ("rfq", "rfp", "tender", "procurement", "purchase order")):
        return "tender"
    if "hiring" in hay or "vacancy" in hay:
        return "hiring"
    return "expansion"


def _index_metadata(track, url, matched_target, matched_industry, scope):
    return {
        "internal_source": "corporate_public_index",
        "source_kind": "corporate_public_search",
        "corporate_signal_track": track,
        "source_scope": scope,
        "source_index_domain": (urlparse(url).hostname or "").lower(),
        "verification_status": "unverified_corporate_index",
        "review_required": True,
        "provisional": True,
        "source_fetched": False,
        "snippet_only": True,
        "fetch_method": "public_search_index",
        "official_source_verified": False,
        "active_tender_verified": False,
        "buyer_verified": False,
        "current_deadline_verified": False,
        "relevance_keyword_match": bool(matched_target or matched_industry),
        "matched_target": matched_target,
        "matched_industry": matched_industry,
    }


def collect_indexed(settings, max_queries=24, max_results=80, searcher=None, when=None):
    """Conservative public search. Unverified findings can reach Review, not Inbox."""
    searcher = searcher or SearchEngine()
    plans = plan_queries(settings, max_queries=max_queries, on=when)
    products = _terms(settings.get("targets") or [], 15)
    industries = _terms(settings.get("industries") or [], 12)
    max_results = max(1, min(int(max_results), 160))
    collected = {}
    counts = {"direct": 0, "signals": 0, "errors": 0, "queries": 0, "raw": 0}
    for plan in plans:
        counts["queries"] += 1
        try:
            organic = searcher.search(plan["query"], country=_geo(settings), pages=1,
                                      recent=False, preserve_operators=True)
        except Exception as exc:
            counts["errors"] += 1
            log.warning("[CORP-COVERAGE] track=%s search_error=%s", plan["track"], str(exc)[:100])
            continue
        count = 0
        counts["raw"] += len(organic or [])
        for result in organic or []:
            if not isinstance(result, dict):
                continue
            url = str(result.get("link") or result.get("url") or "").strip()
            if not _valid_public_url(url):
                continue
            if plan["site_host"] and not _host_matches(url, plan["site_host"]):
                continue
            title = " ".join(str(result.get("title") or "").split())
            snippet = " ".join(str(result.get("snippet") or "").split())
            hay = (title + " " + snippet).casefold()
            matched_target = _match_term(hay, products)
            matched_industry = _match_term(hay, industries)
            if not _has_evidence(hay, plan["track"]):
                continue
            # Broad queries require portfolio/industry evidence; explicitly
            # configured watchlist domains/names can provide alternate context.
            if not (matched_target or matched_industry or plan["scope"] in ("company_site", "watchlist_name")):
                continue
            if not title or url in collected:
                continue
            category = _classify_watchlist(plan["track"], hay) or plan["category"]
            metadata = _index_metadata(plan["track"], url, matched_target, matched_industry, plan["scope"])
            row = {
                "url": url, "title": title[:300],
                "snippet": ("UNVERIFIED CORPORATE PUBLIC INDEX: named buyer, item, "
                           "purchase intent, active status and dates NOT verified. " + snippet)[:1000],
                "date": str(result.get("date") or "")[:45],
                "category": category, "metadata": metadata,
            }
            collected[url] = row
            count += 1
            if len(collected) >= max_results:
                break
        log.info("[CORP-COVERAGE] track=%s scope=%s indexed=%s qualified=%s",
                 plan["track"], plan["scope"], len(organic or []), count)
        if len(collected) >= max_results:
            break
    for row in collected.values():
        if row["category"] == "tender": counts["direct"] += 1
        else: counts["signals"] += 1
    log.info("[CORP-COVERAGE] total=%s direct=%s signals=%s queries=%s errors=%s raw=%s",
             len(collected), counts["direct"], counts["signals"],
             counts["queries"], counts["errors"], counts["raw"])
    return list(collected.values())


def _bse_http_get(url=BSE_PUBLIC_RSS):
    try:
        req = urllib.request.Request(url, headers={"User-Agent":"Mozilla/5.0", "Accept":"application/rss+xml,application/xml,text/xml"})
        with urllib.request.urlopen(req, timeout=12) as response:
            if getattr(response, "status", 200) != 200:
                return ""
            return response.read(2_000_000).decode("utf-8", "replace")
    except Exception as exc:
        log.info("[CORP-FILINGS] BSE public RSS unavailable: %s", str(exc)[:80])
        return ""


def _text(node, tags):
    for child in node.iter():
        if child.tag.rsplit("}", 1)[-1].lower() in tags:
            value = "".join(child.itertext()).strip()
            if value:
                return unescape(value)
    return ""


def parse_public_filings(feed, settings, max_results=15):
    """Public BSE RSS only. These are *candidate signals*, not verified filings."""
    if not feed or len(feed) > 2_000_000:
        return []
    try:
        root = ET.fromstring(feed)
    except ET.ParseError:
        return []
    terms = _terms(settings.get("targets") or [], 15) + _terms(settings.get("industries") or [], 12)
    watchlist = _watchlist_names(settings.get("company_watchlist") or [])
    if not terms and not watchlist:
        return []
    rows, urls = [], set()
    for item in root.iter():
        if item.tag.rsplit("}", 1)[-1].lower() not in ("item", "entry"):
            continue
        title = _text(item, {"title"})
        description = _text(item, {"description", "summary"})
        link = _text(item, {"link"})
        if not link:
            linknode = next((c for c in item if c.tag.rsplit("}", 1)[-1].lower() == "link"), None)
            link = (linknode.get("href") or "") if linknode is not None else ""
        if not _valid_public_url(link):
            continue
        domain = (urlparse(link).hostname or "").lower()
        if not (domain == "bseindia.com" or domain.endswith(".bseindia.com")):
            continue
        hay = (title + " " + description).casefold()
        if not _match_term(hay, terms + watchlist):
            continue
        if not any(key in hay for key in ("investment", "contract", "order", "expansion", "project", "capex", "plant", "acquisition", "procurement", "technology", "partnership", "facility")):
            continue
        if link in urls:
            continue
        urls.add(link)
        meta = _index_metadata("bse_public_filing", link, _match_term(hay, terms), "", "public_filing_rss")
        meta.update({"internal_source": "corporate_public_index", "source_kind": "bse_public_rss",
                     "fetch_method": "public_rss", "source_fetched": True})
        rows.append({"url":link, "title": title[:300],
                     "snippet": ("UNVERIFIED CORPORATE FILING REFERENCE: check original disclosure. " + description)[:1000],
                     "date": _text(item, {"pubdate", "published", "updated"})[:45],
                     "category":"filing", "metadata":meta})
        if len(rows) >= max_results:
            break
    return rows


def fetch_corporate_intelligence(settings, searcher=None, http_get=None, when=None):
    """Public-only, bounded aggregation of direct buying and indirect signals."""
    max_queries = max(1, min(env_int("CORP_MAX_QUERIES", 24), 60))
    max_results = max(1, min(env_int("CORP_MAX_RESULTS", 80), 160))
    rows = collect_indexed(settings, max_queries, max_results, searcher, when)
    if os.getenv("CORP_BSE_RSS_ENABLED", "1").strip() != "0":
        feed = (http_get or _bse_http_get)()
        existing = {r["url"] for r in rows}
        more = [r for r in parse_public_filings(feed, settings) if r["url"] not in existing]
        rows.extend(more[: max(0, max_results - len(rows))])
        log.info("[CORP-FILINGS] bse_rss_relevant=%s", len(more))
    return rows[:max_results]
