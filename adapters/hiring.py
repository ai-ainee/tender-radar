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
from common import log, fetch_page

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
    """Extract company names from job listing text using regex patterns."""
    if not text:
        return []
    companies = set()
    role_esc = re.escape(role.lower()[:40])

    patterns = [
        r"(?:at|@)\s+([A-Z][A-Za-z0-9&.\s]{2,50}(?:Ltd|Limited|Pvt|Private|Inc|Corp|LLP|Technologies|Solutions|Systems|Engineering|Group|Enterprises)?)",
        r"([A-Z][A-Za-z0-9&.\s]{2,50}(?:Ltd|Limited|Pvt|Private|Inc|Corp|LLP|Technologies|Solutions|Systems|Engineering|Group|Enterprises))\s+(?:is\s+)?hiring",
        r"(?:Company|Employer|Organization)[:\s]+([A-Z][A-Za-z0-9&.\s]{3,50})",
    ]
    for pat in patterns:
        for m in re.finditer(pat, text, re.I | re.M):
            name = m.group(1).strip()
            name = re.sub(r"\s+", " ", name)[:80]
            bad = {
                "the", "a", "an", "this", "that", "we", "our", "your", "apply",
                "job", "role", "position", "hiring", "company", "employer",
                # Navigation / cookie-consent artifacts (from real runs):
                "login", "info", "enterprise", "read", "click", "view",
                "cookies", "privacy", "terms", "policy", "menu", "search",
                "submit", "cancel", "close", "next", "previous",
                # Placeholder extraction artifacts:
                "we can remember you", "understand how you interact",
                "are currently hidden", "currently hidden", "hidden",
                "you need to have", "are prevalent", "prevalent in the industry",
            }
            BAD_PHRASES = (
                "we can remember", "understand how you interact",
                "are currently hidden", "you need to have",
                "are prevalent", "cookie", "privacy policy",
                "terms and conditions", "sign in", "sign up",
            )
            name_low = name.lower()
            if (name and len(name) >= 4 and name_low not in bad
                    and not name_low.startswith(("apply", "click", "view", "read", "login", "sign"))
                    and not any(p in name_low for p in BAD_PHRASES)
                    and len(name.split()) >= 1):
                companies.add(name)
            if len(companies) >= max_companies:
                break
    return list(companies)[:max_companies]


class HiringAdapter(SourceAdapter):
    name = "hiring"
    category = "hiring"
    priority = 50

    def __init__(self):
        self._llm = None

    def is_enabled(self, settings):
        return bool(settings.get("targets"))

    def fetch(self, settings):
        from common import LLMRouter
        if self._llm is None:
            self._llm = LLMRouter()

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

    # ── Naukri (existing scraper — working) ──────────────────────────
    def _fetch_naukri(self, role_pairs, settings):
        if not _HAS_NAUKRI:
            return self._stub("naukri", "hiring_signals not importable")
        leads = []
        for role, product in role_pairs:
            try:
                companies = search_naukri(role, max_results=8)
            except Exception as e:
                log.debug(f"[hiring:naukri] '{role}': {str(e)[:80]}")
                continue
            slug = _slug(role)
            for company in companies or []:
                if not isinstance(company, str) or len(company) < 4:
                    continue
                leads.append(StandardLead(
                    source="hiring",
                    category="hiring",
                    url=f"https://www.naukri.com/{slug}-jobs",
                    title=role,
                    raw_text=f"{role} hiring at {company}",
                    buyer_org=company[:200],
                    country="India",
                    metadata={
                        "internal_source": "naukri",
                        "role": role,
                        "target_product": product,
                        "is_job_url": True,
                    },
                ))
        return leads

    # ── Indeed India (RSS — genuine free, structured) ────────────────
    def _fetch_indeed(self, role_pairs, settings):
        leads = []
        for role, product in role_pairs:
            url = f"https://www.indeed.co.in/rss?q={quote_plus(role)}&l=India"
            body = _http_get(url, timeout=20)
            if not body or "<item>" not in body:
                continue
            for item in self._parse_indeed_rss(body)[:10]:
                leads.append(StandardLead(
                    source="hiring",
                    category="hiring",
                    url=item["url"],
                    title=item["title"],
                    raw_text=item["snippet"],
                    buyer_org=item["company"][:200],
                    country="India",
                    metadata={
                        "internal_source": "indeed",
                        "role": role,
                        "target_product": product,
                        "is_job_url": True,
                    },
                ))
        return leads

    def _parse_indeed_rss(self, xml_text):
        out = []
        try:
            root = ET.fromstring(xml_text)
            for item in root.iter("item"):
                title = (item.findtext("title") or "").strip()
                link = (item.findtext("link") or "").strip()
                desc = (item.findtext("description") or "").strip()
                if not title or not link:
                    continue
                # Indeed RSS title format: "Job Title - Company - Location"
                parts = [p.strip() for p in title.split(" - ")]
                company = ""
                job_title = title[:300]
                if len(parts) >= 3:
                    job_title = parts[0]
                    company = parts[1]
                elif len(parts) == 2:
                    job_title = parts[0]
                    company = parts[1]
                out.append({
                    "title": job_title[:300],
                    "company": company[:200],
                    "url": link,
                    "snippet": re.sub(r"<[^>]+>", " ", desc)[:500],
                })
        except Exception as e:
            log.debug(f"[hiring:indeed] RSS parse error: {str(e)[:80]}")
        return out

    # ── Shine (HTML scrape — best-effort) ────────────────────────────
    def _fetch_shine(self, role_pairs, settings):
        leads = []
        for role, product in role_pairs:
            slug = _slug(role)
            if not slug:
                continue
            url = f"https://www.shine.com/jobs/{slug}-jobs"
            try:
                content = fetch_page(url, max_chars=12000)
            except Exception:
                continue
            if not content or len(content) < 200:
                continue
            companies = _extract_companies_from_text(content, role, 8)
            for company in companies:
                leads.append(StandardLead(
                    source="hiring",
                    category="hiring",
                    url=url,
                    title=role,
                    raw_text=f"{role} hiring at {company}",
                    buyer_org=company[:200],
                    country="India",
                    metadata={
                        "internal_source": "shine",
                        "role": role,
                        "target_product": product,
                        "is_job_url": True,
                        "fetch_method": "html_scrape",
                    },
                ))
        return leads

    # ── TimesJobs (HTML scrape — best-effort) ────────────────────────
    def _fetch_timesjobs(self, role_pairs, settings):
        leads = []
        for role, product in role_pairs:
            url = (f"https://www.timesjobs.com/candidate/job-search.html"
                   f"?searchType=personalizedSearch&from=submit"
                   f"&txtKeywords={quote_plus(role)}&txtLocation=India")
            try:
                content = fetch_page(url, max_chars=12000)
            except Exception:
                continue
            if not content or len(content) < 200:
                continue
            companies = _extract_companies_from_text(content, role, 8)
            for company in companies:
                leads.append(StandardLead(
                    source="hiring",
                    category="hiring",
                    url=url,
                    title=role,
                    raw_text=f"{role} hiring at {company}",
                    buyer_org=company[:200],
                    country="India",
                    metadata={
                        "internal_source": "timesjobs",
                        "role": role,
                        "target_product": product,
                        "is_job_url": True,
                        "fetch_method": "html_scrape",
                    },
                ))
        return leads

    # ── Foundit (HTML scrape — best-effort) ──────────────────────────
    def _fetch_foundit(self, role_pairs, settings):
        """Disabled — Foundit search pages return pure navigation/cookie text.
        Extractor produced junk ('Login', 'Info', 'We Can Remember You').
        Re-enable only if you find a reliable structured endpoint."""
        return []

    # ── LinkedIn (stub — needs paid API or RSSHub proxy) ─────────────
    def _fetch_linkedin(self, role_pairs, settings):
        return self._stub("linkedin", "requires paid API or RSSHub proxy — not genuine free")

    # ── Internshala (stub — freshers-only, not senior buying signals) ─
    def _fetch_internshala(self, role_pairs, settings):
        return self._stub("internshala", "freshers/internships only — not senior hiring signals")


register(HiringAdapter())
