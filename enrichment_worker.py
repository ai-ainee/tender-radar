import os
import json
import requests
import urllib3
import random
import logging
import time
from bs4 import BeautifulSoup
from pypdf import PdfReader
from io import BytesIO
from datetime import datetime
from google import genai
from google.genai import types
from openai import OpenAI, OpenAIError
from tenacity import retry, wait_exponential, stop_after_attempt

# --- CONFIGURATION & LOGGING ---
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger("EnrichmentWorker")
logging.getLogger("google.genai.models").setLevel(logging.ERROR)
logging.getLogger("httpx").setLevel(logging.WARNING)

# ==========================================
# 1. CREDENTIALS & SYSTEM ALERTS
# ==========================================
class APIKeyManager:
    def __init__(self, env_string):
        self.keys = [k.strip() for k in env_string.split(',') if k.strip()]
        self.index = 0
        if not self.keys: raise ValueError("No API keys found. Check GitHub Secrets.")

    def get_current(self): return self.keys[self.index]

    def get_api_key(self): return self.get_current()

    def get_backup_key(self):
        self.index = (self.index + 1) % len(self.keys)
        return self.get_current()

    def rotate(self, service_name):
        self.index = (self.index + 1) % len(self.keys)
        logger.warning(f"{service_name} Quota hit. Rotating to Key #{self.index + 1} of {len(self.keys)}...")
        return self.get_current()

class SystemAlertNotifier:
    def __init__(self):
        self.token = os.getenv("TELEGRAM_BOT_TOKEN", "")
        self.chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
        
    def send(self, message):
        if not self.token or not self.chat_id: return
        try:
            requests.post(
                f"https://api.telegram.org/bot{self.token}/sendMessage", 
                json={"chat_id": self.chat_id, "text": message, "parse_mode": "Markdown"}, 
                timeout=5
            )
        except Exception as e:
            logger.error(f"Telegram alert delivery failed: {e}")

serper_keys = APIKeyManager(os.getenv("SERPER_API_KEYS", ""))
gemini_keys = APIKeyManager(os.getenv("GEMINI_API_KEYS", ""))
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "default_secret")
system_monitor = SystemAlertNotifier()

# ==========================================
# 2. DEEP DOSSIER GENERATOR
# ==========================================
class DossierEngine:
    def __init__(self, key_manager):
        self.keys = key_manager
        self.openai_key = os.getenv("OPENAI_API_KEY", "")
        
        self.current_gemini_key = self.keys.get_current()
        # NEW SDK: Initialize Client
        self.client = genai.Client(api_key=self.current_gemini_key)
        
        # Dynamically build the model stack on startup
        self.gemini_models = self._get_flash_model_stack()
        
        self.user_agents = [
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.1 Safari/605.1.15"
        ]

    def _get_flash_model_stack(self):
        try:
            valid_models = []
            for m in self.client.models.list():
                name = m.name.lower().replace("models/", "")
                banned_keywords = ["audio", "tts", "image", "omni", "vision", "native", "preview", "thinking", "2.5"]
                
                if "flash" in name and not any(bad in name for bad in banned_keywords):
                    if name not in valid_models:
                        valid_models.append(name)
            
            if valid_models:
                valid_models.sort(reverse=True)
                for preferred in ["gemini-3.8-flash", "gemini-3.5-flash", "gemini-1.5-flash"]:
                    if preferred in valid_models:
                        valid_models.insert(0, valid_models.pop(valid_models.index(preferred)))
                
                logger.info(f"🧠 Dynamic Model Stack Built: {valid_models}")
                return valid_models
        except Exception as e:
            logger.warning(f"⚠️ Could not fetch live model list: {e}. Using fallbacks.")
        
        return ["gemini-3.8-flash", "gemini-3.5-flash", "gemini-1.5-flash"]

    def _rotate_gemini_key(self):
        new_key = self.keys.rotate("Gemini")
        if new_key and new_key != self.current_gemini_key:
            logger.info("🔑 Rotating to a backup Gemini API Key...")
            self.current_gemini_key = new_key
            self.client = genai.Client(api_key=self.current_gemini_key)
            return True
        logger.warning("⚠️ No more backup Gemini keys available.")
        return False

    def fetch_content(self, url):
        time.sleep(random.uniform(1.5, 3.0))
        verify_ssl = False if '.gov.in' in url or '.nic.in' in url else True
        try:
            headers = {"User-Agent": random.choice(self.user_agents)}
            res = requests.get(url, headers=headers, timeout=12, verify=verify_ssl)
            res.raise_for_status()

            if 'application/pdf' in res.headers.get('Content-Type', '') or url.lower().endswith('.pdf'):
                return "".join(page.extract_text() + "\n" for page in PdfReader(BytesIO(res.content)).pages[:10]).strip()
                
            soup = BeautifulSoup(res.text, 'html.parser')
            for el in soup(["script", "style", "nav", "footer", "header"]): el.decompose()
            return " ".join(soup.get_text(separator=" ", strip=True).split())[:12000]
        except Exception as e:
            logger.warning(f"Scrape failed [{url}]: {e}")
            return f"Manual Review Required - Source could not be parsed: {e}"

    def generate(self, url, company_name):
        logger.info(f"Generating Deep Dossier for {company_name}...")
        raw_text = self.fetch_content(url)

        if "Manual Review Required" in raw_text:
            return raw_text

        prompt = f"""
        You are a ruthless B2B enterprise sales strategist. Analyze this source document regarding {company_name}.
        Extract the following and format it beautifully with bullet points:
        1. SCOPE: What exactly are they building/buying?
        2. PAIN POINT: What is their likely bottleneck or deadline?
        3. PITCH: A 2-sentence highly aggressive, value-driven cold pitch.
        4. WHATSAPP: A short, casual 1-line WhatsApp opener for the Plant Head / Procurement Director.

        <scraped_data>
        {raw_text}
        </scraped_data>
        """

        max_retries = 3
        for model_name in self.gemini_models:
            delay = 2
            for attempt in range(max_retries):
                try:
                    logger.info(f"🔄 Trying Gemini: {model_name} (Attempt {attempt + 1}/{max_retries})...")
                    
                    # NEW SDK: generate_content
                    response = self.client.models.generate_content(
                        model=model_name,
                        contents=prompt
                    )
                    
                    if response.text:
                        return response.text.strip()

                except Exception as e:
                    error_msg = str(e).lower()
                    if "429" in error_msg or "quota" in error_msg or "exhausted" in error_msg:
                        logger.warning(f"🚨 Rate Limit Exceeded for {model_name}.")
                        if self._rotate_gemini_key():
                            time.sleep(1)
                        else:
                            time.sleep(delay)
                            delay *= 2
                    else:
                        logger.warning(f"⚠️ Gemini API Error: {e}")
                        if attempt < max_retries - 1:
                            time.sleep(delay)
                            delay *= 2
            
            logger.info(f"⏭️ Moving to next Gemini model...\n")
        
        if self.openai_key:
            logger.warning("🚨 CRITICAL: All Gemini options exhausted. Activating OpenAI Fallback...")
            try:
                client = OpenAI(api_key=self.openai_key)
                response = client.chat.completions.create(
                    model="gpt-4o-mini",
                    messages=[{"role": "user", "content": prompt}]
                )
                logger.info("✅ Successfully recovered using OpenAI!")
                return response.choices[0].message.content.strip()
            except OpenAIError as e:
                logger.error(f"🚨 TOTAL SYSTEM FAILURE: OpenAI Error: {e}")

        system_monitor.send("⚠️ *Enrichment Worker Warning*: TOTAL API FAILURE. Both Gemini and OpenAI failed.")
        return "Failed: All API Ecosystems (Gemini & OpenAI) exhausted."

# ==========================================
# 3. DECISION MAKER HUNTER
# ==========================================
class WaterfallEnrichment:
    def __init__(self, key_manager):
        self.keys = key_manager

    def _serper_post(self, endpoint, payload):
        for _ in range(len(self.keys.keys)):
            headers = {'X-API-KEY': self.keys.get_current(), 'Content-Type': 'application/json'}
            try:
                response = requests.post(endpoint, headers=headers, data=payload, timeout=15)
                if response.status_code in [403, 429]:
                    self.keys.rotate("Serper")
                    continue
                return response.json() if response.status_code == 200 else {}
            except Exception as e:
                logger.warning(f"Serper request failed: {e}")
                return {}
        return {}

    def hunt_decision_maker(self, company_name):
        logger.info(f"Hunting contacts for: {company_name}")
        contact_data = {
            "dm_name": "Unknown", "dm_title": "Unknown", 
            "linkedin_url": "", "website": "", "phone": "", "email": ""
        }

        # 1. Decision Maker Search on LinkedIn
        li_query = f'site:linkedin.com/in "{company_name}" (Director OR "Plant Head" OR Procurement)'
        li_res = self._serper_post("https://google.serper.dev/search", json.dumps({"q": li_query, "num": 1, "gl": "in"}))
        
        if li_res and li_res.get("organic"):
            top_hit = li_res["organic"][0]
            contact_data["linkedin_url"] = top_hit.get("link", "")
            contact_data["dm_name"] = top_hit.get("title", "").split("-")[0].strip()
            contact_data["dm_title"] = top_hit.get("snippet", "")[:50] + "..."

        # 2. Business Details on Places
        map_res = self._serper_post("https://google.serper.dev/places", json.dumps({"q": company_name, "location": "India"}))
        if map_res and map_res.get("places"):
            top_place = map_res["places"][0]
            contact_data["website"] = top_place.get("website", "")
            contact_data["phone"] = top_place.get("phoneNumber", "")

        # 3. Domain Email Generation
        if contact_data["website"]:
            domain = contact_data["website"].replace("https://", "").replace("http://", "").split("/")[0].replace("www.", "")
            if contact_data["dm_name"] != "Unknown":
                contact_data["email"] = f"{contact_data['dm_name'].split(' ')[0].lower()}@{domain}"
            else:
                contact_data["email"] = f"info@{domain}"

        return contact_data

# ==========================================
# 4. MASTER EXECUTION
# ==========================================
@retry(wait=wait_exponential(multiplier=2, min=4, max=10), stop=stop_after_attempt(3))
def webhook_post(payload):
    res = requests.post(WEBHOOK_URL, json=payload, timeout=15)
    res.raise_for_status()
    return res

def run_enrichment_worker():
    logger.info("=== Waking Up: Radar Scout Enrichment Worker ===")
    start_time = datetime.now()
    
    try:
        response = requests.get(f"{WEBHOOK_URL}?secret={WEBHOOK_SECRET}&action=get_pending_leads", timeout=15).json()
        pending_leads = response.get("leads", [])
    except Exception as e:
        system_monitor.send(f"🚨 *Enrichment Worker Halted*: Failed to fetch pending leads.\nError: `{e}`")
        logger.error(f"Failed to connect to Google Sheets: {e}")
        return

    if not pending_leads:
        logger.info("Pipeline is clean. No new leads to enrich.")
        return

    logger.info(f"Found {len(pending_leads)} new leads. Initiating...")
    enricher = WaterfallEnrichment(serper_keys)
    dossier_engine = DossierEngine(gemini_keys)
    enriched_count = 0

    for lead in pending_leads:
        org = lead.get('organization', 'Unknown')
        lead_id = lead.get('lead_id', 'REF')
        row_idx = lead.get('row_index')
        
        dossier = dossier_engine.generate(lead.get('url', ''), org)
        contacts = enricher.hunt_decision_maker(org)
        
        try:
            # Push Dossier update
            webhook_post({
                "secret": WEBHOOK_SECRET, "action": "update_lead_dossier", "row_index": row_idx,
                "dossier": dossier, "contacts": contacts
            })
            
            # Push Contact entry
            webhook_post({
                "secret": WEBHOOK_SECRET, "action": "upsert_contact",
                "contact_data": {
                    "linkedin_url": contacts['linkedin_url'], "company_name": org,
                    "row_array": ["", org, contacts['dm_title'], contacts['dm_name'], contacts['linkedin_url'], contacts['email'], contacts['phone'], "", f"C-{lead_id}"]
                }
            })
            logger.info(f"Successfully Enriched: {org}")
            enriched_count += 1
        except Exception as e:
            logger.error(f"Failed to push updates for {org}: {e}")

    duration = str(datetime.now() - start_time).split('.')[0]
    summary_msg = (
        f"🧠 *Enrichment Run Complete*\n"
        f"• Leads Processed: `{len(pending_leads)}`\n"
        f"• Successfully Enriched: `{enriched_count}`\n"
        f"• Duration: `{duration}`"
    )
    system_monitor.send(summary_msg)
    logger.info("Enrichment Worker Cycle Complete.")

if __name__ == "__main__":
    run_enrichment_worker()
