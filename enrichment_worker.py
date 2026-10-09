import os
import json
import requests
import urllib3
import logging
import time
import re
from google import genai
from openai import OpenAI, OpenAIError
from tenacity import retry, wait_exponential, stop_after_attempt

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger("EnrichmentWorker")

logging.getLogger("google.genai.models").setLevel(logging.ERROR)
logging.getLogger("google.genai").setLevel(logging.ERROR)
logging.getLogger("httpx").setLevel(logging.ERROR)

class APIKeyManager:
    def __init__(self, env_string):
        clean_str = env_string.replace('"', '').replace("'", "").replace("\n", "").replace("\r", "").replace(" ", "")
        self.keys = [k for k in clean_str.split(',') if k]
        self.index = 0
    def get_current(self): return self.keys[self.index] if self.keys else ""
    def rotate(self, name): 
        self.index = (self.index + 1) % len(self.keys)
        logger.warning(f"{name} Failover. Rotating to Key #{self.index + 1} of {len(self.keys)}...")
        return self.get_current()

class DossierEngine:
    def __init__(self, key_manager):
        self.keys = key_manager
        self.gemini_models = ["gemini-3.8-flash", "gemini-3.5-flash", "gemini-1.5-flash"]
        self.openai_key = os.getenv("OPENAI_API_KEY", "")
        self._init_client()

    def _init_client(self):
        self.client = genai.Client(api_key=self.keys.get_current())

    def rotate_key(self):
        new_key = self.keys.rotate("Gemini")
        self.client = genai.Client(api_key=new_key)
        return new_key

    def generate(self, raw_text, company_name, segment_problems, partner_services):
        if not raw_text or "Manual Review" in raw_text: return "Source unavailable for deep analysis."
        prompt = f"""
        You are a ruthless B2B enterprise sales strategist. Analyze this source document regarding {company_name}.
        Extract the following intelligence and format it beautifully with bold bullet points:

        CONTEXT:
        - Segment Problems we solve: {segment_problems}
        - Our Partner Services: {partner_services}

        1. THE QUALIFICATION LENS:
           - Problem: What specific bottleneck is {company_name} trying to solve?
           - Urgency: What are the drivers (Why now?) 
           - Outcomes: What is their desired end-state?
           
        2. SOLUTION MAPPING:
           - Recommended Solutions: Which specific features of our offering should we push based on their problems?
           - Partner Services: Which of our partner services ({partner_services}) would they need?
           - Current Tech Stack: Any evidence of confirmed competitor usage or legacy systems?
           
        3. THE ATTACK PLAN:
           - Cold Pitch: A 2-sentence highly aggressive, value-driven email pitch tailored to their urgency drivers.
           - WhatsApp Opener: A short, casual 1-line WhatsApp opener for the Plant Head / Procurement Director.

        <scraped_data>
        {raw_text}
        </scraped_data>
        """
        for model_name in self.gemini_models:
            for key_attempt in range(len(self.keys.keys) or 1):
                try:
                    res = self.client.models.generate_content(model=model_name, contents=prompt)
                    if res.text: return res.text.strip()
                except Exception as e:
                    error_str = str(e).lower()
                    if "429" in error_str or "quota" in error_str:
                        logger.warning(f"⏳ Gemini Rate Limit on ({model_name}). Rotating to next key...")
                        self.rotate_key()
                        time.sleep(1)
                        continue
                    elif "503" in error_str or "unavailable" in error_str:
                        logger.warning(f"⚠️ Gemini 503 ({model_name}). Skipping model...")
                        break
                    elif "404" in error_str or "not_found" in error_str:
                        logger.warning(f"⚠️ Gemini 404 ({model_name}). Skipping model...")
                        break
                    else:
                        logger.warning(f"⚠️ Gemini Error ({model_name}): {e}")
                        time.sleep(2)

        if self.openai_key:
            try: 
                response = OpenAI(api_key=self.openai_key).chat.completions.create(
                    model="gpt-4o-mini", 
                    messages=[{"role": "user", "content": prompt}]
                )
                time.sleep(2) 
                return response.choices[0].message.content.strip()
            except OpenAIError as e:
                logger.error(f"OpenAI Error: {e}")
        return "Failed: Ecosystem Exhausted."

class WaterfallEnrichment:
    def __init__(self, key_manager):
        self.keys = key_manager
    def hunt_decision_maker(self, company_name):
        c = {"dm_name": "Unknown", "dm_title": "Unknown", "linkedin_url": "", "website": "", "phone": "", "email": ""}
        li_res = requests.post("https://google.serper.dev/search", headers={'X-API-KEY': self.keys.get_current()}, json={"q": f'site:linkedin.com/in "{company_name}" (Director OR "Plant Head" OR Procurement)'}, timeout=15).json()
        if li_res.get("organic"):
            top = li_res["organic"][0]
            c["linkedin_url"], c["dm_name"], c["dm_title"] = top.get("link", ""), top.get("title", "").split("-")[0].strip(), top.get("snippet", "")[:50] + "..."
        map_res = requests.post("https://google.serper.dev/places", headers={'X-API-KEY': self.keys.get_current()}, json={"q": company_name, "location": "India"}, timeout=15).json()
        if map_res.get("places"):
            top = map_res["places"][0]
            c["website"], c["phone"] = top.get("website", ""), top.get("phoneNumber", "")
        
        if c["website"]:
            domain = c["website"].replace("https://", "").replace("http://", "").split("/")[0].replace("www.", "")
            if c["dm_name"] != "Unknown":
                clean_name = re.sub(r'(?i)^(Mr|Mrs|Ms|Dr|Prof|Capt)\.?\s*', '', c["dm_name"]).strip()
                c["email"] = f"{clean_name.split(' ')[0].lower()}@{domain}"
            else: c["email"] = f"info@{domain}"
        return c

@retry(wait=wait_exponential(multiplier=2, min=4, max=10), stop=stop_after_attempt(3))
def webhook_post(payload):
    res = requests.post(os.getenv("WEBHOOK_URL"), json=payload, timeout=15)
    res.raise_for_status()
    return res

def run_enrichment_worker():
    logger.info("=== Waking Up: Enrichment Worker ===")
    WEBHOOK_URL, WEBHOOK_SECRET = os.getenv("WEBHOOK_URL", ""), os.getenv("WEBHOOK_SECRET", "")
    
    try:
        settings_req = requests.get(f"{WEBHOOK_URL}?secret={WEBHOOK_SECRET}&action=get_settings", timeout=15).json()
        segmentProblems = ", ".join(settings_req.get("segment_problems", []))
        partnerServices = ", ".join(settings_req.get("partner_services", []))
        pending_leads = requests.get(f"{WEBHOOK_URL}?secret={WEBHOOK_SECRET}&action=get_pending_leads", timeout=15).json().get("leads", [])
    except Exception as e:
        logger.error(f"Worker Halted: Failed to fetch leads. {e}")
        return

    if not pending_leads: return

    enricher = WaterfallEnrichment(APIKeyManager(os.getenv("SERPER_API_KEYS")))
    dossier_engine = DossierEngine(APIKeyManager(os.getenv("GEMINI_API_KEYS")))
    
    for lead in pending_leads:
        org, lead_id = lead.get('organization', 'Unknown'), lead.get('lead_id', 'REF')
        content_res = requests.get(f"https://r.jina.ai/{lead.get('url', '')}", timeout=15)
        raw_text = content_res.text if content_res.status_code == 200 else "Manual Review Required."
        
        dossier = dossier_engine.generate(raw_text, org, segmentProblems, partnerServices)
        contacts = enricher.hunt_decision_maker(org)
        
        try:
            webhook_post({"secret": WEBHOOK_SECRET, "action": "update_lead_dossier", "lead_id": lead_id, "dossier": dossier, "contacts": contacts})
            webhook_post({"secret": WEBHOOK_SECRET, "action": "upsert_contact", "contact_data": {"linkedin_url": contacts['linkedin_url'], "company_name": org, "row_array": ["", org, contacts['dm_title'], contacts['dm_name'], contacts['linkedin_url'], contacts['email'], contacts['phone'], "", f"C-{lead_id}"]}})
            
            tel_token, tel_chat = os.getenv('TELEGRAM_BOT_TOKEN'), os.getenv("TELEGRAM_CHAT_ID")
            if tel_token and tel_chat:
                safe_org = re.sub(r'[_*`\[\]()::]', ' ', org[:35]).strip()
                msg = f"🧠 <b>AI DOSSIER COMPLETE</b>\n\n🏢 <b>Company:</b> {org}\n👤 <b>Contact:</b> {contacts['dm_name']} ({contacts['dm_title']})\n📧 <b>Email:</b> {contacts['email']}\n📞 <b>Phone:</b> {contacts['phone']}\n\n📊 <b>Strategy Dossier:</b>\n{dossier[:1500]}\n"
                keyboard = {"inline_keyboard": [[{"text": "💼 Move to Pipeline", "callback_data": f"pipeline::{safe_org}"}, {"text": "📦 Archive", "callback_data": f"archive::{safe_org}"}]]}
                requests.post(f"https://api.telegram.org/bot{tel_token}/sendMessage", json={"chat_id": tel_chat, "text": msg, "parse_mode": "HTML", "reply_markup": keyboard}, timeout=5)
        except Exception as e: logger.error(f"Update failed for {org}: {e}")

if __name__ == "__main__": run_enrichment_worker()
