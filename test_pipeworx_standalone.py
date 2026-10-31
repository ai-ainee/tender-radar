"""test_pipeworx_standalone.py — test Pipeworx adapter in isolation.

Loads Settings, runs the adapter, prints leads. No AI, no Sheet writes.
"""
import os
import traceback

def w(s):
    print(s, flush=True)
    with open("pipeworx_test_output.txt", "a", encoding="utf-8") as f:
        f.write(str(s) + "\n")

open("pipeworx_test_output.txt", "w").close()

w("=" * 60)
w("Pipeworx Adapter — Standalone Test")
w("=" * 60)

# Load settings
try:
    from common import SheetAPI, inject_api_keys_from_settings
    cfg_raw = SheetAPI().get("get_settings")
    if isinstance(cfg_raw, dict) and isinstance(cfg_raw.get("api_keys"), dict):
        inject_api_keys_from_settings(cfg_raw["api_keys"])
    w("[OK] Settings loaded from Sheet")
except Exception as e:
    w(f"[WARN] Settings load failed: {e}")
    traceback.print_exc()
    cfg_raw = {}

# Build minimal settings dict for adapter
targets_raw = cfg_raw.get("target_products") or []
if isinstance(targets_raw, list):
    targets = [str(t).strip() for t in targets_raw if str(t).strip()]
else:
    targets = [str(targets_raw).strip()] if targets_raw else []

# Fallback targets if Sheet is empty (adapter still runs)
if not targets:
    targets = ["autocad", "revit", "bim"]
    w(f"[WARN] No targets in Sheet, using fallback: {targets}")
else:
    w(f"[OK] Targets from Sheet: {targets[:5]}... ({len(targets)} total)")

settings = {"targets": targets}

# Run the adapter
try:
    from adapters.aggregator_tenders import PipeworxAdapter
    adapter = PipeworxAdapter()
    w(f"\n[INFO] Adapter enabled: {adapter.is_enabled(settings)}")

    leads = adapter.fetch(settings)
    w(f"\n[RESULT] {len(leads)} unique tender(s) collected")

    if leads:
        w("\nFirst 10 tenders:")
        for i, lead in enumerate(leads[:10]):
            w(f"\n  [{i+1}] {lead.title[:100]}")
            w(f"      Org:    {lead.buyer_org[:80]}")
            w(f"      ID:     {lead.tender_id}")
            w(f"      Closes: {lead.deadline}")
            w(f"      URL:    {lead.url[:100]}")
    else:
        w("\n[FAIL] Adapter returned 0 leads — check network or rate limit")

except Exception as e:
    w(f"\n[FATAL] Adapter crashed: {e}")
    traceback.print_exc()

w("\n" + "=" * 60)
w("DONE")
