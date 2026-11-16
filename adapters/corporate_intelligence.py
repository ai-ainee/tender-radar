"""Corporate direct-buy and early signal adapter, public evidence only."""
from adapters import SourceAdapter, register
from common import log
from standard_lead import StandardLead


def _to_lead(record):
    md = dict(record.get("metadata") or {})
    md.update({"internal_source": "corporate_public_index", "review_required": True,
               "buyer_verified": False, "current_deadline_verified": False})
    snippet = str(record.get("snippet") or "")
    return StandardLead(
        source="corporate_intelligence",
        category=str(record.get("category") or "news"),
        url=str(record.get("url") or ""),
        title=str(record.get("title") or "")[:300],
        snippet=snippet[:1000],
        raw_text=snippet[:1400],
        raw_published_at=str(record.get("date") or "")[:45],
        # Do not infer a purchasing company from a publisher or search hostname.
        buyer_org="",
        metadata=md,
    )


class CorporateIntelligenceAdapter(SourceAdapter):
    name = "corporate_intelligence"
    category = "mixed"
    priority = 33

    def is_enabled(self, settings):
        return bool(settings.get("targets") or settings.get("industries"))

    def fetch(self, settings):
        from sources.corporate_intelligence import fetch_corporate_intelligence
        records = fetch_corporate_intelligence(settings)
        leads = [_to_lead(r) for r in records if isinstance(r, dict) and r.get("url")]
        log.info("[corporate_intelligence] leads=%s direct=%s indirect=%s",
                 len(leads), sum(lead.category == "tender" for lead in leads),
                 sum(lead.category != "tender" for lead in leads))
        return leads


register(CorporateIntelligenceAdapter())
