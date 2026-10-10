"""
common.py - shared building blocks for Radar Scout (radar.py + enrichment_worker.py).

Design goals
  * Only free tiers / free no-key services.
  * Never crash because one provider is down: every layer fails over to the next.
  * Plain `requests` for every API (no fragile SDK version issues).
"""
import html
import json
import logging
import os
import random
import re
import time
from io import BytesIO
from urllib.parse import parse_qsl, urlencode, urlparse

import requests
import urllib3
from bs4 import BeautifulSoup

try:
    from pypdf import PdfReader
except Exception:  # pragma: no cover
    PdfReader = None

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
for _noisy in ("httpx", "httpcore", "urllib3", "ddgs", "duckduckgo_search", "pypdf", "primp", "rquest", "openai"):
    logging.getLogger(_noisy).setLevel(logging.ERROR)
log = logging.getLogger("RadarScout")

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
]


# --------------------------------------------------------------------------- env helpers
def env_int(name, default):
    try:
        return int(str(os.getenv(name, "")).strip() or default)
    except ValueError:
        return default


def env_float(name, default):
    try:
        return float(str(os.getenv(name, "")).strip() or default)
    except ValueError:
        return default


def env_list(name):
    raw = os.getenv(name, "") or ""
    return [x.strip() for x in re.split(r"[,\n;]+", raw) if x.strip()]


# --------------------------------------------------------------------------- text helpers
def clean_url(url):
    """Canonical form used for de-duplication (idempotent)."""
    u = (url or "").strip()
    if not u:
        return ""
    if "://" not in u:
        u = "http://" + u
    try:
        p = urlparse(u)
        host = p.netloc.lower()
        if host.startswith("www."):
            host = host[4:]
        path = p.path.rstrip("/")
        q = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
             if not k.lower().startswith("utm_") and k.lower() not in ("fbclid", "gclid")]
        query = "?" + urlencode(q) if q else ""
        return f"{host}{path}{query}".lower()
    except Exception:
        return (url or "").strip().lower()


_PHONE_RE = re.compile(
    r"(?<!\d)(?:\+?91[\s\-]?|0)?([6-9]\d{4}[\s\-]?\d{5}|[6-9]\d{9}|[6-9]\d{2}[\s\-]?\d{3}[\s\-]?\d{4})(?!\d)"
)


def extract_phones(text, limit=5):
    """Indian 10-digit mobile numbers found in text (de-duplicated, order kept)."""
    out = []
    for m in _PHONE_RE.findall(text or ""):
        digits = re.sub(r"\D", "", m)
        if len(digits) == 10 and digits not in out:
            out.append(digits)
        if len(out) >= limit:
            break
    return out


def extract_phone(text):
    p = extract_phones(text, 1)
    return p[0] if p else ""


_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)*\.[A-Za-z]{2,}")
_BAD_EMAIL = re.compile(r"\.(png|jpe?g|gif|svg|webp|css|js)$|noreply|no-reply|example\.|sentry|wixpress|@2x", re.I)


def extract_emails(text, limit=8):
    out = []
    for e in _EMAIL_RE.findall(text or ""):
        e = e.strip(".").lower()
        if _BAD_EMAIL.search(e) or e in out:
            continue
        out.append(e)
        if len(out) >= limit:
            break
    return out


def normalize_company(name):
    if not name or not isinstance(name, str):
        return "Unknown"
    cleaned = re.sub(r"(?i)\b(ltd|pvt|limited|private|inc|corp|llc)\b\.?", "", name)
    cleaned = re.sub(r"[^a-zA-Z0-9\s]", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip().title()
    return cleaned if cleaned else "Unknown"


def extract_json(raw):
    """Tolerant JSON extraction from LLM output (fences, chatter, <think> blocks)."""
    if not raw or not isinstance(raw, str):
        return None
    s = re.sub(r"<think>.*?</think>", "", raw, flags=re.S | re.I).strip()
    s = re.sub(r"^```(?:json)?\s*|\s*```$", "", s, flags=re.I).strip()
    for cand in (s, re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", s)):
        try:
            return json.loads(cand)
        except Exception:
            pass
    dec = json.JSONDecoder()
    for i, m in enumerate(re.finditer(r"[\{\[]", s)):
        if i > 60:
            break
        try:
            obj, _ = dec.raw_decode(s[m.start():])
            return obj
        except Exception:
            continue
    return None


def tg_escape(value):
    return html.escape(str(value if value is not None else ""), quote=False)


# --------------------------------------------------------------------------- key pool
class KeyPool:
    """Comma/newline separated keys from one env var, with per-key cooldowns."""

    def __init__(self, env_name):
        raw = (os.getenv(env_name, "") or "").replace('"', "").replace("'", "")
        self.keys = [k for k in re.split(r"[,\s;]+", raw) if k]
        self.name = env_name
        self.index = 0

    def __bool__(self):
        return bool(self.keys)

    def __len__(self):
        return len(self.keys)

    def ordered(self):
        n = len(self.keys)
        return [self.keys[(self.index + i) % n] for i in range(n)] if n else []

    def advance(self):
        if self.keys:
            self.index = (self.index + 1) % len(self.keys)


# --------------------------------------------------------------------------- telegram
class Telegram:
    def __init__(self):
        self.token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        self.chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()

    @property
    def enabled(self):
        return bool(self.token and self.chat_id)

    def send(self, text, reply_markup=None):
        if not self.enabled:
            return False
        payload = {"chat_id": self.chat_id, "text": (text or "")[:4000], "parse_mode": "HTML",
                   "disable_web_page_preview": True}
        if reply_markup:
            payload["reply_markup"] = reply_markup if isinstance(reply_markup, dict) else json.loads(reply_markup)
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        for _ in range(3):
            try:
                r = requests.post(url, json=payload, timeout=(5, 15))
                if r.status_code == 200:
                    return True
                if r.status_code == 429:
                    try:
                        wait = int(r.json().get("parameters", {}).get("retry_after", 3))
                    except Exception:
                        wait = 3
                    time.sleep(min(wait, 20))
                    continue
                if r.status_code == 400 and payload.get("parse_mode"):
                    # broken HTML -> resend as plain text
                    payload.pop("parse_mode")
                    payload["text"] = html.unescape(re.sub(r"<[^>]+>", "", payload["text"]))
                    continue
                log.warning(f"Telegram HTTP {r.status_code}: {r.text[:200]}")
                return False
            except Exception as e:
                log.warning(f"Telegram send failed: {e}")
                time.sleep(1)
        return False


# --------------------------------------------------------------------------- google sheet (apps script) api
class SheetError(Exception):
    def __init__(self, message, retry=True):
        super().__init__(message)
        self.retry = retry


class SheetAPI:
    def __init__(self):
        self.url = os.getenv("WEBHOOK_URL", "").strip()
        self.secret = os.getenv("WEBHOOK_SECRET", "").strip()
        if not self.url:
            raise SheetError("WEBHOOK_URL secret is missing", retry=False)
        if not self.secret:
            raise SheetError("WEBHOOK_SECRET secret is missing", retry=False)

    def _request(self, method, action, payload=None, retries=3, timeout=(10, 90)):
        last = None
        for attempt in range(1, retries + 1):
            try:
                if method == "GET":
                    params = {"secret": self.secret, "action": action, "cb": int(time.time())}
                    params.update(payload or {})
                    r = requests.get(self.url, params=params, timeout=timeout)
                else:
                    body = {"secret": self.secret, "action": action}
                    body.update(payload or {})
                    r = requests.post(self.url, json=body, timeout=timeout)
                if r.status_code >= 500 or r.status_code == 429:
                    raise SheetError(f"HTTP {r.status_code} from webhook")
                try:
                    data = r.json()
                except ValueError:
                    raise SheetError(
                        "Webhook did not return JSON. Re-deploy the Apps Script as Web app "
                        "(Execute as: Me, Who has access: Anyone) and update WEBHOOK_URL.", retry=False)
                if not isinstance(data, dict):
                    raise SheetError("Unexpected webhook response", retry=False)
                if data.get("status") == "error":
                    raise SheetError(data.get("message", "webhook error"), retry=False)
                return data
            except SheetError as e:
                last = e
                if not e.retry:
                    break
            except requests.RequestException as e:
                last = SheetError(f"network error: {e}")
            if attempt < retries:
                time.sleep(2 * attempt)
        raise last or SheetError("unknown webhook failure")

    def get(self, action, **params):
        return self._request("GET", action, params)

    def post(self, action, **payload):
        return self._request("POST", action, payload)


# --------------------------------------------------------------------------- search (Serper -> Brave -> DuckDuckGo)
def _load_ddgs():
    try:
        from ddgs import DDGS
        return DDGS
    except Exception:
        try:
            from duckduckgo_search import DDGS
            return DDGS
        except Exception:
            return None


GL_MAP = {"india": "in", "united states": "us", "usa": "us", "uk": "gb", "united kingdom": "gb", "uae": "ae",
          "united arab emirates": "ae", "australia": "au", "canada": "ca", "germany": "de", "singapore": "sg",
          "japan": "jp", "saudi arabia": "sa", "south africa": "za", "nepal": "np", "bangladesh": "bd"}


class SearchEngine:
    def __init__(self):
        self.serper = KeyPool("SERPER_API_KEYS")
        self.brave = KeyPool("BRAVE_API_KEYS")
        self._cool = {}
        self.calls = {"serper": 0, "brave": 0, "ddgs": 0}

    @staticmethod
    def clean_query(query):
        q = re.sub(r"(site:|intitle:|inurl:)\S+", " ", str(query or ""), flags=re.I)
        q = re.sub(r'[-"()]', " ", q)
        q = re.sub(r"\bOR\b", " ", q)
        return "join(q.split())[:300]

    @staticmethod
    def gl_code(country):
        c = (country or "").strip().lower()
        return GL_MAP.get(c, c[:2] if len(c) >= 2 else "")

    def _ok(self, provider, key):
        return self._cool.get((provider, key), 0) <= time.time()

    def _serper(self, q, gl, page, recent):
        for key in self.serper.ordered():
            if not self._ok("serper", key):
                continue
            payload = {"q": q, "num": 10, "page": page}
            if gl:
                payload["gl"] = gl
            if recent:
                payload["tbs"] = "qdr:m"
            try:
                r = requests.post("https://google.serper.dev/search",
                                  headers={"X-API-KEY": key, "Content-Type": "application/json"},
                                  json=payload, timeout=(5, 20))
                self.calls["serper"] += 1
                if r.status_code == 200:
                    return [{"link": i.get("link", ""), "title": i.get("title", ""), "snippet": i.get("snippet", "")}
                            for i in r.json().get("organic", []) if i.get("link")]
                # 400 = no credits, 401/403 = bad key, 429 = rate limit
                cool = 60 if r.status_code == 429 else 6 * 3600
                self._cool[("serper", key)] = time.time() + cool
                log.warning(f"Serper key rejected (HTTP {r.status_code}); trying next key / provider")
            except Exception as e:
                self._cool[("serper", key)] = time.time() + 120
                log.warning(f"Serper error: {e}")
        return None

    def _brave(self, q, gl, page, recent):
        for key in self.brave.ordered():
            if not self._ok("brave", key):
                continue
            params = {"q": q, "count": 20, "offset": max(page - 1, 0)}
            if gl:
                params["country"] = gl.upper() if gl != "gb" else "GB"
            if recent:
                params["freshness"] = "pm"
            try:
                r = requests.get("https://api.search.brave.com/res/v1/web/search",
                                 headers={"X-Subscription-Token": key, "Accept": "application/json"},
                                 params=params, timeout=(5, 20))
                self.calls["brave"] += 1
                if r.status_code == 200:
                    res = (r.json().get("web") or {}).get("results", [])
                    return [{"link": i.get("url", ""), "title": i.get("title", ""), "snippet": i.get("description", "")}
                            for i in res if i.get("url")]
                cool = 60 if r.status_code == 429 else 6 * 3600
                self._cool[("brave", key)] = time.time() + cool
                log.warning(f"Brave key rejected (HTTP {r.status_code})")
            except Exception as e:
                self._cool[("brave", key)] = time.time() + 120
                log.warning(f"Brave error: {e}")
        return None

    def _ddgs(self, q, country, recent, max_results=10):
        DDGS = _load_ddgs()
        if DDGS is None:
            log.warning("ddgs package not installed - free fallback search unavailable")
            return []
        for attempt in range(2):
            try:
                kwargs = {"max_results": max_results}
                if recent:
                    kwargs["timelimit"] = "m"
                results = list(DDGS().text(q, **kwargs) or [])
                self.calls["ddgs"] += 1
                return [{"link": i.get("href") or i.get("url") or "", "title": i.get("title", ""),
                         "snippet": i.get("body", "")} for i in results if (i.get("href") or i.get("url"))]
            except Exception as e:
                log.warning(f"DDGS search failed (attempt {attempt + 1}): {e}")
                time.sleep(3 + attempt * 3)
        return []

    def search(self, query, country="", pages=1, recent=False):
        q = self.clean_query(query)
        if not q:
            return []
        gl = self.gl_code(country)
        results, seen = [], set()

        def add(items):
            n = 0
            for it in items or []:
                if it["link"] and it["link"] not in seen:
                    seen.add(it["link"])
                    results.append(it)
                    n += 1
            return n

        for page in range(1, max(1, pages) + 1):
            got = None
            if self.serper:
                got = self._serper(q, gl, page, recent)
            if got is None and self.brave:
                got = self._brave(q, gl, page, recent)
            if got is None:
                break
            if add(got) == 0:
                break
            time.sleep(0.4)
        if not results:
            ddg_q = f"{q} {country}".strip() if country and country.lower() not in q.lower() else q
            add(self._ddgs(ddg_q, country, recent))
        return results


# --------------------------------------------------------------------------- page fetching
_last_jina = [0.0]


def _fetch_jina(url, max_chars):
    gap = 3.2 if not os.getenv("JINA_API_KEY") else 0.5  # keyless Jina limit is ~20 req/min
    wait = gap - (time.time() - _last_jina[0])
    if wait > 0:
        time.sleep(wait)
    headers = {"User-Agent": random.choice(USER_AGENTS), "Accept": "text/plain"}
    if os.getenv("JINA_API_KEY"):
        headers["Authorization"] = "Bearer " + os.getenv("JINA_API_KEY").strip()
    for attempt in range(2):
        try:
            _last_jina[0] = time.time()
            r = requests.get("https://r.jina.ai/" + url, headers=headers, timeout=(5, 30))
            if r.status_code == 200 and r.text:
                return r.text.strip()[:max_chars]
            if r.status_code == 429:
                time.sleep(6)
                continue
            return ""
        except Exception:
            time.sleep(1)
    return ""


def _read_limited(resp, limit):
    buf = bytearray()
    for chunk in resp.iter_content(65536):
        if not chunk:
            break
        buf.extend(chunk)
        if len(buf) >= limit:
            break
    return bytes(buf)


def _pdf_text(data, pages):
    if PdfReader is None:
        return ""
    try:
        reader = PdfReader(BytesIO(data))
        if getattr(reader, "is_encrypted", False):
            try:
                reader.decrypt("")
            except Exception:
                return ""
        parts = []
        for page in reader.pages[:pages]:
            try:
                parts.append(page.extract_text() or "")
            except Exception:
                continue
        return "\n".join(parts).strip()
    except Exception:
        return ""


def _html_text(raw_bytes, encoding, max_chars):
    try:
        text = raw_bytes.decode(encoding or "utf-8", errors="ignore")
        soup = BeautifulSoup(text, "html.parser")
        for el in soup(["script", "style", "nav", "footer", "header", "aside", "noscript", "svg", "form"]):
            el.decompose()
        lines = [ln.strip() for ln in soup.get_text(separator="\n", strip=True).splitlines() if ln.strip()]
        return "\n".join(lines)[:max_chars]
    except Exception:
        return ""


def _fetch_direct(url, max_chars, pdf_pages):
    try:
        with requests.get(url, headers={"User-Agent": random.choice(USER_AGENTS)}, timeout=(5, 15),
                          verify=False, stream=True, allow_redirects=True) as r:
            if r.status_code != 200:
                return ""
            clen = r.headers.get("Content-Length")
            if clen and clen.isdigit() and int(clen) > 12 * 1024 * 1024:
                log.info(f"Skipping oversized file: {url}")
                return ""
            ctype = r.headers.get("Content-Type", "").lower()
            is_pdf = "application/pdf" in ctype or url.lower().split("?")[0].endswith(".pdf")
            data = _read_limited(r, 8 * 1024 * 1024 if is_pdf else 2 * 1024 * 1024)
            if is_pdf or data[:5] == b"%PDF-":
                return _pdf_text(data, pdf_pages)[:max_chars]
            return _html_text(data, r.encoding or r.apparent_encoding, max_chars)
    except Exception:
        return ""


def fetch_page(url, protected_domains=(), max_chars=6000, pdf_pages=6):
    """Direct fetch first; falls back to the free r.jina.ai reader for JS/blocked pages."""
    time.sleep(random.uniform(0.4, 1.0))
    low = (url or "").lower()
    protected = any(d and d in low for d in protected_domains)
    text = "" if protected else _fetch_direct(url, max_chars, pdf_pages)
    if len(text) < 250:
        alt = _fetch_jina(url, max_chars)
        if len(alt) > len(text):
            text = alt
    return text


# --------------------------------------------------------------------------- LLM router (all free tiers)
class LLMRouter:
    """
    Tries, in order: Gemini (free) -> Groq (free) -> OpenRouter ":free" models -> any extra
    OpenAI-compatible endpoint -> OpenAI (only if you set a key).
    Handles per-key / per-model rate limits, dead models and bad keys automatically.
    """
    GEMINI_FALLBACK = ["gemini-2.5-flash", "gemini-2.5-flash-lite", "gemini-2.0-flash"]
    GEMINI_SKIP = ("image", "tts", "audio", "live", "native", "embedding", "robotics", "computer-use", "learnlm",
                   "gemma", "thinking", "vision")

    def __init__(self, min_interval=None):
        self.gemini = KeyPool("GEMINI_API_KEYS")
        self.groq = KeyPool("GROQ_API_KEYS")
        self.openrouter = KeyPool("OPENROUTER_API_KEYS")
        self.extra = KeyPool("EXTRA_LLM_API_KEYS")
        self.openai = KeyPool("OPENAI_API_KEY")
        self.base = {
            "gemini": os.getenv("GEMINI_BASE_URL", "https://generativelanguage.googleapis.com/v1beta").rstrip("/"),
            "groq": os.getenv("GROQ_BASE_URL", "https://api.groq.com/openai/v1").rstrip("/"),
            "openrouter": os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1").rstrip("/"),
            "extra": os.getenv("EXTRA_LLM_BASE_URL", "").strip().rstrip("/"),
            "openai": os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/"),
        }
        self.min_interval = env_float("LLM_MIN_INTERVAL", 5.0) if min_interval is None else min_interval
        self._last_call = 0.0
        self._cool = {}
        self._dead = set()
        self._models = {}
        self.calls = {}
        if not self.available():
            log.warning("No LLM API keys configured (GEMINI_API_KEYS / GROQ_API_KEYS / OPENROUTER_API_KEYS).")

    # ---- configuration
    def available(self):
        return bool(self.gemini or self.groq or self.openrouter or (self.extra and self.base["extra"]) or self.openai)

    def _pools(self):
        return {"gemini": self.gemini, "groq": self.groq, "openrouter": self.openrouter,
                "extra": self.extra if self.base["extra"] else KeyPool("__none__"), "openai": self.openai}

    def _discover_gemini(self):
        override = env_list("GEMINI_MODELS")
        if override:
            return override
        models = []
        for key in self.gemini.ordered()[:2]:
            try:
                r = requests.get(f"{self.base['gemini']}/models", headers={"x-goog-api-key": key},
                                 params={"pageSize": 200}, timeout=20)
                if r.status_code != 200:
                    continue
                for m in r.json().get("models", []):
                    name = (m.get("name") or "").replace("models/", "")
                    low = name.lower()
                    if "generateContent" not in (m.get("supportedGenerationMethods") or []):
                        continue
                    if "flash" not in low or any(x in low for x in self.GEMINI_SKIP):
                        continue
                    if re.search(r"-\d{3}$|\d{2}-\d{2}$", low):
                        continue
                    models.append(name)
                break
            except Exception as e:
                log.warning(f"Gemini model discovery failed: {e}")

        def rank(n):
            low = n.lower()
            ver = re.findall(r"\d+(?:\.\d+)?", low)
            v = float(ver[0]) if ver else 0.0
            stable = not any(x in low for x in ("preview", "exp"))
            return (stable, v, "lite" not in low)

        models = sorted(set(models), key=rank, reverse=True)[:5]
        if models:
            log.info(f"Gemini models selected: {models}")
            return models
        return list(self.GEMINI_FALLBACK)

    def _discover_openrouter(self):
        override = env_list("OPENROUTER_MODELS")
        if override:
            return override
        try:
            r = requests.get(f"{self.base['openrouter']}/models", timeout=20)
            if r.status_code == 200:
                items = []
                for m in r.json().get("data", []):
                    pr = m.get("pricing") or {}
                    mid = m.get("id", "")
                    low = mid.lower()
                    if any(x in low for x in ("vision", "image", "audio", "embed", "guard", "moderation")):
                        continue
                    if mid.endswith(":free") or (str(pr.get("prompt")) in ("0", "0.0") and str(pr.get("completion")) in ("0", "0.0")):
                        items.append((int(m.get("context_length") or 0), mid))
                items.sort(reverse=True)
                ids = [i for _, i in items][:4]
                if ids:
                    return ids
        except Exception as e:
            log.warning(f"OpenRouter discovery failed: {e}")
        return ["meta-llama/llama-3.3-70b-instruct:free"]

    def _models_for(self, provider):
        if provider in self._models:
            return self._models[provider]
        if provider == "gemini":
            m = self._discover_gemini()
        elif provider == "groq":
            m = env_list("GROQ_MODELS") or ["llama-3.3-70b-versatile", "llama-3.1-8b-instant"]
        elif provider == "openrouter":
            m = self._discover_openrouter()
        elif provider == "extra":
            m = env_list("EXTRA_LLM_MODELS")
        else:
            m = env_list("OPENAI_MODELS") or ["gpt-4o-mini"]
        self._models[provider] = m
        return m

    # ---- cooldown helpers
    def _cooling(self, provider, key, model):
        now = time.time()
        return self._cool.get((provider, key, "*"), 0) > now or self._cool.get((provider, key, model), 0) > now

    def _soonest(self):
        now = time.time()
        waits = [u - now for u in self._cool.values() if u > now]
        return min(waits) if waits else None

    def _throttle(self):
        wait = self.min_interval - (time.time() - self._last_call)
        if wait > 0:
            time.sleep(wait)
        self._last_call = time.time()

    # ---- single HTTP call -> (outcome, text)
    def _http(self, provider, model, key, prompt):
        self._throttle()
        self.calls[provider] = self.calls.get(provider, 0) + 1
        if provider == "gemini":
            body = {"contents": [{"role": "user", "parts": [{"text": prompt}]}],
                    "generationConfig": {"temperature": 0.2, "responseMimeType": "application/json",
                                         "maxOutputTokens": 8192}}
            return requests.post(f"{self.base['gemini']}/models/{model}:generateContent",
                                 headers={"x-goog-api-key": key, "Content-Type": "application/json"},
                                 json=body, timeout=(10, 120))
        body = {"model": model, "messages": [{"role": "user", "content": prompt}], "temperature": 0.2,
                "response_format": {"type": "json_object"}}
        headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
        if provider == "openrouter":
            headers["HTTP-Referer"] = "https://github.com/radar-scout"
            headers["X-Title"] = "Radar Scout"
        r = requests.post(f"{self.base[provider]}/chat/completions", headers=headers, json=body, timeout=(10, 120))
        if r.status_code == 400:  # some free models reject response_format
            body.pop("response_format", None)
            r = requests.post(f"{self.base[provider]}/chat/completions", headers=headers, json=body, timeout=(10, 120))
        return r

    @staticmethod
    def _text_from(provider, r):
        try:
            data = r.json()
            if provider == "gemini":
                parts = data["candidates"][0]["content"]["parts"]
                return "".join(p.get("text", "") for p in parts)
            return data["choices"][0]["message"]["content"] or ""
        except Exception:
            return ""

    def _call(self, provider, model, key, prompt):
        try:
            r = self._http(provider, model, key, prompt)
        except requests.RequestException as e:
            log.warning(f"{provider}/{model} network error: {e}")
            return "transient", ""
        code = r.status_code
        body = (r.text or "")[:600]
        low = body.lower()
        if code == 200:
            text = self._text_from(provider, r)
            return ("ok", text) if text.strip() else ("transient", "")
        if code == 429:
            compact = low.replace(" ", "").replace("_", "")
            if "limit:0" in compact:
                return "dead", ""
            daily = "perday" in compact or "daily" in compact
            try:
                retry_after = float(r.headers.get("retry-after", "")) if r.headers.get("retry-after") else None
            except ValueError:
                retry_after = None
            cool = 6 * 3600 if daily else min(max(retry_after or 65, 5), 300)
            self._cool[(provider, key, model)] = time.time() + cool
            log.warning(f"{provider}/{model} rate-limited ({'daily' if daily else 'per-minute'}); rotating")
            return "rate", ""
        if code == 404:
            log.warning(f"{provider}/{model} not found - disabling for this run")
            return "dead", ""
        if code in (401, 403) or (code == 400 and ("api key not valid" in low or "api_key_invalid" in low)):
            self._cool[(provider, key, "*")] = time.time() + 24 * 3600
            log.warning(f"{provider} key rejected (HTTP {code}) - skipping this key")
            return "badkey", ""
        if code == 400 and any(x in low for x in ("json mode", "not supported", "unsupported", "not enabled")):
            return "dead", ""
        log.warning(f"{provider}/{model} HTTP {code}: {body[:160]}")
        return "transient", ""

    # ---- public API
    def generate_json(self, prompt, validator=None, max_rounds=2):
        validator = validator or (lambda d: d is not None)
        pools = self._pools()
        order = ["gemini", "groq", "openrouter", "extra", "openai"]
        for rnd in range(max_rounds):
            for provider in order:
                pool = pools[provider]
                if not pool:
                    continue
                for model in self._models_for(provider):
                    if (provider, model) in self._dead:
                        continue
                    transient = 0
                    for key in pool.ordered():
                        if self._cooling(provider, key, model):
                            continue
                        outcome, text = self._call(provider, model, key, prompt)
                        if outcome == "ok":
                            data = extract_json(text)
                            if data is not None and validator(data):
                                return data
                            log.warning(f"{provider}/{model} returned unusable JSON; trying next model")
                            break
                        if outcome == "dead":
                            self._dead.add((provider, model))
                            break
                        if outcome == "transient":
                            transient += 1
                            if transient >= 2:
                                break
                        # rate / badkey -> next key
            wait = self._soonest()
            if rnd < max_rounds - 1 and wait is not None and wait <= 75:
                log.info(f"All LLM routes busy - waiting {wait + 1:.0f}s for the quota window to reset")
                time.sleep(wait + 1)
                continue
            break
        return None
