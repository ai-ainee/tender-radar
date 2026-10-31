"""
test_all_platforms.py — One script to test free access to all 7 platforms.

Tests:
  1. TenderKosh — Free API (needs TENDERKOSH_API_KEY env var)
  2. Pipeworx — Free MCP (keyless, confirmed working)
  3. Bidrove — Free MCP (keyless, tests web search endpoint)
  4. TenderKart — Free web search (tests HTML scraping)
  5. BidAssist — Free web search (tests HTML scraping)
  6. Tender247 — Free web search (tests HTML scraping)
  7. TenderTiger — Free web search (tests HTML scraping)

Writes results to all_platforms_output.txt for artifact download.
"""
import json
import os
import re
import requests

def w(s):
    print(s, flush=True)
    with open("all_platforms_output.txt", "a", encoding="utf-8") as f:
        f.write(str(s) + "\n")

open("all_platforms_output.txt", "w").close()

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

HEADERS = {"User-Agent": UA, "Accept": "text/html,application/json"}

# ── TEST 1: TenderKosh Free API ──────────────────────────────────────
w("=" * 60)
w("TEST 1: TenderKosh (1,000 req/day free)")
w("=" * 60)

tk_key = os.getenv("TENDERKOSH_API_KEY", "").strip()
if not tk_key:
    w("[SKIP] TENDERKOSH_API_KEY not set in env")
else:
    try:
        r = requests.get(
            "https://api.tenderkosh.com/v1/tenders",
            headers={"Authorization": f"Bearer {tk_key}", **HEADERS},
            params={"sector": "Infrastructure", "limit": 3},
            timeout=30,
        )
        w(f"HTTP {r.status_code}")
        if r.status_code == 200:
            data = r.json()
            tenders = data.get("tenders", data if isinstance(data, list) else [])
            w(f"Tenders returned: {len(tenders)}")
            for t in tenders[:3]:
                w(f"  • {t.get('title', '')[:80]}")
        else:
            w(f"Error: {r.text[:300]}")
    except Exception as e:
        w(f"FAILED: {e}")

# ── TEST 2: Pipeworx MCP (50 calls/day, keyless) ─────────────────────
w("\n" + "=" * 60)
w("TEST 2: Pipeworx MCP (50 calls/day, keyless)")
w("=" * 60)

try:
    r = requests.post(
        "https://gateway.pipeworx.io/india-tenders/mcp",
        json={
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "india_search_tenders",
                       "arguments": {"query": "software", "limit": 3}}
        },
        headers={"Content-Type": "application/json"},
        timeout=60,
    )
    w(f"HTTP {r.status_code}")
    data = r.json()
    content = data.get("result", {}).get("content", [])
    if content and content[0].get("text"):
        result = json.loads(content[0]["text"])
        tenders = result.get("tenders", [])
        w(f"Tenders returned: {len(tenders)}")
        for t in tenders[:3]:
            w(f"  • {t.get('title', '')[:80]}")
            w(f"    Org: {t.get('organisation', '')[:60]}")
    else:
        w(f"Empty content: {str(data)[:200]}")
except Exception as e:
    w(f"FAILED: {e}")

# ── TEST 3: Bidrove Web Search (free, no account) ────────────────────
w("\n" + "=" * 60)
w("TEST 3: Bidrove Web Search (free, no account)")
w("=" * 60)

try:
    # Bidrove MCP is a local stdio server, but the web search is public.
    # Try fetching the search page directly.
    r = requests.get(
        "https://bidrove.com/search",
        params={"q": "software"},
        headers=HEADERS,
        timeout=30,
    )
    w(f"HTTP {r.status_code}, {len(r.text)} chars")
    if r.status_code == 200:
        # Look for tender-like patterns in HTML
        titles = re.findall(r'<a[^>]+>([^<]{20,200})</a>', r.text)
        tender_titles = [t for t in titles if any(
            w in t.lower() for w in ("tender", "bid", "rfp", "procurement", "software", "license")
        )]
        w(f"Tender-like anchors found: {len(tender_titles)}")
        for t in tender_titles[:5]:
            w(f"  • {t[:120]}")
    else:
        w(f"Preview: {r.text[:300]}")
except Exception as e:
    w(f"FAILED: {e}")

# ── TEST 4-7: Website Scraping (free web search) ─────────────────────
PLATFORMS = [
    ("TenderKart", "https://tenderkart.in/search", "q"),
    ("BidAssist", "https://bidassist.com/search", "q"),
    ("Tender247", "https://www.tender247.com/search", "keyword"),
    ("TenderTiger", "https://www.tendertiger.co.in/search", "q"),
]

for name, url, param in PLATFORMS:
    w(f"\n{'=' * 60}")
    w(f"TEST: {name} Web Search (free)")
    w(f"{'=' * 60}")
    try:
        r = requests.get(url, params={param: "software"}, headers=HEADERS, timeout=30)
        w(f"HTTP {r.status_code}, {len(r.text)} chars")
        if r.status_code == 200:
            # Look for tender titles in anchors
            anchors = re.findall(r'<a[^>]+>([^<]{15,200})</a>', r.text)
            hits = [a for a in anchors if any(
                w in a.lower() for w in ("tender", "bid", "rfp", "software", "license", "procurement")
            )]
            w(f"Tender-like anchors: {len(hits)}")
            for h in hits[:5]:
                w(f"  • {h[:120]}")
        else:
            w(f"Preview: {r.text[:200]}")
    except Exception as e:
        w(f"FAILED: {e}")

w("\n" + "=" * 60)
w("ALL TESTS COMPLETE")
w("=" * 60)
