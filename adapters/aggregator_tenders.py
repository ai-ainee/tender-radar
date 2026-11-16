"""Optional Pipeworx government tenders bridge (aggregator, not official).

Uses target terms from Google Sheets Settings, never hardcoded software terms.
Failures are isolated; the collector must not block CPPP/state discovery.
"""
import os
import re
import json
from datetime import date
from urllib.parse import urlparse
import requests
from adapters import SourceAdapter, register
from standard_lead import StandardLead
from common import log

MCP_URL = "https://gateway.pipeworx.io/india-tenders/mcp"
GEM_ID = re.compile(r"^GEM/20\d{2}/B/\d{5,9}$", re.I)
CPPP_ID = re.compile(r"^20\d{2}_[A-Z0-9_]+_\d{3,}_\d+$", re.I)


def _queries_from_settings(settings, max_queries=3, today=None):
    terms = []
    for raw in settings.get("targets") or []:
        val = " ".join(str(raw).split())
        if 2 <= len(val) <= 90 and val.casefold() not in {t.casefold() for t in terms}:
            terms.append(val)
    if not terms or max_queries <= 0:
        return []
    today = today or date.today()
    count = min(max_queries, len(terms))
    start = ((today.toordinal() - 1) * count) % len(terms)
    return [terms[(start + i) % len(terms)] for i in range(count)]


def _canonical_source_id(raw):
    val = str(raw or "").strip().upper()
    return val if GEM_ID.fullmatch(val) or CPPP_ID.fullmatch(val) else ""


class PipeworxAdapter(SourceAdapter):
    name = "pipeworx_tenders"
    category = "tender"
    priority = 85

    def is_enabled(self, settings):
        return bool(settings.get("targets")) and os.getenv("PIPEWORX_ENABLED", "1") != "0"

    def fetch(self, settings):
        leads, seen_urls = [], set()
        limit = max(0, min(6, int(os.getenv("PIPEWORX_MAX_KEYWORD_CALLS", "3"))))
        terms = _queries_from_settings(settings, max_queries=limit)
        # Anonymous-service limits may change; errors never block other adapters.
        calls = []
        # Preserve the existing opt-in safeguard against irrelevant defence notices.
        if os.getenv("RADAR_PIPEWORX_DEFENCE_FEED", "0") == "1":
            calls.append(("india_defence_tenders", {}))
        calls.extend(("india_search_tenders", {"query": kw, "limit": 20}) for kw in terms)
        for tool_name, arguments in calls:
            data = self._call_tool(tool_name, arguments)
            if not isinstance(data, dict):
                continue
            rows = data.get("tenders") or []
            added = 0
            for item in rows if isinstance(rows, list) else []:
                if not isinstance(item, dict):
                    continue
                lead = self._to_lead(item)
                if not lead or lead.url in seen_urls:
                    continue
                seen_urls.add(lead.url)
                leads.append(lead)
                added += 1
            log.info("[pipeworx] %s returned %d candidate(s), accepted %d",
                     tool_name, len(rows), added)
        log.info("[pipeworx] %d provisional government references from %d calls",
                 len(leads), len(calls))
        return leads

    def _call_tool(self, tool_name, args, timeout=35):
        payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                   "params": {"name": tool_name, "arguments": args}}
        try:
            resp = requests.post(MCP_URL, json=payload,
                                 headers={"Content-Type": "application/json"}, timeout=timeout)
            resp.raise_for_status()
            reply = resp.json()
            if reply.get("error"):
                log.warning("[pipeworx] %s service error: %s", tool_name,
                            str(reply["error"])[:160])
                return None
            items = (reply.get("result") or {}).get("content") or []
            for part in items:
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    data = json.loads(part["text"])
                    if isinstance(data, dict) and isinstance(data.get("tenders"), list):
                        return data
            return None
        except Exception as exc:
            log.warning("[pipeworx] %s unavailable: %s", tool_name, str(exc)[:150])
            return None

    def _to_lead(self, record):
        url = str(record.get("source_url") or record.get("url") or "").strip()
        if urlparse(url).scheme not in ("https", "http") or not urlparse(url).hostname:
            return None
        source_ref = str(record.get("tender_id") or "").strip()
        # Preserve the original source URL; never invent or append a tender ID.
        tid = _canonical_source_id(source_ref)
        buyer = str(record.get("organisation") or "").strip()
        title = str(record.get("title") or "")[:300]
        if not title:
            return None
        return StandardLead(
            source="pipeworx", category="tender", url=url, title=title,
            tender_id=tid, buyer_org=buyer[:200], country="India",
            raw_published_at=str(record.get("published_at") or ""),
            snippet=("UNVERIFIED THIRD-PARTY TENDER REFERENCE. Confirm buyer, "
                     "item, latest deadline, status and corrigenda on original "
                     "portal before bidding. " + title)[:1000],
            raw_text=(f"Tender: {title}\n"
                      f"Claimed source reference: {source_ref}\n"
                      f"Source-listed organization: {buyer}\n"
                      f"Portal: {record.get('portal_label', '')}\n"
                      f"Reference number: {record.get('reference_number', '')}\n"
                      f"Organization chain: {record.get('organisation_chain', '')}\n"
                      f"Bid opening (UNVERIFIED): {record.get('bid_opening_at', '')}\n"
                      f"Source-listed closing date (UNVERIFIED): "
                      f"{record.get('bid_submission_closes_at', '')}\n")[:1500],
            metadata={
                "internal_source": "pipeworx",
                "source_listed_buyer": buyer,
                "source_reported_tender_id": source_ref,
                "source_reported_deadline": record.get("bid_submission_closes_at") or "",
                "portal": record.get("portal", ""),
                "verification_status": "unverified_aggregator",
                "review_required": True,
                "buyer_verified": False,
                "active_tender_verified": False,
                "current_deadline_verified": False,
                "official_source_verified": False,
                "provisional_tender": True,
                "snippet_only": True,
                "fetch_method": "third_party_aggregator",
            },
        )


register(PipeworxAdapter())
