import os
import json
import logging
import warnings
import asyncio
import aiohttp
import requests
import re
import urllib.parse
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
        valid_models = []
        for m in client.models.list():
            name = m.name.lower()
            banned_keywords = ["audio", "tts", "image", "omni", "vision", "native", "preview", "thinking", "2.5"]
            if "flash" in name and not any(bad in name for bad in banned_keywords):
                valid_models.append(name)
        if valid_models:
            valid_models.sort(reverse=True)
            for preferred in ["models/gemini-3.5-flash-lite", "models/gemini-1.5-flash"]:
                if preferred in valid_models:
                    valid_models.insert(0, valid_models.pop(valid_models.index(preferred)))
            BEST_MODEL_STACK = valid_models
            return BEST_MODEL_STACK
    except Exception: pass
    BEST_MODEL_STACK = ["gemini-3.5-flash-lite", "gemini-1.5-flash"]
    return BEST_MODEL_STACK

async def async_serper_search(session, query, num=3):
    if not SERPER_KEY: return []
    try:
        async with session.post("https://google.serper.dev/search", headers={'X-API-KEY': SERPER_KEY, 'Content-Type': 'application/json'}, data=json.dumps({"q": query, "gl": "in", "num": num}), timeout=25) as response:
            if response.status == 200: return [r.get("snippet", "") for r in (await response.json()).get("organic", [])]
    except Exception: pass
    return []

# --- HTML Regex Cleaner for Telegram ---
def convert_markdown_to_html(text):
    """Converts Gemini Markdown (**bold**, *italic*) to Telegram-safe HTML (<b>bold</b>, <i>italic</i>)"""
    if not text: return ""
    text = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', text)
    text = re.sub(r'__(.+?)__', r'<b>\1</b>', text)
    text = re.sub(r'\*(.+?)\*', r'<i>\1</i>', text)
    text = re.sub(r'_(.+?)_', r'<i>\1</i>', text)
    return text

@retry(wait=wait_exponential(multiplier=2, min=4, max=30), stop=stop_after_attempt(5))
def generate_deal_dossier(lead, context_data):
    client = get_next_gemini_client()
    if not client: return "AI Unavailable."
    model_stack = get_flash_model_stack(client)
    
    # --- UPGRADED PROMPT: Added 3-Sentence Cold Pitch Generation ---
    prompt = f"""
Write an Executive Deal Brief for sales outreach:
Target: {lead['org']} (Location: {lead.get('city')}, {lead.get('state')})
Decision Maker: {lead.get('dm_name')} ({lead.get('dm_title')})
Target Product: {lead.get('industry')}
Context: {json.dumps(context_data)}

Output JSON with key 'dossier' containing:
1. Executive Profile & Core Operations
2. Current Capex, Project Signals & Recent Milestones
3. Tactical Value Proposition & Entry Pitch
4. [NEW SECTION] "Ready-to-Send Cold Pitch": Write a highly personalized 3-sentence message (Observation, Value Bridge, Call-to-Action) addressed to the Decision Maker. DO NOT use placeholders like [Your Name].

Keep the formatting clean and professional.
"""
    schema = {"type": "OBJECT", "properties": {"dossier": {"type": "STRING"}}}
    
    for model_name in model_stack:
        try:
            chat = client.chats.create(model=model_name)
            res = chat.send_message(prompt, config=types.GenerateContentConfig(response_mime_type="application/json", response_schema=schema, temperature=0.2))
            raw_text = res.text.strip()
            if raw_text.startswith("```"): raw_text = raw_text.replace("```json", "").replace("```JSON", "").replace("```", "").strip()
            return json.loads(raw_text).get("dossier", "No intel generated.")
        except Exception: continue
    return "Failed to generate AI dossier."

async def process_lead_intel(session, lead, sem):
    async with sem:
        print(f"[*] Deep Intel Synthesis: {lead['org']}", flush=True)
        results = await asyncio.gather(
            async_serper_search(session, f'"{lead["org"]}" company profile India turnover'), 
            async_serper_search(session, f'"{lead["org"]}" ("contract awarded" OR "expansion" OR "orders" OR "capex")'), 
            async_serper_search(session, f'"{lead["dm_name"]}" "{lead["org"]}" LinkedIn')
        )
        
        raw_dossier = await asyncio.to_thread(generate_deal_dossier, lead, {"profile": results[0], "news": results[1], "dm_info": results[2]})
        
        # --- HTML CRASH PROTECTION ---
        # Truncate BEFORE HTML conversion to prevent slicing a <b> or <i> tag in half
        if len(raw_dossier) > 3200:
            raw_dossier = raw_dossier[:3200] + "\n\n... [Truncated]"
            
        # Convert to Telegram-safe HTML
        dossier = convert_markdown_to_html(raw_dossier)
        
        for attempt in range(3):
            try:
                # Updates the dossier in the LEADS tab
                async with session.post(WEBHOOK, json={"secret": SECRET, "action": "update_lead_dossier", "lead_id": lead['lead_id'], "dossier": dossier}, timeout=30) as response: 
                    await response.read()
                    print(f"    ✅ Dossier Stored in Leads Tab: {lead['org']}", flush=True)
                    break
            except Exception: await asyncio.sleep(2)
        
        # --- TELEGRAM OUTREACH & PIPELINE DISPATCH ---
        if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
            # Extract Cold Pitch for the 1-Click Buttons
            pitch_text = f"Hi {lead.get('dm_name', 'Team')}, I saw your update regarding {lead['org']}'s requirements. Would love to connect and share details on how we can support your project."
            
            # Search for the generated pitch inside the dossier
            pitch_match = re.search(r'Ready-to-Send Cold Pitch.*?:?\s*(.*?)(?=\n\n|$)', raw_dossier, re.IGNORECASE | re.DOTALL)
            if pitch_match and pitch_match.group(1):
                pitch_text = pitch_match.group(1).strip()
            
            # Clean HTML and markdown out of the pitch for the URL payload
            pitch_text = re.sub(r'<[^>]+>', '', pitch_text).replace('**', '').replace('__', '')
            
            buttons = []
            
            # 1-Click WhatsApp Button Logic
            phone = str(lead.get('phone', '')).strip()
            clean_phone = "".join(filter(str.isdigit, phone))
            if len(clean_phone) == 10:
                clean_phone = "91" + clean_phone
                
            if len(clean_phone) >= 10:
                wa_url = f"https://wa.me/{clean_phone}?text={urllib.parse.quote(pitch_text)}"
                buttons.append([{"text": "💬 WhatsApp DM", "url": wa_url}])
                
            # 1-Click Mailto Button Logic
            email = str(lead.get('email', '')).strip()
            if email and email.lower() != "n/a" and "@" in email:
                subject = urllib.parse.quote(f"Partnership regarding {lead.get('org', 'your requirements')}")
                body = urllib.parse.quote(pitch_text)
                mailto_url = f"mailto:{email}?subject={subject}&body={body}"
                buttons.append([{"text": "📧 Send Email", "url": mailto_url}])
            
            # Standard CRM Workflow Buttons
            buttons.append([{"text": "🚀 Move to Pipeline", "callback_data": f"topipeline_{lead['lead_id']}"}])
            buttons.append([{"text": "🗑️ Drop Lead", "callback_data": f"droplead_{lead['lead_id']}"}])
            
            msg = (
                f"📊 <b>LEAD INTEL BRIEF READY</b>\n\n"
                f"🏢 <b>Target:</b> {lead['org']}\n"
                f"📍 <b>Location:</b> {lead.get('city')}, {lead.get('state')}\n"
                f"👤 <b>DM:</b> {lead.get('dm_name')} ({lead.get('dm_title')})\n"
                f"📞 <b>Contact:</b> {lead.get('phone', 'N/A')} | {lead.get('email', 'N/A')}\n\n"
                f"<b>--- DOSSIER ---</b>\n{dossier}"
            )
            
            payload = {
                "chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML", "disable_web_page_preview": True,
                "reply_markup": {"inline_keyboard": buttons}
            }
            try:
                async with session.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage", json=payload, timeout=30) as response: 
                    await response.read()
            except Exception: pass

async def run_intel():
    print(">>> 🧠 DEAL ANALYST ACTIVE (Auto-Pitch & Safe-HTML Edition)", flush=True)
    if not WEBHOOK or not SECRET: return
    try:
        pending = requests.post(WEBHOOK, json={"secret": SECRET, "action": "get_leads_intel"}, timeout=30).json().get("pending_leads", [])
    except Exception: return
    if not pending: return print("    -> No leads require deep intel.", flush=True)

    sem = asyncio.Semaphore(2)
    async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(limit=5)) as session:
        await asyncio.gather(*[process_lead_intel(session, lead, sem) for lead in pending])

if __name__ == "__main__":
    asyncio.run(run_intel())
