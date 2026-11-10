"""adapters/tenders.py — Tenders category adapter.

Sources:
  ✅ GeM      — via sources.gem (Playwright). Skipped if module not yet present.
  ✅ CPPP     — via sources.cppp if present, else inline Jina/ScraperAPI fallback.
  🔲 State    — via sources.state_portals if present (optional, MA first).

Every source import is guarded so the adapter runs correctly whether or not
the corresponding sources/ module has been deployed yet. This lets you add
source files one at a time without breaking the adapter.

Design: each _try_* helper returns the number of leads it produced and
appends to `leads` in place. Sources that aren't installed return 0/-1
silently, so the log only shows lines for modules that actually ran.
"""
import os
import re
import urllib.request
from urllib.parse import urlparse
import xml.etree.ElementTree as ET

from adapters import SourceAdapter, register
from standard_lead import StandardLead
from common import log

# Legacy CPPP module — kept as last-resort fallback
try:
    from cppp_feed import fetch_cppp as _fetch_cppp_legacy
    _HAS_CPPP_LEGACY = True
except ImportError:
    _fetch_cppp_legacy = None
    _HAS_CPPP_LEGACY = False

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")


# A GeM bid reference found by web search is NOT evidence that the actual
# government bid is active, that its buyer is confirmed, or that its deadline
# is accurate. Preserve this distinction after source-to-StandardLead mapping.
_GEM_REF = re.compile(r"\bGEM\s*/\s*(20\d{2})\s*/\s*B\s*/\s*(\d{5,9})\b", re.I)
_GEM_DIRECT_PATH = re.compile(
    r"^/(?:showbidDocument/\d+|bidding/bid/[^?#]+)", re.I
)


def _gem_source_status(r):
    """Fail closed: only direct GeM links without fallback markers are official links.

    'official_listing_link' means the link was taken from GeM's listing,
    not that the tender is active. Everything else remains unverified.
    """
    url = str(r.get("url") or "").strip()
    snippet = str(r.get("snippet") or "")
    title = str(r.get("title") or "")
    embedded = r.get("metadata") if isinstance(r.get("metadata"), dict) else {}
    marked_unverified = (
        "UNVERIFIED SEARCH-INDEX DISCOVERY" in snippet.upper()
        or "UNVERIFIED SEARCH-INDEX DISCOVERY" in title.upper()
        or embedded.get("provisional") is True
        or str(embedded.get("verification_status", "")).lower() in
           ("unverified_index", "provisional", "unverified")
    )
    parsed = urlparse(url)
    is_direct_geM_link = (
        parsed.scheme == "https"
        and (parsed.hostname or "").lower() == "bidplus.gem.gov.in"
        and bool(_GEM_DIRECT_PATH.match(parsed.path))
    )
    return "official_listing_link" if is_direct_geM_link and not marked_unverified else "unverified_index"


def _gem_bid_refs(value):
    """Extract distinct, canonical GeM bid numbers (never guess IDs from URLs)."""
    return {
        f"GEM/{m.group(1)}/B/{m.group(2)}"
        for m in _GEM_REF.finditer(str(value or ""))
    }


def _safe_gem_tender_id(metadata, title, snippet):
    """Map a bid reference only if all available evidence agrees.

    Source-index metadata identifies the selected bid; title/snippet confirm
    it where present. A conflict or ambiguity leaves tender_id unset so the
    normal URL fallback applies, rather than attaching a WRONG canonical ID.
    """
    source_ref = str(metadata.get("indexed_bid_reference") or "").strip()
    source_refs = _gem_bid_refs(source_ref)
    # Metadata should contain only a bid number, not a free-text statement.
    if source_ref:
        match = re.fullmatch(
            r"GEM\s*/\s*(20\d{2})\s*/\s*B\s*/\s*(\d{5,9})",
            source_ref, re.I,
        )
        if not match:
            return "", "invalid-indexed-reference"
        source_refs = {f"GEM/{match.group(1)}/B/{match.group(2)}"}

    title_refs = _gem_bid_refs(title)
    snippet_refs = _gem_bid_refs(snippet)
    all_refs = source_refs | title_refs | snippet_refs
    if len(all_refs) > 1:
        return "", "conflicting-bid-references"
    if not all_refs:
        return "", "no-bid-number"
    return next(iter(all_refs)), "canonical"


def _dict_to_lead(source_name, r):
    source_metadata = r.get("metadata") if isinstance(r.get("metadata"), dict) else {}
    metadata = dict(source_metadata)
    metadata["internal_source"] = source_name
    raw_snippet = str(r.get("snippet") or "")
    title = str(r.get("title") or "")
    url = str(r.get("url") or "")

    tender_id = ""
    if source_name == "gem":
        status = _gem_source_status(r)
        # The result includes only an excerpt from a listing or search index.
        # Neither form proves active status, deadline or purchasing buyer.
        metadata.update({
            "verification_status": status,
            "official_source_verified": status == "official_listing_link",
            "active_tender_verified": False,
            "snippet_only": True,
            "fetch_method": ("official_listing" if status == "official_listing_link"
                             else "search_index"),
            "provisional_tender": status == "unverified_index",
        })
        tender_id, mapping_reason = _safe_gem_tender_id(metadata, title, raw_snippet)
        if tender_id:
            metadata["indexed_bid_reference"] = tender_id
            metadata["tender_id_mapping"] = "canonical_from_index_or_listing"
        else:
            # Record the raw source claim for diagnostics but do not expose an
            # ambiguous/invalid claim as a canonical indexed_bid_reference.
            source_claim = metadata.pop("indexed_bid_reference", None)
            if source_claim:
                metadata["unconfirmed_indexed_bid_reference"] = str(source_claim)
            metadata["tender_id_mapping"] = mapping_reason
            if mapping_reason != "no-bid-number":
                log.warning("[tenders] GeM ID mapping skipped (%s): %s",
                            mapping_reason, url[:150])
        if status == "unverified_index":
            # Keep a warning in the content seen by the AI, even if the source
            # metadata is not included in the model prompt.
            if "UNVERIFIED SEARCH-INDEX DISCOVERY" not in raw_snippet.upper():
                raw_snippet = (
                    "UNVERIFIED SEARCH-INDEX DISCOVERY: official GeM tender, "
                    "buyer, item, deadline and active status NOT verified. " + raw_snippet
                )

    return StandardLead(
        source="tenders",
        category="tender",
        url=url,
        tender_id=tender_id,  # Set BEFORE StandardLead computes its stable id.
        title=title[:300],
        snippet=raw_snippet[:1000],
        raw_text=raw_snippet[:3000],
        raw_published_at=str(r.get("date") or ""),
        country="India",
        metadata=metadata,
    )


class TendersAdapter(SourceAdapter):
    name = "tenders"
    category = "tender"
    priority = 15

    def is_enabled(self, settings):
        if not settings.get("targets"):
            return False
        countries = settings.get("countries") or []
        if not countries:
            return True
        return any("india" in str(c).lower() for c in countries)

    def fetch(self, settings):
        leads = []
        targets = settings.get("targets") or []
        if not targets:
            return leads

        # ── GeM (via sources.gem / Playwright) ─────────────────────
        gem_count = self._try_gem(targets, leads)
        log.info(f"[tenders] gem: {gem_count} lead(s)")

        # ── CPPP (sources.cppp if present, else inline) ────────────
        cppp_count = self._try_cppp(targets, leads)
        log.info(f"[tenders] cppp: {cppp_count} lead(s)")

        # ── State portals (optional; only logs if module present) ──
        state_count = self._try_state(targets, leads)
        if state_count >= 0:
            log.info(f"[tenders] state: {state_count} lead(s)")

        log.info(f"[tenders] total: {len(leads)} lead(s)")
        return leads

    def _try_gem(self, targets, leads):
        try:
            from sources.gem import fetch_gem
        except ImportError:
            return 0  # module not deployed yet — silent skip
        try:
            results = fetch_gem(targets, max_results=80)
            appended = 0
            provisional = 0
            for r in results or []:
                if isinstance(r, dict) and r.get("url"):
                    lead = _dict_to_lead("gem", r)
                    leads.append(lead)
                    appended += 1
                    md = lead.metadata
                    if md.get("provisional_tender"):
                        provisional += 1
                        log.info(
                            "[GEM-PROVISIONAL] ref=%s url=%s (NOT VERIFIED)",
                            md.get("indexed_bid_reference", "UNKNOWN"),
                            lead.url[:250],
                        )
            log.info(
                "[tenders] gem provenance: %d indexed-unverified, "
                "%d official-listing links (active status not verified)",
                provisional, appended - provisional,
            )
            return appended
        except Exception as e:
            log.warning(f"[tenders] gem: ERROR {str(e)[:120]}")
            return 0

    def _try_cppp(self, targets, leads):
        # Preferred: sources.cppp module (has direct + Jina + ScraperAPI chain)
        try:
            from sources.cppp import fetch_cppp as _fetch_cppp_new
            try:
                results = _fetch_cppp_new(targets, max_results=100)
                for r in results or []:
                    if isinstance(r, dict) and r.get("url"):
                        leads.append(_dict_to_lead("cppp", r))
                return len(results or [])
            except Exception as e:
                log.warning(f"[tenders] cppp (sources): ERROR {str(e)[:120]}")
                # Fall through to inline fallback
        except ImportError:
            pass  # sources.cppp not deployed — use inline

        # Fallback: inline CPPP (works today via Jina)
        try:
            results = self._fetch_cppp_inline(targets)
            for r in results or []:
                if isinstance(r, dict) and r.get("url"):
                    leads.append(_dict_to_lead("cppp", r))
            return len(results or [])
        except Exception as e:
            log.warning(f"[tenders] cppp inline: ERROR {str(e)[:120]}")
            return 0

    def _try_state(self, targets, leads):
        """Returns -1 if module not present (silent), else lead count."""
        try:
            from sources.state_portals import fetch_state_portals
        except ImportError:
            return -1
        try:
            results = fetch_state_portals(targets, states=["mh"])
            for r in results or []:
                if isinstance(r, dict) and r.get("url"):
                    leads.append(_dict_to_lead("state", r))
            return len(results or [])
        except Exception as e:
            log.warning(f"[tenders] state: ERROR {str(e)[:120]}")
            return 0

    def _fetch_cppp_inline(self, targets):
        """Original CPPP logic — works today, kept as fallback."""
        # Legacy cppp_feed.py (if still present in repo)
        if _HAS_CPPP_LEGACY:
            try:
                raw = _fetch_cppp_legacy(targets, max_results=100)
                if raw:
                    return [r for r in raw
                            if isinstance(r, dict) and r.get("url")]
            except Exception as e:
                log.debug(f"[tenders:cppp-legacy] {str(e)[:80]}")

        # Jina Reader proxy fallback
        jina_key = os.getenv("JINA_API_KEY", "").strip()
        headers = {"User-Agent": UA}
        if jina_key:
            headers["Authorization"] = f"Bearer {jina_key}"

        for cppp_url in (
            "https://eprocure.gov.in/cppp/rssdldtlist",
            "https://eprocure.gov.in/cppp/latestactivetendersnew/cpppdata",
        ):
            try:
                req = urllib.request.Request(
                    f"https://r.jina.ai/{cppp_url}",
                    headers=headers,
                )
                with urllib.request.urlopen(req, timeout=30) as r:
                    body = r.read(1_500_000).decode("utf-8", errors="replace")
                if not body or "<item>" not in body:
                    continue
                kw_lower = [str(k).lower() for k in targets if k]
                out = []
                root = ET.fromstring(body)
                for item in root.iter("item"):
                    title = (item.findtext("title") or "").strip()
                    link = (item.findtext("link") or "").strip()
                    desc = (item.findtext("description") or "").strip()
                    date = (item.findtext("pubDate") or "").strip()
                    if not title or not link:
                        continue
                    hay = (title + " " + desc).lower()
                    if kw_lower and not any(k in hay for k in kw_lower):
                        continue
                    out.append({
                        "title": title[:300],
                        "url": link,
                        "snippet": re.sub(r"<[^>]+>", " ", desc)[:500],
                        "date": date[:30],
                    })
                    if len(out) >= 100:
                        break
                if out:
                    return out
            except Exception as e:
                log.debug(f"[tenders:cppp-jina] {str(e)[:80]}")
                continue

        return []


register(TendersAdapter())
