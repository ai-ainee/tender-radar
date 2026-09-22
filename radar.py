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

log(">>> UNIVERSAL COMMERCIAL, TENDER & RECRUITMENT RADAR ACTIVE")

# ---------------------------------------------------------------------------
# 1. Environment Secrets & Config
# ---------------------------------------------------------------------------
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

COMMERCIAL_PATTERNS = re.compile(
    r"\b(tender|tenders|rfp|bid|bids|bidding|gem|eprocure|procurement|supply|quotation|eoi|nit|license|licenses|subscription|renewal|contract|hiring|vacancy|drafter|modeler|architect|engineer|job|jobs|capex|expansion|project win|awarded|contractor|consultancy|freelance|subcontract|indiamart|rera|ireps)\b",
    re.IGNORECASE,
)

LOCATION_PATTERNS = re.compile(
    r"\b(New Delhi|Delhi|NCR|Mumbai|Bengaluru|Bangalore|Chennai|Kolkata|Hyderabad|Pune|Ahmedabad|Noida|Gurgaon|Gurugram|Jaipur|Lucknow|Chandigarh|Kochi|Bhopal|Indore|Patna|Coimbatore|Vadodara|Surat|Nagpur|Maharashtra|Karnataka|Tamil Nadu|Uttar Pradesh|Gujarat|Telangana|Haryana|Kerala|Rajasthan|Madhya Pradesh)\b",
    re.IGNORECASE,
)

EMAIL_REGEX = re.compile(r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+")
PHONE_REGEX = re.compile(r"(?:\+91[- ]?)?[6789]\d{9}\b")


def load_products():
    if not os.path.exists(PRODUCTS_FILE):
        return ["AutoCAD", "Autodesk Revit", "Civil 3D"]
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
    """Extracts root website/portal domain cleanly."""
    try:
        parsed = urllib.parse.urlparse(url)
        if "google.com" in parsed.netloc:
            return "https://gem.gov.in"
        return f"{parsed.scheme}://{parsed.netloc}"
    except Exception:
        return "Web Portal"


def resolve_clean_url(url):
    """Returns portal domain and full destination link."""
    domain = extract_base_website(url)
    return domain, url


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
        log(f"  -> Sheet updated! Status: {res.status_code}")
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
        log(f"  -> Telegram alert sent! Status: {res.status_code}")
    except Exception as e:
        log(f"  -> Telegram Send Error: {e}")


def fetch_all_opportunities(product):
    stream_queries = [
        f'"{product}" (site:gem.gov.in OR site:eprocure.gov.in OR site:ireps.gov.in OR "tender notice") India',
        f'"{product}" (hiring OR vacancy OR "job opening" OR drafter OR modeler) (site:linkedin.com/jobs OR site:naukri.com OR site:indeed.com) India',
        f'"{product}" (capex OR "project win" OR "awarded contract" OR "EPC contract" OR "new manufacturing unit") India',
        f'"{product}" (site:indiamart.com OR "request for proposal" OR "subcontract" OR "design consultancy") India',
        f'"{product}" (RERA OR "metro rail" OR "smart city" OR "expressway" OR CPWD) India',
        f'"{product}" (freelance OR drafting OR "BIM outsourcing" OR "2D to 3D conversion") India'
    ]

    all_items = []
    seen_in_scan = set()

    for q in stream_queries:
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
                            "product": product
                        })
        except Exception as e:
            log(f"Fetch notice for stream: {e}")

    return all_items


def deep_scan_contact_info(item):
    text = f"{item['title']} {item['summary']}"
    emails = EMAIL_REGEX.findall(text)
    phones = PHONE_REGEX.findall(text)

    if not emails or not phones:
        try:
            r = requests.get(item["link"], headers=HEADERS, timeout=4)
            if r.status_code == 200 and r.text:
                page_text = r.text[:2500]
                if not emails:
                    found_emails = EMAIL_REGEX.findall(page_text)
                    if found_emails:
                        emails = [e for e in found_emails if not e.endswith((".png", ".jpg", ".jpeg"))]
                if not phones:
                    phones = PHONE_REGEX.findall(page_text)
        except Exception:
            pass

    email = emails[0] if emails else "Not Listed"
    phone = phones[0] if phones else "Not Listed"
    return email, phone


def extract_lead_locally(item):
    text = f"{item['title']} {item['summary']}".lower()

    if any(k in text for k in ["naukri", "linkedin", "indeed", "hiring", "drafter", "engineer vacancy", "modeler"]):
        ltype = "💼 Hiring Lead (Software Requirement)"
    elif any(k in text for k in ["capex", "expansion", "awarded", "project win", "new plant", "inauguration", "epc"]):
        ltype = "🏗 Capex / Project Expansion"
    elif any(k in text for k in ["gem.gov", "eprocure", "ireps", "tender", "nit", "bid"]):
        ltype = "🏛 Government / GeM Tender"
    elif any(k in text for k in ["indiamart", "freelance", "subcontract", "consultancy assignment"]):
        ltype = "🤝 B2B Sub-Consultancy / Freelance"
    elif any(k in text for k in ["rera", "metro rail", "smart city", "infrastructure", "cpwd"]):
        ltype = "🏢 Infrastructure & Real Estate Project"
    else:
        ltype = "📋 Commercial Procurement RFP"

    loc_match = LOCATION_PATTERNS.search(item["title"] + " " + item["summary"])
    address = f"{loc_match.group(0)}, India" if loc_match else "India"

    email, phone = deep_scan_contact_info(item)
    website, clean_link = resolve_clean_url(item["link"])
    org_guess = item["title"].split("-")[-1].strip() if "-" in item["title"] else "Industry Enterprise / Buyer"

    clean_summary = re.sub(r"<[^>]+>", " ", item['summary']).strip()
    if len(clean_summary) < 25:
        clean_summary = item['title']

    return {
        "is_lead": True,
        "lead_type": ltype,
        "org": org_guess,
        "address": address,
        "website": website,
        "contact_person": "Procurement / HR Lead",
        "email": email,
        "phone": phone,
        "summary": clean_summary[:180],
        "clean_link": clean_link
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
    You are an Indian commercial procurement, EPC and hiring intelligence analyst.
    Evaluate the following search items for commercial opportunities:
    {items_block}

    Assign each item to its exact category:
    - Government / GeM Tender
    - Hiring Lead (Software/Revit Drafter demand)
    - Capex / Project Expansion (EPC, industrial plant, metro rail)
    - B2B Sub-Consultancy / Freelance (IndiaMART or subcontracting RFP)
    - Infrastructure & Real Estate Project (RERA, development awards)
    - Non-Lead (Reject software piracy or generic software tutorials)

    Reply ONLY with a raw JSON list:
    [
      {{
        "item_index": 0,
        "is_lead": true or false,
        "lead_type": "Selected Category from above",
        "org": "Hiring Company / Developer / PSU Name",
        "address": "City, State or India",
        "website": "Domain URL or Base Website",
        "contact_person": "Officer / HR Name or Not Listed",
        "email": "Email or Not Listed",
        "phone": "Phone or Not Listed",
        "summary": "1 concise sentence stating the scope of software, licenses, or project requirements"
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
            log(f"Client init note: {e}")

    products = load_products()
    seen = load_seen()
    log(f"Active Monitoring Queries: {products}")

    candidates = []
    for prod in products:
        log(f"Sweeping all commercial streams for: {prod}")
        items = fetch_all_opportunities(prod)
        for item in items:
            if item["link"] in seen:
                continue
            seen.add(item["link"])
            save_seen(item["link"])

            text_blob = f"{item['title']} {item['summary']}"
            if COMMERCIAL_PATTERNS.search(text_blob):
                candidates.append(item)

    log(f"Total actionable leads identified: {len(candidates)}")
    if not candidates:
        log("No new opportunities detected across channels this cycle.")
        return

    evaluations = try_gemini_analysis(client, candidates[:10])

    leads_recorded = 0
    if evaluations:
        for res in evaluations:
            idx = res.get("item_index")
            if idx is not None and idx < len(candidates) and res.get("is_lead") is True:
                item = candidates[idx]
                leads_recorded += 1
                dispatch_lead(item, res)
    else:
        log("--> Processing leads through Local Multi-Channel Rule Engine...")
        for item in candidates[:10]:
            res = extract_lead_locally(item)
            if res.get("is_lead") is True:
                leads_recorded += 1
                dispatch_lead(item, res)

    log(f"\nCompleted run. Total leads recorded and pushed: {leads_recorded}")


def dispatch_lead(item, data):
    prod = item["product"]
    org = data.get("org", "Buyer / Recruiter / Enterprise")
    address = data.get("address", "India")
    website = data.get("website", extract_base_website(item["link"]))
    ltype = data.get("lead_type", "Commercial Lead")
    contact = data.get("contact_person", "Not Listed")
    email = data.get("email", "Not Listed")
    phone = data.get("phone", "Not Listed")
    summary = data.get("summary", item['title'])
    link = data.get("clean_link", item["link"])

    log(f"\n>>> [CONFIRMED COMMERCIAL LEAD]: {item['title'][:70]}")
    log(f"    Category: {ltype} | Company: {org} | Location: {address}")

    push_to_google_sheet(prod, ltype, org, address, website, contact, email, phone, summary, link)

    msg = (
        f"🚨 *New Commercial Opportunity!*\n\n"
        f"🏷 *Category:* {ltype}\n"
        f"🏢 *Company / Buyer:* {org}\n"
        f"📍 *Location:* {address}\n"
        f"🌐 *Source / Portal:* {website}\n"
        f"📦 *Product / Requirement:* {prod}\n"
        f"👤 *Contact Person:* {contact}\n"
        f"📧 *Email:* {email}\n"
        f"📞 *Phone:* {phone}\n"
        f"📝 *Summary:* {summary}\n\n"
        f"🔗 [Full Information Link]({link})"
    )
    send_telegram(msg)


if __name__ == "__main__":
    main()
