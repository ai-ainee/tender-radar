import os
import json
import re
import time
import random
import logging
import requests
import urllib3
from bs4 import BeautifulSoup
from google import genai
from openai import OpenAI, OpenAIError
from tenacity import retry, wait_exponential, stop_after_attempt

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger("EnrichmentWorker")

class APIKeyManager:
    def __init__(self, env_string):
        clean_str = env_string.replace('"', '').replace("'", "").replace("\n", "").replace("\r", "").replace(" ", "")
        self.keys = [k for k in clean_str.split(',') if k]
        self.index = 0
        if not self.keys: raise ValueError("No API keys found. Check environment secrets.")
    def get_current(self): return self.keys[self.index]
    def rotate(self, service_name):
        self.index = (self.index + 1) % len(self.keys)
        logger.warning(f"{service_name} Failover. Rotating to Key #{self.index + 1} of {len(self.keys)}...")
        return self.get_current()

class TelegramNotifier:
    def __init__(self):
        self.token, self.chat_id = os.getenv("TELEGRAM_BOT_TOKEN", ""), os.getenv("TELEGRAM_CHAT_ID", "")
    def send(self, message, reply_markup=None):
        if not self.token or not self.chat_id: return
        payload = {"chat_id": self.chat_id, "text": message, "parse_mode": "HTML", "disable_web_page_preview": True}
        if reply_markup: payload["reply_markup"] = reply_markup
        try: requests.post(f"https://api.telegram.org/bot{self.token}/sendMessage", json=payload, timeout=5)
        except Exception: pass

serper_keys = APIKeyManager(os.getenv("SERPER_API_KEYS", ""))
gemini_keys = APIKeyManager(os.getenv("GEMINI_API_KEYS", ""))
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "default_secret")
telegram = TelegramNotifier()

class OSINTResearcher:
    def __init__(self, serper_km):
        self.serper_km = serper_km
        self.user_agents = [
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15"
        ]

    def search(self, query):
        clean_q = re.sub(r'[-"()]', ' ', query)
        clean_q = " ".join(clean_q.split())
        for attempt in range(len(self.serper_km.keys) or 1):
            key = self.serper_km.get_current()
            try:
                res = requests.post(
                    "https://google.serper.dev/search",
                    headers={'X-API-KEY': key, 'Content-Type': 'application/json'},
                    json={"q": clean_q, "gl": "in", "num": 5},
                    timeout=15
                )
                if res.status_code == 200:
                    data = res.json()
                    return data.get("organic", [])
                else:
                    self.serper_km.rotate("Serper")
            except Exception:
                self.serper_km.rotate("Serper")
        return []

    def fetch_url(self, url):
        time.sleep(random.uniform(1.0, 2.0))
        target_url = f"https://r.jina.ai/{url}"
        try:
            headers = {"User-Agent": random.choice(self.user_agents)}
            res = requests.get(target_url, headers=headers, timeout=15)
            if res.status_code == 200:
                soup = BeautifulSoup(res.text, 'html.parser')
                return soup.get_text(separator="\n", strip=True)[:6000]
        except Exception:
            pass
        return ""

class EnrichmentAI:
    def __init__(self, key_manager):
        self.keys = key_manager
        self.openai_key = os.getenv("OPENAI_API_KEY", "")
        # Model list preserved exactly as configured
        self.gemini_models = ["gemini-3.8-flash", "gemini-3.5-flash", "gemini-1.5-flash"]
        self._init_client()

    def _init_client(self):
        self.client = genai.Client(api_key=self.keys.get_current())

    def rotate_key(self):
        new_key = self.keys.rotate("Gemini")
        self.client = genai.Client(api_key=new_key)
        return new_key

    def analyze_company(self, company_name, reference_url, osint_text):
        prompt = f"""
        You are an elite Executive Headhunter and Senior Corporate B2B Account Strategist.
        TARGET ORGANIZATION: {company_name}
        REFERENCE URL: {reference_url}
        OSINT RESEARCH DATA:
        {osint_text[:10000]}

        YOUR GOAL:
        1. Identify the single highest-value Decision Maker (Director, CEO, Managing Director, VP Procurement, Chief Project Engineer, Head of Engineering, or BIM Lead).
        2. Synthesize an executive-ready Deep Intel Dossier tailored to pitching high-value commercial solutions.

        OUTPUT STRICT JSON WITH SCHEMA:
        {{
          "dm_name": (string) Decision Maker Full Name (or "Procurement Head" if specific name unverified),
          "dm_title": (string) Official Designation / Title,
          "email": (string) Official email or verified pattern (e.g. info@ / contact@ / name@),
          "phone": (string) 10-digit mobile or corporate office landline,
          "website": (string) Official company website,
          "linkedin_url": (string) Personal LinkedIn profile or corporate page,
          "dossier": (string) Markdown formatted brief with sections:
             ### 🏢 Executive Profile & Operations
             ### ⚙️ Current Capex, Project Signals & Expansion
             ### 🎯 Key Buying Triggers & Operational Bottlenecks
             ### 🚀 Ready-to-Send Cold Outreach Pitch
        }}
        """
        for model_name in self.gemini_models:
            for attempt in range(len(self.keys.keys) or 1):
                try:
                    response = self.client.models.generate_content(
                        model=model_name,
                        contents=prompt,
                        config={'response_mime_type': 'application/json'}
                    )
                    if response.text:
                        return json.loads(response.text.strip().replace("```json", "").replace("```", "").strip())
                except Exception as e:
                    err = str(e).lower()
                    if "429" in err or "quota" in err:
                        self.rotate_key()
                        time.sleep(1)
                    else:
                        break

        if self.openai_key:
            try:
                res = OpenAI(api_key=self.openai_key).chat.completions.create(
                    model="gpt-4o-mini",
                    messages=[{"role": "user", "content": prompt}],
                    response_format={"type": "json_object"}
                )
                return json.loads(res.choices[0].message.content.strip())
            except Exception:
                pass
        return None

def process_enrichment():
    logger.info("=== Starting Enrichment Worker ===")
    try:
        res = requests.get(f"{WEBHOOK_URL}?secret={WEBHOOK_SECRET}&action=get_pending_leads", timeout=20)
        data = res.json()
    except Exception as e:
        logger.error(f"❌ Failed to fetch pending leads: {e}")
        return

    leads = data.get("leads", [])
    if not leads:
        logger.info("✅ No pending leads in Leads sheet requiring enrichment.")
        return

    logger.info(f"📋 Found {len(leads)} leads awaiting enrichment.")
    researcher = OSINTResearcher(serper_keys)
    ai = EnrichmentAI(gemini_keys)

    for lead in leads:
        lead_id = lead.get("lead_id", "").strip()
        org = lead.get("organization", "").strip()
        ref_url = lead.get("url", "").strip()

        if not lead_id or not org: continue
        logger.info(f"\n🔍 Enriching: {org} (ID: {lead_id})...")

        q1 = f'"{org}" (Director OR CEO OR MD OR "Procurement" OR "Head of Projects" OR "Chief Engineer") linkedin'
        q2 = f'"{org}" corporate office contact email phone website'
        
        snippets = []
        for q in [q1, q2]:
            for item in researcher.search(q):
                snippets.append(f"{item.get('title', '')}\n{item.get('snippet', '')}\nLink: {item.get('link', '')}")

        scraped_text = ""
        if ref_url:
            scraped_text = researcher.fetch_url(ref_url)
        osint_payload = "\n\n".join(snippets) + "\n\n" + scraped_text

        intel = ai.analyze_company(org, ref_url, osint_payload)
        if not intel:
            logger.warning(f"⚠️ Failed to synthesize intel for {org}")
            continue

        dm_name = intel.get("dm_name", "N/A")
        dm_title = intel.get("dm_title", "N/A")
        email = intel.get("email", "")
        phone = intel.get("phone", "")
        website = intel.get("website", "")
        linkedin = intel.get("linkedin_url", "")
        dossier = intel.get("dossier", "")

        logger.info(f"💾 Saving dossier for {org} to Google Sheets...")
        update_payload = {
            "secret": WEBHOOK_SECRET,
            "action": "update_lead_dossier",
            "lead_id": lead_id,
            "dossier": dossier,
            "contacts": {
                "dm_name": dm_name,
                "email": email,
                "phone": phone,
                "website": website,
                "linkedin_url": linkedin
            }
        }
        try:
            r = requests.post(WEBHOOK_URL, json=update_payload, timeout=20).json()
            logger.info(f"   Update result: {r.get('message', 'Done')}")
        except Exception as e:
            logger.error(f"   Failed to write dossier: {e}")

        contact_payload = {
            "secret": WEBHOOK_SECRET,
            "action": "upsert_contact",
            "contact_data": {
                "company_name": org,
                "linkedin_url": linkedin,
                "row_array": [lead_id, dm_name, org, dm_title, phone, email, linkedin, ""]
            }
        }
        try:
            requests.post(WEBHOOK_URL, json=contact_payload, timeout=15)
        except Exception:
            pass

        clean_phone = re.sub(r'[^0-9]', '', str(phone))
        if len(clean_phone) == 10: clean_phone = "91" + clean_phone
        
        keyboard = None
        if len(clean_phone) >= 10:
            wa_text = requests.utils.quote(f"Hi {dm_name if dm_name != 'N/A' else 'Team'}, I saw your active project update regarding {org}. Would love to share details.")
            keyboard = json.dumps({"inline_keyboard": [[{"text": "💬 WhatsApp DM", "url": f"https://wa.me/{clean_phone}?text={wa_text}"}]]})

        tg_msg = f"🎯 <b>LEAD ENRICHED: {org}</b>\n\n"
        tg_msg += f"👤 <b>Contact:</b> {dm_name} (<i>{dm_title}</i>)\n"
        if phone: tg_msg += f"📞 <b>Phone:</b> {phone}\n"
        if email: tg_msg += f"✉️ <b>Email:</b> {email}\n"
        if website: tg_msg += f"🌐 <b>Website:</b> <a href='{website}'>{website}</a>\n"
        tg_msg += f"\n📋 <i>Deep Intel Dossier compiled and saved to CRM.</i>"
        
        telegram.send(tg_msg, reply_markup=keyboard)
        time.sleep(2)

    logger.info("🏁 Enrichment batch completed successfully.")

if __name__ == "__main__":
    process_enrichment()
