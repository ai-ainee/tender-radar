"""adapters/tenders.py — Tenders category adapter (CPPP only).

After testing all 28 candidate sources against GitHub Actions IPs:
  - GeM: WAF blocks datacenter IPs (connection refused, DNS fail)
  - CPPP: Cloudflare WAF returns HTTP 403 on direct hits — but Jina
          Reader proxy bypasses it
  - IREPS/NHAI: guessed URLs don't exist, needs real endpoint research
  - 13 state portals: require session cookies (JSESSIONID warm-up)
  - 10 PSUs: require authenticated portals
  - Defence: requires login

Only CPPP works from a datacenter IP. It uses Jina Reader as proxy
when direct fetch is blocked.

To add another source back later: only if verified working from a
GitHub Actions IP. Don't add sources speculatively — each failed source
costs ~10s of runtime per run with zero value.
"""
import os
import re
import urllib.request
import xml.etree.ElementTree as ET

from adapters import SourceAdapter, register
from standard_lead import StandardLead
from common import log

try:
    from cppp_feed import fetch_cppp
    _HAS_CPPP = True
except ImportError:
    fetch_cppp = None
    _HAS_CPPP = False

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
        try:
            results = self._fetch_cppp(settings)
            log.info(f"[tenders] cppp: {len(results)} lead(s)")
            leads.extend(results)
        except Exception as e:
            log.warning(f"[tenders] cppp: ERROR {str(e)[:120]}")
        return leads

    def _fetch_cppp(self, settings):
        targets = settings.get("targets") or []
        if not targets:
            return []

        # Try direct CPPP RSS first
        if _HAS_CPPP:
            try:
                raw = fetch_cppp(targets, max_results=100)
                if raw:
                    return [_dict_to_lead("cppp", r) for r in raw
                            if isinstance(r, dict) and r.get("url")]
            except Exception as e:
                log.debug(f"[tenders:cppp-direct] {str(e)[:80]}")

        # Fall back to Jina Reader proxy (bypasses Cloudflare WAF)
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
                    return [_dict_to_lead("cppp", r) for r in out]
            except Exception as e:
                log.debug(f"[tenders:cppp-jina] {str(e)[:80]}")
                continue

        return []


register(TendersAdapter())
