import os
import sys
import json
import re
import urllib.parse
from datetime import datetime, timezone
import xml.etree.ElementTree as ET
import requests

def log(msg):
    print(msg, flush=True)

log(">>> MULTI-CHANNEL COMMERCIAL & HIRING RADAR ONLINE")

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
GOOGLE_SHEET_WEBHOOK = os.environ.get("GOOGLE_SHEET_WEBHOOK")

PRODUCTS_FILE = "products.txt"
SEEN_FILE = "seen_links.txt"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

# Detection patterns across jobs, expansion, capex, and tenders
COMMERCIAL_PATTERNS = re.compile(
    r"\b(hiring|vacancy|drafter|architect|designer|engineer|job|jobs|tender|rfp|bid|gem|eprocure|capex|expansion|project win|awarded|contractor|consultancy|freelance|subcontract)\b",
    re.IGNORECASE,
)

# Known Indian Hiring / B2B / News / Govt Portals
PORTAL_PATTERNS = re.compile(
    r"\b(linkedin\.com|naukri\.com|indeed\.com|freelancer|upwork|foundit|gem\.gov\.in|eprocure\.gov\.in|constructionweekonline|livemint|economictimes|financialexpress)\b",
    re.IGNORECASE,
)

LOCATION_PATTERNS = re.compile(
    r"\b(New Delhi|Delhi|Mumbai|Bengaluru|Bangalore|Chennai|Kolkata|Hyderabad|Pune|Ahmedabad|Noida|Gurgaon|Gurugram|Jaipur|Lucknow|Chandigarh|Maharashtra|Karnataka|Tamil Nadu|Uttar Pradesh|Gujarat)\b",
    re.IGNORECASE,
)

EMAIL_REGEX = re.compile(r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+")
PHONE_REGEX = re.compile(r"(?:\+91[- ]?)?[6789]\d{9}\b")

def load_products():
    if not os.path.exists(PRODUCTS_FILE):
        return ["AutoCAD", "Autodesk Revit"]
    with open(PRODUCTS_FILE, "r", encoding="utf-8") as f:
        return [line.strip() for line in f if line.strip() and not line.startswith("#")]

def load_seen():
    if os.path.exists(SEEN_FILE):
        with open(SEEN_FILE, "r", encoding="utf-8") as f:
            return set(line.strip() for line in f if line.strip())
    return set()

def save_seen(link):
    with open(SEEN_FILE, "a", encoding="utf-8") as f:
        f.write(link + "\n")

def extract_base_website(url):
    try:
        parsed = urllib.parse.urlparse(url)
        return f"{parsed.scheme}://{parsed.netloc}"
    except Exception:
        return "Web Link"

def push_to_google_sheet(product, ltype, org, address, website, contact, email, phone, summary, link):
    if not GOOGLE_SHEET_WEBHOOK:
        return
    payload = {
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        "product": product,
        "type": ltype,
        "org": org,
        "address": address,
        "website": website,
        "contact_person": contact,
        "email": email,
        "phone": phone,
        "summary": summary,
        "link": link,
    }
    try:
        res = requests.post(GOOGLE_SHEET_WEBHOOK, json=payload, timeout=10)
        log(f"  -> Sheet updated: {res.status_code}")
    except Exception as e:
        log(f"  -> Sheet Push Error: {e}")

def send_telegram(text):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "Markdown",
        "disable_web_page_preview": False,
    }
    try:
        res = requests.post(url, json=payload, timeout=10)
        log(f"  -> Telegram dispatched: {res.status_code}")
    except Exception as e:
        log(f"  -> Telegram Error: {e}")

def fetch_multichannel_leads(product):
    """
    Sweeps multiple commercial intent channels:
    1. Hiring & Jobs (Naukri, LinkedIn, Indeed)
    2. Capex, Expansions & EPC Project Wins
    3. Government Tenders & GeM Bids
    4. Freelancing & Sub-contracting RFPs
    """
    channels = [
        # Channel 1: Job Postings (Immediate Software/License Need)
        f'"{product}" (hiring OR vacancy OR drafter OR "job opening") (site:linkedin.com/jobs OR site:naukri.com OR site:indeed.com) India',
        
        # Channel 2: Industry Capex, New Projects & Expansions
        f'"{product}" (capex OR "project win" OR "bagged order" OR expansion OR "EPC contract" OR plant) India',
        
        # Channel 3: Government Bids & Tenders
        f'"{product}" (site:gem.gov.in OR site:eprocure.gov.in OR "tender notice") India',
        
        # Channel 4: Corporate RFPs, Consultation & Freelance
        f'"{product}" ("request for proposal" OR "consultancy assignment" OR "subcontract" OR freelance) India',
    ]

    all_items = []
    seen_in_scan = set()

    for q in channels:
        encoded = urllib.parse.quote(q)
        url = f"https://news.google.com/rss/search?q={encoded}&hl=en-IN&gl=IN&ceid=IN:en"
        try:
            resp = requests.get(url, headers=HEADERS, timeout=8)
            if resp.status_code == 200 and resp.content:
                root = ET.fromstring(resp.content)
                for item in root.findall(".//item")[:5]:
                    link = item.findtext("link", "").strip()
                    title = item.findtext("title", "").strip()
                    desc = item.findtext("description", "").strip()
                    if link and title and link not in seen_in_scan:
                        seen_in_scan.add(link)
                        all_items.append({
                            "title": title,
                            "link": link,
                            "summary": desc,
                            "product": product,
                        })
        except Exception as e:
            log(f"Error fetching channel: {e}")

    return all_items

def parse_lead_locally(item):
    """Fallback rule classifier to categorize lead type without relying solely on AI."""
    text = f"{item['title']} {item['summary']}".lower()

    # Determine Lead Category
    if any(k in text for k in ["naukri", "linkedin", "indeed", "hiring", "drafter", "engineer vacancy"]):
        ltype = "💼 Hiring Lead (Software Requirement)"
    elif any(k in text for k in ["capex", "expansion", "bagged", "project win", "new plant", "inauguration"]):
        ltype = "🏗 Capex / Project Expansion"
    elif any(k in text for k in ["gem.gov", "eprocure", "tender", "nit"]):
        ltype = "🏛 Government / GeM Tender"
    elif any(k in text for k in ["freelance", "upwork", "subcontract"]):
        ltype = "🤝 Freelance / Sub-Consulting"
    else:
        ltype = "📋 B2B Corporate RFP"

    # Identify City/Location
    loc_match = LOCATION_PATTERNS.search(item["title"] + " " + item["summary"])
    address = f"{loc_match.group(0)}, India" if loc_match else "India"

    # Extract Contacts
    emails = EMAIL_REGEX.findall(item["summary"])
    phones = PHONE_REGEX.findall(item["summary"])
    email = emails[0] if emails else "Not Listed"
    phone = phones[0] if phones else "Not Listed"

    # Determine Organization or Source Domain
    website = extract_base_website(item["link"])
    org_guess = item["title"].split("-")[-1].strip() if "-" in item["title"] else "Industry Buyer"

    clean_summary = re.sub(r"<[^>]+>", " ", item['summary']).strip()
    if len(clean_summary) < 25:
        clean_summary = item['title']

    return {
        "is_lead": True,
        "lead_type": ltype,
        "org": org_guess,
        "address": address,
        "website": website,
        "contact_person": "HR / Procurement Manager",
        "email": email,
        "phone": phone,
        "summary": clean_summary[:180]
    }

def try_gemini_analysis(client, batch):
    if not client:
        return None

    items_block = ""
    for idx, it in enumerate(batch):
        clean_title = it['title'].replace('"', "'")
        clean_desc = re.sub(r"<[^>]+>", " ", it['summary']).replace('"', "'")[:200]
        items_block += f"\n--- ITEM {idx} ---\nTitle: {clean_title}\nSnippet: {clean_desc}\nLink: {it['link']}\n"

    prompt = f"""
    You are an Indian commercial intelligence analyst.
    Evaluate these candidate items for commercial opportunities:
    {items_block}

    Categorize each item as one of:
    - Hiring Lead (Hiring CAD/Revit team indicates software/training need)
    - Capex / Project Win (EPC, Infrastructure, Plant expansion)
    - GeM / Government Tender
    - Corporate RFP / Sub-consultancy
    - Non-Lead (Reject pure tutorials/crack software)

    Reply ONLY with a raw JSON array:
    [
      {{
        "item_index": 0,
        "is_lead": true or false,
        "lead_type": "Category from above",
        "org": "Hiring Company / Developer / PSU Name",
        "address": "City, State or India",
        "website": "Domain URL",
        "contact_person": "HR / Officer / Not Listed",
        "email": "Email or Not Listed",
        "phone": "Phone or Not Listed",
        "summary": "1-sentence summary of the job/project opportunity"
      }}
    ]
    """

    try:
        response = client.models.generate_content(
            model="gemini-3.6-flash",
            contents=prompt,
        )
        raw = (
            response.text.strip()
            .removeprefix("```json")
            .removeprefix("```")
            .removesuffix("```")
            .strip()
        )
        return json.loads(raw)
    except Exception as e:
        log(f"  [AI Notice] API throttled/offline ({e}). Using Local Multi-Channel Engine.")
        return None

def main():
    client = None
    if GEMINI_API_KEY:
        try:
            from google import genai
            client = genai.Client(api_key=GEMINI_API_KEY)
        except Exception as e:
            log(f"Client init warning: {e}")

    products = load_products()
    seen = load_seen()
    log(f"Active Monitoring Queries: {products}")

    candidates = []
    for prod in products:
        log(f"Scanning Multi-Channel Feeds for: {prod}")
        items = fetch_multichannel_leads(prod)
        for item in items:
            if item["link"] in seen:
                continue
            seen.add(item["link"])
            save_seen(item["link"])

            text_blob = f"{item['title']} {item['summary']}"
            if COMMERCIAL_PATTERNS.search(text_blob):
                candidates.append(item)

    log(f"Total High-Intent Candidates Gathered: {len(candidates)}")
    if not candidates:
        log("No new opportunities detected across channels.")
        return

    # Try Gemini analysis on the top candidates, fallback to local parser if rate-limited
    evaluations = try_gemini_analysis(client, candidates[:8])

    leads_recorded = 0
    if evaluations:
        for res in evaluations:
            idx = res.get("item_index")
            if idx is not None and idx < len(candidates) and res.get("is_lead") is True:
                item = candidates[idx]
                leads_recorded += 1
                dispatch_lead(item, res)
    else:
        log("--> Categorizing leads via Local Multi-Channel Engine...")
        for item in candidates[:8]:
            res = parse_lead_locally(item)
            if res["is_lead"]:
                leads_recorded += 1
                dispatch_lead(item, res)

    log(f"\nWorkflow complete. Leads processed: {leads_recorded}")

def dispatch_lead(item, data):
    prod = item["product"]
    org = data.get("org", "Commercial Buyer / Recruiter")
    address = data.get("address", "India")
    website = data.get("website", extract_base_website(item["link"]))
    ltype = data.get("lead_type", "Commercial Opportunity")
    contact = data.get("contact_person", "Not Listed")
    email = data.get("email", "Not Listed")
    phone = data.get("phone", "Not Listed")
    summary = data.get("summary", item['title'])
    link = item["link"]

    log(f"\n>>> [QUALIFIED COMMERCIAL LEAD]: {item['title'][:70]}")
    log(f"    Channel: {ltype} | Company: {org} | Location: {address}")

    push_to_google_sheet(prod, ltype, org, address, website, contact, email, phone, summary, link)

    msg = (
        f"🚨 *New Commercial Lead Detected!*\n\n"
        f"🏷 *Category:* {ltype}\n"
        f"🏢 *Company / Buyer:* {org}\n"
        f"📍 *Location:* {address}\n"
        f"🌐 *Source / Website:* {website}\n"
        f"📦 *Relevant Software:* {prod}\n"
        f"👤 *Contact:* {contact}\n"
        f"📧 *Email:* {email}\n"
        f"📞 *Phone:* {phone}\n"
        f"📝 *Summary:* {summary}\n\n"
        f"🔗 [Direct Source Link]({link})"
    )
    send_telegram(msg)

if __name__ == "__main__":
    main()
