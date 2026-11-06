"""Optional, product-agnostic V32 search-vector adapter.

Uses existing Google Sheets settings and the existing V31 AI/router pipeline.
No CRM writes or persistent cache mutations occur here.
"""
import os
from datetime import datetime
from common import SearchEngine, clean_url, env_int, log
from standard_lead import StandardLead
from adapters import SourceAdapter

# Commercial signals recovered from the earlier broad B2B autopilot.
# Templates are generic: {target} is read from the CRM settings, never hardcoded.
VECTORS = (
    ("tender", "{target} tender procurement RFP {year}"),
    ("tender", "{target} rate contract annual procurement"),
    ("tender", "{target} expression of interest vendor empanelment"),
    ("tender", "{target} RFQ purchase requirement"),
    ("news", "{target} corporate procurement purchase order"),
    ("news", "{target} supply agreement contract awarded"),
    ("expansion", "{industry} new project investment capex {year}"),
    ("expansion", "{industry} new plant factory commissioning {year}"),
    ("expansion", "{industry} industrial land allotment expansion"),
    ("expansion", "{industry} environmental clearance new project"),
    ("expansion", "{industry} project RERA registration launch"),
    ("expansion", "{industry} greenfield brownfield expansion"),
    ("expansion", "{industry} memorandum of understanding investment"),
    ("expansion", "{industry} funding capacity expansion"),
    ("expansion", "{industry} new subsidiary incorporation operations"),
    ("hiring", "{target} specialist hiring company careers"),
    ("hiring", "{industry} project team recruitment expansion"),
    ("news", "{industry} technology adoption digital transformation project"),
    ("news", "{target} deployment implementation new customer"),
)

class HybridIntelligenceAdapter(SourceAdapter):
    name = "hybrid_intelligence"
    category = "expansion"
    priority = 35

    def is_enabled(self, settings):
        return os.getenv("RADAR_HYBRID_ENABLED", os.getenv("RADAR_V32_HYBRID", "1")).strip().lower() in ("1", "true", "yes") and bool(
            settings.get("targets") or settings.get("industries") or settings.get("directives")
        )

    def fetch(self, settings):
        search = SearchEngine()
        targets = [str(x).strip() for x in (settings.get("targets") or []) if str(x).strip()]
        directives = [str(x).strip() for x in (settings.get("directives") or []) if str(x).strip()]
        # A special prompt is an AI instruction, not a literal search query.
        # Reusing entire prompts as search terms polluted results in V32.
        if not targets:
            targets = directives[:4]
        industries = [str(x).strip() for x in (settings.get("industries") or []) if str(x).strip() and str(x).upper() != "ALL_SECTORS"]
        countries = [str(x).strip() for x in (settings.get("countries") or []) if str(x).strip()]
        states = [str(x).strip() for x in (settings.get("states") or []) if str(x).strip()]
        country = countries[0] if countries else ""
        location = states[0] if states else country
        max_queries = max(1, min(env_int("RADAR_V32_MAX_QUERIES", 12), 60))
        max_results = max(1, min(env_int("RADAR_V32_MAX_RESULTS", 50), 200))
        max_targets = max(1, min(env_int("RADAR_V32_MAX_TARGETS", 4), 30))
        year = datetime.now().year
        # Each run gets direct procurement, expansion, hiring, and adoption.
        # Rotating across the whole vector list formerly starved some tracks.
        groups = {cat: [] for cat in ("tender", "hiring", "expansion", "news")}
        for category, template in VECTORS:
            groups[category].append(template)
        day = datetime.now().toordinal()
        for category, templates in groups.items():
            turn = day % len(templates)
            groups[category] = templates[turn:] + templates[:turn]
        chosen_targets = targets[:max_targets] or industries[:max_targets]
        chosen_industries = industries[:max_targets] or chosen_targets
        selected = []
        # Interleave source categories and target words fairly.
        for round_idx in range(max_queries * 2):
            if len(selected) >= max_queries:
                break
            for category in ("tender", "hiring", "expansion", "news"):
                if len(selected) >= max_queries:
                    break
                if not groups[category]:
                    continue
                target = chosen_targets[round_idx % len(chosen_targets)][:85]
                industry = chosen_industries[round_idx % len(chosen_industries)][:85]
                template = groups[category][(round_idx // max(1, len(chosen_targets))) % len(groups[category])]
                # Without sectors, only expand on signals tied to the target.
                if category == "expansion" and not industries:
                    template = "{target} new project expansion investment {year}"
                query = template.format(target=target, industry=industry, year=year)
                if location:
                    query += " " + location[:60]
                pair = (category, query[:235], target)
                if pair not in selected:
                    selected.append(pair)
        seen, leads = set(), []
        for category, query, target in selected:
            if len(leads) >= max_results:
                break
            try:
                results = search.search(query, country=country, pages=1, recent=(category == "tender"))
            except Exception as exc:
                log.warning("[v32] search error %s: %s", category, str(exc)[:120])
                continue
            for result in results:
                url = (result.get("link") or "").strip()
                key = clean_url(url)
                if not key or key in seen or not url.startswith(("http://", "https://")):
                    continue
                seen.add(key)
                title = (result.get("title") or "").strip()
                snippet = (result.get("snippet") or result.get("summary") or "").strip()
                if not title and not snippet:
                    continue
                leads.append(StandardLead(
                    source=self.name, category=category, url=url,
                    title=title[:350], snippet=snippet[:1600],
                    raw_text=(title + "\n" + snippet)[:2200],
                    country="", structured={"search_target": target, "search_query": query,
                                                 "signal_type": category, "discovery_only": True},
                    metadata={"hybrid": True, "snippet_only": True, "target_product": target,
                              "search_query": query},
                ))
                if len(leads) >= max_results:
                    break
        log.info("[v32] hybrid: %d results from %d queries (target count %d)",
                 len(leads), len(selected), min(len(targets), max_targets))
        return leads
