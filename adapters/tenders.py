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


def _dict_to_lead(source_name, r):
    return StandardLead(
        source="tenders",
        category="tender",
        url=r.get("url", ""),
        title=str(r.get("title", ""))[:300],
        raw_text=str(r.get("snippet", ""))[:3000],
        raw_published_at=str(r.get("date", "")),
        country="India",
        metadata={"internal_source": source_name},
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
            for r in results or []:
                if isinstance(r, dict) and r.get("url"):
                    leads.append(_dict_to_lead("gem", r))
            return len(results or [])
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
