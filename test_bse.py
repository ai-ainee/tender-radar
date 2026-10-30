"""test_bse.py — final BSE attempt via bse+ScraperAPI. Always writes output."""
import os
import traceback

def w(s):
    print(s, flush=True)
    with open("bse_diagnose_output.txt", "a", encoding="utf-8") as f:
        f.write(s + "\n")

open("bse_diagnose_output.txt", "w").close()

w("=" * 60)
w("BSE FINAL ATTEMPT — bse package + ScraperAPI session proxy")
w("=" * 60)

# Load keys from Sheet the same way radar.py does
try:
    from common import SheetAPI, inject_api_keys_from_settings
    cfg = SheetAPI().get("get_settings")
    if isinstance(cfg, dict) and isinstance(cfg.get("api_keys"), dict):
        inject_api_keys_from_settings(cfg["api_keys"])
    w(f"env SCRAPERAPI_API_KEYS: {'SET' if os.getenv('SCRAPERAPI_API_KEYS') else 'EMPTY'}")
    w(f"env HTTPS_PROXY (before fetch): {os.getenv('HTTPS_PROXY') or 'not set'}")
except Exception as e:
    w(f"[WARN] Could not load Settings: {e}")
    traceback.print_exc()

try:
    from sources.bse import fetch_bse
    w("\nCalling fetch_bse([], max_days=15, max_results=20)...")
    result = fetch_bse([], max_days=15, max_results=20)
    w(f"RESULT: {len(result)} row(s)")
    for i, r in enumerate(result[:5]):
        w(f"  [{i}] company={str(r.get('company',''))[:40]!r}")
        w(f"      title={str(r.get('title',''))[:80]!r}")
        w(f"      date={r.get('date')}")
except Exception as e:
    w(f"[FAIL] fetch_bse raised: {e}")
    traceback.print_exc()

w("=" * 60)
w("DONE")
