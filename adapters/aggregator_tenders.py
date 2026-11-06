"""adapters/aggregator_tenders.py — Pipeworx MCP tenders (free, keyless).

Two Pipeworx tools chained:
  1. india_defence_tenders — 25 open MoD tenders, no keyword needed
  2. india_search_tenders — keyword search across CPPP + state portals

Rate limit: 50 calls/day anonymous. This adapter uses 7 calls per run
(1 defence + 6 keyword searches), so it fits 7 harvest runs/day.

Verified against live Pipeworx endpoint 2026-10-31.
"""
import json
import os
import requests
from urllib.parse import urlparse, parse_qs

from adapters import SourceAdapter, register
from standard_lead import StandardLead
from common import log

MCP_URL = "https://gateway.pipeworx.io/india-tenders/mcp"
MCP_HEADERS = {"Content-Type": "application/json"}

# No static portfolio keywords. A campaign for pumps, medical equipment, BIM,
# or cloud services must query the user's configured targets, not CAD alone.


class PipeworxAdapter(SourceAdapter):
    name = "pipeworx_tenders"
    category = "tender"
    priority = 85

    def is_enabled(self, settings):
        return bool(settings.get("targets"))

    def fetch(self, settings):
        leads = []
        seen_tids = set()

        # Unfiltered defence notices produced most of the previous AI Trash.
        # Keep that broad feed as an explicit option, not the default.
        if os.getenv("RADAR_PIPEWORX_DEFENCE_FEED", "0") == "1":
            result = self._call_tool("india_defence_tenders", {})
            if result and isinstance(result.get("tenders"), list):
                for t in result["tenders"]:
                    tid = (t.get("tender_id") or "").strip()
                    if tid and tid not in seen_tids and t.get("source_url"):
                        seen_tids.add(tid)
                        leads.append(self._to_lead(t))

        # ── Phase 2: Keyword search (CPPP + all portals) ──
        configured = settings.get("targets") or []
        keywords = list(dict.fromkeys(str(s).strip()[:70] for s in configured if str(s).strip()))[:6]
        for kw in keywords:
            result = self._call_tool("india_search_tenders",
                                     {"query": kw, "limit": 20})
            if not result or not isinstance(result.get("tenders"), list):
                continue
            kw_added = 0
            for t in result["tenders"]:
                tid = (t.get("tender_id") or "").strip()
                if not tid or tid in seen_tids or not t.get("source_url"):
                    continue
                seen_tids.add(tid)
                leads.append(self._to_lead(t))
                kw_added += 1
            if kw_added:
                log.info(f"[pipeworx] '{kw}': {kw_added} new tender(s)")

        log.info(f"[pipeworx] total: {len(leads)} unique tender(s)")
        return leads

    def _call_tool(self, tool_name, args, timeout=60):
        payload = {
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": tool_name, "arguments": args},
        }
        try:
            r = requests.post(MCP_URL, json=payload,
                              headers=MCP_HEADERS, timeout=timeout)
            data = r.json()
            content = data.get("result", {}).get("content", [])
            if not content or not content[0].get("text"):
                return None
            return json.loads(content[0]["text"])
        except Exception as e:
            log.warning(f"[pipeworx] {tool_name} failed: {str(e)[:150]}")
            return None

    def _to_lead(self, t):
        tid = (t.get("tender_id") or "").strip()
        source_url = (t.get("source_url") or "").strip()
        # Do not append a second tid= parameter to portal URLs already carrying
        # a distinct tender ID: that can break official tender detail pages.
        query_ids = parse_qs(urlparse(source_url).query)
        has_id = any(k.lower() in ("tid", "tenderid", "tender_id", "bidid") for k in query_ids)
        url = (source_url + ("&" if "?" in source_url else "?") + "tid=" + tid) \
            if source_url and tid and not has_id else source_url

        return StandardLead(
            source="pipeworx",
            category="tender",
            url=url,
            title=(t.get("title") or "")[:300],
            buyer_org=(t.get("organisation") or "")[:200],
            deadline=t.get("bid_submission_closes_at") or "",
            tender_id=tid,
            raw_published_at=t.get("published_at") or "",
            raw_text=(
                f"Tender: {t.get('title', '')}\n"
                f"Reference: {t.get('reference_number', '')}\n"
                f"Organisation: {t.get('organisation', '')}\n"
                f"Chain: {t.get('organisation_chain', '')}\n"
                f"Portal: {t.get('portal_label', '')}\n"
                f"Published: {t.get('published_at', '')}\n"
                f"Bid closes: {t.get('bid_submission_closes_at', '')}\n"
                f"Bid opens: {t.get('bid_opening_at', '')}\n"
            ),
            country="India",
            metadata={
                "internal_source": "pipeworx",
                "portal": t.get("portal", ""),
                "target_product": "",  # specific match assessed from the tender title
            },
        )


register(PipeworxAdapter())
