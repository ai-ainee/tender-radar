"""adapters/filings.py — Corporate filings category adapter.

Sources:
  ✅ BSE      — via sources.bse (7-day window, alternate endpoint fallback).
                Falls back to legacy bse_filings.py if sources.bse not deployed.
  ✅ NSE      — via nse_filings.py (works, 1-3 leads/run)
  🔲 SEBI     — disabled (0 leads across 3+ runs)
  🔲 RBI      — disabled (all macro news, AI-rejected)
  🔲 MCA21    — requires login
  🔲 Patents  — requires API key

The BSE path was producing 0 leads in every run because the legacy module
used a 3-day window (too narrow for the ~5-15 announcements BSE publishes
per week across all sectors) and the primary endpoint returns the literal
string "No Record Found!" when the window is empty. sources.bse fixes both
with a 7-day window and alternate endpoint fallback.
"""
import re
import urllib.request
import xml.etree.ElementTree as ET

from adapters import SourceAdapter, register
from standard_lead import StandardLead
from common import log

# Legacy BSE module — used as fallback only if sources.bse is not deployed
try:
    from bse_filings import fetch_bse_filings as _fetch_bse_legacy
    _HAS_BSE_LEGACY = True
except ImportError:
    _fetch_bse_legacy = None
    _HAS_BSE_LEGACY = False

try:
    from nse_filings import fetch_nse_filings
    _HAS_NSE = True
except ImportError:
    fetch_nse_filings = None
    _HAS_NSE = False

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")


def _http_get(url, timeout=20, max_bytes=2_000_000):
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": UA,
            "Accept": "application/xml,application/atom+xml,text/xml,text/html;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-IN,en;q=0.9",
        })
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read(max_bytes).decode("utf-8", errors="replace")
    except Exception as e:
        log.debug(f"[filings] HTTP error {url[:60]}: {str(e)[:80]}")
        return ""


def _parse_rss(xml_text, keywords, source_name):
    """Parse RSS 2.0 or Atom. Kept for future SEBI/RBI re-enablement."""
    if not xml_text:
        return []
    kw_lower = [str(k).lower() for k in (keywords or []) if k]
    out = []
    try:
        root = ET.fromstring(xml_text)
        is_atom = root.tag.endswith("feed")
        item_tag = "entry" if is_atom else "item"
        for item in root.iter(item_tag):
            title = (item.findtext("title") or "").strip()
            link = (item.findtext("link") or "").strip()
            if not link and is_atom:
                for ln in item.findall("link"):
                    if ln.get("rel") in (None, "alternate"):
                        link = ln.get("href") or ""
                        break
            desc = (item.findtext("description")
                    or item.findtext("summary") or "").strip()
            date = (item.findtext("pubDate")
                    or item.findtext("published")
                    or item.findtext("updated") or "").strip()
            if not title or not link:
                continue
            if kw_lower:
                hay = (title + " " + desc).lower()
                if not any(k in hay for k in kw_lower):
                    continue
            out.append({
                "title": title[:300],
                "url": link,
                "snippet": re.sub(r"<[^>]+>", " ", desc)[:500],
                "date": date[:30],
            })
    except Exception as e:
        log.debug(f"[filings:{source_name}] RSS parse: {str(e)[:80]}")
    return out


def _dict_to_lead(source_name, r):
    return StandardLead(
        source="filings",
        category="filing",
        url=r.get("url", ""),
        title=str(r.get("title") or r.get("subject") or "")[:300],
        raw_text=str(r.get("snippet") or r.get("details") or "")[:3000],
        buyer_org=str(r.get("company") or "")[:200],
        raw_published_at=str(r.get("date") or ""),
        country="India",
        metadata={"internal_source": source_name},
    )


class FilingsAdapter(SourceAdapter):
    name = "filings"
    category = "filing"
    priority = 20

    def is_enabled(self, settings):
        return bool(settings.get("targets") or settings.get("industries"))

    def fetch(self, settings):
        leads = []
        for name, fetcher in [
            ("bse",     self._fetch_bse),
            ("nse",     self._fetch_nse),
            ("sebi",    self._fetch_sebi),
            ("rbi",     self._fetch_rbi),
            ("mca21",   self._fetch_mca21),
            ("patents", self._fetch_patents),
        ]:
            try:
                results = fetcher(settings)
                log.info(f"[filings] {name}: {len(results)} lead(s)")
                leads.extend(results)
            except Exception as e:
                log.warning(f"[filings] {name}: ERROR {str(e)[:120]}")
        return leads

    def _keywords(self, settings):
        """Filings never mention product names. Filter by industry +
        universal business-intent signals. Product relevance is judged
        downstream by the AI evaluator."""
        industries = settings.get("industries") or []
        industry_kw = [k for k in industries if k and k != "ALL_SECTORS"]
        intent_kw = [
            "capex", "expansion", "order", "contract", "acquisition",
            "investment", "facility", "plant", "construction", "tender",
            "commissioning", "greenfield", "brownfield", "joint venture",
            "subsidiary", "IPO", "rights issue", "fundraise",
        ]
        return industry_kw + intent_kw

    def _fetch_bse(self, settings):
        kw = self._keywords(settings)

        # Preferred: sources.bse (7-day window + alternate endpoint fallback)
        try:
            from sources.bse import fetch_bse
        except ImportError:
            fetch_bse = None

        if fetch_bse is not None:
            try:
                raw = fetch_bse(kw, max_days=15, max_results=60)
                if not isinstance(raw, list):
                    log.info(f"[filings:bse] sources.bse non-list ({type(raw).__name__})")
                    raw = []
                if raw:
                    log.debug(f"[filings:bse] sources.bse returned {len(raw)} row(s)")
                    return [_dict_to_lead("bse", r) for r in raw
                            if isinstance(r, dict) and r.get("url")]
                # Empty result — try legacy fallback in case sources.bse
                # had a transient failure
            except Exception as e:
                log.warning(f"[filings:bse] sources.bse ERROR {str(e)[:120]} — trying legacy")

        # Fallback: legacy bse_filings.py (3-day window)
        if not _HAS_BSE_LEGACY:
            return []
        try:
            raw = _fetch_bse_legacy(kw, max_days=3, max_results=60)
        except Exception as e:
            log.debug(f"[filings:bse] legacy {str(e)[:100]}")
            return []
        if not isinstance(raw, list):
            log.info(f"[filings:bse] legacy non-list ({type(raw).__name__})")
            return []
        return [_dict_to_lead("bse", r) for r in raw
                if isinstance(r, dict) and r.get("url")]

    def _fetch_nse(self, settings):
        if not _HAS_NSE:
            return []
        kw = self._keywords(settings)
        try:
            raw = fetch_nse_filings(kw, max_results=60)
        except Exception as e:
            log.debug(f"[filings:nse] {str(e)[:100]}")
            return []
        if not isinstance(raw, list):
            log.info(f"[filings:nse] non-list response ({type(raw).__name__}) — treating as 0 leads")
            return []
        return [_dict_to_lead("nse", r) for r in raw
                if isinstance(r, dict) and r.get("url")]

    def _fetch_sebi(self, settings):
        # Disabled: 0 leads across all runs
        return []

    def _fetch_rbi(self, settings):
        # Disabled: all leads were macro news (repo auctions, bond auctions) — AI rejected
        return []

    def _fetch_mca21(self, settings):
        log.info("[filings] mca21: not implemented (requires authentication)")
        return []

    def _fetch_patents(self, settings):
        log.info("[filings] patents: not implemented (requires API key or heavy scraping)")
        return []


register(FilingsAdapter())
