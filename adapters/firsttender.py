"""adapters/firsttender.py - FirstTender.com specialized adapter.

Deploy 1.1 changes:
  - Removed `site:` operator from queries (was stripped by SearchEngine
    → returning 0 leads). Now uses "firsttender.com <target> tender"
    which Google still prioritizes to that domain.
  - Added pre-check against CompanyDedup to skip CRM-known companies
    before fetch.
"""
import re

from common import SearchEngine, clean_url, fetch_page, log
from standard_lead import StandardLead
from adapters import SourceAdapter, register


DOMAIN = "firsttender.com"
DOMAIN_TOKEN = "firsttender"     # for query — Google treats as domain hint


class FirstTenderAdapter(SourceAdapter):
    name = "firsttender"
    category = "tender"
    priority = 30

    def __init__(self):
        self._search = None

    def is_enabled(self, settings):
        countries = settings.get("countries") or []
        if not countries:
            return bool(settings.get("targets"))
        return any("india" in str(c).lower() for c in countries)

    def fetch(self, settings):
        if self._search is None:
            self._search = SearchEngine()

        targets = settings.get("targets") or []
        states = settings.get("states") or []
        protected = settings.get("protected") or []
        company_dedup = settings.get("company_dedup")

        if not targets:
            return []

        # Deploy 1.1: removed `site:` operator.
        # Queries are "firsttender.com <target> tender" which Google
        # still prioritises to firsttender.com results without triggering
        # the site:-stripping behaviour of SearchEngine.clean_query().
        queries = []
        for target in targets[:5]:
            queries.append(f"{DOMAIN} {target} tender")
            queries.append(f"{DOMAIN} {target} bid india")
        if states:
            queries.append(f"{DOMAIN} tender {states[0]}")

        leads = []
        seen_urls = set()
        precheck_dupes = 0

        for q in queries:
            try:
                results = self._search.search(q, country="india", pages=1, recent=True)
            except Exception as e:
                log.warning(f"[firsttender] search failed: {str(e)[:100]}")
                continue

            for res in results:
                raw = (res.get("link") or "").strip()
                if not raw or DOMAIN not in raw.lower():
                    continue
                c = clean_url(raw)
                if not c or c in seen_urls:
                    continue
                seen_urls.add(c)

                # Deploy 1.1 hotfix: pre-check removed from aggregator adapters.
                # These adapters fetch category pages (e.g. /bids/building-tenders.html)
                # which don't describe a specific company. Pre-checking against
                # CRM known-set caused false positives (matched on aggregator's
                # own brand name in the page title).
                # Pre-check remains active only in the search adapter.
                pass

                try:
                    content = fetch_page(raw, protected_domains=protected)
                except Exception as e:
                    log.debug(f"[firsttender] fetch failed: {str(e)[:60]}")
                    content = ""

                if not content or len(content) < 120:
                    continue

                structured = _extract_hints(content)

                lead = StandardLead(
                    source=self.name,
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
                        "aggregator": "firsttender",
                    },
                )
                leads.append(lead)

        log.info(f"[firsttender] {len(leads)} lead(s) collected, "
                 f"{precheck_dupes} pre-check dupes skipped")
        return leads


def _extract_hints(text):
    """Extract tender hints via regex. Not exhaustive — just aids the AI."""
    hints = {}
    if not text:
        return hints

    # Value: ₹45,00,000 / Rs 4.5 Cr / INR 45 Lakh
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

    # Deadline
    for pat in (
        r"(?:closing|bid\s+end|deadline|last\s+date)[:\s]+(\d{1,2}[-/\s]\w{3,9}[-/\s]\d{2,4})",
        r"(?:closing|bid\s+end|deadline)[:\s]+(\d{4}-\d{2}-\d{2})",
        r"(?:closing|bid\s+end|deadline)[:\s]+(\d{1,2}/\d{1,2}/\d{2,4})",
    ):
        m = re.search(pat, text, re.I)
        if m:
            hints["deadline"] = m.group(1).strip()
            break

    # Tender ID
    m = re.search(r"\b(?:GEM|NIT|EOI|RFP)[/-][A-Z0-9][A-Z0-9/_-]{3,}\b", text)
    if m:
        hints["tender_id"] = m.group(0)

    # Buyer org
    m = re.search(r"(?:Buyer|Issued\s+by|Tender\s+by|Organisation)[:\s]+([A-Z][A-Za-z\s&.,]{5,80})", text)
    if m:
        hints["buyer"] = m.group(1).strip()[:80]

    return hints


register(FirstTenderAdapter())
