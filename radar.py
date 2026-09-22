import json
import os
import urllib.parse
from datetime import datetime, timezone
import feedparser
from google import genai
import requests

# Load keys from GitHub Secrets
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
GOOGLE_SHEET_WEBHOOK = os.environ.get("GOOGLE_SHEET_WEBHOOK")

PRODUCTS_FILE = "products.txt"
SEEN_FILE = "seen_links.txt"


def load_products():
    if not os.path.exists(PRODUCTS_FILE):
        return []
    with open(PRODUCTS_FILE, "r", encoding="utf-8") as f:
        return [
            line.strip()
            for line in f
            if line.strip() and not line.startswith("#")
        ]


def load_seen():
    if os.path.exists(SEEN_FILE):
        with open(SEEN_FILE, "r", encoding="utf-8") as f:
            return set(line.strip() for line in f if line.strip())
    return set()


def save_seen(link):
    with open(SEEN_FILE, "a", encoding="utf-8") as f:
        f.write(link + "\n")


def push_to_google_sheet(product, ltype, org, contact, email, phone, summary, link):
    if not GOOGLE_SHEET_WEBHOOK:
        return
    payload = {
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        "product": product,
        "type": ltype,
        "org": org,
        "contact_person": contact,
        "email": email,
        "phone": phone,
        "summary": summary,
        "link": link,
    }
    try:
        requests.post(GOOGLE_SHEET_WEBHOOK, json=payload, timeout=12)
    except Exception as e:
        print(f"Error appending to Google Sheet: {e}")


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
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        print(f"Telegram alert error: {e}")


def analyze_with_ai(client, product, title, summary):
    prompt = f"""
    You are an expert sales and tender procurement intelligence agent focusing on India.
    Analyze this web announcement/listing for the product: "{product}".

    Item Title: {title}
    Item Content: {summary}

    Determine if this represents a REAL Indian commercial opportunity:
    - Central Government / PSU tender (GeM, CPPP, Railways, Defense, MES, CPWD)
    - State e-Procurement tenders (UP, Maharashtra, Karnataka, Gujarat, etc.)
    - Private B2B trade inquiries / bulk buyer posts
    - Corporate Capex expansion, factory setup, or new project contracts won

    Extract any contact details, email addresses, phone numbers, or officer names if mentioned.
    If none are mentioned, mark them as "Not Listed".

    Reply ONLY with valid raw JSON (do NOT wrap with markdown quotes or ```json):
    {{
      "is_lead": true or false,
      "lead_type": "GeM/Govt Tender / State Tender / Capex Expansion / Private B2B",
      "org": "Name of Authority, Department, PSU, or Company",
      "contact_person": "Officer/Manager Name or 'Not Listed'",
      "email": "Email address or 'Not Listed'",
      "phone": "Phone/Mobile number or 'Not Listed'",
      "summary": "1 concise sentence summarizing what is to be supplied or procured"
    }}
    """
    try:
        response = client.models.generate_content(
            model="gemini-2.5-flash", contents=prompt
        )
        clean = (
            response.text.strip()
            .removeprefix("```json")
            .removeprefix("```")
            .removesuffix("```")
            .strip()
        )
        return json.loads(clean)
    except Exception as e:
        print(f"AI Parse Error: {e}")
        return {"is_lead": False}


def main():
    if not GEMINI_API_KEY:
        print("Missing GEMINI_API_KEY secret.")
        return

    client = genai.Client(api_key=GEMINI_API_KEY)
    products = load_products()
    seen = load_seen()

    if not products:
        print("No products found in products.txt.")
        return

    print(f"Scanning for {len(products)} products across Indian portals...")

    for prod in products:
        query = f'"{prod}" ("gem.gov.in" OR "eprocure" OR "tender" OR "NIT" OR "awarded contract" OR "setting up facility" OR "wins order")'
        encoded = urllib.parse.quote(query)
        rss_url = f"https://news.google.com/rss/search?q={encoded}&hl=en-IN&gl=IN&ceid=IN:en"

        feed = feedparser.parse(rss_url)
        for entry in feed.entries[:8]:
            link = getattr(entry, "link", "")
            title = getattr(entry, "title", "")
            summary = getattr(entry, "summary", "")

            if not link or link in seen:
                continue

            result = analyze_with_ai(client, prod, title, summary)
            if result.get("is_lead") is True:
                org = result.get("org", "Govt / Enterprise")
                ltype = result.get("lead_type", "Tender / Capex")
                contact = result.get("contact_person", "Not Listed")
                email = result.get("email", "Not Listed")
                phone = result.get("phone", "Not Listed")
                lead_summary = result.get("summary", "N/A")

                # 1. Send data with contact columns to Google Sheet
                push_to_google_sheet(
                    prod, ltype, org, contact, email, phone, lead_summary, link
                )

                # 2. Send instant Telegram push notification to mobile
                msg = (
                    f"🚨 *New Indian Commercial Lead!*\n\n"
                    f"📦 *Product:* {prod}\n"
                    f"🏛 *Organization:* {org}\n"
                    f"📋 *Type:* {ltype}\n"
                    f"👤 *Contact Person:* {contact}\n"
                    f"📧 *Email:* {email}\n"
                    f"📞 *Phone:* {phone}\n"
                    f"📝 *Summary:* {lead_summary}\n\n"
                    f"🔗 [Open Tender / Opportunity Link]({link})"
                )
                send_telegram(msg)
                print(f"[LEAD LOGGED] {title}")

            seen.add(link)
            save_seen(link)


if __name__ == "__main__":
    main()
