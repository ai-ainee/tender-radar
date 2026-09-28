import os
import re
import json
import logging
import warnings
import asyncio
import aiohttp
import requests
from google import genai
from google.genai import types
from tenacity import retry, wait_exponential, stop_after_attempt

warnings.filterwarnings("ignore")
logging.getLogger("google.genai.models").setLevel(logging.ERROR)

WEBHOOK = os.environ.get("GOOGLE_SHEET_WEBHOOK")
SECRET = os.environ.get("WEBHOOK_SECRET")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
SERPER_KEY = os.environ.get("SERPER_API_KEY")

raw_keys = os.environ.get("GEMINI_API_KEY", "")
GEMINI_KEYS = [k.strip() for k in raw_keys.split(",") if k.strip()]
current_key_index = 0

def get_next_gemini_client():
    global current_key_index
    if not GEMINI_KEYS: return None
    key = GEMINI_KEYS[current_key_index]
    current_key_index = (current_key_index + 1) % len(GEMINI_KEYS)
    return genai.Client(api_key=key)

BEST_MODEL_STACK = []
def get_flash_model_stack(client):
    global BEST_MODEL_STACK
    if BEST_MODEL_STACK: return BEST_MODEL_STACK
    try:
        valid_models = [m.name.lower() for m in client.models.list() if re.match(r'^models/gemini-\d+\.\d+-flash$', m.name.lower())]
        if valid_models:
            valid_models.sort(key=lambda x: float(re.search(r'\d+\.\d+', x).group()), reverse=True)
            BEST_MODEL_STACK = valid_models
            return BEST_MODEL_STACK
    except Exception: pass
    BEST_MODEL_STACK = ["gemini-2.5-flash", "gemini-2.0-flash"]
    return BEST_MODEL_STACK

async def async_serper_search(session, query, num=3):
    if not SERPER_KEY: return []
    try:
        async with session.post("https://google.serper.dev/search", headers={'X-API-KEY': SERPER_KEY, 'Content-Type': 'application/json'}, data=json.dumps({"q": query, "gl": "in", "num": num}), timeout=20) as response:
            if response.status == 200: return [r.get("snippet", "") for r in (await response.json()).get("organic", [])]
    except Exception: pass
    return []

@retry(wait=wait_exponential(multiplier=2, min=4, max=30), stop=stop_after_attempt(5))
def generate_deal_dossier(lead, context_data):
    client = get_next_gemini_client()
    if not client: return "AI Unavailable."
    model_stack = get_flash_model_stack(client)
    
    prompt = f"Write Deal Strategy Brief for {lead['org']} (DM: {lead['dm_name']}, Industry: {lead['industry']}). Context: {json.dumps(context_data)}. Output JSON with key 'dossier' containing text report: 1. Company Profile 2. Recent Signals 3. Sales Pitch Strategy."
    schema = {"type": "OBJECT", "properties": {"dossier": {"type": "STRING"}}}
    
    for model_name in model_stack:
        try:
            res = client.models.generate_content(model=model_name, contents=prompt, config=types.GenerateContentConfig(response_mime_type="application/json", response_schema=schema, temperature=0.2))
            raw_text = res.text.strip()
            if raw_text.startswith("```"): raw_text = re.sub(r'^```(?:json)?|```$', '', raw_text, flags=re.IGNORECASE | re.MULTILINE).strip()
            return json.loads(raw_text).get("dossier", "No intel generated.")
        except Exception as e:
            if "503" in str(e) or "500" in str(e) or "limit: 0" in str(e): continue
            raise e
    raise Exception("Models unavailable.")

async def async_send_dossier(session, lead, dossier_text):
    # SAFE TRUNCATION BEFORE HTML FORMATTING
    if len(dossier_text) > 3000:
        dossier_text = dossier_text[:3000] + "\n\n... [Truncated due to Telegram limits]"
        
    msg = f"📊 <b>DEAL STRATEGY BRIEF</b>\n\n🏢 <b>Target:</b> {lead['org']}\n👤 <b>DM:</b> {lead['dm_name']} ({lead.get('dm_title', 'Decision Maker')})\n📞 <b>Contact:</b> {lead.get('phone', 'N/A')} | {lead.get('email', 'N/A')}\n\n<b>--- DOSSIER ---</b>\n{dossier_text}"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML", "disable_web_page_preview": True, "reply_markup": {"inline_keyboard": [[{"text": "🏆 WIN DEAL (Close)", "callback_data": f"windeal_{lead['lead_id']}"}], [{"text": "🗑️ Drop Lead", "callback_data": f"dropdeal_{lead['lead_id']}"}]]}}
    try:
        async with session.post(f"[https://api.telegram.org/bot](https://api.telegram.org/bot){TELEGRAM_BOT_TOKEN}/sendMessage", json=payload, timeout=30) as response: await response.read()
    except Exception: pass

async def process_lead_intel(session, lead, sem):
    async with sem:
        print(f"[*] Gathering OSINT Intel for Lead: {lead['org']}", flush=True)
        results = await asyncio.gather(async_serper_search(session, f'"{lead["org"]}" company profile India'), async_serper_search(session, f'"{lead["org"]}" recent news OR projects OR financials'), async_serper_search(session, f'"{lead["dm_name"]}" "{lead["org"]}" LinkedIn'))
        dossier = await asyncio.to_thread(generate_deal_dossier, lead, {"profile": results[0], "news": results[1], "dm_info": results[2]})
        
        try:
            async with session.post(WEBHOOK, json={"secret": SECRET, "action": "promote_to_deal", "lead_id": lead['lead_id'], "dossier": dossier}, timeout=30) as response: 
                await response.read()
                print(f"    ✅ Dossier Created: {lead['org']}", flush=True)
        except Exception: pass
        await async_send_dossier(session, lead, dossier)

async def run_intel():
    print(">>> 🧠 DEAL ANALYST ACTIVE (Production)", flush=True)
    if not WEBHOOK or not SECRET: return
    try: pending = requests.post(WEBHOOK, json={"secret": SECRET, "action": "get_leads_intel"}, timeout=30).json().get("pending_leads", [])
    except Exception as e: return print(f"❌ Failed to fetch pending leads: {e}", flush=True)
    if not pending: return print("    -> No Leads require Intel.", flush=True)
        
    client = get_next_gemini_client()
    if client: get_flash_model_stack(client)
    
    sem = asyncio.Semaphore(2)
    async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(limit=5), timeout=aiohttp.ClientTimeout(total=90)) as session:
        await asyncio.gather(*[process_lead_intel(session, lead, sem) for lead in pending])

if __name__ == "__main__":
    asyncio.run(run_intel())
