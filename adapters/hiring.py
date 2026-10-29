"""adapters/hiring.py — Hiring category adapter (all genuine free sources).

Sources wrapped internally:
  ✅ Naukri     — existing scraper from hiring_signals.py (works today)
  ✅ Indeed     — RSS feed: rss.indeed.com/rss?q=...&l=India (structured)
  ⚙ Shine      — HTML search page scrape (best-effort, may need tuning)
  ⚙ TimesJobs  — HTML search page scrape (best-effort, may need tuning)
  ⚙ Foundit    — HTML search page scrape (best-effort, may need tuning)
  🔲 LinkedIn  — stub (requires paid API or RSSHub proxy, not free)
  🔲 Internshala— stub (freshers-only, not senior buying signals)

Role generation: LLM generates job titles from target_products (via
hiring_signals.generate_role_patterns, cached per-run). All sources
search the same role list — 1 LLM call total per run.

To activate a stub: replace `return self._stub(...)` with real fetcher.
To add a source not listed: add `("<name>", self._fetch_<name>)` to the
sources list in fetch(), and write the `_fetch_<name>` method.
"""
import re
import urllib.request
import xml.etree.ElementTree as ET
from urllib.parse import quote_plus

from adapters import SourceAdapter, register
from standard_lead import StandardLead
from common import log, fetch_page, SearchEngine

# ── Existing Naukri + role generation (graceful import) ──────────────
try:
    from hiring_signals import generate_role_patterns, search_naukri
    _HAS_NAUKRI = True
except ImportError:
    generate_role_patterns = None
    search_naukri = None
    _HAS_NAUKRI = False


UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")


def _http_get(url, timeout=15, max_bytes=1_500_000):
    """Plain HTTP GET with browser UA. Returns text or '' on error."""
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": UA,
            "Accept": "application/rss+xml,application/xml,text/html;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-IN,en;q=0.9",
        })
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read(max_bytes).decode("utf-8", errors="replace")
    except Exception as e:
        log.debug(f"[hiring] HTTP error {url[:60]}: {str(e)[:80]}")
        return ""


def _slug(s):
    """Convert 'Senior BIM Engineer' → 'senior-bim-engineer'."""
    return re.sub(r"[^a-z0-9]+", "-", str(s or "").lower()).strip("-")


def _extract_companies_from_text(text, role, max_companies=8):
    """Extract company names from job listing text."""
    if not text:
        return []
    companies = set()

    patterns = [
        r"(?:\b[Aa]t\b|@)\s*([A-Z][A-Za-z0-9&. ]{2,50}(?:Ltd|Limited|Pvt|Private|Inc|Corp|LLP|Technologies|Solutions|Systems|Engineering|Group|Enterprises)?)",
        r"([A-Z][A-Za-z0-9&. ]{2,50}(?:Ltd|Limited|Pvt|Private|Inc|Corp|LLP|Technologies|Solutions|Systems|Engineering|Group|Enterprises))\s+(?:is\s+)?[Hh]iring",
        r"(?:Company|Employer|Organization)[:\s]+([A-Z][A-Za-z0-9&. ]{3,50})",
    ]

    SINGLE_WORD_JUNK = {
        "the", "a", "an", "this", "that", "we", "our", "your",
        "apply", "job", "role", "position", "hiring", "company", "employer",
        "login", "info", "home", "menu", "search", "next", "previous",
        "view", "read", "click", "submit", "cancel", "close", "enterprise",
    }

    NAV_PHRASES = (
        "we can remember you",
        "understand how you interact",
        "are currently hidden",
        "you need to have",
        "are prevalent in the industry",
    )

    for pat in patterns:
        for m in re.finditer(pat, text, re.M):    # NO re.I — was matching lowercase
            name = m.group(1).strip()
            name = re.sub(r"\s+", " ", name)[:80]
            if not name or len(name) < 4:
                continue
            name_low = name.lower().strip()

            if name_low in SINGLE_WORD_JUNK:
                continue
            if any(p in name_low for p in NAV_PHRASES):
                continue
            if name_low.startswith(("apply now", "read more", "click here",
                                   "view details", "login to", "sign in",
                                   "submit application", "send message")):
                continue

            companies.add(name)
            if len(companies) >= max_companies:
                break
        if len(companies) >= max_companies:
            break

    return list(companies)[:max_companies]

class HiringAdapter(SourceAdapter):
    name = "hiring"
    category = "hiring"
    priority = 50

    def __init__(self):
        self._llm = None
        self._search = None

    def is_enabled(self, settings):
        return bool(settings.get("targets"))

    def fetch(self, settings):
        from common import LLMRouter
        if self._llm is None:
            self._llm = LLMRouter()
        if self._search is None:
            self._search = SearchEngine()

        targets = settings.get("targets") or []
        if not targets:
            return []

        # Generate role list once — 1 LLM call total, shared across all sources
        roles_by_product = {}
        if generate_role_patterns:
            try:
                roles_by_product = generate_role_patterns(self._llm, targets)
            except Exception as e:
                log.warning(f"[hiring] role generation failed: {str(e)[:100]}")

        if not roles_by_product:
            log.warning("[hiring] no roles generated — skipping all sources")
            return []

        # Flatten to unique role list, cap 3 roles per product
        role_pairs = []  # [(role, target_product)]
        seen = set()
        for product, roles in roles_by_product.items():
            if not isinstance(roles, list):
                continue
            for role in roles[:3]:
                r = str(role).strip()
                if r and r.lower() not in seen:
                    seen.add(r.lower())
                    role_pairs.append((r, product))

        log.info(f"[hiring] {len(role_pairs)} unique role(s) across {len(roles_by_product)} product(s)")

        leads = []
        for source_name, fetcher in [
            ("naukri",    self._fetch_naukri),
            ("indeed",    self._fetch_indeed),
            ("shine",     self._fetch_shine),
            ("timesjobs", self._fetch_timesjobs),
            ("foundit",   self._fetch_foundit),
            # Stubs — not genuine free
            ("linkedin",  self._fetch_linkedin),
            ("internshala", self._fetch_internshala),
        ]:
            try:
                results = fetcher(role_pairs, settings)
                log.info(f"[hiring] {source_name}: {len(results)} lead(s)")
                leads.extend(results)
            except Exception as e:
                log.warning(f"[hiring] {source_name}: ERROR {str(e)[:120]}")
        return leads

    def _stub(self, name, note=""):
        log.info(f"[hiring] {name}: not implemented ({note})")
        return []
    def _search_job_board(self, role_pairs, settings, site_domains):
        """Find job postings on target boards via SearchEngine site: queries.

        Replaces broken native scrapers — Naukri/Shine/TimesJobs are React
        SPAs requiring JS execution, and Indeed's public RSS endpoint was
        retired in ~2023. Google already indexes every one of these pages,
        and SearchEngine (Serper → Brave → DDG) can find them for free.

        Cost: len(role_pairs) * len(site_domains) queries, capped at 3 roles.
        With 4 domains and 3 roles = 12 queries/run — well within Serper's
        2500/month free tier (~0.5%).

        Returns a list of StandardLead with company extracted from the SERP
        title (Indeed/Naukri titles follow "Role - Company - Location").
        """
        if not self._search:
            return []

        leads = []
        seen_urls = set()

        for role, product in role_pairs[:3]:  # cap at 3 roles to limit queries
            for domain in site_domains:
                # Quoted role for phrase match; "India" for geo bias
                query = f'site:{domain} "{role}" India'
                try:
                    results = self._search.search(
                        query, country="india", pages=1, recent=False,
                    )
                except Exception as e:
                    log.debug(f"[hiring:{domain}] search error: {str(e)[:80]}")
                    continue

                if not results:
                    continue

                for res in results[:5]:  # cap 5 per (role, domain)
                    url = (res.get("link") or "").strip()
                    if not url or url in seen_urls:
                        continue
                    seen_urls.add(url)

                    title = res.get("title") or ""
                    snippet = res.get("snippet") or ""

                    # Extract company from title. Common formats:
                    #   Indeed:   "AutoCAD Draftsman - Acme Corp - Mumbai"
                    #   Naukri:   "Senior BIM Engineer - L&T Construction"
                    #   Shine:    "CAD Designer - Tata Projects Ltd, Pune"
                    company = ""
                    parts = [p.strip() for p in title.split(" - ")]
                    if len(parts) >= 3:
                        company = parts[1]           # Indeed's 3-part format
                    elif len(parts) == 2:
                        company = parts[1]           # Naukri's 2-part format
                    elif " at " in title.lower():
                        company = title.split(" at ", 1)[-1].split(",")[0].strip()

                    # Skip when no real company name extracted — the SERP title
                    # was a category/aggregation page ("CAD Draftsman Jobs in India",
                    # "AutoCAD Jobs in Bangalore", etc.), not a specific employer.
                    # These always return IRRELEVANT / JOB_PORTAL from the AI —
                    # filtering here saves 5-15s per wasted AI call.
                    _company_low = (company or "").strip().lower()
                    _BAD_COMPANY_NAMES = {
                        "jobs", "job", "careers", "career", "hiring",
                        "vacancies", "vacancy", "recruiter", "recruitment",
                        "apply", "apply now", "view all jobs", "search results",
                        "job openings", "job listing", "job postings",
                        "senior", "junior", "fresher", "lead", "manager",
                        "engineer", "designer", "drafter", "draftsman",
                        "indeed", "naukri", "glassdoor", "shine",
                        "linkedin", "foundit", "timesjobs", "instahyre",
                        "ambitionbox", "talentmate", "expertini",
                    }
                    if (
                        not company
                        or len(_company_low) < 3
                        or _company_low in _BAD_COMPANY_NAMES
                        or any(_company_low.startswith(b + " ") for b in _BAD_COMPANY_NAMES)
                    ):
                        log.debug(f"[hiring] skip (no company in title): {title[:80]}")
                        continue

                    leads.append(StandardLead(
                        source="hiring",
                        category="hiring",
                        url=url,
                        title=title[:300],
                        raw_text=snippet[:500],
                        buyer_org=company[:200],
                        country="India",
                        metadata={
                            "internal_source": f"serper_{domain.replace('.', '_')}",
                            "role": role,
                            "target_product": product,
                            "is_job_url": True,
                            "fetch_method": "serper_site_search",
                        },
                    ))

        return leads
    # ── Job-board fetchers — all via Serper site: queries ────────────
    # Native fetchers (RSS / HTML scrape) have been retired by each board:
    #   Naukri     — React SPA, needs Playwright
    #   Indeed     — public RSS endpoint killed in ~2023
    #   Shine      — JS-rendered job cards
    #   TimesJobs  — JS-rendered
    #   Foundit    — JS-rendered
    # Google indexes all of them. Serper's organic results include the
    # company name in the SERP title, which is all we need.

    def _fetch_naukri(self, role_pairs, settings):
        return self._search_job_board(role_pairs, settings, ["naukri.com"])

    def _fetch_indeed(self, role_pairs, settings):
        return self._search_job_board(role_pairs, settings, ["in.indeed.com"])

    def _fetch_shine(self, role_pairs, settings):
        return self._search_job_board(role_pairs, settings, ["shine.com"])

    def _fetch_timesjobs(self, role_pairs, settings):
        return self._search_job_board(role_pairs, settings, ["timesjobs.com"])

    def _fetch_foundit(self, role_pairs, settings):
        return self._search_job_board(role_pairs, settings, ["foundit.in"])

    # ── LinkedIn (stub — needs paid API or RSSHub proxy) ─────────────
    def _fetch_linkedin(self, role_pairs, settings):
        return self._stub("linkedin", "requires paid API or RSSHub proxy — not genuine free")

    # ── Internshala (stub — freshers-only, not senior buying signals) ─
    def _fetch_internshala(self, role_pairs, settings):
        return self._stub("internshala", "freshers/internships only — not senior hiring signals")


register(HiringAdapter())
