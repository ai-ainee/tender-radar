"""adapters/firsttender.py - FirstTender.com specialized adapter.

FirstTender is an Indian tender aggregator that re-publishes GeM/CPPP/state
tenders in clean, readable HTML. Unlike GeM itself, FirstTender pages are
fetchable from GitHub Actions IPs.

Strategy: run domain-targeted queries, fetch result pages, wrap in StandardLead.
FirstTender pages have consistent tender structure (value, deadline, buyer)
but parsing HTML is fragile — so we pass content to the AI for extraction.

FirstTender surfaced the Grid Controller ₹305 Cr tender in an earlier run.
This adapter makes that hit repeatable and discoverable systematically.
"""
import re

from common import SearchEngine, clean_url, fetch_page, log
from standard_lead import StandardLead
from adapters import SourceAdapter, register


DOMAIN = "firsttender.com"


class FirstTenderAdapter(SourceAdapter):
    name = "firsttender"
    category = "tender"
    priority = 30           # runs before generic search

    def __init__(self):
        self._search = None

    def is_enabled(self, settings):
        # Only if target countries include India
        countries = settings.get("countries") or []
        if not countries:
            return bool(settings.get("targets"))
        return any("india" in str(c).lower() for c in countries)

    def fetch(self, settings):
        if self._search is None:
            self._search = SearchEngine()

        targets = settings.get("targets") or []
        states = settings.get("states") or []
        banned_sites = settings.get("banned_sites") or []
        protected = settings.get("protected") or []

        if not targets:
            return []

        # Build domain-restricted queries — 2 per target
        queries = []
        for target in targets[:5]:
            queries.append(f"site:{DOMAIN} {target} tender")
            queries.append(f"site:{DOMAIN} {target} bid india")

        if states:
            queries.append(f"site:{DOMAIN} tender {states[0]}")

        leads = []
        seen_urls = set()

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

                # Fetch page
                try:
                    content = fetch_page(raw, protected_domains=protected)
                except Exception as e:
                    log.debug(f"[firsttender] fetch failed: {str(e)[:60]}")
                    content = ""

                if not content or len(content) < 120:
                    continue

                # Extract any visible tender details for structured hint
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

        log.info(f"[firsttender] {len(leads)} lead(s) collected")
        return leads


# ── Light structured hints (best-effort; AI still verifies) ───────────
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

    # Deadline: "Closing: 15-Nov-2026" / "Bid end: 2026-11-15"
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
