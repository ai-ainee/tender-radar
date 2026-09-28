import os
import json
import logging
import warnings
import asyncio
import aiohttp
import requests
import time
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
        valid_models = [m.name.lower() for m in client.models.list() if "flash" in m.name.lower()]
        if valid_models:
            valid_models.sort(reverse=True)
            BEST_MODEL_STACK = valid_models
            return BEST_MODEL_STACK
    except Exception: pass
    BEST_MODEL_STACK = ["gemini-2.5-flash", "gemini-2.0-flash"]
    return BEST_MODEL_STACK

async def async_serper_search(session, query, num=3):
    if not SERPER_KEY: return []
    try:
        async with session.post("https://google.serper.dev/search", headers={'X-API-KEY': SERPER_KEY, 'Content-Type': 'application/json'}, data=json.dumps({"q": query, "gl": "in", "num": num}), timeout=30) as response:
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
            # SAFE JSON STRIPPER
            if raw_text.startswith("```"):
                raw_text = raw_text.replace("```json", "").replace("```JSON", "").replace("```", "").strip()
            return json.loads(raw_text).get("dossier", "No intel generated.")
        except Exception as e:
            if "503" in str(e) or "500" in str(e) or "limit: 0" in str(e): continue
            raise e
    raise Exception("Models unavailable.")

async def process_lead_intel(session, lead, sem):
    async with sem:
        print(f"[*] Gathering OSINT Intel for Lead: {lead['org']}", flush=True)
        results = await asyncio.gather(async_serper_search(session, f'"{lead["org"]}" company profile India'), async_serper_search(session, f'"{lead["org"]}" recent news OR projects OR financials'), async_serper_search(session, f'"{lead["dm_name"]}" "{lead["org"]}" LinkedIn'))
        dossier = await asyncio.to_thread(generate_deal_dossier, lead, {"profile": results[0], "news": results[1], "dm_info": results[2]})
        
        try:
            async with session.post(WEBHOOK, json={"secret": SECRET, "action": "promote_to_deal", "lead_id": lead['lead_id'], "dossier": dossier}, timeout=60) as response: 
                await response.read()
                print(f"    ✅ Dossier Created: {lead['org']}", flush=True)
        except Exception: pass
        
        if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
            d_text = dossier[:3000] + "\n\n... [Truncated]" if len(dossier) > 3000 else dossier
            msg = f"📊 <b>DEAL STRATEGY BRIEF</b>\n\n🏢 <b>Target:</b> {lead['org']}\n👤 <b>DM:</b> {lead['dm_name']}\n📞 <b>Contact:</b> {lead.get('phone', 'N/A')} | {lead.get('email', 'N/A')}\n\n<b>--- DOSSIER ---</b>\n{d_text}"
            payload = {"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML", "disable_web_page_preview": True, "reply_markup": {"inline_keyboard": [[{"text": "🏆 WIN DEAL", "callback_data": f"windeal_{lead['lead_id']}"}], [{"text": "🗑️ Drop Lead", "callback_data": f"dropdeal_{lead['lead_id']}"}]]}}
            try:
                async with session.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage", json=payload, timeout=30) as response: await response.read()
            except Exception: pass

async def run_intel():
    print(">>> 🧠 DEAL ANALYST ACTIVE (Safe Regex & Retry Version)", flush=True)
    if not WEBHOOK or not SECRET: return
    
    pending = []
    for attempt in range(3):
        try:
            pending = requests.post(WEBHOOK, json={"secret": SECRET, "action": "get_leads_intel"}, timeout=60).json().get("pending_leads", [])
            break
        except Exception as e:
            print(f"    ⚠️ Sheets API timeout. Retrying {attempt+1}/3...", flush=True)
            time.sleep(5)
            if attempt == 2: return print("❌ Failed to fetch pending leads.", flush=True)
            
    if not pending: return print("    -> No Leads require Intel.", flush=True)
        
    client = get_next_gemini_client()
    if client: get_flash_model_stack(client)
    
    sem = asyncio.Semaphore(2)
    async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(limit=5), timeout=aiohttp.ClientTimeout(total=90)) as session:
        await asyncio.gather(*[process_lead_intel(session, lead, sem) for lead in pending])

if __name__ == "__main__":
    asyncio.run(run_intel())
