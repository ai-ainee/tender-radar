"""adapters/company.py — First-party company / portal signals adapter.

Watchlist entries can be either:
  • Full URLs to a specific tender/procurement page:
      https://www.nhai.gov.in/tenders
      https://www.cpwd.gov.in/tenders
      https://www.mhada.gov.in/en/tenders
  • Bare domains (auto-tries /tenders, /procurement, /rfp, etc.):
      tataprojects.com
      larsentoubro.com

For each entry, this adapter:
  1. Fetches the page
  2. Extracts procurement-signal links (tender, rfp, rfq, eoi, procurement,
     empanelment, vendor registration, bid, invitation to bid, supply,
     purchase, request for quotation)
  3. Emits one StandardLead per matching link, classified as category="tender"

If sources.company_watchlist is deployed, this adapter delegates to it.
Otherwise it uses the inline fallback below (same logic, no external dep).
"""
import re
from urllib.parse import urljoin, urlparse

from adapters import SourceAdapter, register
from standard_lead import StandardLead
from common import log, fetch_page

# Fixed public vendor registration portals — best-effort, often 0 leads
VENDOR_REGS = (
    ("vendor_reg_lt",  "https://www.larsentoubro.com/corporate/suppliers/",          "L&T"),
    ("vendor_reg_ril", "https://www.ril.com/OurCompany/SupplierRegistration.aspx",   "Reliance"),
)

# Common paths to try if a watchlist entry is a bare domain
PROCUREMENT_PATHS = (
    "/tenders", "/tender", "/procurement", "/rfp", "/rfq",
    "/eoi", "/empanelment", "/vendor-registration", "/vendors",
)

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


def _dict_to_lead(source_name, company, url, title, snippet):
    return StandardLead(
        source="company",
        category="tender",   # watchlist URLs are procurement pages
        url=url,
        title=str(title)[:300],
        raw_text=str(snippet)[:3000],
        buyer_org=str(company)[:200],
        country="India",
        metadata={"internal_source": source_name, "fetch_method": "watchlist"},
    )


def _clean_anchor(html):
    """Strip tags from an anchor body, collapse whitespace."""
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html or "")).strip()


class CompanyAdapter(SourceAdapter):
    name = "company"
    category = "tender"   # watchlist = tenders; vendor_reg still emits category="tender"
    priority = 80

    def is_enabled(self, settings):
        return bool(settings.get("targets"))

    def fetch(self, settings):
        leads = []
        watchlist = settings.get("company_watchlist") or []
        if isinstance(watchlist, str):
            watchlist = [w.strip() for w in watchlist.split(",") if w.strip()]

        # ── Watchlist (delegate to sources.company_watchlist if deployed) ──
        if watchlist:
            try:
                from sources.company_watchlist import fetch_company_watchlist
                results = fetch_company_watchlist(watchlist, max_per_site=5)
                for r in results or []:
                    if isinstance(r, dict) and r.get("url"):
                        leads.append(_dict_to_lead(
                            "watchlist",
                            r.get("company", ""),
                            r["url"],
                            r.get("title", ""),
                            r.get("snippet", ""),
                        ))
                log.info(f"[company] watchlist (via sources): {len(leads)} lead(s)")
            except ImportError:
                # Inline fallback — same logic, no external module
                before = len(leads)
                for entry in watchlist[:20]:
                    try:
                        inline = self._crawl_entry(entry)
                        log.info(f"[company] {entry}: {len(inline)} lead(s)")
                        leads.extend(inline)
                    except Exception as e:
                        log.warning(f"[company] {entry}: ERROR {str(e)[:120]}")
                log.info(f"[company] watchlist (inline): {len(leads) - before} lead(s)")
            except Exception as e:
                log.warning(f"[company] sources.company_watchlist: ERROR {str(e)[:120]}")
        else:
            log.info("[company] watchlist empty in Settings — skipping")

        # ── Fixed vendor registration portals (best-effort) ──
        for source_name, url, company in VENDOR_REGS:
            try:
                results = self._fetch_vendor_reg(source_name, url, company)
                log.info(f"[company] {source_name}: {len(results)} lead(s)")
                leads.extend(results)
            except Exception as e:
                log.warning(f"[company] {source_name}: ERROR {str(e)[:120]}")

        return leads

    def _crawl_entry(self, entry):
        """Fetch one watchlist entry, return procurement-signal leads.

        Accepts either a full URL (with path) or a bare domain.
        """
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

        leads = []
        for url in urls_to_try:
            try:
                html = fetch_page(url, max_chars=30000)
            except Exception:
                continue
            if not html or len(html) < 500:
                continue
            hits = self._extract_signal_links(html, url, host)
            for h in hits:
                leads.append(_dict_to_lead(
                    "watchlist", company, h["url"], h["title"], h["title"]
                ))
                if len(leads) >= 5:
                    return leads
            if leads:
                return leads  # first working path per entry

        return leads

    def _extract_signal_links(self, html, base_url, host):
        """Find anchors whose text indicates procurement signals, same-host only."""
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
            if len(out) >= 5:
                break
        return out

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
            leads.append(_dict_to_lead(source_name, company, url, title, title))
            if len(leads) >= 5:
                break
        return leads


register(CompanyAdapter())
