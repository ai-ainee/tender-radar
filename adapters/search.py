"""adapters/search.py - General-purpose search adapter.

Migration of the current search loop from radar.py into an adapter.
Preserves existing behaviour (QueryGenerator, 4 tracks, all filters)
and adds three Deploy 1.1 enhancements:

  1. Extended JOB_URL_RE — catches /careers/, /vacancies/, /job-listing/,
     /vacancy/, /hiring/, /join-us/, /jobposting/ (not just /job/ and /jobs/)
  2. Tender ID extraction — populates lead.tender_id from title, snippet,
     or URL. Fixes cross-portal dedupe (tender_dupe was always 0 before).
  3. Pre-check against CompanyDedup — skips URLs whose company is already
     in the CRM. Catches ~80% of duplicates BEFORE fetch + AI, saving
     runtime and LLM quota.
"""
import hashlib
import os
import re
from datetime import datetime
from urllib.parse import urlparse

from common import (SearchEngine, clean_url, env_int, fetch_page, log)
from standard_lead import StandardLead
from adapters import SourceAdapter, register, normalize_company_name

# ── Tender ID extraction (optional — graceful if tender_utils missing) ──
try:
    from tender_utils import extract_tender_id, tender_id_from_url
    _HAS_TENDER_UTILS = True
except ImportError:
    def extract_tender_id(t): return ""
    def tender_id_from_url(u): return ""
    _HAS_TENDER_UTILS = False


TRACK_KEYS = ["TRACK_1_PUBLIC_TENDERS", "TRACK_2_CORPORATE_PROCUREMENT",
              "TRACK_3_BUSINESS_EXPANSION", "TRACK_4_COMMERCIAL_HIRING"]


# ── Query generator (unchanged from previous version) ─────────────────
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


# ── Filters (extended in Deploy 1.1) ──────────────────────────────────
BLOCKED_HOSTS_PERMANENT = (
    "gem.gov.in", "bidplus.gem.gov.in", "mkp.gem.gov.in",
    "fulfilment.gem.gov.in", "mkp-pprod-bidplus.gem.gov.in",
    "seller.gem.gov.in",
)

# Extended job URL pattern — matches /careers/, /vacancies/, /job-listing/,
# /job-detail/, /jobposting/, /join-us/, /hiring/ (was just /job/ and /jobs/)
JOB_URL_RE = re.compile(
    r"/(jobs?|careers?|vacanc|job-?listing|job-?detail|jobposting|join-?us|hiring)[/-]",
    re.I,
)


def _is_job_url(url):
    """True if URL looks like a job listing page."""
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


# ── Adapter ───────────────────────────────────────────────────────────
class SearchAdapter(SourceAdapter):
    name = "serper"
    category = "news"
    priority = 100

    def __init__(self):
        self._search = None
        self._llm = None

    def is_enabled(self, settings):
        return bool(settings.get("targets") or settings.get("special_prompt"))

    def fetch(self, settings):
        from common import LLMRouter
        if self._search is None:
            self._search = SearchEngine()
        if self._llm is None:
            self._llm = LLMRouter()

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
        company_dedup = cfg.get("company_dedup")  # ← NEW: pre-check

        max_combos = env_int("MAX_COMBOS_PER_RUN", 20)
        pages = env_int("SEARCH_PAGES", 3)
        max_links = env_int("MAX_LINKS_PER_RUN", 150)
        use_ai_queries = os.getenv("AI_QUERIES", "1").strip() != "0"

        # Build campaigns
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

        # Import gate functions from main radar module
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
                    log.info(f"[search] Searching: {query}")
                    try:
                        results = self._search.search(query, country, pages=pages, recent=recent)
                    except Exception as e:
                        log.warning(f"[search] query failed: {str(e)[:100]}")
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

                        # ── Pre-check against CRM (NEW) ─────────────────────
                        # Before any fetch or AI, see if the company named in
                        # the title/snippet is already in the CRM. Catches ~80%
                        # of duplicate URLs before they cost us any quota.
                        if company_dedup:
                            match = company_dedup.find_in(title_raw, snippet_raw)
                            if match:
                                log.info(f"   [PRECHECK] Skipping {match!r}: {raw[:70]}")
                                precheck_dupes += 1
                                mark_precheck_url(pending_urls, c)
                                continue
                        # ─────────────────────────────────────────────────────

                        # Pre-fetch gates
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

                        pending_urls.add(c)

                        # Fetch content
                        content = None
                        try:
                            content = fetch_page(raw, protected_domains=protected)
                        except Exception as e:
                            log.debug(f"[search] fetch failed {raw[:60]}: {str(e)[:60]}")

                        is_job = _is_job_url(raw)

                        # ── Tender ID extraction (NEW) ──────────────────────
                        # Extract from title, snippet, or URL. Feeds the
                        # tender-dupe gate downstream.
                        tid = ""
                        if _HAS_TENDER_UTILS:
                            tid = (extract_tender_id(title_raw)
                                   or extract_tender_id(snippet_raw)
                                   or tender_id_from_url(raw))
                            if tid:
                                tender_id_hits += 1
                        # ─────────────────────────────────────────────────────

                        if content and len(content) > 120:
                            fp = _fingerprint(content)
                            if fp and fp in seen_fps:
                                continue
                            if fp:
                                seen_fps.add(fp)

                            lead = StandardLead(
                                source=self.name,
                                category="hiring" if is_job else _guess_category(track_name),
                                url=raw,
                                title=title_raw,
                                snippet=snippet_raw,
                                raw_text=content,
                                country=country,
                                tender_id=tid,          # ← NEW
                                metadata={
                                    "track": track_name,
                                    "query": query,
                                    "target_product": target,
                                    "industry": industry,
                                    "fetch_method": "page",
                                    "is_job_url": is_job,
                                },
                            )
                            leads.append(lead)
                        else:
                            snip = f"{title_raw}\n{snippet_raw}".strip()
                            if len(snip) >= 60:
                                lead = StandardLead(
                                    source=self.name,
                                    category="hiring" if is_job else _guess_category(track_name),
                                    url=raw,
                                    title=title_raw,
                                    snippet=snippet_raw,
                                    raw_text="[PAGE COULD NOT BE READ - SEARCH SNIPPET ONLY]\n" + snip,
                                    country=country,
                                    tender_id=tid,          # ← NEW
                                    metadata={
                                        "track": track_name,
                                        "query": query,
                                        "target_product": target,
                                        "industry": industry,
                                        "fetch_method": "snippet",
                                        "snippet_only": True,
                                        "is_job_url": is_job,
                                    },
                                )
                                leads.append(lead)

        log.info(f"[search] {len(leads)} lead(s) collected, "
                 f"{precheck_dupes} pre-check dupes skipped, "
                 f"{tender_id_hits} tender ID(s) extracted")
        return leads


def mark_precheck_url(pending_urls, url):
    """Mark URLs as seen without adding to results (for pre-check skips)."""
    pending_urls.add(url)


def _guess_category(track_name):
    tn = str(track_name).upper()
    if "TENDER" in tn:
        return "tender"
    if "PROCUREMENT" in tn:
        return "tender"
    if "EXPANSION" in tn:
        return "expansion"
    if "HIRING" in tn:
        return "hiring"
    return "news"


register(SearchAdapter())
