import os
import sys
import json
import re
import time
import urllib.parse
import io
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
import xml.etree.ElementTree as ET
import requests
from bs4 import BeautifulSoup
from pydantic import BaseModel, Field
from typing import List

try:
    from pypdf import PdfReader
except ImportError:
    PdfReader = None

def log(msg):
    print(msg, flush=True)

log(">>> ENTERPRISE RADAR 9.12 ACTIVE (FULLY CONSOLIDATED & BULLETPROOF)")

GOOGLE_SHEET_WEBHOOK = os.environ.get("GOOGLE_SHEET_WEBHOOK")
PRODUCTS_FILE = "products.txt"
NEGATIVE_FILE = "negative_keywords.txt"
SEEN_FILE = "seen_links.txt"
MAX_LEADS_PER_RUN = 100

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
})

COMMERCIAL_PATTERNS = re.compile(
    r"\b(tender|tenders|rfp|bid|bids|bidding|gem|eprocure|procurement|supply|quotation|eoi|nit|"
    r"corrigendum|addendum|extension|license|licenses|subscription|renewal|contract|hiring|vacancy|"
    r"drafter|modeler|architect|engineer|job|jobs|capex|expansion|project win|awarded|contractor|"
    r"consultancy|freelance|subcontract|indiamart|rera|ireps|environmental clearance|seiaa|"
    r"allotted land|dpr|feasibility|empanelment|appointed as|joins as|head of bim|chief architect|"
    r"series a|series b|raises funding|acquired|acquisition|merger|ipo|drhp)\b",
    re.IGNORECASE,
)

LOCATION_PATTERNS = re.compile(
    r"\b(New Delhi|Delhi|NCR|Mumbai|Bengaluru|Bangalore|Chennai|Kolkata|Hyderabad|Pune|Ahmedabad|"
    r"Noida|Gurgaon|Gurugram|Jaipur|Lucknow|Chandigarh|Kochi|Bhopal|Indore|Patna|Coimbatore|Vadodara|"
    r"Surat|Nagpur|Thane|Pimpri|Nashik|Visakhapatnam|Madurai|Salem|Ghaziabad|Kanpur|Agra|Varanasi|"
    r"Maharashtra|Karnataka|Tamil Nadu|Uttar Pradesh|Gujarat|Telangana|Haryana|Kerala|Rajasthan|"
    r"Madhya Pradesh|Bihar|West Bengal|Andhra Pradesh|Punjab|Odisha|Jharkhand|Chhattisgarh|Assam|Uttarakhand)\b",
    re.IGNORECASE,
)

STATE_MAP = {
    'new delhi': 'Delhi', 'delhi': 'Delhi', 'ncr': 'Delhi/NCR',
    'noida': 'Uttar Pradesh', 'greater noida': 'Uttar Pradesh',
    'gurgaon': 'Haryana', 'gurugram': 'Haryana', 'faridabad': 'Haryana',
    'ghaziabad': 'Uttar Pradesh', 'meerut': 'Uttar Pradesh',
    'mumbai': 'Maharashtra', 'pune': 'Maharashtra', 'nagpur': 'Maharashtra',
    'thane': 'Maharashtra', 'pimpri': 'Maharashtra', 'nashik': 'Maharashtra', 'maharashtra': 'Maharashtra',
    'bengaluru': 'Karnataka', 'bangalore': 'Karnataka', 'karnataka': 'Karnataka',
    'chennai': 'Tamil Nadu', 'coimbatore': 'Tamil Nadu', 'tamil nadu': 'Tamil Nadu',
    'ahmedabad': 'Gujarat', 'vadodara': 'Gujarat', 'surat': 'Gujarat', 'gujarat': 'Gujarat',
    'lucknow': 'Uttar Pradesh', 'kanpur': 'Uttar Pradesh', 'uttar pradesh': 'Uttar Pradesh',
    'hyderabad': 'Telangana', 'telangana': 'Telangana',
    'visakhapatnam': 'Andhra Pradesh', 'andhra pradesh': 'Andhra Pradesh',
    'kolkata': 'West Bengal', 'west bengal': 'West Bengal',
    'patna': 'Bihar', 'bihar': 'Bihar',
    'kochi': 'Kerala', 'kerala': 'Kerala',
    'jaipur': 'Rajasthan', 'rajasthan': 'Rajasthan',
    'bhopal': 'Madhya Pradesh', 'indore': 'Madhya Pradesh', 'madhya pradesh': 'Madhya Pradesh',
    'chandigarh': 'Chandigarh', 'ludhiana': 'Punjab', 'punjab': 'Punjab',
    'bhubaneswar': 'Odisha', 'odisha': 'Odisha', 'ranchi': 'Jharkhand', 'jharkhand': 'Jharkhand',
    'raipur': 'Chhattisgarh', 'chhattisgarh': 'Chhattisgarh', 'guwahati': 'Assam', 'assam': 'Assam',
    'dehradun': 'Uttarakhand', 'uttarakhand': 'Uttarakhand'
}

EXPIRED_YEARS_PATTERN = re.compile(r"\b(2018|2019|2020|2021|2022|2023)\b")
EMAIL_REGEX = re.compile(r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+")
PHONE_REGEX = re.compile(r"(?:\+91[- ]?)?[6789]\d{9}\b")
VALUE_PATTERNS = re.compile(r"(?:₹|Rs\.?|INR|\$)\s*[\d,]+(?:\.\d+)?\s*(?:Cr(?:ore)?|Lakh|L|K|Million|M|Billion|B)?\b", re.IGNORECASE)
DEADLINE_PATTERNS = re.compile(r"(?:due|closing|last|end)\s*(?:date|time)?[:\s\-]+(\d{1,2}[-\/.]\d{1,2}[-\/.]\d{2,4}|\d{1,2}\s+[A-Za-z]{3,9}\s+\d{4})", re.IGNORECASE)
QUANTITY_PATTERNS = re.compile(r"(\d+)\s*(?:nos|qty|licenses|users|seats|posts|openings|positions|units)\b", re.IGNORECASE)
EMD_PATTERNS = re.compile(r"(?:emd|earnest money|bid security)[:\s\-]+(?:₹|Rs\.?|INR)?\s*[\d,]+", re.IGNORECASE)

class APIKeyPool:
    def __init__(self):
        raw_keys = os.environ.get("GEMINI_API_KEYS") or os.environ.get("GEMINI_API_KEY") or ""
        self.keys = [k.strip() for k in raw_keys.split(",") if k.strip()]
        self.current_index = 0

    def get_current_key(self):
        return self.keys[self.current_index] if self.keys else None

    def rotate_key(self):
        if not self.keys or len(self.keys) <= 1: return False
        old_idx = self.current_index
        self.current_index = (self.current_index + 1) % len(self.keys)
        if self.current_index == 0: return False
        return True

    def get_client(self):
        key = self.get_current_key()
        if not key: return None
        try:
            from google import genai
            return genai.Client(api_key=key)
        except Exception:
            return None

KEY_POOL = APIKeyPool()

class LeadData(BaseModel):
    item_index: int = Field(description="The index number of the evaluated item.")
    is_lead: bool = Field(description="True if this is a commercial lead, hiring mandate, or corporate signal.")
    lead_type: str = Field(description="Strictly one of: 'Corporate Signal (M&A / Funding)', 'Government / GeM Tender', 'Hiring Mandate', 'Private Capex / Expansion Win', 'Upstream Project Clearance', 'Leadership Move', 'Architect / Consultant Empanelment', 'B2B Sub-Consultancy', or 'Non-Lead'.")
    org: str = Field(description="Target enterprise, PSU, builder, or hiring entity.")
    address: str = Field(description="City or district location in India.")
    state: str = Field(description="Specific Indian State. 'Pan-India' if not specific.")
    contact_person: str = Field(description="Identified decision maker or HR contact. 'Not Listed' if absent.")
    email: str = Field(description="Email address. 'Not Listed' if absent.")
    phone: str = Field(description="Phone number. 'Not Listed' if absent.")
    estimated_value: str = Field(description="Contract budget, capex value, or 'Not Disclosed'.")
    quantity: str = Field(description="Required seats, units, or scope volume.")
    deadline: str = Field(description="Closing deadline, or 'Immediate / Open'.")
    emd_fee: str = Field(description="EMD/Tender fee. STRICTLY 'N/A' if private.")
    priority: str = Field(description="Strictly one of: '🔥 High Urgency', '⚡ Warm', or '🌱 Strategic Nurture'.")
    eligibility: str = Field(description="Required vendor criteria, technical certifications, or strategic note.")
    summary: str = Field(description="A clean, one-sentence executive summary.")

class LeadBatchResponse(BaseModel):
    leads: List[LeadData]

def load_products():
    if not os.path.exists(PRODUCTS_FILE): return ["AutoCAD", "Revit"]
    with open(PRODUCTS_FILE, "r", encoding="utf-8") as f:
        return [line.strip() for line in f if line.strip() and not line.startswith("#")]

def load_negatives():
    if not os.path.exists(NEGATIVE_FILE): return []
    with open(NEGATIVE_FILE, "r", encoding="utf-8") as f:
        return [line.strip().lower() for line in f if line.strip() and not line.startswith("#")]

def load_seen():
    if os.path.exists(SEEN_FILE):
        with open(SEEN_FILE, "r", encoding="utf-8") as f:
            return set(line.strip() for line in f if line.strip())
    return set()

def save_seen(link):
    with open(SEEN_FILE, "a", encoding="utf-8") as f:
        f.write(link + "\n")

def unwrap_destination_url(initial_url):
    if "news.google.com" not in initial_url: return initial_url
    try:
        resp = SESSION.get(initial_url, allow_redirects=True, timeout=10)
        final_url = resp.url
        if "google.com" in final_url:
            match = re.search(r'data-n-url="([^"]+)"', resp.text)
            if not match: match = re.search(r'<meta[^>]+http-equiv="refresh"[^>]+content="[^"]*url=([^"]+)"', resp.text, re.IGNORECASE)
            if match: return match.group(1).replace("&amp;", "&")
        return final_url
    except Exception:
        return initial_url

def extract_base_website(url):
    try: return f"{urllib.parse.urlparse(url).scheme}://{urllib.parse.urlparse(url).netloc}"
    except Exception: return "Web Portal"

def format_pubdate(pubdate_str):
    if not pubdate_str: return "Not Listed"
    try:
        dt = parsedate_to_datetime(pubdate_str)
        if dt.tzinfo is None: dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    except Exception:
        return pubdate_str

def is_item_recent(pubdate_str, max_days):
    if not pubdate_str: return True
    try:
        dt = parsedate_to_datetime(pubdate_str)
        if dt.tzinfo is None: dt = dt.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt) <= timedelta(days=max_days)
    except Exception:
        return True

def push_to_google_sheet(payload):
    if not GOOGLE_SHEET_WEBHOOK: return True
    try:
        res = SESSION.post(GOOGLE_SHEET_WEBHOOK, json=payload, timeout=10)
        return True
    except Exception:
        return True

def send_telegram(text, is_media_source=False, target_state="Pan-India"):
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not bot_token: return
    
    active_chat_id = None
    if is_media_source:
        active_chat_id = os.environ.get("TELEGRAM_CHAT_ID_INDUSTRY_MEDIA")
    
    if not active_chat_id:
        generic_fallbacks = ["pan-india", "india", "not listed", "", "unknown", "pan india", "rest of india"]
        clean_state = (target_state or "").strip().lower()
        if clean_state not in generic_fallbacks:
            safe_state_key = target_state.upper().replace(" ", "_").replace("-", "_")
            active_chat_id = os.environ.get(f"TELEGRAM_CHAT_ID_{safe_state_key}")
            
    if not active_chat_id:
        active_chat_id = os.environ.get("TELEGRAM_CHAT_ID")
        
    if not active_chat_id: return

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {"chat_id": active_chat_id, "text": text, "parse_mode": "Markdown", "disable_web_page_preview": False}
    try:
        SESSION.post(url, json=payload, timeout=10)
    except Exception:
        pass

def deep_scrape_content(url):
    try:
        response = SESSION.get(url, timeout=10)
        if response.status_code != 200: return ""
        if "application/pdf" in response.headers.get("Content-Type", "") or url.lower().endswith(".pdf"):
            if not PdfReader: return "[PDF Detected]"
            pdf = PdfReader(io.BytesIO(response.content))
            return "".join([(page.extract_text() or "") + " " for page in pdf.pages[:3]])[:10000]
        soup = BeautifulSoup(response.content, 'html.parser')
        for script in soup(["script", "style", "noscript", "header", "footer"]): script.extract()
        return re.sub(r'\s+', ' ', soup.get_text(separator=' ', strip=True))[:10000]
    except Exception:
        pass
    return ""

def fetch_direct_cppp_tenders(product):
    url = "https://eprocure.gov.in/cppp/latestactivetenders/1/xml"
    items = []
    try:
        resp = SESSION.get(url, timeout=10)
        if resp.status_code == 200:
            root = ET.fromstring(resp.content)
            for tender in root.findall(".//Tender")[:40]:
                title = tender.findtext("Title", "")
                link = tender.findtext("TenderURL", "")
                desc = tender.findtext("Description", "")
                if product.lower() in title.lower() or product.lower() in desc.lower():
                    items.append({
                        "title": f"[DIRECT CPPP TENDER] {title}", "link": link, "summary": desc,
                        "product": product, "raw_pubdate": datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S GMT")
                    })
    except Exception:
        pass
    return items

def fetch_all_opportunities(product, time_window_query, max_age_days):
    all_items = fetch_direct_cppp_tenders(product)
    seen_in_scan = set([i["link"] for i in all_items])

    stream_queries = [
        f'"{product}" (site:constructionbusinesstoday.com OR site:constructionweekonline.in OR site:moneycontrol.com OR site:economictimes.indiatimes.com OR site:themachinist.in) {time_window_query}',
        f'"{product}" (site:gem.gov.in OR site:eprocure.gov.in OR site:ireps.gov.in OR site:etenders.gov.in) India {time_window_query}',
        f'"{product}" ("tender notice" OR "request for proposal" OR "corrigendum" OR "bid invitation" OR "nit") India {time_window_query}',
        f'"{product}" (capex OR "project win" OR "awarded contract" OR "EPC contract" OR "new manufacturing plant") India {time_window_query}',
        f'"{product}" ("Environmental Clearance" OR "DPR approved" OR RERA OR "Detailed Project Report" OR "allotted land" OR MIDC OR GIDC) India {time_window_query}',
        f'"{product}" ("Empanelment of Architects" OR "EOI for Architectural" OR "design consultancy") India {time_window_query}',
        f'"{product}" (hiring OR vacancy OR "job opening") (site:linkedin.com/jobs OR site:naukri.com) India {time_window_query}'
    ]

    for q in stream_queries:
        encoded = urllib.parse.quote(q)
        url = f"https://news.google.com/rss/search?q={encoded}&hl=en-IN&gl=IN&ceid=IN:en"
        try:
            resp = SESSION.get(url, timeout=10)
            if resp.status_code == 200 and resp.content:
                root = ET.fromstring(resp.content)
                for item in root.findall(".//item"):
                    link, title, desc, raw_pubdate = [item.findtext(k, "").strip() for k in ["link", "title", "description", "pubDate"]]
                    if not is_item_recent(raw_pubdate, max_age_days) or EXPIRED_YEARS_PATTERN.search(title + " " + desc): continue
                    if link and title and link not in seen_in_scan:
                        seen_in_scan.add(link)
                        all_items.append({"title": title, "link": link, "summary": desc, "product": product, "raw_pubdate": raw_pubdate})
        except Exception:
            pass
    return all_items

def try_gemini_analysis(batch):
    if not batch: return None
    items_block = ""
    for idx, it in enumerate(batch):
        deep_text = deep_scrape_content(it['real_link'])
        context_payload = deep_text if len(deep_text) > 300 else it['summary']
        items_block += f"\n--- ITEM {idx} ---\nTitle: {it['title']}\nLink: {it['real_link']}\nData: {context_payload}\n"

    prompt = f"Analyze the following text and extract leads. Extract contacts, emails, phones, and metadata.\nData: {items_block}"
    
    client = KEY_POOL.get_client()
    if not client: return None
    try:
        from google.genai import types
        response = client.models.generate_content(
            model='gemini-2.5-pro', contents=prompt,
            config=types.GenerateContentConfig(response_mime_type="application/json", response_schema=LeadBatchResponse, temperature=0.1)
        )
        res = json.loads(response.text)
        if res and "leads" in res: return res["leads"]
    except Exception:
        pass
    return None

def main():
    products = load_products()
    negatives = load_negatives()
    seen = load_seen()

    time_query = "when:2d"
    max_age = 2

    candidates = []
    for prod in products:
        items = fetch_all_opportunities(prod, time_query, max_age)
        for item in items:
            if item["link"] in seen: continue
            combined_text = f"{item['title']} {item['summary']}".lower()
            if any(neg in combined_text for neg in negatives): continue
            
            seen.add(item["link"])
            save_seen(item["link"])

            if COMMERCIAL_PATTERNS.search(combined_text):
                item["real_link"] = unwrap_destination_url(item["link"])
                candidates.append(item)

    if not candidates: return

    leads_recorded = 0
    for i in range(0, len(candidates), 10):
        if leads_recorded >= MAX_LEADS_PER_RUN:
            log(f">>> [Lead Limit Reached] Reached maximum cap of {MAX_LEADS_PER_RUN} leads for this run. Stopping.")
            break

        batch = candidates[i:i + 10]
        evaluations = try_gemini_analysis(batch)
        if evaluations:
            for res_dict in evaluations:
                if leads_recorded >= MAX_LEADS_PER_RUN: break
                idx = res_dict.get("item_index")
                if idx is not None and idx < len(batch) and res_dict.get("is_lead") is True:
                    if dispatch_lead(batch[idx], res_dict):
                        leads_recorded += 1

def dispatch_lead(item, data):
    prod = item["product"]
    org = data.get("org", "Commercial Buyer")
    address = data.get("address", "India")
    state = data.get("state", "Pan-India")
    
    if state.lower() in ["pan-india", "india", "not listed", ""] and address.lower() in STATE_MAP:
        state = STATE_MAP[address.lower()]

    real_link = item.get("real_link", item["link"])
    website = extract_base_website(real_link)
    
    is_media = any(domain in website for domain in ["constructionbusinesstoday.com", "constructionweekonline.in", "moneycontrol.com", "economictimes.indiatimes.com", "themachinist.in"])
    if is_media:
        data["lead_type"] = "📰 Industry Media News"

    ltype, contact, email, phone = data.get("lead_type", "Commercial Lead"), data.get("contact_person", "Not Listed"), data.get("email", "Not Listed"), data.get("phone", "Not Listed")
    summary, estimated_value, quantity, deadline, emd_fee = data.get("summary", item['title']), data.get("estimated_value", "Not Disclosed"), data.get("quantity", "1 Requirement"), data.get("deadline", "Check Notice"), data.get("emd_fee", "N/A")
    priority = data.get("priority", "⚡ Warm")
    
    # Urgent Booster Check
    if any(k in summary.lower() for k in ["urgent", "immediate", "flash", "closing soon"]):
        priority = "🔥 High Urgency (ACTION REQUIRED)"

    pub_date, app_date = format_pubdate(item.get("raw_pubdate", "")), datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    log(f">>> [RECORDED]: {org} | State: {state} | Type: {ltype}")

    push_to_google_sheet({
        "appearance_date": app_date, "published_date": pub_date, "deadline": deadline, "priority": priority, "product": prod, "type": ltype,
        "estimated_value": estimated_value, "quantity": quantity, "emd_fee": emd_fee, "org": org, "address": address, "state": state,
        "website": website, "contact_person": contact, "email": email, "phone": phone, "eligibility": data.get("eligibility", "N/A"), "summary": summary, "link": real_link
    })

    emd_str = f"💳 *EMD / Tender Fee:* {emd_fee}\n" if emd_fee != 'N/A' else ""
    msg = (
        f"🚨 *Intelligence Signal Alert!*\n\n"
        f"🎯 *Priority Level:* {priority}\n"
        f"🏷 *Category:* {ltype}\n"
        f"🏢 *Entity / Buyer:* {org}\n"
        f"📦 *Product / Subject:* {prod} ({quantity})\n"
        f"💰 *Budget / Value:* {estimated_value}\n"
        f"⏳ *Key Deadline / Date:* `{deadline}`\n"
        f"{emd_str}"
        f"📍 *Location:* {address}, {state}\n"
        f"📅 *Published:* {pub_date}\n"
        f"👤 *Stakeholder / Contact:* {contact}\n"
        f"📧 *Email:* {email}\n"
        f"📞 *Phone:* {phone}\n"
        f"📝 *Sales Summary:* {summary}\n"
        f"🌐 *Portal:* {website}\n\n"
        f"🔗 [Open Original Document Link]({real_link})"
    )
    send_telegram(msg, is_media_source=is_media, target_state=state)
    return True

if __name__ == "__main__":
    main()
