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

# Silence all annoying warnings
warnings.filterwarnings("ignore")
logging.getLogger("google.genai.models").setLevel(logging.ERROR)
logging.getLogger("google.genai.discovery").setLevel(logging.ERROR)

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
        valid_models = []
        for m in client.models.list():
            name = m.name.lower()
            if re.match(r'^models/gemini-\d+\.\d+-flash$', name):
                valid_models.append(name)
        if valid_models:
            valid_models.sort(key=lambda x: float(re.search(r'\d+\.\d+', x).group()), reverse=True)
            BEST_MODEL_STACK = valid_models
            return BEST_MODEL_STACK
    except Exception: pass
    BEST_MODEL_STACK = ["gemini-2.5-flash", "gemini-2.0-flash"]
    return BEST_MODEL_STACK

async def async_serper_search(session, query, num=3):
    if not SERPER_KEY: return []
    url = "[https://google.serper.dev/search](https://google.serper.dev/search)"
    payload = json.dumps({"q": query, "gl": "in", "num": num})
    headers = {'X-API-KEY': SERPER_KEY, 'Content-Type': 'application/json'}
    try:
        async with session.post(url, headers=headers, data=payload, timeout=20) as response:
            if response.status == 200:
                data = await response.json()
                return [r.get("snippet", "") for r in data.get("organic", [])]
    except Exception: pass
    return []

@retry(wait=wait_exponential(multiplier=2, min=4, max=30), stop=stop_after_attempt(5))
def generate_deal_dossier(lead, context_data):
    client = get_next_gemini_client()
    if not client: return "AI Unavailable."
    
    model_stack = get_flash_model_stack(client)
    
    prompt = f"""
You are an elite Enterprise B2B Sales Strategist. 
Your Account Executive just moved this company into the "LEADS" stage and needs a Deal Strategy Brief.

Target Company: {lead['org']}
Decision Maker: {lead['dm_name']} ({lead.get('dm_title', 'Decision Maker')})
Industry: {lead['industry']}

Web OSINT Context Collected:
{json.dumps(context_data)}

Output a JSON object with a single key "dossier". The value must be an actionable, executive-level text report with EXACTLY these 3 sections (Use emojis and bullet points):
1. 🏢 Company Profile: (Size, market positioning, core business).
2. 📰 Recent Signals: (Summarize recent news, financial health, or major projects).
3. 🎯 Sales Pitch Strategy: (How should we approach {lead['dm_name']}? What pain points should we target based on their industry?)
"""
    schema = {"type": "OBJECT", "properties": {"dossier": {"type": "STRING"}}}
    
    for model_name in model_stack:
        try:
            res = client.models.generate_content(
                model=model_name, contents=prompt,
                config=types.GenerateContentConfig(response_mime_type="application/json", response_schema=schema, temperature=0.2)
            )
            raw_text = res.text.strip()
            if raw_text.startswith("```"): raw_text = re.sub(r'^```(?:json)?|```$', '', raw_text, flags=re.IGNORECASE | re.MULTILINE).strip()
            return json.loads(raw_text).get("dossier", "No intel generated.")
        except Exception as e:
            err_str = str(e)
            if "503" in err_str or "500" in err_str or "limit: 0" in err_str:
                continue
            else:
                raise e

    raise Exception("All Gemini models in the stack are currently unavailable.")

async def async_send_dossier(session, lead, dossier_text):
    msg = f"📊 <b>DEAL STRATEGY BRIEF</b>\n\n" \
          f"🏢 <b>Target:</b> {lead['org']}\n" \
          f"👤 <b>DM:</b> {lead['dm_name']} ({lead.get('dm_title', 'Decision Maker')})\n" \
          f"📞 <b>Contact:</b> {lead.get('phone', 'N/A')} | {lead.get('email', 'N/A')}\n\n" \
          f"<b>--- DOSSIER ---</b>\n{dossier_text}"
    if len(msg) > 4000: msg = msg[:3990] + "...\n(Truncated)"
          
    reply_markup = {"inline_keyboard": [
        [{"text": "🏆 WIN DEAL (Close)", "callback_data": f"windeal_{lead['lead_id']}"}],
        [{"text": "🗑️ Drop Lead", "callback_data": f"dropdeal_{lead['lead_id']}"}]
    ]}
    
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML", 
        "reply_markup": reply_markup, "disable_web_page_preview": True
    }
    try:
        async with session.post(url, json=payload, timeout=30) as response: 
            await response.read()
    except Exception: pass

async def process_lead_intel(session, lead, sem):
    async with sem:
        print(f"[*] Gathering OSINT Intel for Lead: {lead['org']}", flush=True)
        
        q1 = async_serper_search(session, f'"{lead["org"]}" company profile India')
        q2 = async_serper_search(session, f'"{lead["org"]}" recent news OR projects OR financials')
        q3 = async_serper_search(session, f'"{lead["dm_name"]}" "{lead["org"]}" LinkedIn')
        
        results = await asyncio.gather(q1, q2, q3)
        context_data = {"profile": results[0], "news": results[1], "dm_info": results[2]}
        
        dossier = await asyncio.to_thread(generate_deal_dossier, lead, context_data)
        
        payload = {"secret": SECRET, "action": "promote_to_deal", "lead_id": lead['lead_id'], "dossier": dossier}
        try:
            async with session.post(WEBHOOK, json=payload, timeout=30) as response: 
                await response.read()
                print(f"    ✅ Dossier Created: {lead['org']}", flush=True)
        except Exception: pass
            
        await async_send_dossier(session, lead, dossier)

async def run_intel():
    print(">>> 🧠 DEAL ANALYST ACTIVE (Anti-Hang Version)", flush=True)
    if not WEBHOOK or not SECRET: return
    try:
        res = requests.post(WEBHOOK, json={"secret": SECRET, "action": "get_leads_intel"}, timeout=30)
        pending = res.json().get("pending_leads", [])
    except Exception as e: 
        print(f"❌ Failed to fetch pending leads: {e}", flush=True)
        return
        
    if not pending: 
        print("    -> No Leads require Intel.", flush=True)
        return
        
    client = get_next_gemini_client()
    if client: get_flash_model_stack(client)
    
    sem = asyncio.Semaphore(2)
    connector = aiohttp.TCPConnector(limit=5)
    timeout = aiohttp.ClientTimeout(total=90)
    async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
        tasks = [process_lead_intel(session, lead, sem) for lead in pending]
        await asyncio.gather(*tasks)

if __name__ == "__main__":
    asyncio.run(run_intel())
