"""test_gem_serper.py — does Serper return GeM/CPPP URLs for site: queries?

This test tells us whether a 10-line change to discovery.py is enough, or
whether we need to write a direct state-portal scraper instead.
"""
import os

def w(s):
    print(s, flush=True)
    with open("gem_serper_output.txt", "a", encoding="utf-8") as f:
        f.write(s + "\n")

open("gem_serper_output.txt", "w").close()

w("=" * 60)
w("GeM/CPPP Discovery via Serper — site: query test")
w("=" * 60)

# Load API keys from Sheet the same way radar.py does
try:
    from common import SheetAPI, inject_api_keys_from_settings, SearchEngine
    cfg = SheetAPI().get("get_settings")
    if isinstance(cfg, dict) and isinstance(cfg.get("api_keys"), dict):
        inject_api_keys_from_settings(cfg["api_keys"])
    w(f"Serper keys: {'SET' if os.getenv('SERPER_API_KEYS') else 'EMPTY'}")
except Exception as e:
    w(f"[WARN] Settings load failed: {e}")

search = SearchEngine()

queries = [
    ("A. site:gem.gov.in direct",
     "autocad software license tender site:gem.gov.in"),
    ("B. site:bidplus.gem.gov.in",
     "autocad software license tender site:bidplus.gem.gov.in"),
    ("C. site:eprocure.gov.in",
     "autocad software license tender site:eprocure.gov.in"),
    ("D. Combined gov sites",
     "autocad software license tender "
     "(site:gem.gov.in OR site:bidplus.gem.gov.in OR site:eprocure.gov.in)"),
    ("E. Broad — no site filter",
     "autocad software license tender india government"),
]

for label, q in queries:
    w(f"\n--- {label} ---")
    try:
        results = search.search(q, country="india", pages=1, recent=False) or []
    except Exception as e:
        w(f"ERROR: {e}")
        continue

    gov_hits = sum(
        1 for r in results
        if any(d in (r.get("link") or "").lower()
               for d in ("gem.gov.in", "eprocure.gov.in", ".nic.in", ".gov.in"))
    )
    w(f"Total results: {len(results)}, gov-site hits: {gov_hits}")
    for r in results[:5]:
        link = (r.get("link") or "")[:100]
        title = (r.get("title") or "")[:70]
        w(f"  • {link}")
        w(f"    {title}")

w("\n" + "=" * 60)
w("DONE")
