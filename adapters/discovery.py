"""adapters/discovery.py — Search + aggregator sources, self-contained.

Consolidates all free discovery sources into one category adapter:

  • firsttender.com      (aggregator, search + page fetch)
  • tenderdetail.com     (aggregator, search + page fetch)
  • serper               (4-track query generator; SearchEngine internally
                          tries Serper → Brave → DuckDuckGo automatically)
  • google_news_rss      (Google News search RSS — unlimited free)
  • bing_news_rss        (DISABLED — returned 0, feed unreliable)

All queries and filters are product-agnostic — driven entirely by
`target_products` from Settings. No hardcoded industry names anywhere.
"""
import hashlib
import os
import re
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime
from urllib.parse import urlparse, quote_plus

from common import (SearchEngine, clean_url, env_int, fetch_page, log)
from standard_lead import StandardLead
from adapters import SourceAdapter, register, normalize_company_name

try:
    from tender_utils import extract_tender_id, tender_id_from_url
    _HAS_TENDER_UTILS = True
except ImportError:
    def extract_tender_id(t): return ""
    def tender_id_from_url(u): return ""
    _HAS_TENDER_UTILS = False


TRACK_KEYS = ["TRACK_1_PUBLIC_TENDERS", "TRACK_2_CORPORATE_PROCUREMENT",
              "TRACK_3_BUSINESS_EXPANSION", "TRACK_4_COMMERCIAL_HIRING"]

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")


# ── Query negative operators ─────────────────────────────────────────
# Tender/RFQ queries on a service-named product ("Autocad drafting services")
# surface CAD outsourcing shops (Cad Crowd, Alpha Cad, etc.) instead of buyers.
# Append negatives to steer Google toward supply/procurement pages.
# These survive SearchEngine.clean_query() after the fix in common.py.
_NEG_TENDER = " -services -outsourcing -training -course -indiamart -tradeindia -naukri -justdial -freelance -provider"
_NEG_HIRING = " -indiamart -tradeindia -naukri -justdial -freelance"


def _add_negatives(q):
    """Append negative operators appropriate to query intent."""
    ql = str(q or "").lower()
    is_hiring = any(w in ql for w in ("hiring", "vacancy", "jobs", "recruit"))
    return q + (_NEG_HIRING if is_hiring else _NEG_TENDER)


# ═══════════════════════════════════════════════════════════════════════
# RSS helpers
# ═══════════════════════════════════════════════════════════════════════
def _http_get(url, timeout=15, max_bytes=1_500_000):
    """Plain HTTP GET with UA. Returns text or '' on any error."""
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": UA,
            "Accept": "application/rss+xml,application/xml,text/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-IN,en;q=0.9",
        })
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read(max_bytes).decode("utf-8", errors="replace")
    except Exception as e:
        log.debug(f"[discovery] RSS HTTP error {url[:60]}: {str(e)[:80]}")
        return ""


def _parse_rss_items(xml_text, keywords, source_name):
    """Parse RSS → list of {title, url, snippet, date, source_url, source_text}."""
    if not xml_text:
        return []
    kw_lower = [str(k).lower() for k in (keywords or []) if k]
    out = []
    try:
        root = ET.fromstring(xml_text)
        for item in root.iter("item"):
            title = (item.findtext("title") or "").strip()
            link = (item.findtext("link") or "").strip()
            desc = (item.findtext("description") or "").strip()
            date = (item.findtext("pubDate") or "").strip()
            # Extract <source url="..."> element — critical for Google News
            # vendor detection (Google always includes it).
            source_elem = item.find("source")
            source_url = source_elem.get("url", "") if source_elem is not None else ""
            source_text = (source_elem.text or "").strip() if source_elem is not None else ""
            if not title or not link:
                continue
            if kw_lower:
                hay = (title + " " + desc).lower()
                if not any(k in hay for k in kw_lower):
                    continue
            out.append({
                "title": title[:300],
                "url": link,
                "snippet": re.sub(r"<[^>]+>", " ", desc)[:500],
                "date": date[:30],
                "source_url": source_url[:200],
                "source_text": source_text[:100],
            })
    except Exception as e:
        log.debug(f"[discovery:{source_name}] RSS parse error: {str(e)[:80]}")
    return out

def _is_vendor_source_url(source_url, source_text, targets):
    """True if the article was published by a target-product vendor itself.

    Critical for Google News RSS where the item link is news.google.com
    (a redirect URL), but the <source url> attribute reveals the actual
    publisher. When that's autodesk.com, the article is a vendor press
    release — not a buyer signal.

    Product-agnostic — matches against target_products AND against
    VENDOR_DOMAIN_KEYWORDS from adapters/__init__.py.
    """
    src_low = (source_text or "").lower()
    url_low = (source_url or "").lower()
    if not src_low and not url_low:
        return False

    # 1. Match against target product names
    for t in targets:
        t_low = str(t).lower().strip()
        if not t_low:
            continue
        first_word = t_low.split()[0]
        if len(first_word) >= 4:
            if first_word in src_low or first_word in url_low:
                return True

    # 2. Match against known vendor domains
    try:
        from adapters import VENDOR_DOMAIN_KEYWORDS
        for kw in VENDOR_DOMAIN_KEYWORDS:
            if kw in url_low:
                return True
    except Exception:
        pass

    return False

def _looks_like_vendor_announcement(title, target_products):
    """Product-agnostic check for vendor press releases.

    Detects:
      1. Title STARTS with a target product name
         "Revit 2027 release notes" → vendor
         "Autodesk announces Q3 results" → vendor

      2. Title contains vendor-announcement verbs near the start
         "release notes", "unveils", "what's new in", "webinar:", etc.

    Works for ANY industry — driven entirely by target_products from
    Settings. Change target_products to "SAP ERP" and the filter
    automatically starts detecting SAP press releases instead.
    """
    title_low = (title or "").lower().strip()
    for t in target_products:
        t_low = str(t).lower().strip()
        if not t_low:
            continue
        if title_low.startswith(t_low + " ") or title_low.startswith(t_low + ":"):
            return True
        first = t_low.split()[0]
        if len(first) >= 4 and (title_low.startswith(first + " ") or title_low.startswith(first + ":")):
            return True

    VENDOR_VERBS = (
        "release notes", "announces", "now available", "product update",
        "version ", "launches new", "unveils", "general availability",
        "what's new in", "release:", "webinar:", "tutorial:",
        "how to use", "getting started with", "tutorial for",
        "case study:", "success story:", "whitepaper:",
    )
    head = title_low[:120]
    if any(verb in head for verb in VENDOR_VERBS):
        return True
    return False


# ═══════════════════════════════════════════════════════════════════════
# Query generator
# ═══════════════════════════════════════════════════════════════════════
class QueryGenerator:
    def __init__(self, target, industry, country, states, llm,
                 use_ai=True, combo_offset=0):
        self.target = target.strip()
        self.industry = industry.strip() if industry and industry != "ALL_SECTORS" else ""
        self.country = (country or "").strip()
        self.states = [s.strip() for s in states if s.strip()]
        self.year = datetime.now().year
        self.llm = llm
        self.use_ai = use_ai
        self.combo_offset = combo_offset

    @staticmethod
    def _normalize(data):
        if not isinstance(data, dict):
            return {}
        for parent in ("tracks", "queries", "data", "result"):
            if isinstance(data.get(parent), dict):
                data = data[parent]
                break
        out = {}
        for k, v in data.items():
            if isinstance(v, str):
                v = [v]
            if not isinstance(v, list) or not v:
                continue
            ck = "".join(ch for ch in str(k).upper() if ch.isalnum())
            queries = [str(x).strip() for x in v if str(x).strip()][:2]
            if not queries:
                continue
            for n, name in enumerate(TRACK_KEYS, start=1):
                label = name.split("_", 2)[2].replace("_", "")
                if f"TRACK{n}" in ck or label in ck:
                    out[name] = queries
                    break
        return out

    def _ai_tracks(self, prompt):
        if not self.use_ai or not self.llm or not self.llm.available():
            return {}
        data = self.llm.generate_json(prompt, validator=lambda d: isinstance(d, dict))
        return self._normalize(data)

    def _merge(self, ai, fallback):
        merged = dict(fallback)
        merged.update({k: v for k, v in ai.items() if v})
        return merged

    def build_special_tracks(self, special_prompt):
        sp = special_prompt
        fb = {
            "TRACK_1_PUBLIC_TENDERS": [f"{sp} public tenders", f"{sp} GeM bids"],
            "TRACK_2_CORPORATE_PROCUREMENT": [f"{sp} vendor empanelment", f"{sp} corporate RFQ"],
            "TRACK_3_BUSINESS_EXPANSION": [f"{sp} expansion capex", f"{sp} new facility"],
            "TRACK_4_COMMERCIAL_HIRING": [f"{sp} hiring specialists", f"{sp} team expansion"],
        }
        prompt = f"""You are an expert SEO and B2B search strategist.
MANDATORY CAMPAIGN DIRECTIVE: "{special_prompt}"
Generate Google search queries to find buyers, tenders, or expansions matching this directive.

CRITICAL RULES:
1. Keep queries between 3 and 7 words.
2. No quotes, no operators, plain text only.

TRACK_1_PUBLIC_TENDERS: public tenders, GeM bids, or infrastructure bids.
TRACK_2_CORPORATE_PROCUREMENT: private RFQs or vendor empanelment.
TRACK_3_BUSINESS_EXPANSION: new facilities or commercial project launches.
TRACK_4_COMMERCIAL_HIRING: companies hiring specialists.
Return ONLY JSON with exactly those 4 keys, each an array of 2 strings."""
        return self._merge(self._ai_tracks(prompt), fb)

    def build_tracks(self):
        if self.states:
            day_offset = datetime.now().toordinal() % len(self.states)
            state = self.states[(day_offset + self.combo_offset) % len(self.states)]
        else:
            state = ""
        t, ind, yr = self.target, self.industry, self.year
        loc = state or self.country
        loc_s = f" {loc}" if loc else ""
        ind_s = f"{ind} " if ind else ""
        state_rule = f"Restrict the queries to {state}." if state else \
                     "DO NOT restrict to a single state (search nationwide)."
        fb = {
            "TRACK_1_PUBLIC_TENDERS": [f"{t} tender{loc_s}", f"{ind_s}public tenders{loc_s}"],
            "TRACK_2_CORPORATE_PROCUREMENT": [f"{t} RFQ{loc_s}", f"vendor empanelment {ind_s}{loc_s}"],
            "TRACK_3_BUSINESS_EXPANSION": [f"{t} project expansion{loc_s}", f"{ind_s}new facility construction{loc_s}"],
            "TRACK_4_COMMERCIAL_HIRING": [f"hiring {t} specialist{loc_s}", f"{ind_s}team expansion{loc_s}"],
        }
        industry_line = f"INDUSTRY FOCUS: {ind}" if ind else "INDUSTRY: all commercial/industrial sectors"
        prompt = f"""You are an expert SEO and B2B search strategist.
Generate Google search queries to find buyers, tenders, or expansions related to '{t}'.
{industry_line}
{f"Geography: {self.country}" if self.country else ""}
{f"Region: {state}" if state else ""}

CRITICAL RULES:
1. Keep queries between 3 and 7 words.
2. Each track has 2 queries:
   - Query 1: Include the product name '{t}'. {state_rule}
   - Query 2: Search for the broad project or industry requirement. {state_rule}
3. No quotes, no operators, plain text only.

TRACK_1_PUBLIC_TENDERS: government tenders, GeM bids, or public infrastructure bids.
TRACK_2_CORPORATE_PROCUREMENT: private RFQs or vendor empanelment.
TRACK_3_BUSINESS_EXPANSION: new facilities or commercial project launches.
TRACK_4_COMMERCIAL_HIRING: companies hiring specialists.
Return ONLY JSON with exactly those 4 keys, each an array of 2 strings."""
        return self._merge(self._ai_tracks(prompt), fb)


# ═══════════════════════════════════════════════════════════════════════
# Aggregator hint extraction
# ═══════════════════════════════════════════════════════════════════════
def _extract_hints(text):
    hints = {}
    if not text:
        return hints

    m = re.search(r"(?:₹|Rs\.?|INR)\s*([\d,]+(?:\.\d+)?)\s*(crore|cr|lakh|lac|lak)?", text, re.I)
    if m:
        try:
            amount = float(m.group(1).replace(",", ""))
            unit = (m.group(2) or "").lower()
            if "cr" in unit:
                amount *= 10000000
            elif "lakh" in unit or "lac" in unit or "lak" in unit:
                amount *= 100000
            hints["value"] = amount
        except ValueError:
            pass

    for pat in (
        r"(?:closing|bid\s+end|deadline|last\s+date)[:\s]+(\d{1,2}[-/\s]\w{3,9}[-/\s]\d{2,4})",
        r"(?:closing|bid\s+end|deadline)[:\s]+(\d{4}-\d{2}-\d{2})",
        r"(?:closing|bid\s+end|deadline)[:\s]+(\d{1,2}/\d{1,2}/\d{2,4})",
    ):
        m = re.search(pat, text, re.I)
        if m:
            hints["deadline"] = m.group(1).strip()
            break

    m = re.search(r"\b(?:GEM|NIT|EOI|RFP)[/-][A-Z0-9][A-Z0-9/_-]{3,}\b", text)
    if m:
        hints["tender_id"] = m.group(0)

    m = re.search(r"(?:Buyer|Issued\s+by|Tender\s+by|Organisation)[:\s]+([A-Z][A-Za-z\s&.,]{5,80})", text)
    if m:
        hints["buyer"] = m.group(1).strip()[:80]

    return hints


# ═══════════════════════════════════════════════════════════════════════
# Filters
# ═══════════════════════════════════════════════════════════════════════
BLOCKED_HOSTS_PERMANENT = (
    "gem.gov.in", "bidplus.gem.gov.in", "mkp.gem.gov.in",
    "fulfilment.gem.gov.in", "mkp-pprod-bidplus.gem.gov.in",
    "seller.gem.gov.in",
)

JOB_URL_RE = re.compile(
    r"/(jobs?|careers?|vacanc|job-?listing|job-?detail|jobposting|join-?us|hiring)[/-]",
    re.I,
)


def _is_job_url(url):
    low = (url or "").lower()
    if JOB_URL_RE.search(low):
        return True
    host = urlparse(url or "").netloc.lower()
    if re.match(r"^(careers?\.|jobs?\.|hiring\.)", host):
        return True
    return False


def _is_gem_blocked(url):
    host = urlparse(url or "").netloc.lower().split(":")[0]
    return any(host == bh or host.endswith("." + bh) for bh in BLOCKED_HOSTS_PERMANENT)


def _is_banned_domain(url, banned_sites):
    low = (url or "").lower()
    return any(b in low for b in banned_sites if b)


def _fingerprint(text):
    norm = re.sub(r"[^a-z0-9]", "", (text or "").lower())[:3000]
    return hashlib.sha1(norm.encode()).hexdigest()[:16] if len(norm) >= 200 else ""


def mark_precheck_url(pending_urls, url):
    pending_urls.add(url)


def _guess_category(track_name):
    tn = str(track_name).upper()
    if "TENDER" in tn:      return "tender"
    if "PROCUREMENT" in tn: return "tender"
    if "EXPANSION" in tn:   return "expansion"
    if "HIRING" in tn:      return "hiring"
    return "news"


# ═══════════════════════════════════════════════════════════════════════
# DiscoveryAdapter
# ═══════════════════════════════════════════════════════════════════════
class DiscoveryAdapter(SourceAdapter):
    name = "discovery"
    category = "tender"
    priority = 30

    def __init__(self):
        self._search = None
        self._llm = None

    def is_enabled(self, settings):
        has_targets = bool(settings.get("targets"))
        has_special = bool(settings.get("special_prompt"))
        countries = settings.get("countries") or []
        india_ok = (not countries) or any("india" in str(c).lower() for c in countries)
        if has_special:
            return True
        if has_targets and india_ok:
            return True
        return False

    def fetch(self, settings):
        from common import LLMRouter
        if self._search is None:
            self._search = SearchEngine()
        if self._llm is None:
            self._llm = LLMRouter()

        leads = []
        for source_name, fetcher in [
            ("firsttender",    self._fetch_firsttender),
            ("tenderdetail",   self._fetch_tenderdetail),
            ("serper",         self._fetch_serper),
            ("google_news",    self._fetch_google_news),
            ("bing_news",      self._fetch_bing_news),
        ]:
            try:
                results = fetcher(settings)
                log.info(f"[discovery] {source_name}: {len(results)} lead(s)")
                leads.extend(results)
            except Exception as e:
                log.warning(f"[discovery] {source_name}: ERROR {str(e)[:120]}")
        return leads

    # ── Aggregator fetchers ──────────────────────────────────────────
    def _fetch_aggregator(self, settings, domain, source_name):
        targets = settings.get("targets") or []
        states = settings.get("states") or []
        protected = settings.get("protected") or []
        if not targets:
            return []

        queries = []
        for target in targets[:5]:
            queries.append(f"{domain} {target} tender")
            if source_name == "firsttender":
                queries.append(f"{domain} {target} bid india")
        if states:
            if source_name == "firsttender":
                queries.append(f"{domain} tender {states[0]}")
            else:
                queries.append(f"{domain} empanelment {states[0]}")

        leads = []
        seen_urls = set()

        for q in queries:
            try:
                results = self._search.search(q, country="india", pages=1, recent=True)
            except Exception as e:
                log.warning(f"[discovery:{source_name}] search failed: {str(e)[:100]}")
                continue

            for res in results:
                raw = (res.get("link") or "").strip()
                if not raw or domain not in raw.lower():
                    continue
                c = clean_url(raw)
                if not c or c in seen_urls:
                    continue
                seen_urls.add(c)

                try:
                    content = fetch_page(raw, protected_domains=protected)
                except Exception as e:
                    log.debug(f"[discovery:{source_name}] fetch failed: {str(e)[:60]}")
                    content = ""

                if not content or len(content) < 120:
                    continue

                structured = _extract_hints(content)

                leads.append(StandardLead(
                    source="discovery",
                    category="tender",
                    url=raw,
                    title=res.get("title") or "",
                    snippet=res.get("snippet") or "",
                    raw_text=content,
                    country="India",
                    state=structured.get("state", ""),
                    buyer_org=structured.get("buyer", ""),
                    value_inr=structured.get("value", 0.0),
                    deadline=structured.get("deadline", ""),
                    tender_id=structured.get("tender_id", ""),
                    structured=structured,
                    metadata={
                        "query": q,
                        "fetch_method": "page",
                        "aggregator": source_name,
                        "internal_source": source_name,
                    },
                ))
        return leads

    def _fetch_firsttender(self, settings):
        return self._fetch_aggregator(settings, "firsttender.com", "firsttender")

    def _fetch_tenderdetail(self, settings):
        return self._fetch_aggregator(settings, "tenderdetail.com", "tenderdetail")

    # ── Serper ──────────────────────────────────────────────────────
    def _fetch_serper(self, settings):
        cfg = settings
        special = cfg.get("special_prompt") or ""
        targets = cfg.get("targets") or []
        industries = cfg.get("industries") or ["ALL_SECTORS"]
        countries = cfg.get("countries") or []
        states = cfg.get("states") or []
        country = countries[0] if countries else ""
        banned_sites = cfg.get("banned_sites") or []
        banned_kw = cfg.get("banned_kw") or []
        protected = cfg.get("protected") or []
        job_portals = cfg.get("job_portals") or []
        country_list = countries
        company_dedup = cfg.get("company_dedup")

        max_combos = env_int("MAX_COMBOS_PER_RUN", 20)
        pages = env_int("SEARCH_PAGES", 3)
        max_links = env_int("MAX_LINKS_PER_RUN", 150)
        use_ai_queries = os.getenv("AI_QUERIES", "1").strip() != "0"

        campaigns = []
        if special:
            gen = QueryGenerator(special, "Special Campaign", country, states,
                                 self._llm, use_ai_queries, combo_offset=0)
            campaigns.append((special, "Special Campaign", gen))
        else:
            combos = [(t, i) for t in targets for i in industries]
            if len(combos) > max_combos:
                offset = (datetime.now().toordinal() * max_combos) % len(combos)
                combos = (combos[offset:] + combos[:offset])[:max_combos]
            for idx, (t, i) in enumerate(combos):
                gen = QueryGenerator(t, i, country, states, self._llm,
                                     use_ai_queries, combo_offset=idx)
                campaigns.append((t, i, gen))

        try:
            from radar import likely_in_target
        except ImportError:
            try:
                from radar_v3 import likely_in_target
            except ImportError:
                def likely_in_target(url, snippet, *a, **k): return True

        leads = []
        pending_urls = set()
        seen_fps = set()
        precheck_dupes = 0
        tender_id_hits = 0

        for target, industry, gen in campaigns:
            if len(leads) >= max_links:
                break
            tracks = gen.build_special_tracks(special) if special else gen.build_tracks()
            for track_name, queries in tracks.items():
                for query in queries:
                    if len(leads) >= max_links:
                        break
                    recent = any(w in query.lower() for w in
                                 ("tender", "rfp", "bid", "procurement", "loa",
                                  "award", "contract", "order", "empanelment"))
                    search_query = _add_negatives(query)
                    log.info(f"[search] Searching: {search_query}")
                    try:
                        results = self._search.search(search_query, country, pages=pages, recent=recent)
                    except Exception as e:
                        log.warning(f"[discovery:serper] query failed: {str(e)[:100]}")
                        continue

                    for res in results:
                        if len(leads) >= max_links:
                            break
                        raw = (res.get("link") or "").strip()
                        c = clean_url(raw)
                        if not raw or not c or c in pending_urls:
                            continue

                        snippet_raw = res.get("snippet") or ""
                        title_raw = res.get("title") or ""
                        low_url = raw.lower()
                        protected_hit = any(p in low_url for p in protected)

                        if company_dedup:
                            match = company_dedup.find_in(title_raw, snippet_raw)
                            if match:
                                log.info(f"   [PRECHECK] Skipping {match!r}: {raw[:70]}")
                                precheck_dupes += 1
                                mark_precheck_url(pending_urls, c)
                                continue

                        if not protected_hit:
                        if _is_gem_blocked(raw):
                            mark_precheck_url(pending_urls, c)
                            continue
                        if _is_banned_domain(raw, banned_sites):
                            mark_precheck_url(pending_urls, c)
                            continue
                        is_job = _is_job_url(raw)
                        if is_job and any(d in low_url for d in job_portals):
                            mark_precheck_url(pending_urls, c)
                            continue
                        if country_list and not likely_in_target(
                            raw, snippet_raw, country_list, states,
                            settings.get("cities") or []
                        ):
                            mark_precheck_url(pending_urls, c)
                            continue
                        if _is_gem_blocked(raw):
                            mark_precheck_url(pending_urls, c)
                            continue
                        if _is_banned_domain(raw, banned_sites):
                            mark_precheck_url(pending_urls, c)
                            continue

                        pending_urls.add(c)

                        content = None
                        try:
                            content = fetch_page(raw, protected_domains=protected)
                        except Exception as e:
                            log.debug(f"[discovery:serper] fetch failed {raw[:60]}: {str(e)[:60]}")

                        is_job = _is_job_url(raw)

                        tid = ""
                        if _HAS_TENDER_UTILS:
                            tid = (extract_tender_id(title_raw)
                                   or extract_tender_id(snippet_raw)
                                   or tender_id_from_url(raw))
                            if tid:
                                tender_id_hits += 1

                        if content and len(content) > 120:
                            fp = _fingerprint(content)
                            if fp and fp in seen_fps:
                                continue
                            if fp:
                                seen_fps.add(fp)

                            leads.append(StandardLead(
                                source="discovery",
                                category="hiring" if is_job else _guess_category(track_name),
                                url=raw,
                                title=title_raw,
                                snippet=snippet_raw,
                                raw_text=content,
                                country=country,
                                tender_id=tid,
                                metadata={
                                    "track": track_name,
                                    "query": query,
                                    "target_product": target,
                                    "industry": industry,
                                    "fetch_method": "page",
                                    "is_job_url": is_job,
                                    "internal_source": "serper",
                                },
                            ))
                        else:
                            snip = f"{title_raw}\n{snippet_raw}".strip()
                            if len(snip) >= 60:
                                leads.append(StandardLead(
                                    source="discovery",
                                    category="hiring" if is_job else _guess_category(track_name),
                                    url=raw,
                                    title=title_raw,
                                    snippet=snippet_raw,
                                    raw_text="[PAGE COULD NOT BE READ - SEARCH SNIPPET ONLY]\n" + snip,
                                    country=country,
                                    tender_id=tid,
                                    metadata={
                                        "track": track_name,
                                        "query": query,
                                        "target_product": target,
                                        "industry": industry,
                                        "fetch_method": "snippet",
                                        "snippet_only": True,
                                        "is_job_url": is_job,
                                        "internal_source": "serper",
                                    },
                                ))

        log.info(f"[discovery:serper] {len(leads)} lead(s) collected, "
                 f"{precheck_dupes} pre-check dupes skipped, "
                 f"{tender_id_hits} tender ID(s) extracted")
        return leads

    # ── Google News RSS (Fix B applied) ─────────────────────────────
    def _fetch_google_news(self, settings):
        """Google News RSS — 3 intent groups per target for wider coverage.

        Each product fires 3 separate RSS queries:
          1. Procurement intents (tender, RFP, empanelment)
          2. Expansion intents (capex, new facility, manufacturing plant)
          3. Vendor registration intents (supplier registration, onboarding)

        Zero API cost — pure HTTP GET against Google News RSS.
        """
        targets = (settings.get("targets") or [])[:3]
        if not targets:
            return []

        INTENT_GROUPS = (
            ("tender", "procurement", "RFP", "RFQ", "empanelment"),
            ("expansion", "capex", "new facility", "manufacturing plant"),
            ("vendor registration", "supplier registration", "vendor onboarding"),
        )

        leads = []
        for target in targets:
            for group in INTENT_GROUPS:
                intent_str = " OR ".join(group)
                q = quote_plus(f'"{target}" ({intent_str})')
                url = (f"https://news.google.com/rss/search"
                       f"?q={q}&hl=en-IN&gl=IN&ceid=IN:en")
                body = _http_get(url, timeout=15)
                for r in _parse_rss_items(body, [], "google_news")[:15]:
                    # Skip vendor announcements detected via title
                    if _looks_like_vendor_announcement(r.get("title", ""), targets):
                        continue
                    if _is_vendor_source_url(r.get("source_url", ""),
                                              r.get("source_text", ""), targets):
                        continue
                    leads.append(_dict_to_lead("google_news", r))

        log.info(f"[discovery:google_news] {len(leads)} lead(s) collected")
        return leads[:100]

    # ── Bing News (disabled — returns 0) ────────────────────────────
    def _fetch_bing_news(self, settings):
        """Disabled — Bing News RSS returns 0 leads with no HTTP error
        (feed empty or deprecated). Google News RSS covers the same
        ground with better reliability. Re-enable only if Bing fixes
        their RSS endpoint."""
        return []


def _dict_to_lead(source_name, r):
    """Convert an RSS dict to StandardLead."""
    return StandardLead(
        source="discovery",
        category="news",
        url=r.get("url", ""),
        title=str(r.get("title", ""))[:300],
        raw_text=str(r.get("snippet", ""))[:3000],
        raw_published_at=str(r.get("date", "")),
        country="India",
        metadata={
            "fetch_method": "rss",
            "internal_source": source_name,
        },
    )


register(DiscoveryAdapter())
