import os
import json
import asyncio
import aiohttp
import requests
from google import genai
from google.genai import types

WEBHOOK = os.environ.get("GOOGLE_SHEET_WEBHOOK")
SECRET = os.environ.get("WEBHOOK_SECRET")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
SERPER_KEY = os.environ.get("SERPER_API_KEY")
GEMINI_KEY = os.environ.get("GEMINI_API_KEY")

async def async_serper_search(session, query, num=3):
    if not SERPER_KEY: return []
    url = "https://google.serper.dev/search"
    payload = json.dumps({"q": query, "gl": "in", "num": num})
    headers = {'X-API-KEY': SERPER_KEY, 'Content-Type': 'application/json'}
    try:
        async with session.post(url, headers=headers, data=payload, timeout=10) as response:
            if response.status == 200:
                data = await response.json()
                return [r.get("snippet", "") for r in data.get("organic", [])]
    except Exception:
        pass
    return []

def generate_deal_dossier(lead, context_data):
    if not GEMINI_KEY: return None
    client = genai.Client(api_key=GEMINI_KEY)
    
    prompt = f"""
You are an elite Enterprise Deal Strategist. 
Your Account Executive just moved this company into the "LEADS" stage. 
Write a highly actionable "Deal Intelligence Dossier" based on the search context.

Target Company: {lead['org']}
Decision Maker: {lead['dm_name']} ({lead['dm_title']})
Product Category: {lead['industry']}

Search Context Collected:
{json.dumps(context_data)}

Output a JSON object with a single key "dossier". The value must be a beautifully formatted text report with these 3 sections (Use emojis and line breaks):
1. 🏢 Company Profile (Size, what they do, market position).
2. 📰 Recent Signals (Any recent news, projects, or financial health indicators).
3. 🎯 Sales Strategy (How should the Account Executive pitch them? What pain points should they mention to {lead['dm_name']}?)
"""
    schema = {"type": "OBJECT", "properties": {"dossier": {"type": "STRING"}}}
    
    try:
        res = client.models.generate_content(
            model='gemini-2.5-flash', contents=prompt,
            config=types.GenerateContentConfig(response_mime_type="application/json", response_schema=schema, temperature=0.2)
        )
        return json.loads(res.text).get("dossier", "No intel generated.")
    except Exception as e:
        print(f"⚠️ AI Error: {e}")
        return "Failed to generate intel."

async def async_send_dossier(session, lead, dossier_text):
    msg = f"📊 <b>DEAL INTELLIGENCE GATHERED</b>\n\n" \
          f"🏢 <b>Target:</b> {lead['org']}\n" \
          f"👤 <b>DM:</b> {lead['dm_name']}\n" \
          f"📞 <b>Contact:</b> {lead['phone']} | {lead['email']}\n\n" \
          f"<b>--- DEAL STRATEGY DOSSIER ---</b>\n{dossier_text}"
          
    reply_markup = {"inline_keyboard": [
        [{"text": "🏆 WIN DEAL (Convert to Deal)", "callback_data": f"closedeal_{lead['lead_id']}"}],
        [{"text": "🗑️ Drop Lead", "callback_data": f"droplead_{lead['lead_id']}"}]
    ]}
    
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": msg[:4000], "parse_mode": "HTML", "reply_markup": reply_markup}
    try:
        async with session.post(url, json=payload, timeout=10) as response:
            await response.read()
    except Exception:
        pass

async def process_lead_intel(session, lead):
    print(f"[*] Gathering deep intel for Lead: {lead['org']}")
    
    # Run deep OSINT queries simultaneously
    q1 = async_serper_search(session, f'"{lead["org"]}" company profile India')
    q2 = async_serper_search(session, f'"{lead["org"]}" recent news OR projects OR financials')
    q3 = async_serper_search(session, f'"{lead["dm_name"]}" "{lead["org"]}" LinkedIn')
    
    results = await asyncio.gather(q1, q2, q3)
    context_data = {"profile": results[0], "news": results[1], "dm_info": results[2]}
    
    # Generate the Deal Strategy
    dossier = await asyncio.to_thread(generate_deal_dossier, lead, context_data)
    
    # Update Google Sheets
    payload = {"secret": SECRET, "action": "update_lead_intel", "row_index": lead['row_index'], "dossier": dossier}
    try:
        async with session.post(WEBHOOK, json=payload, timeout=10) as response:
            await response.read()
    except Exception:
        pass
        
    # Send massive Telegram Report
    await async_send_dossier(session, lead, dossier)

async def run_intel():
    print(">>> 🧠 DEAL ANALYST V1 ACTIVE")
    if not WEBHOOK or not SECRET: return
    try:
        res = requests.post(WEBHOOK, json={"secret": SECRET, "action": "get_leads_intel"}, timeout=15)
        pending = res.json().get("pending_leads", [])
    except Exception:
        return
        
    if not pending:
        print("    -> No new leads requiring intel. Sleeping.")
        return
        
    print(f"    -> Gathering intel for {len(pending)} active Leads.")
    connector = aiohttp.TCPConnector(limit=5)
    async with aiohttp.ClientSession(connector=connector) as session:
        tasks = [process_lead_intel(session, lead) for lead in pending]
        await asyncio.gather(*tasks)

if __name__ == "__main__":
    asyncio.run(run_intel())
