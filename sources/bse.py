"""sources/bse.py — BSE via bse package + ScraperAPI proxy.

Why this shape:
  • Akamai blocks datacenter IPs at the TLS layer (403 on direct requests).
  • ScraperAPI's residential proxy bypasses that (200).
  • But the BSE API also requires session cookies from bseindia.com
    before it will return JSON — a plain proxy request gets the SPA
    homepage HTML instead.
  • The `bse` package does the warmup (gets cookies) correctly.
  • We inject the ScraperAPI proxy via HTTPS_PROXY so its internal
    requests.Session routes everything — warmup AND API call — through
    the same residential IP (session-persistent proxy).
"""
import os
import tempfile
import time
import urllib.parse
from datetime import datetime, timedelta

from common import log

try:
    from bse import BSE
    _HAS_BSE_PKG = True
except ImportError:
    _HAS_BSE_PKG = False


def _get_scraperapi_key():
    raw = os.getenv("SCRAPERAPI_API_KEYS", "")
    keys = [k.strip() for k in raw.replace('"', "").replace("'", "").split(",") if k.strip()]
    return keys[0] if keys else None


def fetch_bse(keywords, max_days=15, max_results=60):
    if not _HAS_BSE_PKG:
        log.warning("[bse] bse package not installed — skipping")
        return []

    key = _get_scraperapi_key()
    if not key:
        log.warning("[bse] SCRAPERAPI_API_KEYS not set — skipping")
        return []

    max_days = max(int(max_days), 15)
    kw_lower = [str(k).lower().strip() for k in (keywords or []) if k]

    from_date = datetime.now() - timedelta(days=max_days)
    to_date = datetime.now()

    # ScraperAPI session-persistent proxy URL.
    # The -session-number-N suffix keeps the same residential IP for all
    # requests in the session, so BSE cookies from warmup survive the
    # handoff to the API endpoint.
    session_num = int(time.time()) % 10000
    proxy_url = (
        f"http://scraperapi-session-number-{session_num}:{key}"
        f"@proxy-server.scraperapi.com:8001"
    )

    # Save + inject proxy env vars for the bse package's requests.Session
    saved_http = os.environ.get("HTTP_PROXY")
    saved_https = os.environ.get("HTTPS_PROXY")
    os.environ["HTTP_PROXY"] = proxy_url
    os.environ["HTTPS_PROXY"] = proxy_url

    data = None
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            with BSE(download_folder=tmpdir) as bse:
                # Belt and suspenders — also set on session if the package
                # exposes it (depends on bse package version internals)
                for attr in ("session", "_session"):
                    sess = getattr(bse, attr, None)
                    if sess is not None and hasattr(sess, "proxies"):
                        try:
                            sess.proxies.update(
                                {"http": proxy_url, "https": proxy_url}
                            )
                        except Exception:
                            pass
                        break

                log.info(f"[bse] warming up + fetching via ScraperAPI (last {max_days}d)")
                data = bse.announcements(from_date=from_date, to_date=to_date)
    except Exception as e:
        log.warning(f"[bse] bse+ScraperAPI failed: {str(e)[:200]}")
        return []
    finally:
        if saved_http is None:
            os.environ.pop("HTTP_PROXY", None)
        else:
            os.environ["HTTP_PROXY"] = saved_http
        if saved_https is None:
            os.environ.pop("HTTPS_PROXY", None)
        else:
            os.environ["HTTPS_PROXY"] = saved_https

    if not isinstance(data, dict) or not isinstance(data.get("Table"), list):
        log.info(
            f"[bse] no Table in response: "
            f"{list(data.keys()) if isinstance(data, dict) else type(data).__name__}"
        )
        return []

    rows = data["Table"]
    if not rows:
        log.info(f"[bse] 0 announcements in last {max_days}d")
        return []

    log.info(f"[bse] {len(rows)} raw announcement(s)")

    out = []
    for item in rows:
        if not isinstance(item, dict):
            continue
        subject = str(item.get("NEWSSUB") or item.get("HEADLINE") or "").strip()
        details = str(item.get("MORE") or "").strip()
        scrip = str(item.get("SLONGNAME") or item.get("Scripname") or "").strip()
        if not subject:
            continue
        if kw_lower:
            hay = (subject + " " + details + " " + scrip).lower()
            if not any(k in hay for k in kw_lower):
                continue
        attachment = str(item.get("ATTACHMENTNAME") or "").strip()
        if attachment:
            pdf_url = (
                "https://www.bseindia.com/xml-data/corpfiling/AttachLive/"
                + urllib.parse.quote(attachment)
            )
        else:
            pdf_url = (
                "https://www.bseindia.com/corporates/ann.html?scrip="
                + urllib.parse.quote(scrip)
            )
        out.append({
            "url": pdf_url,
            "title": subject[:300],
            "snippet": details[:800],
            "company": scrip[:200],
            "date": str(item.get("NEWS_DT") or "")[:10],
        })
        if len(out) >= max_results:
            break

    log.info(f"[bse] {len(out)} matching row(s) after keyword filter")
    return out
