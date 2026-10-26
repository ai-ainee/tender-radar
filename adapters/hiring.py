"""adapters/hiring.py - Hiring signals adapter.

Migrates hiring_signals.py into the adapter framework. Reuses the existing
LLM-driven role generation + Naukri search. Produces StandardLead with
category="hiring" and buyer_org populated from the company name.

Hiring leads always route to Review (never Inbox) — they're predictive,
not procurement signals. The evaluator tags them HIRING_SIGNAL.
"""
from common import LLMRouter, fetch_page, log
from standard_lead import StandardLead
from adapters import SourceAdapter, register


class HiringAdapter(SourceAdapter):
    name = "naukri"
    category = "hiring"
    priority = 50           # runs after tenders, before generic search

    def is_enabled(self, settings):
        countries = settings.get("countries") or []
        if not countries:
            return bool(settings.get("targets"))
        return any("india" in str(c).lower() for c in countries)

    def fetch(self, settings):
        targets = settings.get("targets") or []
        if not targets:
            return []

        try:
            from hiring_signals import collect_hiring_signals
        except ImportError:
            log.warning("[hiring] hiring_signals.py not present — adapter disabled")
            return []

        llm = LLMRouter()
        if not llm.available():
            log.info("[hiring] no LLM keys available — skipping")
            return []

        try:
            hits = collect_hiring_signals(targets, llm, max_per_role=8)
        except Exception as e:
            log.warning(f"[hiring] collect_hiring_signals failed: {str(e)[:150]}")
            return []

        leads = []
        for h in hits:
            if not isinstance(h, dict):
                continue
            company = str(h.get("company") or "").strip()
            role = str(h.get("role") or "").strip()
            target = str(h.get("target_product") or "").strip()
            if not company or not role:
                continue

            slug = role.replace(" ", "-").lower()
            url = f"https://www.naukri.com/{slug}-jobs"

            lead = StandardLead(
                source=self.name,
                category="hiring",
                url=url,
                title=f"{role} hiring at {company}",
                snippet=f"{company} is hiring {role}",
                raw_text=(
                    f"[HIRING SIGNAL]\n"
                    f"COMPANY: {company}\n"
                    f"ROLE: {role}\n"
                    f"TARGET_PRODUCT: {target}\n"
                    f"Likely deployment within 60-90 days."
                ),
                buyer_org=company,
                buyer_type="Private",
                country="India",
                target_solution=target,
                metadata={
                    "role": role,
                    "target_product": target,
                    "fetch_method": "hiring_search",
                },
            )
            leads.append(lead)

        log.info(f"[hiring] {len(leads)} lead(s) collected")
        return leads


register(HiringAdapter())
