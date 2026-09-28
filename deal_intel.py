import os
import re
import json
import asyncio
import aiohttp
import requests
from google import genai
from google.genai import types
from tenacity import retry, wait_exponential, stop_after_attempt

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

BEST_MODEL_CACHE = None
def get_best_gemini_model(client):
    global BEST_MODEL_CACHE
    if BEST_MODEL_CACHE: return BEST_MODEL_CACHE
    try:
        valid_models = [m.name.lower() for m in client.models.list() if re.match(r'^models/gemini-\d+\.\d+-flash$', m.name.lower())]
        if valid_models:
            valid_models.sort(key=lambda x: float(re.search(r'\d+\.\d+', x).group()), reverse=True)
            BEST_MODEL_CACHE = valid_models[0]
            return BEST_MODEL_CACHE
    except Exception: pass
    BEST_MODEL_CACHE = "gemini-2.5-flash"
    return BEST_MODEL_CACHE

async def async_serper_search(session, query, num=3):
    if not SERPER_KEY: return []
    url = "https://google.serper.dev/search"
    payload = json.dumps({"q": query, "gl": "in", "num": num})
    headers = {'X-API-KEY': SERPER_KEY, 'Content-Type': 'application/json'}
    try:
        # Increased to 20 seconds for slower API responses
        async with session.post(url, headers=headers, data=payload, timeout=20) as response:
            if response.status == 200:
                data = await response.json()
                return [r.get("snippet", "") for r in data.get("organic", [])]
    except Exception: pass
    return []

# ADDED RETRY LOGIC: If Gemini times out writing the dossier, wait and retry.
@retry(wait=wait_exponential(multiplier=2, min=4, max=30), stop=stop_after_attempt(5))
def generate_deal_dossier(lead, context_data):
    client = get_next_gemini_client()
    if not client: return "AI Unavailable."
    
    prompt = f"""
You are an elite Enterprise B2B Sales Strategist. 
Your Account Executive just pushed this company into the "LEADS" stage and needs a Deal Strategy Brief.

Target Company: {lead['org']}
Decision Maker: {lead['dm_name']} ({lead['dm_title']})
Industry: {lead['industry']}

Web OSINT Context Collected:
{json.dumps(context_data)}

Output a JSON object with a single key "dossier". The value must be an actionable, executive-level text report with EXACTLY these 3 sections (Use emojis and bullet points):
1. 🏢 Company Profile: (Size, market positioning, what they do).
2. 📰 Recent Signals: (Summarize recent news, financials, or major projects found in the context).
3. 🎯 Sales Pitch Strategy: (How should we approach {lead['dm_name']}? What pain points should we target based on their industry?)
"""
    schema = {"type": "OBJECT", "properties": {"dossier": {"type": "STRING"}}}
    try:
        res = client.models.generate_content(
            model=get_best_gemini_model(client), contents=prompt,
            config=types.GenerateContentConfig(response_mime_type="application/json", response_schema=schema, temperature=0.2)
        )
        raw_text = res.text.strip()
        if raw_text.startswith("```"): raw_text = re.sub(r'^```(?:json)?|```$', '', raw_text, flags=re.IGNORECASE | re.MULTILINE).strip()
        return json.loads(raw_text).get("dossier", "No intel generated.")
    except Exception as e: 
        print(f"    ⚠️ AI Timeout/Error: {e} - Retrying...")
        raise e

async def async_send_dossier(session, lead, dossier_text):
    msg = f"📊 <b>DEAL STRATEGY BRIEF</b>\n\n🏢 <b>Target:</b> {lead['org']}\n👤 <b>DM:</b> {lead['dm_name']} ({lead['dm_title']})\n📞 <b>Contact:</b> {lead['phone']} | {lead['email']}\n\n<b>--- DOSSIER ---</b>\n{dossier_text}"
    if len(msg) > 4000: msg = msg[:3990] + "...\n(Truncated)"
          
    reply_markup = {"inline_keyboard": [
        [{"text": "🏆 WIN DEAL (Close)", "callback_data": f"windeal_{lead['lead_id']}"}],
        [{"text": "🗑️ Drop Lead", "callback_data": f"dropdeal_{lead['lead_id']}"}]
    ]}
    
    url = f"[https://api.telegram.org/bot](https://api.telegram.org/bot){TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML", "reply_markup": reply_markup, "disable_web_page_preview": True}
    try:
        # Telegram can be slow to accept massive messages. Increased to 30.
        async with session.post(url, json=payload, timeout=30) as response: await response.read()
    except Exception as e:
        print(f"    ⚠️ Telegram send failed: {e}")

async def process_lead_intel(session, lead, sem):
    async with sem: # THIS IS THE TRAFFIC LIGHT
        print(f"[*] Gathering OSINT Intel for Lead: {lead['org']}")
        
        q1 = async_serper_search(session, f'"{lead["org"]}" company profile India')
        q2 = async_serper_search(session, f'"{lead["org"]}" recent news OR projects OR financials')
        q3 = async_serper_search(session, f'"{lead["dm_name"]}" "{lead["org"]}" LinkedIn')
        
        results = await asyncio.gather(q1, q2, q3)
        context_data = {"profile": results[0], "news": results[1], "dm_info": results[2]}
        
        dossier = await asyncio.to_thread(generate_deal_dossier, lead, context_data)
        
        payload = {"secret": SECRET, "action": "promote_to_deal", "lead_id": lead['lead_id'], "dossier": dossier}
        try:
            async with session.post(WEBHOOK, json=payload, timeout=30) as response: await response.read()
        except Exception: pass
            
        await async_send_dossier(session, lead, dossier)

async def run_intel():
    print(">>> 🧠 DEAL ANALYST V1 ACTIVE")
    if not WEBHOOK or not SECRET: return
    try:
        res = requests.post(WEBHOOK, json={"secret": SECRET, "action": "get_leads_intel"}, timeout=30)
        pending = res.json().get("pending_leads", [])
    except Exception: return
    if not pending: return
        
    client = get_next_gemini_client()
    if client: get_best_gemini_model(client)
    
    # SET TRAFFIC LIGHT TO 2 CONCURRENT LEADS
    sem = asyncio.Semaphore(2)
    connector = aiohttp.TCPConnector(limit=5)
    timeout = aiohttp.ClientTimeout(total=60)
    async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
        tasks = [process_lead_intel(session, lead, sem) for lead in pending]
        await asyncio.gather(*tasks)

if __name__ == "__main__":
    asyncio.run(run_intel())
