import os
import json
import hashlib
import requests
import urllib3
import re
import uuid
import time
import random
import logging
from bs4 import BeautifulSoup
from pypdf import PdfReader
from io import BytesIO
from datetime import datetime
from urllib.parse import urlparse
from google import genai
from openai import OpenAI, OpenAIError
from tenacity import retry, wait_exponential, stop_after_attempt
from ddgs import DDGS

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger("RadarScout")

logging.getLogger("google.genai.models").setLevel(logging.ERROR)
logging.getLogger("google.genai").setLevel(logging.ERROR)
logging.getLogger("httpx").setLevel(logging.ERROR)
logging.getLogger("duckduckgo_search").setLevel(logging.ERROR)
logging.getLogger("ddgs").setLevel(logging.ERROR)
logging.getLogger("pypdf").setLevel(logging.ERROR)

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

class SystemAlertNotifier:
    def __init__(self):
        self.token, self.chat_id = os.getenv("TELEGRAM_BOT_TOKEN", ""), os.getenv("TELEGRAM_CHAT_ID", "")
    def send(self, message):
        if not self.token or not self.chat_id: return
        try: requests.post(f"https://api.telegram.org/bot{self.token}/sendMessage", json={"chat_id": self.chat_id, "text": message, "parse_mode": "Markdown"}, timeout=5)
        except Exception: pass

serper_keys = APIKeyManager(os.getenv("SERPER_API_KEYS", ""))
gemini_keys = APIKeyManager(os.getenv("GEMINI_API_KEYS", ""))
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "default_secret")
system_monitor = SystemAlertNotifier()

CACHE_FILE = "seen_links.txt"
if not os.path.exists(CACHE_FILE): open(CACHE_FILE, 'w').close()

def clean_url(url):
    try:
        p = urlparse(url.strip())
        netloc = p.netloc.lower().replace('www.', '')
        path = p.path.rstrip('/')
        query = f"?{p.query}" if p.query else ""
        return f"{netloc}{path}{query}".lower()
    except Exception: 
        return url.strip().lower()

def extract_indian_phone_robust(text):
    if not text: return ""
    pattern = r'(?:(?:\+91|91|0)[\s\-]?)?([6-9]\d{4}[\s\-]?\d{5}|[6-9]\d{2}[\s\-]?\d{3}[\s\-]?\d{4}|[6-9]\d{9})\b'
    matches = re.findall(pattern, text)
    if matches:
        clean = re.sub(r'[^0-9]', '', matches[0])
        if len(clean) == 12 and clean.startswith('91'): clean = clean[2:]
        elif len(clean) == 11 and clean.startswith('0'): clean = clean[1:]
        if len(clean) == 10: return clean
    return ""

@retry(wait=wait_exponential(multiplier=2, min=4, max=10), stop=stop_after_attempt(3), reraise=True)
def load_hybrid_cache():
    seen = set()
    try:
        with open(CACHE_FILE, 'r') as f: seen.update(clean_url(line) for line in f if line.strip())
    except Exception as e: logger.warning(f"Local cache unreadable: {e}")
        
    logger.info("🔄 Fetching cloud cache (seen links) from Google Sheets...")
    res = requests.post(WEBHOOK_URL, json={"secret": WEBHOOK_SECRET, "action": "get_all_urls"}, timeout=20)
    res.raise_for_status() 
    data = res.json()
    if data.get("status") != "success": raise ValueError(f"Webhook error: {data.get('message', 'Unknown error')}")
    seen.update(clean_url(u) for u in data.get("urls", []) if u.strip())
    logger.info(f"✅ Cloud cache synchronized. Total memory: {len(seen)} links.")
    return seen

def save_to_cache(link):
    with open(CACHE_FILE, 'a') as f: f.write(link + '\n')

class DynamicB2BEvaluator:
    def __init__(self, key_manager):
        self.keys = key_manager
        self.openai_key = os.getenv("OPENAI_API_KEY", "")
        # Your preferred seed models
        self.gemini_models = ["gemini-3.8-flash", "gemini-3.5-flash", "gemini-1.5-flash"]
        self.dead_models = set()  # Permanently blacklists models that return 404
        self._init_client()

    def _init_client(self):
        self.client = genai.Client(api_key=self.keys.get_current())

    def rotate_key(self):
        new_key = self.keys.rotate("Gemini")
        self.client = genai.Client(api_key=new_key)
        return new_key

    def auto_discover_best_model(self):
        """
        Queries Google's API directly to find all active models authorized
        for this API key and selects the best available Flash model.
        """
        logger.info("🔍 Querying Google API to auto-discover active models...")
        try:
            available_flash_models = []
            for m in self.client.models.list():
                clean_name = m.name.replace("models/", "")
                # Filter for text-generation flash models that aren't blacklisted
                if "flash" in clean_name.lower() and clean_name not in self.dead_models:
                    available_flash_models.append(clean_name)

            if available_flash_models:
                logger.info(f"✅ Auto-discovered active models: {available_flash_models}")
                return available_flash_models
        except Exception as e:
            logger.warning(f"⚠️ Live model discovery failed: {e}")
        return []

    def generate_json(self, prompt, max_retries=3):
        for attempt in range(max_retries):
            # Filter out blacklisted 404 models
            active_models = [m for m in self.gemini_models if m not in self.dead_models]

            # If all configured models failed or are blacklisted, auto-discover live models
            if not active_models:
                discovered = self.auto_discover_best_model()
                if discovered:
                    for d in discovered:
                        if d not in self.gemini_models:
                            self.gemini_models.append(d)
                    active_models = [m for m in self.gemini_models if m not in self.dead_models]
                else:
                    logger.error("❌ No active Gemini models available for this API key.")

            for model_name in active_models:
                for key_attempt in range(len(self.keys.keys) or 1):
                    try:
                        response = self.client.models.generate_content(
                            model=model_name,
                            contents=prompt,
                            config={'response_mime_type': 'application/json'}
                        )
                        if response.text:
                            return json.loads(response.text.strip().replace("```json", "").replace("```", "").strip())
                    except Exception as e:
                        error_str = str(e).lower()
                        if "429" in error_str or "quota" in error_str:
                            logger.warning(f"⏳ Gemini Rate Limit on ({model_name}). Rotating key with cool-off...")
                            self.rotate_key()
                            if self.keys.index == 0:
                                logger.info("⏳ All keys cooling down. Waiting 20s for RPM window to clear...")
                                time.sleep(20)
                            else:
                                time.sleep(3)
                            continue
                        elif "503" in error_str or "unavailable" in error_str:
                            logger.warning(f"⚠️ Gemini 503 ({model_name}). Skipping model...")
                            break
                        elif "404" in error_str or "not_found" in error_str:
                            # Blacklist this model so it is never called again
                            logger.warning(f"⚠️ Gemini 404: '{model_name}' is not supported/enabled. Blacklisting permanently.")
                            self.dead_models.add(model_name)
                            break
                        else:
                            logger.warning(f"⚠️ Gemini Error ({model_name}): {e}")
                            time.sleep(2)

            # Fallback to OpenAI if Gemini pool is exhausted
            if self.openai_key:
                try:
                    response = OpenAI(api_key=self.openai_key).chat.completions.create(
                        model="gpt-4o-mini",
                        messages=[{"role": "user", "content": prompt}],
                        response_format={"type": "json_object"}
                    )
                    time.sleep(1)
                    return json.loads(response.choices[0].message.content.strip())
                except OpenAIError as e:
                    logger.error(f"OpenAI Error: {e}")

            logger.warning(f"⏳ Cycle {attempt + 1}/{max_retries} rate-limited. Pausing 20s before retrying...")
            time.sleep(20)

        return None

    def evaluate_batch(self, batch, target, industry, country, states, geo_rule, ban_rule):
        if not batch: return []
        # Trimmed to 3000 chars to stay safely below Token-Per-Minute thresholds
        items_block = "\n".join([f"--- ITEM {i} ---\nTRACK: {x['track']}\n<scraped_data>\n{x['raw_text'][:3000]}\n</scraped_data>\n" for i, x in enumerate(batch)])

        ind_line = f"INDUSTRY / VERTICAL: {industry}" if industry and industry != "ALL_SECTORS" else "INDUSTRY / VERTICAL: All Commercial & Industrial Sectors (Extract dynamically)"
        geo_line = f"TARGET GEOGRAPHY: {country}" if country else ""
        states_line = f"REGIONAL FOCUS: {', '.join(states)}" if states else ""

        prompt = f"""
        Your Role: You are an elite B2B sales intelligence analyst evaluating genuine commercial buying intent and enterprise business opportunities.
        TARGET PRODUCT / SOLUTION: {target}
        {ind_line}
        {geo_line}
        {states_line}
        {geo_rule}
        {ban_rule}

        CORE EVALUATION OBJECTIVE:
        Analyze the raw scraped text to identify real, verifiable companies, organizations, or government bodies with active commercial requirements, capital projects, operational expansions, or procurement needs where '{target}' is relevant.

        QUALIFICATION RULES:
        1. VALID B2B PROSPECTS:
           - Government agencies, public sector undertakings, or municipal entities issuing active tenders, bids, or RFPs.
           - Commercial corporations, enterprises, or growing businesses launching projects, expanding facilities, investing capital, or procuring solutions.
           - Companies issuing RFQs, seeking vendor empanelment, or hiring specialized teams/heads for this function.
        2. STRICT EXCLUSIONS:
           - Educational institutions, training academies, tutorials, or student coursework.
           - Closed or expired procurement bids where the deadline has clearly passed.
           - Freelancers, individuals, or casual blog discussions.
           - Generic global articles lacking an identifiable corporate/public buying entity.

        STRICT JSON OUTPUT SCHEMA:
        Return STRICT JSON with key "leads" containing an array of objects:
        "item_index": (integer) matching input index,
        "is_valid": (boolean) true if a genuine commercial buyer or enterprise lead is found,
        "confidence": (string) "HIGH", "MEDIUM", or "LOW",
        "entity_role": (string) "BUYER", "PROJECT_BUYER", "SERVICE_USER", "SELLER", or "IRRELEVANT",
        "organization": (string) Official trading name of the company or procuring department,
        "industry": (string) Specific sector of the organization,
        "city": (string), "state": (string),
        "target_solution": (string) The specific solution or requirement relevant to this lead,
        "why_engage_now": (string) 2-3 sentences: concrete commercial signal, urgency trigger, and sales opportunity,
        "product_usage": (string) [Confirmed User / Prospective Buyer / Competitor / Unknown] + Evidence from text,
        "upcoming_events": (string) Tender closing date, milestone deadline, or 'Unknown',
        "dm_name": (string) Name of key contact person or procurement official if mentioned, else "",
        "phone": (string) Direct phone or procurement helpline if mentioned, else ""

        BATCH:
        {items_block}
        """
        data = self.generate_json(prompt)
        if isinstance(data, dict):
            return data.get("leads", data)
        return data if isinstance(data, list) else []

class DynamicQueryGenerator:
    def __init__(self, target, industry, country, states, evaluator):
        self.target = target.strip()
        self.industry = industry.strip() if industry and industry != "ALL_SECTORS" else ""
        self.country = country.strip() if country else ""
        self.states = [s.strip() for s in states if s.strip()]
        self.year = datetime.now().year
        self.evaluator = evaluator

    def _normalize_ai_tracks(self, data):
        if not isinstance(data, dict):
            return None
        
        for parent in ["tracks", "queries", "data", "result"]:
            if parent in data and isinstance(data[parent], dict):
                data = data[parent]
                break

        norm = {}
        for k, v in data.items():
            if isinstance(v, list) and v:
                clean_k = re.sub(r'[^a-zA-Z0-9]', '', str(k)).upper()
                if any(x in clean_k for x in ["TRACK1", "TENDER", "PUBLIC"]):
                    norm["TRACK_1_PUBLIC_TENDERS"] = [str(x) for x in v[:2]]
                elif any(x in clean_k for x in ["TRACK2", "CORPORATE", "PROCUREMENT", "VENDOR"]):
                    norm["TRACK_2_CORPORATE_PROCUREMENT"] = [str(x) for x in v[:2]]
                elif any(x in clean_k for x in ["TRACK3", "EXPANSION", "CAPEX", "FACILITY"]):
                    norm["TRACK_3_BUSINESS_EXPANSION"] = [str(x) for x in v[:2]]
                elif any(x in clean_k for x in ["TRACK4", "HIRING", "SCALING", "CAREER"]):
                    norm["TRACK_4_COMMERCIAL_HIRING"] = [str(x) for x in v[:2]]
        
        return norm if len(norm) == 4 else None

    def build_tracks(self):
        state = random.choice(self.states) if self.states else ""
        yr = self.year
        t = self.target
        ind = self.industry
        country = self.country

        geo_desc = f"in {state}" if state else (f"in {country}" if country else "")
        ind_desc = f"(Industry: '{ind}')" if ind else "(Industry: ALL SECTORS - Universal B2B)"
        logger.info(f"🧠 Generating Search Vectors for '{t}' {ind_desc} {geo_desc}...")

        industry_instruction = f"SPECIFIC INDUSTRY FOCUS: Focus strictly on '{ind}'." if ind else "INDUSTRY SCOPE: ALL SECTORS (Unrestricted universal search across Manufacturing, Infrastructure, EPC, Commercial Real Estate, Industrial Capex, Healthcare, and Public Procurement)."

        prompt = f"""
        You are an elite B2B Sales Intelligence Strategist.
        Generate aggressive, diverse Google Search queries to uncover enterprise buyers, commercial tenders, and corporate organizations purchasing or expanding in '{t}'.
        {industry_instruction}
        {f"Target Geography: {country}" if country else ""}
        {f"Regional Focus: {state}" if state else ""}

        GENERATE 4 DYNAMIC COMMERCIAL TRACKS (2 queries each):
        TRACK 1 (PUBLIC SECTOR TENDERS & RFPS):
        - Government procurement, public authority tenders, bids, and RFPs for '{t}'.
        TRACK 2 (CORPORATE PROCUREMENT & VENDOR EMPANELMENT):
        - Private corporations issuing RFQs, vendor empanelment notices, or commercial supply contracts for '{t}'.
        TRACK 3 (CAPEX & BUSINESS EXPANSION SIGNALS):
        - Commercial companies announcing facility expansion, capex investments, or major project launches requiring '{t}'.
        TRACK 4 (COMMERCIAL TEAM HIRING & SCALING):
        - Corporate enterprises actively hiring dedicated specialists, department heads, or teams for '{t}'.

        SEARCH RULES:
        - Plain text queries ONLY (maximum 12 words per query).
        - DO NOT use Google search operators like site:, inurl:, quotes (""), or minus (-).
        - Respond strictly with JSON containing these exact keys:
          "TRACK_1_PUBLIC_TENDERS", "TRACK_2_CORPORATE_PROCUREMENT", "TRACK_3_BUSINESS_EXPANSION", "TRACK_4_COMMERCIAL_HIRING".
          Each key must map to an array of 2 strings.
        """
        raw_data = self.evaluator.generate_json(prompt)
        normalized = self._normalize_ai_tracks(raw_data)
        
        if normalized:
            logger.info("✅ Successfully generated dynamic query matrix using Gemini AI.")
            return normalized

        logger.warning("⚠️ AI query generation failed or returned invalid schema. Executing dynamic randomized fallback...")
        return self._dynamic_fallback_tracks(t, ind, state, country, yr)

    def _dynamic_fallback_tracks(self, t, ind, state, country, yr):
        location = state if state else country
        loc_str = f" {location}" if location else ""
        ind_str = f"{ind} " if ind else ""

        public_terms = random.choice([
            ["procurement tender bid", "RFP tender notice"],
            ["commercial works tender", "turnkey project bid tender"],
            ["public procurement notice", "e-procurement RFP proposal"]
        ])
        
        corp_terms = random.choice([
            ["corporate vendor empanelment supplier", "enterprise supplier contract requirement"],
            ["commercial RFQ requirement vendor registration", "corporate procurement partner onboarding"]
        ])
        
        expansion_terms = random.choice([
            ["commercial facility investment project expansion", "capex expansion greenfield project"],
            ["new plant facility construction project", "enterprise business capacity expansion"]
        ])

        hiring_terms = random.choice([
            ["enterprise hiring lead specialist", "corporate department expansion hiring"],
            ["team expansion careers opening", "senior technical lead hiring"]
        ])

        return {
            "TRACK_1_PUBLIC_TENDERS": [
                f"{t} {public_terms[0]} {yr}{loc_str}".strip(),
                f"{t} {public_terms[1]}{loc_str}".strip()
            ],
            "TRACK_2_CORPORATE_PROCUREMENT": [
                f"{ind_str}{t} {corp_terms[0]}{loc_str}".strip(),
                f"{t} {corp_terms[1]} {yr}".strip()
            ],
            "TRACK_3_BUSINESS_EXPANSION": [
                f"{ind_str}{t} {expansion_terms[0]} {yr}{loc_str}".strip(),
                f"{t} {expansion_terms[1]}{loc_str}".strip()
            ],
            "TRACK_4_COMMERCIAL_HIRING": [
                f"{t} {hiring_terms[0]}{loc_str}".strip(),
                f"{ind_str}{t} {hiring_terms[1]} {yr}".strip()
            ]
        }

class DataEngine:
    def __init__(self, key_manager):
        self.serper_keys = key_manager
        self.user_agents = [
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15"
        ]

    def search_until_leads_found(self, query, seen_links_cache, country_name="", target_new_urls=4):
        clean_q = re.sub(r'(site:|intitle:|inurl:)\S+', '', str(query), flags=re.IGNORECASE)
        clean_q = re.sub(r'[-"()]', ' ', clean_q)
        clean_q = re.sub(r'\bOR\b', ' ', clean_q)
        clean_q = " ".join(clean_q.split())
        
        logger.info(f"🔍 Searching: {clean_q}")
        results = []

        gl_code = None
        if country_name:
            c_clean = country_name.strip().lower()
            gl_map = {
                "india": "in", "united states": "us", "usa": "us", "uk": "gb",
                "united kingdom": "gb", "uae": "ae", "australia": "au",
                "canada": "ca", "germany": "de", "singapore": "sg", "japan": "jp"
            }
            gl_code = gl_map.get(c_clean, c_clean[:2])

        page_num = 1
        new_urls_found = 0

        while page_num <= 5:  # Optimized to 5 pages to maintain clean execution times
            payload_dict = {
                "q": clean_q,
                "num": 10,
                "page": page_num
            }
            if gl_code: payload_dict["gl"] = gl_code
            if any(k in clean_q.lower() for k in ["tender", "rfp", "bid", "procurement"]):
                payload_dict["tbs"] = "qdr:m"

            page_success = False
            organic = []
            
            for attempt in range(len(self.serper_keys.keys) or 1):
                current_key = self.serper_keys.get_current()
                if not current_key: break
                try:
                    response = requests.post(
                        "https://google.serper.dev/search",
                        headers={'X-API-KEY': current_key, 'Content-Type': 'application/json'},
                        json=payload_dict,
                        timeout=15
                    )
                    if response.status_code != 200:
                        logger.error(f"❌ Serper Key Rejected on Page {page_num} (Status {response.status_code}). Rotating...")
                        self.serper_keys.rotate("Serper")
                        continue
                    
                    data = response.json()
                    organic = data.get("organic", [])
                    page_success = True
                    break
                except Exception as e:
                    logger.error(f"❌ Serper Error on Page {page_num}: {e}")
                    self.serper_keys.rotate("Serper")

            if not page_success or not organic:
                break

            page_fresh_count = 0
            for item in organic:
                raw_url = item.get("link", "")
                if raw_url:
                    c_url = clean_url(raw_url)
                    results.append({"link": raw_url, "snippet": item.get("snippet", "")})
                    if c_url not in seen_links_cache:
                        new_urls_found += 1
                        page_fresh_count += 1

            logger.info(f"   [Page {page_num}] {len(organic)} results returned, {page_fresh_count} unseen (Total new found: {new_urls_found})")

            if new_urls_found >= target_new_urls:
                break

            page_num += 1
            time.sleep(0.5)

        if not results:
            try:
                with DDGS() as ddgs:
                    ddg_q = f"{clean_q} {country_name}".strip() if country_name else clean_q
                    for item in ddgs.text(ddg_q, region='wt-wt', max_results=10): 
                        results.append({"link": item.get("href"), "snippet": item.get("body", "")})
            except Exception as ddg_err:
                logger.error(f"🚨 DDGS Fallback failed: {ddg_err}")

        return results

    def fetch(self, url, protected_domains=None):
        time.sleep(random.uniform(0.8, 1.5))
        protected_domains = protected_domains or []
        is_protected = any(pd in url.lower() for pd in protected_domains if pd)
        target_url = f"https://r.jina.ai/{url}" if is_protected else url

        try:
            headers = {"User-Agent": random.choice(self.user_agents)}
            try:
                res = requests.get(target_url, headers=headers, timeout=12, verify=True)
            except requests.exceptions.SSLError:
                res = requests.get(target_url, headers=headers, timeout=12, verify=False)

            if res.status_code != 200 and not is_protected:
                res = requests.get(f"https://r.jina.ai/{url}", headers=headers, timeout=12)
            res.raise_for_status()

            if 'application/pdf' in res.headers.get('Content-Type', '') or url.lower().endswith('.pdf'):
                return "".join(page.extract_text() + "\n" for page in PdfReader(BytesIO(res.content)).pages[:8]).strip()
                
            soup = BeautifulSoup(res.text, 'html.parser')
            for el in soup(["script", "style", "nav", "footer", "header", "aside"]): el.decompose()
            return "\n".join([line.strip() for line in soup.get_text(separator="\n", strip=True).splitlines() if line.strip()])[:6000]
        except Exception: return None

class WebhookRouter:
    def __init__(self, url, secret):
        self.url, self.secret = url, secret

    def normalize_company(self, name):
        if not name or not isinstance(name, str): return "Unknown"
        cleaned = re.sub(r'(?i)\b(ltd|pvt|limited|private|inc|corp|llc)\b\.?', '', name)
        cleaned = re.sub(r'[^a-zA-Z0-9\s]', ' ', cleaned)
        cleaned = re.sub(r'\s+', ' ', cleaned).strip().title()
        return cleaned if cleaned else "Unknown"

    @retry(wait=wait_exponential(multiplier=2, min=4, max=10), stop=stop_after_attempt(3))
    def route_and_push(self, doc, ai_result, default_target, default_industry):
        company_name = self.normalize_company(ai_result.get('organization', 'Unknown'))
        if company_name.lower() in ["unknown", "unknown firm", ""]:
            return "SKIPPED_UNKNOWN"

        try:
            check = requests.post(self.url, json={"secret": self.secret, "action": "pre_flight_check", "company_name": company_name}, timeout=15).json()
            if check.get("exists") is True:
                logger.info(f"[-] Dropped Duplicate in CRM: {company_name}")
                return "DUPLICATE"
        except Exception: pass

        capture_date = datetime.now().strftime("%Y-%m-%d %H:%M")
        is_valid = ai_result.get('is_valid', False)
        confidence = ai_result.get('confidence', 'LOW')
        role = ai_result.get('entity_role', 'IRRELEVANT')
        product_usage = str(ai_result.get('product_usage', 'Unknown')).strip()
        solution = ai_result.get('target_solution') or default_target

        phone = ai_result.get('phone', '')
        if not phone or len(re.sub(r'[^0-9]', '', str(phone))) < 10:
            phone = extract_indian_phone_robust(doc.get('raw_text', ''))
        dm_name = ai_result.get('dm_name', '')

        if default_industry and default_industry != "ALL_SECTORS":
            industry_val = default_industry
        else:
            industry_val = ai_result.get('industry', 'Commercial Enterprise')

        target_sheet = "🗑 AI_Trash" if not is_valid else ("🤝 Partners & Suppliers" if role == "SELLER" else ("⚠️ Needs Review" if confidence == "LOW" else "📥 Inbox"))
        lead_id = f"{str(uuid.uuid4())[:8].upper()}::{hashlib.md5(doc['url'].encode()).hexdigest()[:10]}"

        if target_sheet == "🗑 AI_Trash": 
            row_data = [capture_date, company_name, doc['url'], ai_result.get('why_engage_now', ''), ""]
        elif target_sheet == "🤝 Partners & Suppliers": 
            row_data = [lead_id, capture_date, company_name, ai_result.get('city', ''), ai_result.get('state', ''), solution, doc['url'], "", "", "", ""]
        else: 
            row_data = [
                capture_date,
                ai_result.get('upcoming_events', 'Unknown'),
                role,
                industry_val,
                ai_result.get('state', 'N/A'),
                ai_result.get('city', 'N/A'),
                company_name,
                solution,
                ai_result.get('why_engage_now', ''),
                doc['url'],
                product_usage,
                "",
                lead_id
            ]

        logger.info(f"[*] Routing {company_name} [{role}] -> {target_sheet}")
        requests.post(self.url, json={
            "secret": self.secret, 
            "action": "insert_lead", 
            "target_sheet": target_sheet, 
            "company_name": company_name, 
            "signal_brief": ai_result.get('why_engage_now', ''), 
            "row_data": row_data,
            "contact_phone": phone,
            "dm_name": dm_name
        }, timeout=15)
        return target_sheet

if __name__ == "__main__":
    logger.info("=== Waking Up: Radar Scout (Fully Audited Engine) ===")
    start_time, leads_pushed, scanned_links = datetime.now(), 0, 0
    session_companies = set()
    
    try: seen_links = load_hybrid_cache()
    except Exception as e:
        system_monitor.send(f"🚨 *Radar Scout Halted*: Cache failure.\nError: `{e}`")
        logger.error("System exit triggered."); exit(1)
        
    try:
        settings_req = requests.get(f"{WEBHOOK_URL}?secret={WEBHOOK_SECRET}&action=get_settings&cb={int(time.time())}", timeout=30).json()
        
        TARGETS = [t.strip() for t in settings_req.get("target_products", []) if t.strip()]
        if not TARGETS:
            logger.error("❌ No Search Targets found in Settings (Column A). Execution stopped.")
            exit(1)
            
        INDUSTRIES = [i.strip() for i in settings_req.get("industry_keywords", []) if i.strip()]
        industries_to_run = INDUSTRIES if INDUSTRIES else ["ALL_SECTORS"]
        
        COUNTRIES = [c.strip() for c in settings_req.get("target_countries", []) if c.strip()]
        COUNTRY = COUNTRIES[0] if COUNTRIES else ""
        
        STATES = [s.strip() for s in settings_req.get("target_states", []) if s.strip()]
        
        BANNED_KW = [k.strip().lower() for k in settings_req.get("banned_keywords", []) if k.strip()]
        BANNED_SITES = [s.strip().lower() for s in settings_req.get("banned_websites", []) if s.strip()]
        PROTECTED_DOMAINS = [d.strip().lower() for d in settings_req.get("protected_domains", []) if d.strip()]
        
        geo_rule = f"GEOGRAPHY RULE: Must have verifiable commercial operations or active requirements in {COUNTRY}." if COUNTRY else ""
        ban_rule = f"Banned domains to ignore: {', '.join(BANNED_SITES)}" if BANNED_SITES else ""
    except Exception as e:
        logger.error(f"Settings Error: {e}")
        exit(1)

    engine = DataEngine(serper_keys)
    evaluator = DynamicB2BEvaluator(gemini_keys)
    router = WebhookRouter(WEBHOOK_URL, WEBHOOK_SECRET)
    
    for TARGET in TARGETS:
        for ind in industries_to_run:
            generator = DynamicQueryGenerator(TARGET, ind, COUNTRY, STATES, evaluator)
            tracks = generator.build_tracks()

            for track_name, queries in tracks.items():
                docs_to_evaluate = []
                for query in queries:
                    search_results = engine.search_until_leads_found(
                        query, seen_links_cache=seen_links, country_name=COUNTRY, target_new_urls=3
                    )
                    
                    for res in search_results:
                        raw_link = res.get("link", "").strip()
                        snippet = res.get("snippet", "").strip()
                        if not raw_link: continue
                        
                        c_link = clean_url(raw_link)
                        if c_link in seen_links: continue
                        scanned_links += 1
                        
                        if not any(pd in raw_link.lower() for pd in PROTECTED_DOMAINS if pd):
                            if any(bd in raw_link.lower() for bd in BANNED_SITES if bd) or any(bx in snippet.lower() for bx in BANNED_KW if bx):
                                save_to_cache(c_link); seen_links.add(c_link); continue
                                
                        save_to_cache(c_link); seen_links.add(c_link)
                        content = engine.fetch(raw_link, protected_domains=PROTECTED_DOMAINS)
                        if content: 
                            docs_to_evaluate.append({"track": track_name, "url": raw_link, "raw_text": content})
                            
                if docs_to_evaluate:
                    # Optimized Batch Size of 4 to cut API calls by 50%
                    for i in range(0, len(docs_to_evaluate), 4):
                        batch = docs_to_evaluate[i:i+4]
                        raw_verdicts = evaluator.evaluate_batch(batch, TARGET, ind, COUNTRY, STATES, geo_rule, ban_rule)
                        time.sleep(4)  # Safe cooldown preventing Free Tier 15 RPM overruns
                        
                        # ZERO DROP SAFEGUARD:
                        if not raw_verdicts:
                            logger.warning("⚠️ High network saturation. Preserving batch to Review tab.")
                            for item in batch:
                                fallback_verdict = {
                                    "item_index": 0,
                                    "is_valid": True,
                                    "confidence": "LOW",
                                    "entity_role": "BUYER",
                                    "organization": "Unparsed Prospect",
                                    "industry": ind if ind != "ALL_SECTORS" else "General Enterprise",
                                    "why_engage_now": "Direct capture from active procurement signal. AI evaluation timed out.",
                                    "product_usage": "Prospective Buyer - Captured from signal",
                                    "upcoming_events": "Unknown"
                                }
                                router.route_and_push(item, fallback_verdict, TARGET, ind)
                            continue
                        
                        ai_verdicts = raw_verdicts if isinstance(raw_verdicts, list) else [raw_verdicts]
                        
                        for verdict in ai_verdicts:
                            try:
                                idx = int(verdict.get("item_index"))
                            except (TypeError, ValueError):
                                idx = None

                            if idx is not None and 0 <= idx < len(batch):
                                comp_name = router.normalize_company(verdict.get('organization', ''))
                                if comp_name.lower() in ["unknown", "unknown firm", ""]:
                                    continue
                                if comp_name in session_companies:
                                    logger.info(f"[-] Dropped In-Flight Duplicate: {comp_name}")
                                    continue
                                
                                session_companies.add(comp_name)
                                if router.route_and_push(batch[idx], verdict, TARGET, ind) in ["📥 Inbox", "⚠️ Needs Review"]: 
                                    leads_pushed += 1

    system_monitor.send(f"🏁 *Radar Scout Complete*\n• Links Scanned: `{scanned_links}`\n• Sent to CRM: `{leads_pushed}`\n• Duration: `{str(datetime.now() - start_time).split('.')[0]}`")
