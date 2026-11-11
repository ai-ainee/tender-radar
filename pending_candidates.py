"""Bounded public-evidence-only queue for candidates deferred by the AI budget.

Keeps URLs discovered near end of a harvest available for the next run.
It does NOT mark them verified, bypass existing quality gates, or write CRM.
Persist the JSON file via GitHub Actions along with the other cache files.
"""
from __future__ import annotations
import json
import os
from datetime import datetime, date
from pathlib import Path
from urllib.parse import urlparse
from standard_lead import StandardLead

DEFAULT_PATH = "radar_pending_candidates.json"
ALLOWED_METADATA = {
    "internal_source", "verification_status", "government_portal", "government_portal_name",
    "government_portal_kind", "government_portal_url", "source_index_domain", "portal",
    "source_listed_buyer", "source_reported_tender_id", "source_reported_deadline",
    "possible_gem_bid_refs", "indexed_bid_reference", "provisional_tender", "provisional",
    "official_source_verified", "source_fetched", "active_tender_verified", "buyer_verified",
    "current_deadline_verified", "requires_corrigendum_check", "review_required",
    "snippet_only", "fetch_method", "matched_target", "relevance_keyword_match", "track", "is_job_url",
    "google_news_rss", "source_publisher_url", "vendor_publisher",
    "corporate_signal_track", "source_kind", "source_scope", "matched_industry",
}


def _key(lead):
    # Only exact duplicate URLs/strong canonical IDs; never guess cross-site sameness.
    return f"{lead.source}|{lead.url.strip()}"


def _as_minimal(lead):
    md = lead.metadata if isinstance(lead.metadata, dict) else {}
    return {
        "source": str(lead.source or "")[:60],
        "category": str(lead.category or "")[:30],
        "url": str(lead.url or "")[:1500],
        "title": str(lead.title or "")[:300],
        "snippet": str(lead.snippet or "")[:1000],
        "raw_text": str(lead.raw_text or "")[:1400],
        "buyer_org": str(lead.buyer_org or "")[:200],
        "buyer_dept": str(lead.buyer_dept or "")[:200],
        "tender_id": str(lead.tender_id or "")[:120],
        "country": str(lead.country or "")[:80],
        "state": str(lead.state or "")[:120],
        "city": str(lead.city or "")[:120],
        "raw_published_at": str(lead.raw_published_at or "")[:60],
        "metadata": {k: v for k, v in md.items() if k in ALLOWED_METADATA
                     and isinstance(v, (str, int, float, bool, list))},
    }


def load_pending(path=DEFAULT_PATH, max_entries=500, max_age_days=30, today=None):
    today = today or date.today()
    if not Path(path).exists():
        return []
    try:
        if Path(path).stat().st_size > 3_000_000:
            return []
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or raw.get("version") != 1:
            return []
        items = raw.get("items")
        if not isinstance(items, list):
            return []
        out, seen = [], set()
        for entry in items[:max_entries]:
            if not isinstance(entry, dict) or not isinstance(entry.get("lead"), dict):
                continue
            try:
                created = date.fromisoformat(entry.get("saved_on", ""))
                age = (today - created).days
                if age < 0 or age > max_age_days:
                    continue
                lead = StandardLead.from_dict(entry["lead"])
                p = urlparse(lead.url)
                if p.scheme not in ("https", "http") or not p.hostname:
                    continue
                k = _key(lead)
                if k in seen:
                    continue
                seen.add(k)
                out.append(lead)
            except (ValueError, TypeError, KeyError):
                continue
        return out
    except (OSError, ValueError, TypeError):
        return []


def save_pending(leads, path=DEFAULT_PATH, max_entries=500, today=None):
    today = today or date.today()
    found, entries = set(), []
    for lead in leads:
        if not isinstance(lead, StandardLead):
            continue
        p = urlparse(str(lead.url or ""))
        if p.scheme not in ("http", "https") or not p.hostname:
            continue
        key = _key(lead)
        if key in found:
            continue
        found.add(key)
        entries.append({"saved_on": today.isoformat(), "lead": _as_minimal(lead)})
        if len(entries) >= max_entries:
            break
    parent = Path(path).parent
    parent.mkdir(parents=True, exist_ok=True)
    temp = str(path) + ".tmp"
    try:
        Path(temp).write_text(json.dumps({"version": 1, "items": entries},
                                         ensure_ascii=False, indent=1) + "\n",
                              encoding="utf-8")
        os.replace(temp, path)
        return len(entries)
    except OSError:
        try:
            Path(temp).unlink(missing_ok=True)
        except OSError:
            pass
        return -1
