"""
Radar Scout health check - run weekly by the workflow, on demand (job = health) or locally: python healthcheck.py
Verifies the things that silently rot: libraries, OCR tools, API keys, the Apps Script webhook, a live LLM call, a live search,
and Telegram. Sends a ✅/⚠️/❌ summary to Telegram and exits 1 when something CRITICAL is broken.
"""
import importlib
import os
import shutil
import sys
import time

from common import LLMRouter, SearchEngine, SheetAPI, SheetError, Telegram, log, run_job, tg_escape

RUN_INFO = {}
OK, WARN, BAD = "✅", "⚠️", "❌"


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

    try:                                              # the Apps Script webhook (also proves the secret + deployment are right)
        t0 = time.time()
        cfg = SheetAPI().get("get_settings")
        n_targets = len(cfg.get("target_products") or [])
        add(OK if n_targets else WARN, f"Apps Script webhook OK in {time.time() - t0:.1f}s; {n_targets} target product(s) in Settings", False)
    except SheetError as e:
        add(BAD, f"Apps Script webhook FAILED: {e} (check WEBHOOK_URL / WEBHOOK_SECRET and that the latest deployment is 'New version')", True)

    if live:
        try:
            out = llm.generate_json('Return only this JSON: {"ok": true}', validator=lambda d: isinstance(d, dict))
            used = ", ".join(f"{k}:{v}" for k, v in llm.calls.items()) or "?"
            add(OK if out else BAD, f"Live LLM call {'worked' if out else 'FAILED - every provider failed (quota, bad keys or outage)'} ({used})", not out)
        except Exception as e:
            add(BAD, f"Live LLM call crashed: {e}", True)
        try:
            res = SearchEngine().search("public tender notice", country="india")
            add(OK if res else BAD, f"Live search returned {len(res or [])} result(s)" if res else "Live search returned NOTHING (keys exhausted or blocked)", not res)
        except Exception as e:
            add(BAD, f"Live search crashed: {e}", True)

    crit = state["critical"]
    head = f"🩺 <b>Radar health check</b> - {'ALL GOOD' if not any(l.startswith((BAD, WARN)) for l in lines) else ('PROBLEMS' if crit else 'warnings')}"
    Telegram().send(head + "\n" + "\n".join(tg_escape(l) for l in lines))
    RUN_INFO.update(stats={"critical": crit, "checks": len(lines)})
    if crit:
        RUN_INFO.update(degraded=True, degraded_reason=f"{crit} critical check(s) failed")
    print("\n".join(lines))
    return 1 if crit else 0


if __name__ == "__main__":
    sys.exit(run_job("health", main, RUN_INFO))
