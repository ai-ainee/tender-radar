"""
Radar Scout health check - run weekly by the workflow, on demand (job = health) or locally: python healthcheck.py

Deploy 2 additions:
  • Adapter health — which of the 4 source adapters are registered + enabled
  • Phone coverage — % of Contact Master rows with mobile / email / both
  • CompanyDedup size — how many companies are in the pre-check cache

Verifies the things that silently rot: libraries, OCR tools, API keys, the Apps Script
webhook, a live LLM call, a live search, Telegram. Sends a ✅/⚠️/❌ summary to Telegram
and exits 1 when something CRITICAL is broken.
"""
import importlib
import os
import shutil
import sys
import time

from common import (LLMRouter, SearchEngine, SheetAPI, SheetError, Telegram,
                    log, run_job, tg_escape)

RUN_INFO = {}
OK, WARN, BAD = "✅", "⚠️", "❌"


# ─────────────────────────────────────────────────────────────────────
# Existing: libraries
# ─────────────────────────────────────────────────────────────────────
def check_libs(add):
    for mod, why, critical in (("requests", "HTTP", True), ("bs4", "HTML parsing", True), ("pypdf", "PDF text", False),
                               ("dns", "e-mail MX checks", False), ("ddgs", "free search fallback", False),
                               ("pytesseract", "OCR for scanned PDFs", False), ("pdf2image", "OCR for scanned PDFs", False),
                               ("playwright", "JS/gated portal crawling", False)):
        try:
            importlib.import_module(mod)
        except Exception:
            add(BAD if critical else WARN, f"Python library '{mod}' missing ({why})", critical)
    for tool, why in (("tesseract", "OCR"), ("pdftoppm", "PDF to image for OCR")):
        if not shutil.which(tool):
            add(WARN, f"'{tool}' not installed ({why}); scanned PDFs will be skipped", False)

    # Deploy 2: verify the new phone/adapter modules are present
    for mod, why, critical in (("standard_lead", "StandardLead schema", True),
                               ("phone_extractor", "Phone extraction waterfall", True),
                               ("adapters", "Source adapter framework", True)):
        try:
            importlib.import_module(mod)
        except Exception as e:
            add(BAD, f"New module '{mod}' missing/broken ({why}): {str(e)[:80]}", critical)


# ─────────────────────────────────────────────────────────────────────
# Existing: API keys
# ─────────────────────────────────────────────────────────────────────
def check_keys(add):
    llm = LLMRouter()
    pools = {"Gemini": llm.gemini, "Groq": llm.groq, "OpenRouter": llm.openrouter, "Extra LLM": llm.extra}
    have = {n: len(p) for n, p in pools.items() if p}
    if not have:
        add(BAD, "No LLM API keys configured (GEMINI_API_KEYS / GROQ_API_KEYS / OPENROUTER_API_KEYS)", True)
    else:
        add(OK, "LLM keys: " + ", ".join(f"{n} x{c}" for n, c in have.items()), False)
        if len(have) == 1 and sum(have.values()) == 1:
            add(WARN, "Only ONE LLM key configured - add a second provider as a backup", False)
    if llm.openai:
        add(WARN, "OPENAI_API_KEY is set - this provider is PAID. Remove the secret to stay 100% free", False)
    s = SearchEngine()
    sk = len(getattr(s, "serper", []) or []) if hasattr(s, "serper") else 0
    bk = len(getattr(s, "brave", []) or []) if hasattr(s, "brave") else 0
    add(OK if (sk or bk) else WARN, f"Search keys: Serper x{sk}, Brave x{bk}" + ("" if (sk or bk) else " (only the free DuckDuckGo fallback; it is often blocked on GitHub IPs)"), False)
    if not (os.getenv("TELEGRAM_BOT_TOKEN") and os.getenv("TELEGRAM_CHAT_ID")):
        add(WARN, "Telegram is not configured - you will get no alerts from Python", False)
    return llm


# ─────────────────────────────────────────────────────────────────────
# NEW: Adapter health — which adapters are registered + enabled
# ─────────────────────────────────────────────────────────────────────
def check_adapters(add, sheet):
    try:
        from adapters import get_all, get_enabled
    except Exception as e:
        add(BAD, f"Adapter framework not importable: {str(e)[:80]}", True)
        return

    all_adapters = get_all()
    if not all_adapters:
        add(BAD, "No adapters registered — adapter auto-register failed", True)
        return

    # Fetch settings to test is_enabled()
    try:
        cfg = sheet.get("get_settings")
    except SheetError as e:
        add(WARN, f"Cannot check adapter enablement (settings fetch failed): {str(e)[:80]}", False)
        return

    enabled_names = {a.name for a in get_enabled(cfg)}
    all_names = sorted(a.name for a in all_adapters)
    disabled = [n for n in all_names if n not in enabled_names]

    if not enabled_names:
        add(BAD, f"All {len(all_adapters)} adapters are DISABLED for current Settings", True)
        return

    if disabled:
        add(WARN, f"Adapters: {len(enabled_names)}/{len(all_adapters)} enabled — "
                  f"enabled={sorted(enabled_names)} disabled={disabled}", False)
    else:
        add(OK, f"All {len(all_adapters)} adapters enabled: {all_names}", False)


# ─────────────────────────────────────────────────────────────────────
# NEW: Phone coverage — % of Contacts with mobile / email
# ─────────────────────────────────────────────────────────────────────
def check_phone_coverage(add, sheet):
    try:
        data = sheet.get("get_contact_index")
    except SheetError as e:
        add(WARN, f"Cannot check phone coverage (contact index failed): {str(e)[:80]}", False)
        return

    rows = data.get("contacts") if isinstance(data, dict) else None
    if not isinstance(rows, list) or not rows:
        add(WARN, "Contact Master empty — no phone/email coverage data yet", False)
        return

    total = len(rows)
    with_mobile = sum(1 for r in rows if r.get("mobile"))
    with_email = sum(1 for r in rows if r.get("email") and not str(r.get("email", "")).startswith("info@"))
    with_phone_only = sum(1 for r in rows if r.get("phone") and not r.get("mobile"))
    with_both = sum(1 for r in rows if r.get("mobile") and r.get("email"))

    mobile_pct = round(with_mobile / total * 100) if total else 0
    email_pct = round(with_email / total * 100) if total else 0
    both_pct = round(with_both / total * 100) if total else 0

    # Green if decent, yellow if low, red if almost zero
    mobile_level = OK if mobile_pct >= 20 else WARN if mobile_pct >= 5 else BAD
    email_level = OK if email_pct >= 30 else WARN if email_pct >= 10 else BAD

    add(mobile_level,
        f"Phone coverage: {with_mobile}/{total} contacts with mobile ({mobile_pct}%) "
        f"+ {with_phone_only} landline-only", False)
    add(email_level,
        f"Email coverage: {with_email}/{total} contacts with personal email ({email_pct}%)", False)
    if mobile_pct >= 20 and email_pct >= 30:
        add(OK, f"Contact quality healthy — {both_pct}% of contacts have BOTH mobile + email", False)
    elif mobile_pct < 5 and email_pct < 10:
        add(WARN, "Contact coverage very thin — enrichment may need tuning (check phone waterfall logs)", False)


# ─────────────────────────────────────────────────────────────────────
# NEW: CompanyDedup size — pre-check cache health
# ─────────────────────────────────────────────────────────────────────
def check_company_dedup(add):
    try:
        from adapters import CompanyDedup
        # Local cache only (no sheet call — the file lives on the runner)
        dedup = CompanyDedup(sheet=None)
        n = len(dedup)
        if n == 0:
            add(WARN, "CompanyDedup empty — pre-check will not save quota until first run repopulates it", False)
        elif n < 100:
            add(WARN, f"CompanyDedup only {n} companies — pre-check coverage is thin", False)
        else:
            add(OK, f"CompanyDedup cache: {n} companies (pre-check armed)", False)
    except Exception as e:
        add(WARN, f"CompanyDedup cache check failed: {str(e)[:80]}", False)


# ─────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────
def main(live=True):
    started = time.time()
    lines, state = [], {"critical": 0}

    def add(level, msg, critical=False):
        lines.append(f"{level} {msg}")
        if level == BAD and critical:
            state["critical"] += 1
        log.info(f"[health] {level} {msg}")

    check_libs(add)
    llm = check_keys(add)

    # Sheet connection (also required by adapter health + phone coverage checks)
    sheet = None
    try:
        t0 = time.time()
        sheet = SheetAPI()
        cfg = sheet.get("get_settings")
        n_targets = len(cfg.get("target_products") or [])
        add(OK if n_targets else WARN,
            f"Apps Script webhook OK in {time.time() - t0:.1f}s; "
            f"{n_targets} target product(s) in Settings", False)
    except SheetError as e:
        add(BAD, f"Apps Script webhook FAILED: {e} "
                 f"(check WEBHOOK_URL / WEBHOOK_SECRET and that the latest deployment is 'New version')", True)

    # Adapter health (needs a working sheet connection)
    if sheet:
        check_adapters(add, sheet)
        check_phone_coverage(add, sheet)

    # CompanyDedup local cache health (works even without sheet)
    check_company_dedup(add)

    if live:
        # Live LLM check
        try:
            out = llm.generate_json('Return only this JSON: {"ok": true}', validator=lambda d: isinstance(d, dict))
            used = ", ".join(f"{k}:{v}" for k, v in llm.calls.items()) or "?"
            add(OK if out else BAD,
                f"Live LLM call {'worked' if out else 'FAILED - every provider failed (quota, bad keys or outage)'} ({used})",
                not out)
        except Exception as e:
            add(BAD, f"Live LLM call crashed: {e}", True)

        # Live search check
        try:
            res = SearchEngine().search("public tender notice", country="india")
            add(OK if res else BAD,
                f"Live search returned {len(res or [])} result(s)" if res
                else "Live search returned NOTHING (keys exhausted or blocked)",
                not res)
        except Exception as e:
            add(BAD, f"Live search crashed: {e}", True)

    crit = state["critical"]
    has_warn = any(l.startswith(WARN) for l in lines)
    head = (f"🩺 <b>Radar health check</b> - "
            f"{'ALL GOOD' if not any(l.startswith((BAD, WARN)) for l in lines) else ('PROBLEMS' if crit else 'warnings')}")
    Telegram().send(head + "\n" + "\n".join(tg_escape(l) for l in lines))
    RUN_INFO.update(stats={"critical": crit, "checks": len(lines)})
    if crit:
        RUN_INFO.update(degraded=True, degraded_reason=f"{crit} critical check(s) failed")
    print("\n".join(lines))
    return 1 if crit else 0


if __name__ == "__main__":
    sys.exit(run_job("health", main, RUN_INFO))
