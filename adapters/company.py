"""adapters/company.py — First-party company signals adapter.

Reads Settings for a curated watchlist of company domains. For each domain:
  • Crawls careers page (hiring signals)
  • Crawls press/news page (expansion signals)
  • Crawls investor relations page (capex signals)

Also includes two fixed vendor-registration portals that are public:
  ✅ L&T vendor registration
  ✅ Reliance vendor registration

Requires Settings columns:
  • company_watchlist: comma-separated URLs (e.g., "larsentoubro.com,tataprojects.com")

If Settings column is missing, only the fixed vendor registrations run.
"""
import re
from urllib.parse import urlparse

from adapters import SourceAdapter, register
from standard_lead import StandardLead
from common import log, fetch_page, env_list

# Fixed public vendor registration portals — always checked
VENDOR_REGS = (
    ("vendor_reg_lt",  "https://www.larsentoubro.com/corporate/suppliers/",          "L&T"),
    ("vendor_reg_ril", "https://www.ril.com/OurCompany/SupplierRegistration.aspx",   "Reliance"),
)

# Common paths for careers + press pages
CAREERS_PATHS = ("/careers", "/careers/", "/jobs", "/join-us", "/work-with-us")
PRESS_PATHS = ("/press", "/news", "/media", "/press-releases", "/newsroom")
IR_PATHS = ("/investors", "/investor-relations", "/ir", "/financials")


def _dict_to_lead(source_name, company, url, title, snippet):
    return StandardLead(
        source="company",
        category="news",
        url=url,
        title=str(title)[:300],
        raw_text=str(snippet)[:3000],
        buyer_org=str(company)[:200],
        country="India",
        metadata={"internal_source": source_name},
    )


class CompanyAdapter(SourceAdapter):
    name = "company"
    category = "news"
    priority = 80

    def is_enabled(self, settings):
        # Enabled if watchlist is set OR if we have targets (vendor regs always work)
        return bool(settings.get("targets"))

    def fetch(self, settings):
        leads = []

        # 1. Watchlist-based crawling
        watchlist = settings.get("company_watchlist") or []
        if isinstance(watchlist, str):
            watchlist = [w.strip() for w in watchlist.split(",") if w.strip()]

        for domain in watchlist[:20]:  # cap at 20 domains per run
            try:
                results = self._crawl_company(domain)
                log.info(f"[company] {domain}: {len(results)} lead(s)")
                leads.extend(results)
            except Exception as e:
                log.warning(f"[company] {domain}: ERROR {str(e)[:120]}")

        # 2. Fixed vendor registration portals
        for source_name, url, company in VENDOR_REGS:
            try:
                results = self._fetch_vendor_reg(source_name, url, company)
                log.info(f"[company] {source_name}: {len(results)} lead(s)")
                leads.extend(results)
            except Exception as e:
                log.warning(f"[company] {source_name}: ERROR {str(e)[:120]}")

        return leads

    def _base_url(self, domain):
        d = str(domain).strip().rstrip("/")
        if not d.startswith("http"):
            d = f"https://{d}"
        return d

    def _extract_signal_titles(self, content):
        """Extract headers/signal phrases from page content."""
        if not content:
            return []
        out = []
        for line in content.split("\n"):
            line = line.strip()
            if 20 < len(line) < 200 and any(
                kw in line.lower() for kw in (
                    "hiring", "careers", "we're hiring", "join our team",
                    "expansion", "new facility", "new office", "capex",
                    "invest", "announce", "launch", "commission",
                )
            ):
                out.append(line)
                if len(out) >= 5:
                    break
        return out

    def _crawl_company(self, domain):
        base = self._base_url(domain)
        host = urlparse(base).netloc.lower().replace("www.", "")
        company = host.split(".")[0].title()

        leads = []
        for path_group, prefix in (
            (CAREERS_PATHS, "careers"),
            (PRESS_PATHS, "press"),
            (IR_PATHS, "ir"),
        ):
            for path in path_group:
                url = f"{base.rstrip('/')}{path}"
                try:
                    content = fetch_page(url, max_chars=8000)
                except Exception:
                    continue
                if not content or len(content) < 500:
                    continue
                titles = self._extract_signal_titles(content)
                for title in titles[:3]:
                    leads.append(_dict_to_lead(
                        f"{prefix}_watchlist", company, url, title, title
                    ))
                break  # first working path per group only

        return leads

    def _fetch_vendor_reg(self, source_name, url, company):
        try:
            content = fetch_page(url, max_chars=8000)
        except Exception:
            return []
        if not content or len(content) < 500:
            return []

        leads = []
        for m in re.finditer(
            r"(?:Category|Commodity|Product)[:\s]+([A-Z][A-Za-z\s&.,]{3,80})",
            content, re.I,
        ):
            title = f"{company} vendor registration: {m.group(1).strip()}"
            # Use the hardcoded company name (L&T, Reliance) not AI-extracted
            leads.append(_dict_to_lead(source_name, company, url, title, title))
            if len(leads) >= 5:
                break
        return leads


register(CompanyAdapter())
