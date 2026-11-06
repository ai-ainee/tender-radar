/**
 * RADAR SCOUT V33 - COMPATIBLE GOOGLE APPS SCRIPT MASTER
 * Contact Master V2 + ERP Deals Engine (Inventory Ledger + GST) + Telegram control bot
 *
 * V33 changes: buyer and seller CRM deduplication are separate; historical
 * Trash cannot block future opportunities; source-URL notes preserve new
 * events; the existing Telegram, Contacts, Deals and triggers are retained.
 *
 * AFTER PASTING: Deploy > Manage deployments > Edit > New version > Deploy,
 * then run menu "Set Telegram Webhook", "Install Triggers", "Register Bot Commands".
 */
const SCRIPT_PROPERTIES = PropertiesService.getScriptProperties();
const cleanProp_ = (k) => (SCRIPT_PROPERTIES.getProperty(k) || "").trim().replace(/['"]/g, "");

const WEBHOOK_SECRET = cleanProp_("WEBHOOK_SECRET");
const TELEGRAM_BOT_TOKEN = cleanProp_("TELEGRAM_BOT_TOKEN");
const TELEGRAM_CHAT_ID = cleanProp_("TELEGRAM_CHAT_ID");
const TELEGRAM_CHAT_ID_GOV = cleanProp_("TELEGRAM_CHAT_ID_GOV") || TELEGRAM_CHAT_ID;
const TELEGRAM_CHAT_ID_REVIEW = cleanProp_("TELEGRAM_CHAT_ID_REVIEW") || TELEGRAM_CHAT_ID;
const TELEGRAM_CHAT_ID_WATCHLIST = cleanProp_("TELEGRAM_CHAT_ID_WATCHLIST") || TELEGRAM_CHAT_ID;
const TELEGRAM_API_URL = "https://api.telegram.org/bot" + TELEGRAM_BOT_TOKEN;
const SCRIPT_URL = cleanProp_("SCRIPT_URL") || cleanProp_("WEBHOOK_URL");

const GST_RATE = 0.18;
const GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent";
const DAY_MS = 1000 * 60 * 60 * 24;

const TABS = {
  SETTINGS: "⚙️ Settings", INBOX: "📥 Inbox", REVIEW: "⚠️ Needs Review", LEADS: "🎯 Leads",
  PIPELINE: "💼 Pipeline", ARCHIVE: "📦 Archive", CONTACTS: "📇 Contacts",
  PARTNERS: "🤝 Partners & Suppliers", TRASH: "🗑 AI_Trash", DEALS: "💰 Deals", MONITOR: "👁️ Monitoring",
  MANUAL: "🔍 Manual Extraction"
};
const COMPETITOR_TAB = "📊 Competitor Intel";

// General CRM lookups still include all tabs. Opportunity ingestion has a
// smaller role-specific dedupe scope so an old rejected row does not suppress
// a legitimate new buyer, and a buyer is not merged into a seller record.
const DEDUPE_HIERARCHY = [TABS.PIPELINE, TABS.LEADS, TABS.INBOX, TABS.REVIEW, TABS.PARTNERS, TABS.MONITOR, TABS.ARCHIVE, TABS.TRASH];
const BUYER_DEDUPE_TABS = [TABS.PIPELINE, TABS.LEADS, TABS.INBOX, TABS.REVIEW, TABS.MONITOR, TABS.ARCHIVE];
const SELLER_DEDUPE_TABS = [TABS.PARTNERS];
const LOOKUP_TABS = [TABS.REVIEW, TABS.INBOX, TABS.LEADS, TABS.PARTNERS, TABS.MANUAL, TABS.MONITOR, TABS.PIPELINE, TABS.ARCHIVE, TABS.TRASH];
// --- CONTACT MASTER V2 CONFIGURATION ---
const CORE_FIELDS = ['lead_id', 'name', 'organization', 'designation', 'phone', 'email', 'linkedin', 'notes'];

const EXTRA_COLUMNS = [                                // [field key sent by Python, header in the sheet]
  ['unique_id', 'Unique ID'], ['account_key', 'Account Key'], ['department', 'Department'],
  ['buying_role', 'Buying Role'], ['seniority', 'Seniority'], ['reports_to', 'Reports To'],
  ['is_primary', 'Primary Contact'], ['email_status', 'Email Status'], ['confidence', 'Confidence'],
  ['possible_emails', 'Possible Emails (unverified)'], ['evidence', 'Evidence'], ['last_enriched', 'Last Enriched'],
  // ── Deploy 2: phone fields from enrichment waterfall ──
  ['mobile', 'Mobile'], ['phone_type', 'Phone Type'], ['phone_status', 'Phone Status'],
  ['phone_source', 'Phone Source'], ['whatsapp', 'WhatsApp']
];

// =====================================================================
// --- SMALL UTILITIES ---
// =====================================================================
function safeAlert(message) {
  try { SpreadsheetApp.getUi().alert(message); } catch (e) { Logger.log(message); }
}

function escapeHtml(text) {
  if (text === null || text === undefined || text === "") return "";
  return String(text).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

function stripTags_(html) {
  return String(html || "").replace(/<[^>]+>/g, "")
    .replace(/&lt;/g, "<").replace(/&gt;/g, ">").replace(/&quot;/g, '"').replace(/&amp;/g, "&");
}

function clip_(text, max) {
  const s = String(text === null || text === undefined ? "" : text);
  return s.length > max ? s.slice(0, max - 1) + "…" : s;
}

// ─── CHANGED: normalise common suffix variants before comparing (F1) ──────
// "Tata Sons Ltd" == "TATA SONS LIMITED" == "Tata Sons Pvt Ltd"
function normName_(s) {
  return String(s === null || s === undefined ? "" : s)
    .toLowerCase()
    .replace(/\b(private|pvt)\b\.?/g, "pvt")
    .replace(/\b(limited|ltd)\b\.?/g, "ltd")
    .replace(/\b(and|&)\b/g, "")
    .replace(/[^\p{L}\p{N}]/gu, "");
}
// ───────────────────────────────────────────────────────────────────────────

function nameHash_(s) {
  const bytes = Utilities.computeDigest(Utilities.DigestAlgorithm.MD5, normName_(s), Utilities.Charset.UTF_8);
  return bytes.map(b => ((b < 0 ? b + 256 : b) + 0x100).toString(16).slice(1)).join("").slice(0, 12);
}
function cbToken_(companyName) { return "h:" + nameHash_(companyName); }

function findCol_(headers, needles) {
  for (let n = 0; n < needles.length; n++) {
    const needle = needles[n].toLowerCase();
    const idx = headers.findIndex(h => String(h).toLowerCase().includes(needle));
    if (idx > -1) return idx;
  }
  return -1;
}

function safeCell_(v) {
  const s = String(v === null || v === undefined ? "" : v);
  return /^[=+\-@]/.test(s) ? "'" + s : s;
}

function inr_(n) {
  n = Number(n);
  if (!isFinite(n)) n = 0;
  const neg = n < 0;
  n = Math.abs(n);
  const parts = n.toFixed(2).split(".");
  const intPart = parts[0];
  const last3 = intPart.slice(-3);
  let rest = intPart.slice(0, -3);
  if (rest) rest = rest.replace(/\B(?=(\d{2})+(?!\d))/g, ",") + ",";
  return (neg ? "-" : "") + rest + last3 + (parts[1] === "00" ? "" : "." + parts[1]);
}

function toNumber_(v) {
  if (typeof v === "number") return isFinite(v) ? v : 0;
  const n = parseFloat(String(v === null || v === undefined ? "" : v).replace(/[^0-9.\-]/g, ""));
  return isNaN(n) ? 0 : n;
}

function parseDealDate_(raw, refDate) {
  const ref = refDate || new Date();
  if (raw instanceof Date) {
    return isNaN(raw.getTime()) ? null : new Date(raw.getFullYear(), raw.getMonth(), raw.getDate());
  }
  const s = String(raw === null || raw === undefined ? "" : raw).trim();
  if (!s || s.toUpperCase() === "N/A") return null;

  let m = s.match(/^(\d{1,2})[\/\-.](\d{1,2})(?:[\/\-.](\d{2,4}))?$/);
  if (m) {
    const d = parseInt(m[1], 10), mo = parseInt(m[2], 10);
    if (mo < 1 || mo > 12 || d < 1 || d > 31) return null;
    let y;
    if (m[3]) {
      y = m[3].length === 2 ? 2000 + parseInt(m[3], 10) : parseInt(m[3], 10);
    } else {
      y = ref.getFullYear();
      if ((ref.getTime() - new Date(y, mo - 1, d).getTime()) / DAY_MS > 30) y++;
    }
    const out = new Date(y, mo - 1, d);
    return out.getMonth() === mo - 1 ? out : null;
  }

  m = s.match(/^(\d{1,2})[\s\-]([A-Za-z]{3,9})[\s\-,]*(\d{4})$/);
  if (m) {
    const months = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"];
    const mi = months.indexOf(m[2].slice(0, 3).toLowerCase());
    if (mi > -1) return new Date(parseInt(m[3], 10), mi, parseInt(m[1], 10));
  }

  const dt = new Date(s);
  return isNaN(dt.getTime()) ? null : new Date(dt.getFullYear(), dt.getMonth(), dt.getDate());
}

// =====================================================================
// --- TELEGRAM LOW-LEVEL HELPERS (never throw) ---
// =====================================================================
function tgApi_(method, payload) {
  if (!TELEGRAM_BOT_TOKEN) return { ok: false, description: "Missing TELEGRAM_BOT_TOKEN" };
  for (let attempt = 0; attempt < 2; attempt++) {
    try {
      const res = UrlFetchApp.fetch(TELEGRAM_API_URL + "/" + method, {
        method: "post", contentType: "application/json", muteHttpExceptions: true,
        payload: JSON.stringify(payload || {})
      });
      let json;
      try { json = JSON.parse(res.getContentText()); }
      catch (e) { json = { ok: false, description: "Bad response: " + res.getContentText().slice(0, 200) }; }
      if (!json.ok && res.getResponseCode() === 429 && attempt === 0) {
        const wait = (json.parameters && json.parameters.retry_after) ? json.parameters.retry_after : 2;
        Utilities.sleep(Math.min(wait, 20) * 1000 + 500);
        continue;
      }
      return json;
    } catch (err) {
      return { ok: false, description: String(err) };
    }
  }
  return { ok: false, description: "Retry exhausted" };
}

function tgSend_(chatId, text, keyboard) {
  const body = { chat_id: chatId, text: clip_(text, 4000), parse_mode: "HTML", disable_web_page_preview: true };
  if (keyboard && keyboard.length) body.reply_markup = { inline_keyboard: keyboard };
  let res = tgApi_("sendMessage", body);
  if (!res.ok && /parse entities|can't parse/i.test(res.description || "")) {
    body.text = clip_(stripTags_(text), 4000);
    delete body.parse_mode;
    res = tgApi_("sendMessage", body);
  }
  if (!res.ok) Logger.log("tgSend_ failed: " + JSON.stringify(res));
  return res;
}

let CB_ANSWERED_ = false;

function answerCallback(id, text) { 
  UrlFetchApp.fetch(TELEGRAM_API_URL + "/answerCallbackQuery", { 
    method: "post", 
    contentType: "application/json", 
    muteHttpExceptions: true, 
    payload: JSON.stringify({ callback_query_id: id, text: text }) 
  }); 
}

function editMessage(chat, msgId, text) {
  let r = tgApi_("editMessageText", { chat_id: chat, message_id: msgId, text: clip_(text, 4000), parse_mode: "HTML" });
  if (!r.ok && /parse entities|can't parse/i.test(r.description || "")) {
    r = tgApi_("editMessageText", { chat_id: chat, message_id: msgId, text: clip_(stripTags_(text), 4000) });
  }
  if (!r.ok && /message to edit not found|message can't be edited|message is not modified/i.test(r.description || "")) {
    Logger.log("editMessage: skipped - " + r.description);
    return;
  }
  if (!r.ok) Logger.log("editMessage failed: " + JSON.stringify(r));
}

// =====================================================================
// --- MENU / SETUP / DIAGNOSTICS ---
// =====================================================================
function setupProperties() {
  const required = ["WEBHOOK_SECRET", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "SCRIPT_URL"];
  const optional = ["GEMINI_API_KEY", "GITHUB_PAT", "GITHUB_REPO", "TELEGRAM_CHAT_ID_GOV", "TELEGRAM_CHAT_ID_REVIEW", "TELEGRAM_CHAT_ID_WATCHLIST"];
  const missing = required.filter(k => !SCRIPT_PROPERTIES.getProperty(k));
  const missingOpt = optional.filter(k => !SCRIPT_PROPERTIES.getProperty(k));
  safeAlert(missing.length
    ? "❌ Missing Script properties: " + missing.join(", ") + "\n\nAdd them in Project Settings -> Script properties."
    : "✅ All required Script properties are set." + (missingOpt.length ? "\n\nOptional, not set: " + missingOpt.join(", ") + "\n(GEMINI_API_KEY = /briefing & /top5, GITHUB_* = /harvest & instant enrichment)" : ""));
}

function onOpen() {
  SpreadsheetApp.getUi().createMenu('🤖 Radar CRM')
    .addItem('🧪 Test Telegram Alert + Button Test', 'testTelegramNotification')
    .addItem('🔍 Check Webhook Status (Diagnose)', 'diagnoseTelegram')
    .addItem('🧹 Run Auto-Cleanup (Deduplicate)', 'manualRunSelfCleaning')
    .addItem('🚫 Preview Fake-Entity Cleanup (dry run)', 'cleanupFakeEntitiesDryRun')
    .addItem('🗑️ Delete Fake Entities (with confirm)', 'cleanupFakeEntities')
    .addItem('🗑️ Archive Expired Tenders (>30 Days)', 'archiveExpiredTenders')
    .addSeparator()
    .addItem('⏰ Install Triggers (9 AM SLA + On-Edit)', 'installTriggers')
    .addItem('🔗 Set Telegram Webhook', 'setTelegramWebhook')
    .addItem('📋 Register Bot Commands (Menu)', 'registerBotCommands')
    .addItem('🔐 Setup Secure Keys', 'setupProperties')
    .addToUi();
}

function checkTelegramWebhook() { diagnoseTelegram(); }
function setupMorningTrigger() { installTriggers(); }

// ─── CHANGED: also delete stale simple onEdit triggers (F10) ──────────────
function installTriggers() {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  ScriptApp.getProjectTriggers().forEach(t => {
    const fn = t.getHandlerFunction();
    if (fn === "runMorningBriefing" || fn === "onSheetEdit" || fn === "onEdit") ScriptApp.deleteTrigger(t);
  });
  ScriptApp.newTrigger("runMorningBriefing").timeBased().atHour(9).nearMinute(0).everyDays(1).create();
  ScriptApp.newTrigger("onSheetEdit").forSpreadsheet(ss).onEdit().create();
  safeAlert("✅ Triggers installed:\n• 9:00 AM daily SLA briefing\n• On-Edit automation (Qualify / Pipeline / Reject dropdowns, with GitHub enrichment)");
}
// ───────────────────────────────────────────────────────────────────────────

// ─── NEW: register bot commands in Telegram's blue Menu (F9) ──────────────
function registerBotCommands() {
  const commands = [
    { command: "status",    description: "Bot + webhook + GitHub status" },
    { command: "run",       description: "Harvest + enrich back-to-back" },
    { command: "harvest",   description: "Run web harvester now" },
    { command: "enrich",    description: "Enrich pending leads now" },
    { command: "health",    description: "Run health check" },
    { command: "dashboard", description: "Revenue & pipeline numbers" },
    { command: "top5",      description: "Top 5 to close + deadlines" },
    { command: "briefing",  description: "AI briefing of recent signals" },
    { command: "note",      description: "Log a note: /note Company, message" },
    { command: "dossier",   description: "Full dossier: /dossier Company" },
    { command: "ping",      description: "Check the bot is connected" },
    { command: "help",      description: "Show all commands" }
  ];
  tgApi_("deleteMyCommands", {});
  Utilities.sleep(1000);
  const r = tgApi_("setMyCommands", { commands: commands });
  safeAlert(r.ok
    ? "✅ " + commands.length + " bot commands registered. Open Telegram and long-press the Menu button to see them."
    : "❌ setMyCommands failed: " + (r.description || JSON.stringify(r)));
}
// ───────────────────────────────────────────────────────────────────────────

function diagnoseTelegram() {
  const out = [];
  const get = (method) => JSON.parse(UrlFetchApp.fetch(TELEGRAM_API_URL + "/" + method, { muteHttpExceptions: true }).getContentText());

  if (!TELEGRAM_BOT_TOKEN) out.push("❌ TELEGRAM_BOT_TOKEN is not set.");
  else {
    try {
      const me = get("getMe");
      out.push(me.ok ? "✅ Bot token OK: @" + me.result.username : "❌ Bot token rejected: " + me.description);
    } catch (e) { out.push("❌ getMe failed: " + e); }

    try {
      const wi = get("getWebhookInfo");
      const r = wi.result || {};
      if (!r.url) out.push("❌ NO WEBHOOK SET -> buttons cannot work. Run 'Set Telegram Webhook'.");
      else {
        out.push("🔗 Webhook: " + r.url.replace(/tg_secret=[^&]+/, "tg_secret=***"));
        if (/\/dev(\?|$)/.test(r.url)) out.push("❌ Webhook points to /dev (test URL). It must be the /exec URL.");
        if (WEBHOOK_SECRET && !/tg_secret=/.test(r.url)) out.push("❌ Webhook has no tg_secret but WEBHOOK_SECRET is set -> clicks are rejected. Re-run 'Set Telegram Webhook'.");
      }
      out.push("⏳ Pending updates: " + (r.pending_update_count || 0));
      out.push(r.last_error_message
        ? "⚠️ Last Telegram error: " + r.last_error_message + " (" + new Date((r.last_error_date || 0) * 1000) + ")"
        : "✅ No webhook delivery errors reported");
    } catch (e) { out.push("❌ getWebhookInfo failed: " + e); }

    [["TELEGRAM_CHAT_ID", TELEGRAM_CHAT_ID], ["..._GOV", TELEGRAM_CHAT_ID_GOV],
     ["..._REVIEW", TELEGRAM_CHAT_ID_REVIEW], ["..._WATCHLIST", TELEGRAM_CHAT_ID_WATCHLIST]].forEach(pair => {
      if (!pair[1]) { out.push("❌ " + pair[0] + " is empty"); return; }
      try {
        const c = get("getChat?chat_id=" + encodeURIComponent(pair[1]));
        out.push((c.ok ? "✅ " : "❌ ") + pair[0] + " = " + pair[1] + (c.ok ? " (" + (c.result.title || c.result.username || c.result.first_name) + ")" : " -> " + c.description));
      } catch (e) { out.push("❌ " + pair[0] + ": " + e); }
    });
  }

  let base = SCRIPT_URL;
  if (!base) { try { base = ScriptApp.getService().getUrl() || ""; } catch (e) {} }
  if (!base) out.push("❌ SCRIPT_URL not set.");
  else {
    try {
      const r = UrlFetchApp.fetch(base, { muteHttpExceptions: true, followRedirects: true });
      const body = r.getContentText();
      if (r.getResponseCode() === 200 && /Unauthorized|Webhook Active|Online|"status"/.test(body)) {
        out.push("✅ Web app is publicly reachable (deployment access = Anyone).");
      } else {
        out.push("❌ Web app is NOT publicly reachable (HTTP " + r.getResponseCode() + "). Deploy as: Execute as ME, Who has access: ANYONE, then use the /exec URL.");
      }
    } catch (e) { out.push("❌ Could not reach SCRIPT_URL: " + e); }
  }

  const text = out.join("\n");
  Logger.log(text);
  safeAlert(text);
}

function testTelegramNotification() {
  Logger.log("Testing Telegram dispatch...");
  const res = sendTelegramLeadCard(
    "Test Infrastructure Ltd",
    "Testing Telegram & WhatsApp integration.",
    ["2026-10-10", "2026-11-01", "BUYER", "Infrastructure", "Maharashtra", "Mumbai", "Test Infrastructure Ltd", "Target Solution", "Testing", "https://eprocure.gov.in", "Prospective Buyer", "", "TEST::001"],
    TABS.INBOX,
    "9876543210",
    "Rajesh Kumar"
  );
  const ping = tgSend_(TELEGRAM_CHAT_ID,
    "🔘 <b>Button self-test</b>\nTap the button. If you see \"Buttons work\", the webhook is connected.\n<i>(The test lead card above is not in your sheet, so ITS buttons will say \"not found\" - that is expected.)</i>",
    [[{ text: "✅ Tap to test webhook", callback_data: "ping::test" }]]);
  if (res && res.ok && ping && ping.ok) safeAlert("✅ Telegram messages delivered.\nNow tap the 'Tap to test webhook' button in Telegram.");
  else safeAlert("❌ FAILED: " + JSON.stringify(res && res.ok ? ping : res));
}

function setTelegramWebhook() {
  try {
    let baseUrl = SCRIPT_URL;
    if (!baseUrl) {
      try { baseUrl = ScriptApp.getService().getUrl() || ""; } catch (e) {}
    }
    baseUrl = String(baseUrl || "").trim().replace(/['"]/g, "");

    if (!baseUrl.startsWith("https://script.google.com/")) {
      safeAlert("❌ Missing or invalid SCRIPT_URL!\n\nGo to Project Settings -> Script Properties and set:\nProperty: SCRIPT_URL\nValue: your https://script.google.com/macros/s/.../exec link");
      return;
    }
    if (/\/dev(\?|$)/.test(baseUrl) || !/\/exec(\?|$)/.test(baseUrl)) {
      safeAlert("❌ SCRIPT_URL must be the deployed /exec URL (not /dev).\n\nGot: " + baseUrl);
      return;
    }

    const signedUrl = WEBHOOK_SECRET
      ? baseUrl + (baseUrl.includes("?") ? "&" : "?") + "tg_secret=" + encodeURIComponent(WEBHOOK_SECRET)
      : baseUrl;

    const res = tgApi_("setWebhook", {
      url: signedUrl,
      allowed_updates: ["message", "callback_query"],
      drop_pending_updates: true
    });
    safeAlert((res.ok ? "✅ Webhook set.\n\n" : "❌ Webhook failed: " + res.description + "\n\n") + "URL: " + baseUrl + "\n\nNow run 'Check Webhook Status' to verify.");
  } catch (err) {
    safeAlert("Webhook Set Failed: " + err.toString());
  }
}

// =====================================================================
// --- HOUSEKEEPING ---
// =====================================================================
function manualRunSelfCleaning() { runSelfCleaning(true); }

function runSelfCleaning(isManual) {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  let totalRemoved = 0;
  [TABS.INBOX, TABS.REVIEW, TABS.PARTNERS, TABS.TRASH].forEach(name => {
    const sheet = getSheetFlexible(ss, name);
    if (!sheet || sheet.getLastRow() <= 1) return;
    const data = sheet.getDataRange().getValues();
    const linkIdx = data[0].findIndex(h => {
      const s = String(h);
      return s.includes("Source") || s.includes("Link") || s.includes("URL");
    });
    if (linkIdx === -1) return;

    const seen = new Set();
    const toDelete = [];
    for (let i = 1; i < data.length; i++) {
      const link = String(data[i][linkIdx] || "").trim().toLowerCase();
      if (!link) continue;
      if (seen.has(link)) toDelete.push(i + 1); else seen.add(link);
    }
    for (let k = toDelete.length - 1; k >= 0; k--) deleteRowSafe_(sheet, toDelete[k]);
    totalRemoved += toDelete.length;
  });
  if (isManual) safeAlert(`✅ Cleanup Complete!\n\nRemoved ${totalRemoved} duplicate rows.`);
}

function archiveExpiredTenders() {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  const today = new Date();
  let totalArchived = 0;
  [TABS.INBOX, TABS.REVIEW].forEach(sheetName => {
    const sheet = getSheetFlexible(ss, sheetName);
    if (!sheet) return;
    const data = sheet.getDataRange().getValues();
    if (data.length <= 1) return;
    const headers = data[0].map(h => String(h));
    let timestampIdx = headers.findIndex(h => h.includes("Capture"));
    if (timestampIdx === -1) timestampIdx = headers.findIndex(h => h.includes("Date"));
    const linkIdx = headers.findIndex(h => h.includes("Source") || h.includes("Link"));
    if (timestampIdx === -1) return;
    for (let i = data.length - 1; i > 0; i--) {
      const row = data[i], timestamp = new Date(row[timestampIdx]);
      const link = linkIdx > -1 ? String(row[linkIdx]).toLowerCase() : "";
      if ((link.includes("gov") || link.includes("tender")) && !isNaN(timestamp.getTime())) {
        if (Math.ceil(Math.abs(today - timestamp) / DAY_MS) > 30) {
          moveRow(sheet, i + 1, TABS.ARCHIVE, "Expired Tender (>30d)");
          totalArchived++;
        }
      }
    }
  });
  safeAlert(`🧹 Cleanup Complete\n\nArchived ${totalArchived} expired tenders.`);
}

function getSheetFlexible(ss, targetName) {
  if (!ss) ss = SpreadsheetApp.getActiveSpreadsheet();
  let sheet = ss.getSheetByName(targetName);
  if (sheet) return sheet;
  const cleanTarget = String(targetName || "").replace(/[^a-zA-Z0-9]/g, '').toLowerCase();
  if (!cleanTarget) return null;
  for (let s of ss.getSheets()) {
    if (s.getName().replace(/[^a-zA-Z0-9]/g, '').toLowerCase() === cleanTarget) return s;
  }
  return null;
}

function deleteRowSafe_(sheet, rowNumber) {
  try { sheet.deleteRow(rowNumber); }
  catch (e) { sheet.getRange(rowNumber, 1, 1, Math.max(1, sheet.getLastColumn())).clearContent(); }
}

function createJsonResponse(data, code) {
  return ContentService.createTextOutput(JSON.stringify(data)).setMimeType(ContentService.MimeType.JSON);
}

// =====================================================================
// --- WEB APP ENTRY POINTS ---
// =====================================================================
function doPost(e) {
  const lock = LockService.getScriptLock();
  try { lock.waitLock(20000); } catch (err) { return createJsonResponse({ status: "error", message: "Server too busy" }, 429); }

  try {
    const payload = JSON.parse(e.postData.contents);
    if (payload.update_id !== undefined || payload.callback_query || payload.message) {
      return handleTelegramUpdate_(e, payload);
    }
    return handleHarvesterPost_(payload);
  } catch (error) {
    Logger.log("doPost error: " + error + "\n" + (error && error.stack ? error.stack : ""));
    return createJsonResponse({ status: "error", message: error.toString() }, 500);
  } finally {
    try { lock.releaseLock(); } catch (err) {}
  }
}

function handleTelegramUpdate_(e, payload) {
  if (WEBHOOK_SECRET) {
    const given = e && e.parameter && e.parameter.tg_secret ? String(e.parameter.tg_secret).trim() : "";
    if (given !== WEBHOOK_SECRET) {
      return createJsonResponse({ status: "error", message: "Invalid webhook token" }, 401);
    }
  }

  const cache = CacheService.getScriptCache();
  const dedupeKey = payload.update_id !== undefined ? "upd_" + payload.update_id : "";
  if (dedupeKey && cache.get(dedupeKey)) {
    return createJsonResponse({ status: "duplicate" });
  }

  const allowedChats = [TELEGRAM_CHAT_ID, TELEGRAM_CHAT_ID_GOV, TELEGRAM_CHAT_ID_REVIEW, TELEGRAM_CHAT_ID_WATCHLIST]
    .map(x => String(x || "").trim()).filter(Boolean);

  let response;

  if (payload.callback_query) {
    const cb = payload.callback_query;
    const cbChat = cb.message && cb.message.chat ? String(cb.message.chat.id).trim() : "";
    if (!allowedChats.includes(cbChat)) {
      answerCallback(cb.id, "⚠️ Unauthorized Chat ID: " + cbChat);
      response = createJsonResponse({ status: "ignored", message: "chat not authorised" });
    } else {
      try {
        response = handleTelegramCallback(cb);
      } catch (err) {
        Logger.log("handleTelegramCallback failed: " + err + "\n" + (err && err.stack ? err.stack : ""));
        tgSend_(cbChat, "❌ <b>Button error:</b> " + escapeHtml(String(err).slice(0, 300)));
        response = createJsonResponse({ status: "error", message: String(err) });
      }
    }
  }
  else if (payload.message) {
    const msg = payload.message;
    const msgChat = msg.chat ? String(msg.chat.id).trim() : "";
    const rawMsg = String(msg.text || "").trim();

    if (!allowedChats.includes(msgChat)) {
      if (rawMsg.startsWith("/")) {
        tgSend_(msgChat, `⚠️ <b>Unauthorized Chat ID:</b> <code>${escapeHtml(msgChat)}</code>\nAdd this Chat ID to your Script Properties to use bot commands here.`);
      }
      response = createJsonResponse({ status: "ignored", message: "chat not authorised" });
    }
    else if (rawMsg && msg.reply_to_message && msg.reply_to_message.text && msg.reply_to_message.text.includes("Deal Setup:")) {
      try { processDealReply(msg); }
      catch (err) { tgSend_(msgChat, "❌ <b>Deal error:</b> " + escapeHtml(String(err).slice(0, 300))); }
      response = createJsonResponse({ status: "success", message: "deal processed" });
    }
    else {
      const cmdMatch = rawMsg.match(/^(\/[a-zA-Z0-9_]+)(?:@\S+)?\s*([\s\S]*)$/);
      const cmd = cmdMatch ? cmdMatch[1].toLowerCase() : "";
      const cmdArgs = cmdMatch ? cmdMatch[2].trim() : "";

      if (!cmd) {
        response = createJsonResponse({ status: "ignored" });
      } else {
        try {
          runTelegramCommand_(cmd, cmdArgs, msgChat);
        } catch (err) {
          Logger.log("Command " + cmd + " failed: " + err + "\n" + (err && err.stack ? err.stack : ""));
          tgSend_(msgChat, "❌ <b>Error running " + escapeHtml(cmd) + ":</b> " + escapeHtml(String(err).slice(0, 300)));
        }
        response = createJsonResponse({ status: "success", message: "command processed" });
      }
    }
  }
  else {
    response = createJsonResponse({ status: "ignored" });
  }

  if (dedupeKey) cache.put(dedupeKey, "1", 3600);
  return response;
}

// ─── CHANGED: /harvest /enrich /run /health /status added, /help expanded (F8) ─
function runTelegramCommand_(cmd, args, chat) {
  if (cmd === "/ping") { tgSend_(chat, "🏓 <b>pong</b> - webhook is connected."); return; }

  if (cmd === "/start" || cmd === "/help") {
    tgSend_(chat,
      "🤖 <b>Radar CRM commands</b>\n\n" +
      "<b>Pipeline control</b>\n" +
      "/harvest - run the web harvester now\n" +
      "/enrich - enrich pending leads now\n" +
      "/run - harvest + enrich back-to-back\n" +
      "/health - run the health check\n" +
      "/status - bot + webhook + GitHub status\n\n" +
      "<b>Data</b>\n" +
      "/note Company, your note - log a call/note\n" +
      "/dossier Company - full client dossier\n" +
      "/briefing - AI briefing of recent signals\n" +
      "/top5 - top 5 to close / hunt + deadlines\n" +
      "/dashboard - revenue &amp; pipeline numbers\n\n" +
      "/ping - check the bot is connected");
    return;
  }

  if (cmd === "/note") {
    const splitIdx = args.indexOf(",");
    if (splitIdx < 1) { tgSend_(chat, "⚠️ <b>Invalid Format.</b>\nUse: /note Company Name, Your message here"); return; }
    const company = args.substring(0, splitIdx).trim();
    const note = args.substring(splitIdx + 1).trim();
    if (!note) { tgSend_(chat, "⚠️ <b>Invalid Format.</b>\nUse: /note Company Name, Your message here"); return; }
    const loc = findCompanyInCRM(company, true);
    if (loc) {
      appendNoteToExistingDeal(loc, "📞 [CALL/NOTE]: " + note);
      tgSend_(chat, `✅ <b>Note logged for ${escapeHtml(company)}</b>\nLocated in: <i>${escapeHtml(loc.sheetName)}</i>`);
    } else {
      tgSend_(chat, `❌ <b>Not found:</b> ${escapeHtml(company)}\nCheck spelling.`);
    }
    return;
  }

  if (cmd === "/dossier") {
    if (!args) { tgSend_(chat, "⚠️ <b>Invalid Format.</b>\nUse: /dossier Company Name"); return; }
    tgSend_(chat, generateDossier(args));
    return;
  }

  if (cmd === "/briefing") {
    tgSend_(chat, "🧠 <i>AI is analyzing your recent leads across all tabs... give me a few seconds.</i>");
    tgSend_(chat, generateAIBriefing());
    return;
  }

  if (cmd === "/top5") {
    tgSend_(chat, "🎯 <i>Scanning tabs for your TOP 5 highest-priority targets...</i>");
    tgSend_(chat, generateTop5Briefing());
    return;
  }

  if (cmd === "/dashboard") {
    tgSend_(chat, generateDashboardReport());
    return;
  }

  // ─── NEW: pipeline control commands (F8) ─────────────────────────────
  if (cmd === "/harvest") {
    tgSend_(chat, "🌾 <i>Launching harvester on GitHub Actions…</i>");
    const r = triggerGitHubJob_("harvest");
    tgSend_(chat, r.ok
      ? `✅ <b>Harvester dispatched</b>\nRepo: <code>${escapeHtml(cleanProp_("GITHUB_REPO") || "?")}</code>\nJob: <code>harvest</code>\nRef: <code>${escapeHtml(r.ref || "?")}</code>`
      : `❌ <b>Harvest failed to start</b>\n<code>${escapeHtml(r.error || "unknown")}</code>`);
    return;
  }

  if (cmd === "/enrich") {
    tgSend_(chat, "🔬 <i>Launching enrichment worker…</i>");
    const r = triggerGitHubJob_("enrich");
    tgSend_(chat, r.ok
      ? `✅ <b>Enrichment dispatched</b>\nJob: <code>enrich</code>`
      : `❌ <b>Enrichment failed to start</b>\n<code>${escapeHtml(r.error || "unknown")}</code>`);
    return;
  }

  if (cmd === "/run") {
    tgSend_(chat, "🚀 <i>Running harvest + enrich…</i>");
    const r = triggerGitHubJob_("both");
    tgSend_(chat, r.ok
      ? `✅ <b>Full pipeline dispatched</b>\nJob: <code>both</code>`
      : `❌ <b>Pipeline failed to start</b>\n<code>${escapeHtml(r.error || "unknown")}</code>`);
    return;
  }

  if (cmd === "/health") {
    tgSend_(chat, "🩺 <i>Launching health check…</i>");
    const r = triggerGitHubJob_("health");
    tgSend_(chat, r.ok
      ? `✅ <b>Health check dispatched</b>`
      : `❌ <b>Health check failed</b>\n<code>${escapeHtml(r.error || "unknown")}</code>`);
    return;
  }

  if (cmd === "/status") {
    try {
      const wi = tgApi_("getWebhookInfo", {});
      const me = tgApi_("getMe", {});
      const webhook = (wi.result && wi.result.url) ? "✅ set" : "❌ missing";
      const pending = (wi.result && wi.result.pending_update_count) || 0;
      const lastErr = (wi.result && wi.result.last_error_message) || "none";
      tgSend_(chat,
        `🤖 <b>Radar status</b>\n\n` +
        `Bot: @${escapeHtml((me.result && me.result.username) || "?")}\n` +
        `Webhook: ${webhook}\n` +
        `Pending updates: ${pending}\n` +
        `Last error: ${escapeHtml(lastErr)}\n` +
        `GitHub repo: <code>${escapeHtml(cleanProp_("GITHUB_REPO") || "(not set)")}</code>`);
    } catch (e) {
      tgSend_(chat, "❌ /status failed: " + escapeHtml(String(e)));
    }
    return;
  }
  // ──────────────────────────────────────────────────────────────────────
}
// ───────────────────────────────────────────────────────────────────────────

// ---------------------------------------------------------------------
// Python harvester / GitHub Actions calls
// ---------------------------------------------------------------------
function handleHarvesterPost_(payload) {
  if (!WEBHOOK_SECRET || payload.secret !== WEBHOOK_SECRET) {
    return createJsonResponse({ status: "error", message: "Unauthorized" }, 401);
  }

  if (payload.action === "pre_flight_check") {
    const existingLoc = findCompanyInCRM(payload.company_name);
    return createJsonResponse({ status: "success", exists: existingLoc !== null, location: existingLoc ? existingLoc.sheetName : null });
  }

   if (payload.action === "insert_lead") {
    const ss = SpreadsheetApp.getActiveSpreadsheet();
    const sheet = getSheetFlexible(ss, payload.target_sheet);
    const sig = String(payload.signal_brief || "");
    const isAiRejected = /^\s*\[AI REJECTED/i.test(sig);
    const isManual = /manual/i.test(String(payload.target_sheet || ""));

    // Manual Extraction tab bypasses dedupe — we want to capture every
    // unprocessable lead even if the company is already in CRM, so the
    // human can decide what to do.
    if (!isManual) {
      const isSeller = /partner|supplier/i.test(String(payload.target_sheet || ""));
      const existingLoc = findCompanyInCRM(payload.company_name, false,
        isSeller ? SELLER_DEDUPE_TABS : BUYER_DEDUPE_TABS);
      if (existingLoc) {
        if (isAiRejected) {
          return createJsonResponse({ status: "duplicate", message: "AI-rejected signal not merged with existing company." });
        }
        const newSignal = sig + (payload.source_url ? " | " + payload.source_url : "");
        const noteResult = appendNoteToExistingDeal(existingLoc, newSignal);
        if (noteResult === "written") {
          return createJsonResponse({ status: "signal_updated", location: existingLoc.sheetName,
            message: "New opportunity signal saved on existing CRM company." });
        }
        if (noteResult === "duplicate") {
          return createJsonResponse({ status: "duplicate", message: "Identical signal already on company record." });
        }
        return createJsonResponse({ status: "error", message: "Existing company found but no signal field could be updated." }, 500);
      }
    }

    if (!sheet) return createJsonResponse({ status: "error", message: "Target sheet not found: " + payload.target_sheet }, 404);
    if (!Array.isArray(payload.row_data)) return createJsonResponse({ status: "error", message: "row_data must be an array" }, 400);

    sheet.appendRow(payload.row_data);
    const cleanTarget = String(payload.target_sheet || "").replace(/[^a-zA-Z0-9]/g, '').toLowerCase();
    if ((cleanTarget.includes("inbox") || cleanTarget.includes("review")) && !isAiRejected && !isManual) {
      try {
        sendTelegramLeadCard(payload.company_name, payload.signal_brief, payload.row_data, payload.target_sheet,
          payload.contact_phone || "", payload.dm_name || "");
      } catch (err) { Logger.log("Lead card failed: " + err); }
    }
    return createJsonResponse({ status: "success", message: "Lead ingested." });
  }

  if (payload.action === "insert_competitor_intel") {
    const intelSheet = getSheetFlexible(SpreadsheetApp.getActiveSpreadsheet(), COMPETITOR_TAB);
    if (!intelSheet) return createJsonResponse({ status: "error", message: "Competitor Intel tab not found." }, 404);
    const today = Utilities.formatDate(new Date(), Session.getScriptTimeZone(), "dd-MMM-yyyy");
    intelSheet.appendRow([
      today,
      payload.project_name || "N/A",
      payload.client_name || "N/A",
      payload.competitor_name || "N/A",
      payload.winning_value || 0,
      payload.our_price || "N/A",
      payload.outcome || "Lost",
      payload.remarks || "Scraped via Python Harvester"
    ]);
    return createJsonResponse({ status: "success", message: "Competitor intel logged." });
  }

  if (payload.action === "upsert_contact") {
    const result = upsertContactV2_(payload.contact_data);
    return createJsonResponse({ status: result.status === "error" ? "error" : "success", message: result.message });
  }

  if (payload.action === "update_lead_dossier") {
    const sheet = getSheetFlexible(SpreadsheetApp.getActiveSpreadsheet(), TABS.LEADS);
    if (!sheet) return createJsonResponse({ status: "error", message: "Leads sheet not found" }, 404);
    const data = sheet.getDataRange().getValues();
    const headers = data[0].map(h => String(h));

    const idCol = headers.findIndex(h => h.includes("Lead ID"));
    const orgCol = headers.findIndex(h => h.includes("Organization") || h.includes("Company")) + 1;
    const dossierCol = headers.findIndex(h => h.includes("Deep Intel") || h.includes("Dossier") || h.includes("Strategy")) + 1;
    const nameCol = headers.findIndex(h => h.includes("DM Name")) + 1;
    const emailCol = headers.findIndex(h => h.includes("DM Email") || h.includes("Email")) + 1;
    const phoneCol = headers.findIndex(h => h.includes("DM Phone") || h.includes("Phone")) + 1;
    const webCol = headers.findIndex(h => h.includes("Website")) + 1;
    const liCol = headers.findIndex(h => h.includes("LinkedIn")) + 1;

    let r = -1;
    const targetId = String(payload.lead_id || "").trim();
    for (let i = 1; i < data.length; i++) {
      if (idCol > -1 && String(data[i][idCol] || "").trim() === targetId) { r = i + 1; break; }
    }

    if (r > 0) {
      if (orgCol > 0 && payload.organization) {
        const curOrg = String(sheet.getRange(r, orgCol).getValue() || "").trim();
        if (/^unidentified\b/i.test(curOrg)) sheet.getRange(r, orgCol).setValue(payload.organization);
      }
      if (dossierCol > 0 && payload.dossier) {
        const oldVal = String(sheet.getRange(r, dossierCol).getValue() || "");
        const keepNotes = /^\[[^\]]*NEW SIGNAL\]/.test(oldVal.trim()) ? "\n\n" + oldVal : "";
        sheet.getRange(r, dossierCol).setValue(payload.dossier + keepNotes);
      }
      if (payload.contacts) {
        if (nameCol > 0 && payload.contacts.dm_name) sheet.getRange(r, nameCol).setValue(payload.contacts.dm_name);
        if (emailCol > 0 && payload.contacts.email) sheet.getRange(r, emailCol).setValue(payload.contacts.email);
        if (phoneCol > 0 && payload.contacts.phone) sheet.getRange(r, phoneCol).setValue(payload.contacts.phone);
        if (webCol > 0 && payload.contacts.website) sheet.getRange(r, webCol).setValue(payload.contacts.website);
        if (liCol > 0 && payload.contacts.linkedin_url) sheet.getRange(r, liCol).setValue(payload.contacts.linkedin_url);
      }
    }
    return createJsonResponse({ status: "success", message: r > 0 ? "Dossier updated." : "Lead ID not found." });
  }

  if (payload.action === "get_all_urls") {
    const allUrls = [];
    const ss = SpreadsheetApp.getActiveSpreadsheet();
    [TABS.INBOX, TABS.REVIEW, TABS.LEADS, TABS.PIPELINE, TABS.ARCHIVE, TABS.TRASH, TABS.PARTNERS].forEach(tabName => {
      const sheet = getSheetFlexible(ss, tabName);
      if (!sheet) return;
      const data = sheet.getDataRange().getValues();
      if (data.length <= 1) return;
      const linkCol = data[0].findIndex(h => { const s = String(h); return s.includes("Source") || s.includes("Link") || s.includes("URL"); });
      if (linkCol > -1) for (let i = 1; i < data.length; i++) if (data[i][linkCol]) allUrls.push(data[i][linkCol]);
    });
    return createJsonResponse({ status: "success", urls: allUrls });
  }

  return createJsonResponse({ status: "error", message: "Unknown action" }, 400);
}

function doGet(e) {
  if (!e || !e.parameter || !e.parameter.secret) return createJsonResponse({ status: "error", message: "Unauthorized" }, 401);
  if (!WEBHOOK_SECRET || e.parameter.secret !== WEBHOOK_SECRET) return createJsonResponse({ status: "error", message: "Unauthorized" }, 401);

  if (e.parameter.action === "get_settings") {
    const sheet = getSheetFlexible(SpreadsheetApp.getActiveSpreadsheet(), TABS.SETTINGS);
    if (!sheet) return createJsonResponse({ status: "error", message: "Settings tab missing." });

    const data = sheet.getDataRange().getValues();

    // ─── Expose Python API keys from Script Properties ────────────────
    // Built unconditionally, BEFORE the header-row check, so a Settings
    // tab with only a header row still delivers keys to Python.
    const _scriptProps = PropertiesService.getScriptProperties();
    const _splitKeys = (v) => (v || "").split(",").map(s => s.trim()).filter(Boolean);
    const apiKeysBlock = {
      gemini_api_keys:     _splitKeys(_scriptProps.getProperty("GEMINI_API_KEYS")),
      groq_api_keys:       _splitKeys(_scriptProps.getProperty("GROQ_API_KEYS")),
      openrouter_api_keys: _splitKeys(_scriptProps.getProperty("OPENROUTER_API_KEYS")),
      serper_api_keys:     _splitKeys(_scriptProps.getProperty("SERPER_API_KEYS")),
      brave_api_keys:      _splitKeys(_scriptProps.getProperty("BRAVE_API_KEYS")),
      scraperapi_api_keys: _splitKeys(_scriptProps.getProperty("SCRAPERAPI_API_KEYS")),
      jina_api_key:        _splitKeys(_scriptProps.getProperty("JINA_API_KEY")),
      openai_api_key:      _splitKeys(_scriptProps.getProperty("OPENAI_API_KEY"))
    };
    // ─────────────────────────────────────────────────────────────────

    if (data.length > 1) {
      const result = {
        special_prompt: "",
        session_cookies: "",
        target_products: [], directives: [], industry_keywords: [], target_countries: [],
        target_states: [], target_cities: [], banned_keywords: [], banned_websites: [],
        protected_domains: [], job_portals: [], buyer_personas: [], company_watchlist: []
      };

      result.api_keys = apiKeysBlock;

      const headers = data[0].map(h => String(h).toLowerCase());

      const specialCol = headers.findIndex(h => h.includes("special") && h.includes("prompt"));
      if (specialCol > -1 && data[1][specialCol]) result.special_prompt = String(data[1][specialCol]).trim();

      const cookieCol = headers.findIndex(h => h.includes("cookie"));
      if (cookieCol > -1 && data[1][cookieCol]) result.session_cookies = String(data[1][cookieCol]).trim();

      const colMap = {
        target_products: headers.findIndex(h => h.includes("product") || h.includes("solution")),
        directives: headers.findIndex(h => h.includes("directive") || /\bloa\b/.test(h) || h.includes("award")),
        industry_keywords: headers.findIndex(h => h.includes("industry")),
        target_countries: headers.findIndex(h => h.includes("countr")),
        target_states: headers.findIndex(h => h.includes("state") || h.includes("region")),
        target_cities: headers.findIndex(h => /\bcit(y|ies)\b/.test(h)),
        banned_keywords: headers.findIndex(h => h.includes("banned") && h.includes("keyword")),
        banned_websites: headers.findIndex(h => h.includes("banned") && (h.includes("site") || h.includes("web"))),
        protected_domains: headers.findIndex(h => h.includes("protected")),
        job_portals: headers.findIndex(h => (h.includes("job") && h.includes("portal")) || h.includes("review domain")),
        buyer_personas: headers.findIndex(h => h.includes("buyer") && h.includes("persona")),
        company_watchlist: headers.findIndex(h => h.includes("company") && h.includes("watchlist"))
      };

      for (let i = 1; i < data.length; i++) {
        for (let key in colMap) {
          if (colMap[key] > -1 && data[i][colMap[key]]) result[key].push(String(data[i][colMap[key]]).trim());
        }
      }
      result.status = "success";
      return createJsonResponse(result);
    }
    return createJsonResponse({ status: "success", api_keys: apiKeysBlock,
      target_products: [], directives: [], industry_keywords: [],
      target_countries: [], target_states: [], target_cities: [] });
  }
  if (e.parameter.action === "get_vendor_domains") {
    // Returns all unique domains from the Partners & Suppliers tab.
    // Used by Python's vendor domain cache to skip vendor URLs before AI.
    const sheet = getSheetFlexible(SpreadsheetApp.getActiveSpreadsheet(), TABS.PARTNERS);
    if (!sheet || sheet.getLastRow() <= 1) {
      return createJsonResponse({ status: "success", domains: [] });
    }
    const data = sheet.getDataRange().getValues();
    const headers = data[0].map(h => String(h).toLowerCase());
    const linkCol = headers.findIndex(h =>
      h.includes("source") || h.includes("link") || h.includes("url")
    );
    if (linkCol === -1) {
      return createJsonResponse({ status: "success", domains: [] });
    }
    const domains = {};
    for (let i = 1; i < data.length; i++) {
      const raw = String(data[i][linkCol] || "").trim();
      if (!raw) continue;
      let host = "";
      const m = raw.toLowerCase().match(/^(?:https?:\/\/)?(?:www\.)?([^\/\?#\s:]+)/);
      if (m && m[1]) host = m[1];
      if (host && host.includes(".")) domains[host] = true;
    }
    return createJsonResponse({ status: "success", domains: Object.keys(domains) });
  }
  if (e.parameter.action === "get_all_companies") {
    // Returns every distinct company name across all CRM tabs.
    // Used by Python's CompanyDedup to skip already-known leads
    // before fetching or calling the AI — saves ~80% of quota spent
    // on duplicates that would be rejected by findCompanyInCRM anyway.
    const companies = [];
    const seen = new Set();
    const ss = SpreadsheetApp.getActiveSpreadsheet();

    for (const tabName of DEDUPE_HIERARCHY) {
      const sheet = getSheetFlexible(ss, tabName);
      if (!sheet || sheet.getLastRow() <= 1) continue;
      const data = sheet.getDataRange().getValues();
      if (data.length <= 1) continue;
      const orgCol = orgColIndex_(data[0]);
      if (orgCol === -1) continue;
      for (let i = 1; i < data.length; i++) {
        const org = String(data[i][orgCol] || "").trim();
        if (org && !seen.has(org)) {
          seen.add(org);
          companies.push(org);
        }
      }
    }

    return createJsonResponse({
      status: "success",
      companies: companies,
      count: companies.length
    });
  }

  if (e.parameter.action === "get_contact_index") {
    return createJsonResponse(getContactIndex_());
  }

  if (e.parameter.action === "get_pending_leads") {
    const sheet = getSheetFlexible(SpreadsheetApp.getActiveSpreadsheet(), TABS.LEADS);
    if (!sheet) return createJsonResponse({ status: "success", leads: [] });

    const data = sheet.getDataRange().getValues();
    const headers = data[0].map(h => String(h));
    const pendingLeads = [];

    const idCol = headers.findIndex(h => h.includes("Lead ID"));
    const orgCol = headers.findIndex(h => h.includes("Organization") || h.includes("Company"));
    const urlCol = headers.findIndex(h => h.includes("Source") || h.includes("Link"));
    const dossierCol = headers.findIndex(h => h.includes("Deep Intel") || h.includes("Dossier") || h.includes("Strategy"));
    if (orgCol === -1) return createJsonResponse({ status: "success", leads: [] });

    for (let i = 1; i < data.length; i++) {
      const dossierVal = dossierCol > -1 ? String(data[i][dossierCol] || "").trim() : "";
      if (data[i][orgCol] && (dossierVal === "" || /^\[[^\]]*NEW SIGNAL\]/.test(dossierVal))) {
        pendingLeads.push({
          lead_id: idCol > -1 ? data[i][idCol] : "",
          organization: data[i][orgCol],
          url: urlCol > -1 ? data[i][urlCol] : "",
          // ── Deploy 2: existing phone (if DM Phone column populated) ──
          phone: headers.findIndex(h => h.includes("DM Phone") || h === "Phone") > -1
                 ? String(data[i][headers.findIndex(h => h.includes("DM Phone") || h === "Phone")] || "")
                 : ""
        });
      }
    }
    return createJsonResponse({ status: "success", leads: pendingLeads });
  }
  return createJsonResponse({ status: "error", message: "Unknown action" });
}

// =====================================================================
// --- TELEGRAM LEAD CARD ---
// =====================================================================
function waNumber_(phone) {
  let digits = String(phone || "").replace(/\D/g, "");
  if (digits.length === 10) digits = "91" + digits;
  else if (digits.length === 11 && digits.charAt(0) === "0") digits = "91" + digits.slice(1);
  return digits.length >= 11 && digits.length <= 15 ? digits : "";
}

function sendTelegramLeadCard(companyName, signalBrief, rowData, sourceTab, directPhone = "", directDmName = "") {
  if (!TELEGRAM_BOT_TOKEN || !TELEGRAM_CHAT_ID) return { ok: false, description: "Missing Token/ChatID" };
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  const sheet = getSheetFlexible(ss, sourceTab);
  let headers = sheet ? sheet.getRange(1, 1, 1, sheet.getLastColumn()).getValues()[0] : [];

  const getColVal = (cands) => {
    for (let c of cands) {
      let idx = headers.findIndex(h => h.toLowerCase().includes(c.toLowerCase()));
      if (idx > -1 && rowData[idx] !== undefined && rowData[idx] !== "") return rowData[idx];
    }
    return "N/A";
  };

  const product = getColVal(["Target Product", "Product Scope", "Target Solution", "Industry"]);
  const location = (getColVal(["City"]) + ", " + getColVal(["State"])).replace("N/A, N/A", "Pan-India").replace("N/A, ", "").replace(", N/A", "");
  const link = getColVal(["Source Link", "URL", "Registry Link"]);
  const usageStatus = getColVal(["Product Usage", "Usage", "Status"]);

  let phone = directPhone || getColVal(["Phone", "Contact No", "DM Phone"]);
  let dmName = directDmName || getColVal(["DM Name", "Primary Contact Name", "Director Name"]);

  if (phone === "N/A" || !phone) {
    let phoneMatch = String(signalBrief).match(/(?:\+?\d{1,3}[\s\-\.]?)?\(?\d{2,4}\)?[\s\-\.]?\d{3,4}[\s\-\.]?\d{4,6}/);
    if (phoneMatch) phone = phoneMatch[0];
  }

  let isReview = String(sourceTab).toLowerCase().includes("review");
  let msg = `${isReview ? "⚠️" : "📥"} <b>RADAR SCOUT SIGNAL: ${isReview ? "NEEDS REVIEW" : "INBOX"}</b>\n\n`;
  msg += `🏢 <b>Company:</b> ${escapeHtml(companyName)}\n`;
  msg += `📍 <b>Location:</b> ${escapeHtml(location)}\n`;
  msg += `📦 <b>Product Scope:</b> ${escapeHtml(product)}\n`;
  if (usageStatus !== "N/A") msg += `⚙️ <b>Usage/Status:</b> ${escapeHtml(usageStatus)}\n`;
  msg += `📝 <b>Signal:</b> ${escapeHtml(signalBrief)}\n`;
  if (dmName !== "N/A" && dmName) msg += `👤 <b>Contact:</b> ${escapeHtml(dmName)}\n`;
  if (link !== "N/A" && link) {
    let safeUrl = String(link).replace(/&/g, "&amp;");
    msg += `🔗 <a href="${safeUrl}">View Source Document</a>\n`;
  }

  const safeToken = cbToken_(companyName);

  let keyboard = [];

  // ─── CHANGED: use waNumber_ so 10-digit Indian numbers get +91 prefix (Bug fix) ─
  const wa = waNumber_(phone);
  if (wa) {
    const waText = encodeURIComponent(`Hi ${dmName !== 'N/A' && dmName ? dmName : 'Team'}, I saw your update regarding requirements for ${product}. Would love to share details on how we can support your project.`);
    keyboard.push([{ text: "💬 WhatsApp DM", url: `https://wa.me/${wa}?text=${waText}` }]);
  }
  // ─────────────────────────────────────────────────────────────────────────────

  keyboard.push(isReview ? [
    { text: "✅ Approve to Inbox", callback_data: `approve::${safeToken}` },
    { text: "👁️ Monitor", callback_data: `monitor::${safeToken}` },
    { text: "🗑️ Reject to Trash", callback_data: `reject::${safeToken}` }
  ] : [
    { text: "⚡ Qualify (Enrich)", callback_data: `promote::${safeToken}` },
    { text: "📝 Generate Deal", callback_data: `deal::${safeToken}` },
    { text: "👁️ Monitor", callback_data: `monitor::${safeToken}` },
    { text: "🗑 Move to Trash", callback_data: `reject::${safeToken}` }
  ]);

  let isGov = false;
  let urlLower = String(link).toLowerCase();
  let compLower = String(companyName).toLowerCase();
  if (urlLower.includes(".gov") || urlLower.includes("nic.in") || urlLower.includes("eprocure") || urlLower.includes("gem.gov") || 
      compLower.includes("government") || compLower.includes("authority") || compLower.includes("department") || compLower.includes("ministry") || compLower.includes("municipal")) {
    isGov = true;
  }

  let targetChatId = TELEGRAM_CHAT_ID; 
  if (isReview) {
    targetChatId = TELEGRAM_CHAT_ID_REVIEW;
  } else if (isGov) {
    targetChatId = TELEGRAM_CHAT_ID_GOV;
  }

  try {
    let response = UrlFetchApp.fetch(TELEGRAM_API_URL + "/sendMessage", {
      method: "post", contentType: "application/json", muteHttpExceptions: true,
      payload: JSON.stringify({ chat_id: targetChatId, text: msg, parse_mode: "HTML", disable_web_page_preview: true, reply_markup: JSON.stringify({ inline_keyboard: keyboard }) })
    });
    let resJson = JSON.parse(response.getContentText());
    if (!resJson.ok && response.getResponseCode() === 429) {
      let retryAfter = (resJson.parameters && resJson.parameters.retry_after) ? resJson.parameters.retry_after : 2;
      Utilities.sleep((retryAfter + 1) * 1000);
      response = UrlFetchApp.fetch(TELEGRAM_API_URL + "/sendMessage", {
        method: "post", contentType: "application/json", muteHttpExceptions: true,
        payload: JSON.stringify({ chat_id: targetChatId, text: msg, parse_mode: "HTML", disable_web_page_preview: true, reply_markup: JSON.stringify({ inline_keyboard: keyboard }) })
      });
      resJson = JSON.parse(response.getContentText());
    }
    return resJson;
  } catch (err) { return { ok: false, description: err.toString() };}
}

// =====================================================================
// --- TELEGRAM BUTTON HANDLER ---
// =====================================================================

// ─── CHANGED: all actions use displayName_ + findCompanyTab (F6) ──────────
function handleTelegramCallback(callbackQuery) {
  const queryId = callbackQuery.id;
  const chatId = callbackQuery.message.chat.id;
  const messageId = callbackQuery.message.message_id;

  // Handle ping:: before the generic answer — a callback can only be
  // answered once, so ping must respond and return before "Processing...".
  if (String(callbackQuery.data || "").indexOf("ping::") === 0) {
    answerCallback(queryId, "✅ Buttons work — webhook is connected");
    return ContentService.createTextOutput("OK");
  }

  answerCallback(queryId, "Processing...");

  try {
    const parts = (callbackQuery.data || "").split("::");
    const action = parts[0];
    const companyQuery = parts[1];
    const ss = SpreadsheetApp.getActiveSpreadsheet();

    const sendError = (msg) => {
      UrlFetchApp.fetch(TELEGRAM_API_URL + "/sendMessage", {
        method: "post", contentType: "application/json", muteHttpExceptions: true,
        payload: JSON.stringify({ chat_id: chatId, text: `⚠️ <b>${msg}</b>`, parse_mode: "HTML" })
      });
    };

    if (action === "approve") {
      const realName = displayName_(companyQuery) || companyQuery;
      if (executeMoveByCompany(companyQuery, TABS.REVIEW, TABS.INBOX)) {
        editMessage(chatId, messageId, `✅ <b>APPROVED:</b> ${escapeHtml(realName)} has been moved to 📥 Inbox.`);
      } else sendError(`Lead "<code>${escapeHtml(companyQuery)}</code>" not found in Review.`);
    } else if (action === "promote") {
      const realName = displayName_(companyQuery) || companyQuery;
      const srcTab = findCompanyTab(companyQuery);
      if (srcTab && executeMoveByCompany(companyQuery, srcTab, TABS.LEADS)) {
        editMessage(chatId, messageId, `🎯 <b>QUALIFIED:</b> ${escapeHtml(realName)} moved to 🎯 Leads. AI Enrichment queued.`);
        triggerGitHubEnrichment();
      } else sendError(`Lead "<code>${escapeHtml(companyQuery)}</code>" not found in CRM.`);
    } else if (action === "pipeline") {
      const realName = displayName_(companyQuery) || companyQuery;
      const srcTab = findCompanyTab(companyQuery);
      if (srcTab && executeMoveByCompany(companyQuery, srcTab, TABS.PIPELINE)) {
        editMessage(chatId, messageId, `💼 <b>ACTIVE DEAL:</b> ${escapeHtml(realName)} is now in 💼 Pipeline.`);
      } else sendError(`Lead "<code>${escapeHtml(companyQuery)}</code>" not found in CRM.`);
    } else if (action === "monitor") {
      const realName = displayName_(companyQuery) || companyQuery;
      const srcTab = findCompanyTab(companyQuery);
      if (srcTab === TABS.MONITOR) {
        editMessage(chatId, messageId, `👁️ <b>ALREADY MONITORING:</b> ${escapeHtml(realName)}`);
      } else if (srcTab && executeMoveByCompany(companyQuery, srcTab, TABS.MONITOR)) {
        editMessage(chatId, messageId, `👁️ <b>WATCHLIST:</b> ${escapeHtml(realName)} is now being monitored.`);
      } else sendError(`Lead "<code>${escapeHtml(companyQuery)}</code>" not found in CRM.`);
    } else if (action === "ack_monitor") {
      const realName = displayName_(companyQuery) || companyQuery;
      editMessage(chatId, messageId, `👁️ <b>MONITORING:</b> Update logged for ${escapeHtml(realName)}.`);
    } else if (action === "archive") {
      const realName = displayName_(companyQuery) || companyQuery;
      const srcTab = findCompanyTab(companyQuery);
      if (srcTab && executeMoveByCompany(companyQuery, srcTab, TABS.ARCHIVE)) {
        editMessage(chatId, messageId, `📦 <b>ARCHIVED:</b> ${escapeHtml(realName)} moved to Archive.`);
      } else sendError(`Lead "<code>${escapeHtml(companyQuery)}</code>" not found in CRM.`);
    } else if (action === "deal") {
      const realName = displayName_(companyQuery) || companyQuery;
      let intelMsg = getCompetitorBriefing(realName);
      if (intelMsg) {
        UrlFetchApp.fetch(TELEGRAM_API_URL + "/sendMessage", {
          method: "post", contentType: "application/json", muteHttpExceptions: true,
          payload: JSON.stringify({ chat_id: chatId, text: intelMsg, parse_mode: "HTML" })
        });
      }
      let msgText = `🟢 Deal Setup: ${realName}\n\nReply to this message with your deal details.\n\nFormat:\nDD/MM\nContact Name, Phone\nProduct, Qty, Price\nRemarks: your notes (optional)`;
      UrlFetchApp.fetch(TELEGRAM_API_URL + "/sendMessage", {
        method: "post", contentType: "application/json", muteHttpExceptions: true, 
        payload: JSON.stringify({ chat_id: chatId, text: msgText, reply_markup: { force_reply: true, selective: true } })
      });
    } else if (action === "reject") {
      const realName = displayName_(companyQuery) || companyQuery;
      const srcTab = findCompanyTab(companyQuery);
      if (srcTab && executeMoveByCompany(companyQuery, srcTab, TABS.TRASH)) {
        editMessage(chatId, messageId, `🗑️ <b>REJECTED:</b> ${escapeHtml(realName)} moved to Trash.`);
      } else sendError(`Lead "<code>${escapeHtml(companyQuery)}</code>" not found in CRM.`);
    }
  } catch (err) {
    UrlFetchApp.fetch(TELEGRAM_API_URL + "/sendMessage", {
        method: "post", contentType: "application/json", muteHttpExceptions: true,
        payload: JSON.stringify({ chat_id: chatId, text: `⚠️ <b>Script Error:</b> <code>${escapeHtml(err.toString())}</code>`, parse_mode: "HTML" })
    });
  }

  return ContentService.createTextOutput("OK");
}
// ───────────────────────────────────────────────────────────────────────────

// ─── NEW: general-purpose GitHub job dispatcher (F7) ──────────────────────
function triggerGitHubJob_(job) {
  const pat  = cleanProp_("GITHUB_PAT");
  const repo = cleanProp_("GITHUB_REPO");
  if (!pat || !repo) {
    return { ok: false, error: "GITHUB_PAT / GITHUB_REPO not set in Script Properties." };
  }
  const ref      = cleanProp_("GITHUB_REF")      || "main";
  const workflow = cleanProp_("GITHUB_WORKFLOW") || "radar_scheduler.yml";

  try {
    const r = UrlFetchApp.fetch(
      `https://api.github.com/repos/${repo}/actions/workflows/${workflow}/dispatches`,
      {
        method: "post",
        contentType: "application/json",
        headers: {
          "Authorization": "Bearer " + pat,
          "Accept": "application/vnd.github+json",
          "User-Agent": "RadarCRM"
        },
        payload: JSON.stringify({ ref: ref, inputs: { job: job } }),
        muteHttpExceptions: true
      }
    );
    const code = r.getResponseCode();
    if (code === 204) return { ok: true, workflow: workflow, ref: ref, job: job };
    const body = r.getContentText().slice(0, 300);
    Logger.log(`GitHub dispatch (${job}) failed: HTTP ${code} ${body}`);
    return { ok: false, error: `HTTP ${code} — ${body}` };
  } catch (e) {
    Logger.log("GitHub dispatch error: " + e);
    return { ok: false, error: String(e) };
  }
}

function triggerGitHubEnrichment() {
  return triggerGitHubJob_("enrich").ok;
}
// ───────────────────────────────────────────────────────────────────────────

function safeBanDomain(link, ss) {
  if (!link || link === "N/A") return;
  const urlLower = String(link).toLowerCase().trim();
  if (["gov", ".edu", ".org", "nic.in", "gem.gov.in", "eprocure", "linkedin.com", "mca.gov.in"].some(d => urlLower.includes(d))) return;
  const match = urlLower.match(/^(?:https?:\/\/)?(?:www\.)?([^\/\?\#\:]+)/i);
  if (match && match[1]) {
    const exclSheet = getSheetFlexible(ss, "🚫 Exclusions");
    if (exclSheet) { exclSheet.appendRow(["", match[1]]); }
    else {
      const settingsSheet = getSheetFlexible(ss, TABS.SETTINGS);
      if (settingsSheet) settingsSheet.appendRow(["", "", "", "", "", match[1]]);
    }
  }
}

// =====================================================================
// --- FINDING COMPANIES ---
// =====================================================================
function matcherFor_(query) {
  const q = String(query === null || query === undefined ? "" : query).trim();
  if (q.indexOf("h:") === 0) {
    const h = q.slice(2);
    return { exact: org => !!org && nameHash_(org) === h, prefix: null };
  }
  const n = normName_(q);
  if (!n) return { exact: () => false, prefix: null };
  return {
    exact: org => normName_(org) === n,
    prefix: n.length >= 15 ? (org => normName_(org).startsWith(n)) : null
  };
}

function orgColIndex_(headers) {
  return headers.findIndex(h => { const s = String(h); return s.includes("Organization") || s.includes("Company"); });
}

// Accepts:
//   • "h:<12-hex>"    → hash token produced by cbToken_()
//   • any plain name  → Unicode-normalised, exact match preferred, then legacy prefix match
// Handles Hindi / Tamil / "&" / "." / "()" / punctuation safely via matcherFor_().
function findRowByCompanyName(sheet, query) {
  if (!sheet) return -1;
  const data = sheet.getDataRange().getValues();
  if (data.length <= 1) return -1;

  const headers = data[0];
  const orgCol  = headers.findIndex(h => {
    const s = String(h).toLowerCase();
    return s.includes("organization") || s.includes("company");
  });
  const idCol   = headers.findIndex(h => {
    const s = String(h).toLowerCase().replace(/\s+/g, " ").trim();
    return s === "lead id" || s === "id" || s === "unique id" || s.includes("lead id");
  });

  const q = String(query === null || query === undefined ? "" : query).trim();
  if (!q) return -1;

  const m = matcherFor_(q);
  const rawId = q.indexOf("h:") === 0 ? "" : q;

  for (let i = data.length - 1; i >= 1; i--) {
    if (rawId && idCol > -1 && data[i][idCol]) {
      if (String(data[i][idCol]).trim().toLowerCase() === rawId.toLowerCase()) return i + 1;
    }
    if (orgCol > -1 && data[i][orgCol]) {
      const cell = data[i][orgCol];
      if (m.exact(cell)) return i + 1;
      if (m.prefix && m.prefix(cell)) return i + 1;
    }
  }

  return -1;
}

function displayName_(token) {
  const q = String(token || "").trim();
  if (!q) return "";
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  const m = matcherFor_(q);
  for (let tab of LOOKUP_TABS) {
    const sheet = getSheetFlexible(ss, tab);
    if (!sheet || sheet.getLastRow() <= 1) continue;
    const data = sheet.getDataRange().getValues();
    const orgCol = orgColIndex_(data[0]);
    if (orgCol === -1) continue;
    for (let i = data.length - 1; i >= 1; i--) {
      if (m.exact(data[i][orgCol]) || (m.prefix && m.prefix(data[i][orgCol]))) return String(data[i][orgCol]).trim();
    }
  }
  return q.indexOf("h:") === 0 ? "" : q;
}

// ─── CHANGED: verify target tab exists, return actual move result (F4) ───
function executeMoveByCompany(token, sourceTab, targetTab) {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  const sheet = getSheetFlexible(ss, sourceTab);
  if (!sheet) return false;
  if (!getSheetFlexible(ss, targetTab)) {
    Logger.log("Missing target tab: " + targetTab);
    return false;
  }
  // If source and target are the same tab, no-op — avoids appending a
  // duplicate copy and deleting the original (which just reorders the row).
  if (sheet.getName() === getSheetFlexible(ss, targetTab).getName()) {
    return true;
  }
  const row = findRowByCompanyName(sheet, token);
  if (row > 0) return moveRow(sheet, row, targetTab);
  return false;
}
// ───────────────────────────────────────────────────────────────────────────

// ─── CHANGED: also search Pipeline (F5) ───────────────────────────────────
function findCompanyTab(token) {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  for (let tab of [TABS.REVIEW, TABS.INBOX, TABS.LEADS, TABS.PIPELINE, TABS.MONITOR]) {
    const sheet = getSheetFlexible(ss, tab);
    if (sheet && findRowByCompanyName(sheet, token) > 0) return tab;
  }
  return null;
}
// ───────────────────────────────────────────────────────────────────────────

function findCompanyInCRM(companyName, allowPartial, tabs) {
  if (!companyName) return null;
  const searchNorm = normName_(companyName);
  const raw = String(companyName).toLowerCase().trim();
  if (raw === "unknown" || !searchNorm) return null;
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  const sheets = [];
  for (let tab of (tabs || DEDUPE_HIERARCHY)) {
    const sheet = getSheetFlexible(ss, tab);
    if (!sheet) continue;
    const data = sheet.getDataRange().getValues();
    if (data.length <= 1) continue;
    const orgCol = orgColIndex_(data[0]);
    if (orgCol === -1) continue;
    sheets.push({ tab: tab, data: data, orgCol: orgCol });
    for (let i = 1; i < data.length; i++) {
      if (data[i][orgCol] && normName_(data[i][orgCol]) === searchNorm) return { sheetName: tab, row: i + 1, col: orgCol + 1 };
    }
  }
  if (allowPartial && searchNorm.length >= 3) {
    for (let s of sheets) {
      for (let i = 1; i < s.data.length; i++) {
        const n = normName_(s.data[i][s.orgCol]);
        if (n && (n.includes(searchNorm) || (n.length >= 4 && searchNorm.includes(n)))) return { sheetName: s.tab, row: i + 1, col: s.orgCol + 1 };
      }
    }
  }
  return null;
}

// ─── CHANGED: skip duplicate notes + guard empty Notes column (F3) ────────
function appendNoteToExistingDeal(loc, signal) {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  const sheet = getSheetFlexible(ss, loc.sheetName);
  const headers = sheet.getRange(1, 1, 1, sheet.getLastColumn()).getValues()[0].map(h => String(h));
  const todayStr = Utilities.formatDate(new Date(), Session.getScriptTimeZone(), "dd-MMM-yyyy");
  const formattedSignal = `[${todayStr}] ${signal}`;
  const urls = String(signal || "").match(/https?:\/\/[^\s|]+/gi) || [];
  const sourceUrl = urls.length ? urls[urls.length - 1].replace(/[),.;]+$/, "") : "";

  if (loc.sheetName === TABS.MONITOR) {
    const rowVals = sheet.getRange(loc.row, 1, 1, sheet.getLastColumn()).getValues()[0];
    const slots = [
      headers.findIndex(h => h.includes("Original Signal")),
      headers.findIndex(h => h.includes("Update 1")),
      headers.findIndex(h => h.includes("Update 2")),
      headers.findIndex(h => h.includes("Update 3")),
      headers.findIndex(h => h.includes("Update 4"))
    ];
    const archiveIdx = headers.findIndex(h => h.includes("Archive"));
    if (slots[0] === -1 || archiveIdx === -1) return "no_field";
    if (sourceUrl && rowVals.some(v => String(v || "").includes(sourceUrl))) return "duplicate";

    let prevPhase = "N/A";
    for (let i = slots.length - 1; i >= 0; i--) {
      if (slots[i] !== -1 && rowVals[slots[i]]) { prevPhase = String(rowVals[slots[i]]); break; }
    }

    let updated = false;
    for (let i = 0; i < slots.length; i++) {
      if (slots[i] !== -1 && (!rowVals[slots[i]] || rowVals[slots[i]] === "")) {
        sheet.getRange(loc.row, slots[i] + 1).setValue(formattedSignal);
        updated = true;
        break;
      }
    }

    if (!updated) {
      const oldArchive = rowVals[archiveIdx] || "";
      const oldestUpdate = rowVals[slots[0]] || "";
      sheet.getRange(loc.row, archiveIdx + 1).setValue(oldArchive ? oldArchive + "\n\n" + oldestUpdate : oldestUpdate);
      for (let i = 0; i < slots.length - 1; i++) {
        sheet.getRange(loc.row, slots[i] + 1).setValue(rowVals[slots[i + 1]]);
      }
      sheet.getRange(loc.row, slots[slots.length - 1] + 1).setValue(formattedSignal);
    }

    const compIdx = headers.findIndex(h => h.includes("Company") || h.includes("Organization"));
    const locIdx = headers.findIndex(h => h.includes("Location") || h.includes("City"));
    const company = (compIdx > -1 && rowVals[compIdx]) ? String(rowVals[compIdx]) : "Unknown";
    const location = (locIdx > -1 && rowVals[locIdx]) ? String(rowVals[locIdx]) : "Unknown";

    const msg = `🔔 <b>WATCHLIST UPDATE: ${escapeHtml(company)}</b>\n📍 <b>Location:</b> ${escapeHtml(location)}\n\n🆕 <b>New Development:</b>\n${escapeHtml(clip_(formattedSignal, 1200))}\n\n⏪ <b>Previous Phase:</b>\n${escapeHtml(clip_(prevPhase, 1200))}`;
    const token = cbToken_(company);
    const kb = [[
      { text: "🚀 Move to Pipeline", callback_data: `pipeline::${token}` },
      { text: "👁️ Keep Monitoring", callback_data: `ack_monitor::${token}` }
    ]];
    tgSend_(TELEGRAM_CHAT_ID_WATCHLIST, msg, kb);
    return "written";
  } else {
    const notesCol = headers.findIndex(h => h.includes("Notes") || h.includes("Intel") || h.includes("Dossier") || h.includes("Why")) + 1;
    if (notesCol > 0) {
      const cell = sheet.getRange(loc.row, notesCol);
      const val = String(cell.getValue() || "");

      // Same event URL means duplicate. Different URLs for the same buyer
      // remain valuable as distinct opportunities, even if the first 200
      // characters of their AI notes happen to be similar.
      const newNorm = normName_(signal);
      const recentNotes = val.split("\n").slice(-20);
      const isDuplicate = sourceUrl
        ? val.includes(sourceUrl)
        : recentNotes.some(n => normName_(n.replace(/^\[[^\]]*\]\s*/, "")) === newNorm);

      if (isDuplicate) {
        Logger.log("Skipped duplicate note for " + loc.sheetName + " row " + loc.row);
        return "duplicate";
      } else {
        cell.setValue((val ? val + "\n" : "") + `[${todayStr} NEW SIGNAL]: ` + signal);
        return "written";
      }
    } else {
      Logger.log("WARNING: no Notes column found in " + loc.sheetName + " — signal NOT logged: " + String(signal).slice(0, 80));
      return "no_field";
    }
  }
}
// ───────────────────────────────────────────────────────────────────────────

// =====================================================================
// --- SHEET AUTOMATION (installable on-edit trigger: menu > Install Triggers) ---
// =====================================================================
function onSheetEdit(e) {
  if (!e || !e.range) return;
  const range = e.range, sheet = range.getSheet(), sheetName = sheet.getName(), value = e.value;
  if (range.getRow() < 2 || !value) return;
  const headers = sheet.getRange(1, 1, 1, sheet.getLastColumn()).getValues()[0].map(h => String(h));

  const actionCol = headers.findIndex(h => h.includes("Action") || h.includes("Move To") || h.includes("Status") || h.includes("Reason")) + 1;
  if (range.getColumn() !== actionCol) return;

  const cleanName = sheetName.replace(/[^a-zA-Z0-9]/g, '').toLowerCase();

  if ((cleanName.includes("inbox") || cleanName.includes("review")) && value === "Qualify") {
    moveRow(sheet, range.getRow(), TABS.LEADS);
    triggerGitHubEnrichment();
  }
  else if (cleanName.includes("lead")) {
    if (value === "Pipeline") {
      moveRow(sheet, range.getRow(), TABS.PIPELINE);
    } else if (value === "Re-enrich") {
      const dossierCol = headers.findIndex(h => h.includes("Deep Intel") || h.includes("Dossier") || h.includes("Enriched At"));
      if (dossierCol > -1) sheet.getRange(range.getRow(), dossierCol + 1).clearContent();
      sheet.getRange(range.getRow(), range.getColumn()).setValue("Queued");
      triggerGitHubEnrichment();
    }
  }
  else if (cleanName.includes("pipeline") && ["Won", "Lost", "Nurture"].includes(value)) {
    moveRow(sheet, range.getRow(), TABS.ARCHIVE, value);
  }
  else if (cleanName.includes("trash")) {
    if (value === "Rescue to Inbox") moveRow(sheet, range.getRow(), TABS.INBOX);
    else if (value === "Rescue to Partners") moveRow(sheet, range.getRow(), TABS.PARTNERS);
    else if (value === "Delete" || value === "Reject") deleteRowSafe_(sheet, range.getRow());
  }
  else if (cleanName.includes("manual")) {
    // Manual Extraction tab — dropdown-driven routing.
    // Row moves based on Status selection. "Pending" is a no-op.
    const v = String(value);
    if (v.includes("Qualify")) {
      moveRow(sheet, range.getRow(), TABS.LEADS);
      triggerGitHubEnrichment();
    } else if (v.includes("Reject")) {
      moveRow(sheet, range.getRow(), TABS.TRASH, "Manual rejected");
    } else if (v.includes("Archive")) {
      moveRow(sheet, range.getRow(), TABS.ARCHIVE, "Manual archived");
    }
    // Pending = no-op, row stays in Manual
  }
  else if (value === "Reject" || value === "Drop") {
    moveRow(sheet, range.getRow(), TABS.ARCHIVE, value);
  }
  else if (value === "Block Domain") {
    const linkCol = headers.findIndex(h => h.includes("Source") || h.includes("Link"));
    if (linkCol > -1) safeBanDomain(sheet.getRange(range.getRow(), linkCol + 1).getValue(), e.source);
    moveRow(sheet, range.getRow(), TABS.TRASH, "Domain Blocked");
  }
}

function moveRow(sourceSheet, rowNumber, targetSheetName, archiveStatus) {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  const targetSheet = getSheetFlexible(ss, targetSheetName);
  if (!targetSheet) return false;

  const numCols = sourceSheet.getLastColumn();
  const sourceRow = sourceSheet.getRange(rowNumber, 1, 1, numCols).getValues()[0];
  const sourceHeaders = sourceSheet.getRange(1, 1, 1, numCols).getValues()[0];
  const targetHeaders = targetSheet.getRange(1, 1, 1, targetSheet.getLastColumn()).getValues()[0];

  const targetRow = new Array(targetHeaders.length).fill("");

  for (let s = 0; s < sourceHeaders.length; s++) {
    const sHead = String(sourceHeaders[s]).trim().toLowerCase();
    if (!sHead || sHead.includes("action") || sHead.includes("move")) continue;

    for (let t = 0; t < targetHeaders.length; t++) {
      const tHead = String(targetHeaders[t]).trim().toLowerCase();
      if (!tHead) continue;
      if (sHead === tHead || (sHead.length > 4 && tHead.includes(sHead)) || (tHead.length > 4 && sHead.includes(tHead))) {
        targetRow[t] = sourceRow[s];
        break;
      }
    }
  }

  const locIdx = targetHeaders.findIndex(h => String(h).toLowerCase().includes("location"));
  if (locIdx > -1 && (!targetRow[locIdx] || targetRow[locIdx] === "")) {
    const cityIdx = sourceHeaders.findIndex(h => String(h).toLowerCase().includes("city"));
    const stateIdx = sourceHeaders.findIndex(h => String(h).toLowerCase().includes("state"));
    const city = cityIdx > -1 ? String(sourceRow[cityIdx] || "") : "";
    const state = stateIdx > -1 ? String(sourceRow[stateIdx] || "") : "";
    const parts = [city, state].filter(x => x && x !== "N/A");
    if (parts.length > 0) targetRow[locIdx] = parts.join(", ");
  }
  const mapAlias = (srcCandidates, tgtCandidates) => {
    const tIdx = targetHeaders.findIndex(h => tgtCandidates.some(c => String(h).toLowerCase().includes(c)));
    if (tIdx > -1 && (!targetRow[tIdx] || targetRow[tIdx] === "")) {
      const sIdx = sourceHeaders.findIndex(h => srcCandidates.some(c => String(h).toLowerCase().includes(c)));
      if (sIdx > -1 && sourceRow[sIdx]) targetRow[tIdx] = sourceRow[sIdx];
    }
  };
  mapAlias(["organization", "company"], ["company", "organization"]);
  mapAlias(["signal", "why", "original signal"], ["original signal", "signal", "why"]);

  if (archiveStatus) {
    const statusColIndex = targetHeaders.findIndex(h => String(h).toLowerCase().includes("status") || String(h).toLowerCase().includes("reason"));
    if (statusColIndex > -1) targetRow[statusColIndex] = archiveStatus;
  }

  targetSheet.appendRow(targetRow);
  deleteRowSafe_(sourceSheet, rowNumber);
  return true;
}

// =====================================================================
// --- CONTACT MASTER V2 LOGIC ---
// =====================================================================
function contactColumns_(sh) {
  const lastCol = Math.max(sh.getLastColumn(), 8);
  const headers = sh.getRange(1, 1, 1, lastCol).getValues()[0].map(h => String(h || '').trim());
  const cols = {};
  CORE_FIELDS.forEach((k, i) => cols[k] = i + 1);
  let next = headers.length;
  while (next > 8 && headers[next - 1] === '') next--;
  EXTRA_COLUMNS.forEach(pair => {
    let idx = headers.indexOf(pair[1]);
    if (idx === -1) {
      next = Math.max(next, 8) + 1;
      sh.getRange(1, next).setValue(pair[1]).setFontWeight('bold');
      idx = next - 1;
      headers[idx] = pair[1];
    }
    cols[pair[0]] = idx + 1;
  });
  return cols;
}

function linkedinKey_(url) {
  const m = String(url || '').toLowerCase().match(/linkedin\.com\/in\/([^\/?#\s]+)/);
  if (!m) return '';
  let slug = m[1];
  try { slug = decodeURIComponent(slug); } catch (e) {}
  return 'in:' + slug.replace(/^-+|-+$/g, '');
}

function normEmail_(e) {
  e = String(e || '').trim().toLowerCase();
  if (e.indexOf('@') < 0) return '';
  const p = e.split('@');
  let local = p[0], dom = p[1];
  if (dom === 'gmail.com' || dom === 'googlemail.com') { local = local.split('+')[0].replace(/\./g, ''); dom = 'gmail.com'; }
  return local + '@' + dom;
}

function getContactIndex_() {
  const sh = getSheetFlexible(SpreadsheetApp.getActiveSpreadsheet(), TABS.CONTACTS);
  if (!sh) return { contacts: [] };
  const cols = contactColumns_(sh);
  const last = sh.getLastRow();
  if (last < 2) return { contacts: [] };
  const width = Math.max.apply(null, Object.keys(cols).map(k => cols[k]));
  const vals = sh.getRange(2, 1, last - 1, width).getValues();
  const out = vals.map((r, i) => {
    return {
      row: i + 2,
      uid: String(r[cols.unique_id - 1] || ''),
      name: String(r[cols.name - 1] || ''),
      email: String(r[cols.email - 1] || ''),
      phone: String(r[cols.phone - 1] || ''),
      linkedin: String(r[cols.linkedin - 1] || ''),
      account_key: String(r[cols.account_key - 1] || ''),
      org: String(r[cols.organization - 1] || ''),
      // ── Deploy 2: return phone metadata for future use ──
      mobile: String(r[cols.mobile - 1] || ''),
      phone_type: String(r[cols.phone_type - 1] || ''),
      phone_status: String(r[cols.phone_status - 1] || ''),
      whatsapp: String(r[cols.whatsapp - 1] || '')
    };
  }).filter(c => c.name || c.email || c.linkedin || c.mobile);
  return { contacts: out };
}

function upsertContactV2_(cd) {
  if (!cd || !cd.fields) return { status: 'error', message: 'contact_data.fields missing' };
  let sh = getSheetFlexible(SpreadsheetApp.getActiveSpreadsheet(), TABS.CONTACTS);
  if (!sh) {
    // Auto-create Contact Master tab on first write
    sh = SpreadsheetApp.getActiveSpreadsheet().insertSheet(TABS.CONTACTS);
    const headers = ["Lead ID", "Name", "Organization", "Designation", "Phone", "Email", "LinkedIn", "Notes"];
    sh.getRange(1, 1, 1, headers.length).setValues([headers]).setFontWeight('bold');
    sh.setFrozenRows(1);
    Logger.log("Created Contacts tab on first contact write");
  }

  const f = cd.fields;
  if (!f.name && !f.email && !f.linkedin) return { status: 'error', message: 'contact needs at least a name, email or linkedin' };

  const cols = contactColumns_(sh);
  const width = Math.max.apply(null, Object.keys(cols).map(k => cols[k]));
  const last = sh.getLastRow();
  const vals = last >= 2 ? sh.getRange(2, 1, last - 1, width).getValues() : [];
  const cell = (r, key) => String(r[cols[key] - 1] || '');

  let hit = -1;
  const wantUid = String(cd.match_uid || '');
  const wantRow = Number(cd.match_row || 0);

  if (wantRow >= 2 && wantRow - 2 < vals.length) {
    const u = cell(vals[wantRow - 2], 'unique_id');
    if (!u || u === wantUid) hit = wantRow - 2;
  }
  if (hit < 0 && f.unique_id) {
    for (let i = 0; i < vals.length; i++) if (cell(vals[i], 'unique_id') === f.unique_id) { hit = i; break; }
  }
  if (hit < 0 && f.email_status === 'found' && f.email) {
    const em = normEmail_(f.email);
    for (let i = 0; i < vals.length; i++) if (normEmail_(cell(vals[i], 'email')) === em) { hit = i; break; }
  }
  if (hit < 0 && linkedinKey_(f.linkedin)) {
    const lk = linkedinKey_(f.linkedin);
    for (let i = 0; i < vals.length; i++) if (linkedinKey_(cell(vals[i], 'linkedin')) === lk) { hit = i; break; }
  }

  if (hit < 0) {
    const row = new Array(width).fill('');
    Object.keys(cols).forEach(k => { if (f[k] !== undefined) row[cols[k] - 1] = f[k]; });
    sh.appendRow(row);
    return { status: 'inserted', message: 'contact added: ' + (f.name || ''), row: sh.getLastRow() };
  }

  const rowNo = hit + 2;
  const existing = vals[hit];
  const upgradeEmail = f.email_status === 'found' && cell(existing, 'email_status') !== 'found';

  Object.keys(cols).forEach(k => {
    const incoming = f[k];
    if (incoming === undefined || incoming === '' || incoming === null) return;
    const cur = String(existing[cols[k] - 1] || '');
    const isLead = k === 'lead_id';
    const shouldWrite = k === 'last_enriched' || cur === '' ||
      (upgradeEmail && (k === 'email' || k === 'email_status')) ||
      (k === 'possible_emails' && cell(existing, 'email_status') !== 'found');
    if (shouldWrite && !(isLead && cur !== '')) sh.getRange(rowNo, cols[k]).setValue(incoming);
  });

  return { status: 'updated', message: 'existing contact merged (row ' + rowNo + ')', row: rowNo };
}

// =====================================================================
// --- ERP DEALS ENGINE (INVENTORY LEDGER & TELEGRAM PARSER) ---
// =====================================================================
const DEAL_FORMAT_HELP =
  "Format:\nDD/MM\nContact Name, Phone\nProduct, Qty, Price\nRemarks: your notes (optional)\n\n" +
  "Example:\n15/11\nRajesh Kumar, 9876543210\nCCTV Camera 4MP, 10, 4500\nRemarks: needs installation";

function processDealReply(message) {
  const chatId = message.chat.id;
  const text = String(message.text || "");
  const botPrompt = String(message.reply_to_message.text || "");

  const companyMatch = botPrompt.match(/Deal Setup:\s*(.+)/);
  if (!companyMatch) return;
  const company = companyMatch[1].trim();

  const lines = text.split('\n').map(l => l.trim()).filter(l => l !== '');
  if (lines.length < 3) {
    tgSend_(chatId, "⚠️ <b>Deal not logged</b> - I need at least a date, a contact line and one product line.\n\n<pre>" + escapeHtml(DEAL_FORMAT_HELP) + "</pre>");
    return;
  }

  const tz = Session.getScriptTimeZone();
  const closeParsed = parseDealDate_(lines[0], new Date());
  const closeDate = closeParsed ? Utilities.formatDate(closeParsed, tz, "dd-MMM-yyyy") : lines[0];

  const contactLine = (lines[1] || "").split(',');
  const contactName = contactLine[0] ? contactLine[0].trim() : "N/A";
  const phone = contactLine[1] ? contactLine[1].trim() : "N/A";

  let remarks = "N/A";
  const products = [];
  const unreadable = [];
  let totalValue = 0, totalGst = 0;

  for (let i = 2; i < lines.length; i++) {
    const line = lines[i];
    const lower = line.toLowerCase();
    if (lower.startsWith("remarks:") || lower.startsWith("remark:")) {
      remarks = line.substring(line.indexOf(':') + 1).trim() || "N/A";
      continue;
    }
    const m = line.match(/^(.+?),\s*([0-9]+(?:\.[0-9]+)?)\s*,\s*(?:₹|rs\.?|inr)?\s*([0-9][0-9,]*(?:\.[0-9]+)?)\s*$/i);
    if (!m) { unreadable.push(line); continue; }
    const qty = parseFloat(m[2]);
    const price = parseFloat(m[3].replace(/,/g, '')) || 0;
    const lineTotal = qty * price;
    const gst = lineTotal * GST_RATE;
    totalValue += lineTotal;
    totalGst += gst;
    products.push({ name: m[1].trim(), qty: qty, price: price, lineTotal: lineTotal, gst: gst, finalTotal: lineTotal + gst });
  }

  if (products.length === 0) {
    tgSend_(chatId, "⚠️ <b>Deal not logged</b> - I couldn't read any product line (need <i>Product, Qty, Price</i>).\n\n<pre>" + escapeHtml(DEAL_FORMAT_HELP) + "</pre>");
    return;
  }

  const dealSheet = getSheetFlexible(SpreadsheetApp.getActiveSpreadsheet(), TABS.DEALS);
  if (!dealSheet) { tgSend_(chatId, "❌ <b>Deals tab not found.</b> Create a tab named " + escapeHtml(TABS.DEALS) + "."); return; }

  const today = Utilities.formatDate(new Date(), tz, "dd-MMM-yyyy");
  const dealId = "DL-" + Utilities.formatDate(new Date(), tz, "yyMMdd") + "-" + Math.floor(100 + Math.random() * 900);

  products.forEach(p => {
    dealSheet.appendRow([today, dealId, safeCell_(company), safeCell_(contactName), safeCell_(phone), closeDate,
      safeCell_(p.name), p.qty, p.price, p.lineTotal, p.gst, p.finalTotal, safeCell_(remarks)]);
  });

  let receipt = `✅ <b>Deal Logged Successfully! (ID: ${dealId})</b>\n`;
  receipt += `🏢 <b>Client:</b> ${escapeHtml(company)}\n`;
  receipt += `👤 <b>Contact:</b> ${escapeHtml(contactName)}\n`;
  receipt += `📅 <b>Expected Close:</b> ${escapeHtml(closeDate)}${closeParsed ? "" : " ⚠️ (date not recognised, saved as typed)"}\n\n`;
  receipt += `📦 <b>Line Items:</b>\n`;
  products.forEach(p => {
    receipt += `• ${escapeHtml(p.name)}: ${p.qty} @ ₹${inr_(p.price)} (₹${inr_(p.lineTotal)})\n`;
  });
  receipt += `\n💵 <b>Subtotal:</b> ₹${inr_(totalValue)}\n`;
  receipt += `🧾 <b>GST (${Math.round(GST_RATE * 100)}%):</b> ₹${inr_(totalGst)}\n`;
  receipt += `💰 <b>Total incl. GST:</b> ₹${inr_(totalValue + totalGst)}\n`;
  if (remarks !== "N/A") receipt += `📝 <b>Remarks:</b> ${escapeHtml(remarks)}\n`;
  if (unreadable.length) receipt += `\n⚠️ <b>Skipped (couldn't read):</b>\n${unreadable.map(escapeHtml).join("\n")}\n`;

  tgSend_(chatId, receipt);
}

// =====================================================================
// --- SLA & FOLLOW-UP ENGINE (9:00 AM CRON JOB) ---
// =====================================================================
function runMorningBriefing() {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  const today = new Date();
  today.setHours(0, 0, 0, 0);

  const critical = [];
  const stalePipeline = [];

  const dealSheet = getSheetFlexible(ss, TABS.DEALS);
  if (dealSheet && dealSheet.getLastRow() > 1) {
    const dData = dealSheet.getDataRange().getValues();
    const dHeads = dData[0];
    const closeCol = dHeads.findIndex(h => String(h).includes("Close"));
    const compCol = dHeads.findIndex(h => String(h).includes("Company"));
    const valCol = findCol_(dHeads, ["final", "total"]);
    const stageCol = dHeads.findIndex(h => { const s = String(h); return s.includes("Stage") || s.includes("Status"); });
    const idCol = dHeads.findIndex(h => String(h).toLowerCase().includes("deal id"));

    if (closeCol > -1 && compCol > -1) {
      const deals = {};
      const order = [];
      for (let i = 1; i < dData.length; i++) {
        const stage = stageCol > -1 ? String(dData[i][stageCol]).toUpperCase() : "";
        if (stage === "WON" || stage === "LOST") continue;
        const closeDate = parseDealDate_(dData[i][closeCol], today);
        if (!closeDate) continue;
        const comp = String(dData[i][compCol] || "").trim();
        if (!comp) continue;
        const key = idCol > -1 && dData[i][idCol] ? String(dData[i][idCol]) : comp + "|" + closeDate.getTime();
        if (!deals[key]) { deals[key] = { comp: comp, closeDate: closeDate, val: 0 }; order.push(key); }
        deals[key].val += valCol > -1 ? toNumber_(dData[i][valCol]) : 0;
      }
      order.forEach(key => {
        const d = deals[key];
        const diffDays = Math.round((d.closeDate.getTime() - today.getTime()) / DAY_MS);
        const tail = `${escapeHtml(d.comp)} (₹${inr_(d.val)})`;
        if (diffDays < 0) critical.push(`🔴 <b>OVERDUE (${-diffDays}d):</b> ${tail}`);
        else if (diffDays === 0) critical.push(`🚨 <b>CLOSING TODAY:</b> ${tail}`);
        else if (diffDays === 1) critical.push(`🟠 <b>24 HRS:</b> ${tail}`);
        else if (diffDays === 2) critical.push(`🟡 <b>48 HRS:</b> ${tail}`);
        else if (diffDays === 3) critical.push(`🔵 <b>72 HRS:</b> ${tail}`);
      });
    }
  }

  const pipeSheet = getSheetFlexible(ss, TABS.PIPELINE);
  if (pipeSheet && pipeSheet.getLastRow() > 1) {
    const pData = pipeSheet.getDataRange().getValues();
    const pHeads = pData[0];
    const dateCol = pHeads.findIndex(h => { const s = String(h); return s.includes("Date") || s.includes("Log"); });
    const pCompCol = pHeads.findIndex(h => { const s = String(h); return s.includes("Company") || s.includes("Organization"); });
    if (dateCol > -1 && pCompCol > -1) {
      for (let i = 1; i < pData.length; i++) {
        const pDate = new Date(pData[i][dateCol]);
        if (isNaN(pDate.getTime()) || !pData[i][pCompCol]) continue;
        const diffDays = Math.floor((today.getTime() - pDate.getTime()) / DAY_MS);
        if (diffDays >= 2) stalePipeline.push({ days: diffDays, text: `⚠️ <b>STALE (${diffDays} days):</b> ${escapeHtml(pData[i][pCompCol])}` });
      }
      stalePipeline.sort((a, b) => b.days - a.days);
    }
  }

  if (critical.length === 0 && stalePipeline.length === 0) return;

  const cap = (arr, n) => arr.slice(0, n).concat(arr.length > n ? [`… and ${arr.length - n} more`] : []);
  let msg = `⏰ <b>MORNING SLA BRIEFING</b>\n\n`;
  if (critical.length > 0) msg += `🎯 <b>DEALS CLOSING WINDOW</b>\n` + cap(critical, 25).join("\n") + `\n\n`;
  if (stalePipeline.length > 0) msg += `💼 <b>STALE PIPELINE (>= 2 Days)</b>\n` + cap(stalePipeline.map(s => s.text), 15).join("\n");

  tgSend_(TELEGRAM_CHAT_ID, msg);
}

// =====================================================================
// --- COMPETITOR INTELLIGENCE ANALYTICS ENGINE ---
// =====================================================================
function getCompetitorBriefing(clientName) {
  const intelSheet = getSheetFlexible(SpreadsheetApp.getActiveSpreadsheet(), COMPETITOR_TAB);
  if (!intelSheet) return null;

  const data = intelSheet.getDataRange().getValues();
  if (data.length <= 1) return null;

  const clientIdx = data[0].findIndex(h => String(h).includes("Client"));
  const compIdx = data[0].findIndex(h => String(h).includes("Competitor"));
  if (clientIdx === -1 || compIdx === -1) return null;

  const cleanTarget = normName_(clientName);
  if (!cleanTarget) return null;

  const matches = [];
  const compCounts = {};
  for (let i = 1; i < data.length; i++) {
    const rowClient = normName_(data[i][clientIdx]);
    if (rowClient && (cleanTarget.includes(rowClient) || rowClient.includes(cleanTarget))) {
      const comp = String(data[i][compIdx]).trim();
      if (comp && comp !== "N/A") {
        matches.push(comp);
        compCounts[comp] = (compCounts[comp] || 0) + 1;
      }
    }
  }
  if (matches.length === 0) return null;

  const topComp = Object.keys(compCounts).reduce((a, b) => compCounts[a] >= compCounts[b] ? a : b);
  const winRate = Math.round((compCounts[topComp] / matches.length) * 100);

  let msg = `🕵️ <b>COMPETITOR INTEL BRIEFING: ${escapeHtml(clientName)}</b>\n\n`;
  msg += `📊 <b>Historical Win Rate:</b> <b>${escapeHtml(topComp)}</b> dominates ${winRate}% of tracked projects for this client.\n`;
  msg += `💡 <b>Pricing Strategy:</b> Ensure your baseline quotes account for ${escapeHtml(topComp)}'s pricing trends. Consider bundling software services as mandatory to protect your hardware margins.\n`;
  return msg;
}

// =====================================================================
// --- INSTANT DOSSIER GENERATOR ---
// =====================================================================
function generateDossier(companyName) {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  const loc = findCompanyInCRM(companyName, true);
  if (!loc) return `❌ <b>Not found:</b> No active records for "${escapeHtml(companyName)}" in the CRM.`;

  const sheet = getSheetFlexible(ss, loc.sheetName);
  const headers = sheet.getRange(1, 1, 1, sheet.getLastColumn()).getValues()[0].map(h => String(h));
  const orgCol = orgColIndex_(headers);
  const canonical = orgCol > -1 ? String(sheet.getRange(loc.row, orgCol + 1).getValue() || companyName) : companyName;
  const canonNorm = normName_(canonical);

  let msg = `🗄️ <b>CLIENT DOSSIER: ${escapeHtml(canonical)}</b>\n\n`;
  msg += `📍 <b>Current Location:</b> ${escapeHtml(loc.sheetName)}\n\n`;

  const dealSheet = getSheetFlexible(ss, TABS.DEALS);
  const dealLines = [];
  if (dealSheet && dealSheet.getLastRow() > 1) {
    const dData = dealSheet.getDataRange().getValues();
    const compIdx = dData[0].findIndex(h => String(h).includes("Company"));
    const valIdx = findCol_(dData[0], ["final", "total"]);
    const closeIdx = dData[0].findIndex(h => String(h).includes("Close"));
    const idIdx = dData[0].findIndex(h => String(h).toLowerCase().includes("deal id"));
    if (compIdx > -1) {
      const grouped = {}, order = [];
      for (let i = 1; i < dData.length; i++) {
        const rowNorm = normName_(dData[i][compIdx]);
        if (rowNorm.length < 3 || !(rowNorm === canonNorm || canonNorm.includes(rowNorm) || rowNorm.includes(canonNorm))) continue;
        const key = idIdx > -1 && dData[i][idIdx] ? String(dData[i][idIdx]) : "row" + i;
        let close = closeIdx > -1 ? dData[i][closeIdx] : "N/A";
        if (close instanceof Date) close = Utilities.formatDate(close, Session.getScriptTimeZone(), "dd-MMM-yyyy");
        if (!grouped[key]) { grouped[key] = { val: 0, close: close, id: idIdx > -1 ? dData[i][idIdx] : "" }; order.push(key); }
        grouped[key].val += valIdx > -1 ? toNumber_(dData[i][valIdx]) : 0;
      }
      order.forEach(k => dealLines.push(`• ${grouped[k].id ? escapeHtml(grouped[k].id) + " - " : ""}₹${inr_(grouped[k].val)} (Closes: ${escapeHtml(grouped[k].close)})`));
    }
  }
  msg += dealLines.length ? `💰 <b>Deals:</b>\n${dealLines.join("\n")}\n\n` : `💰 <b>Deals:</b> None\n\n`;

  const notesCol = headers.findIndex(h => h.includes("Notes") || h.includes("Intel") || h.includes("Dossier") || h.includes("Why")) + 1;
  if (notesCol > 0) {
    const cellVal = String(sheet.getRange(loc.row, notesCol).getValue() || "").trim();
    if (cellVal) {
      const noteLines = cellVal.split("\n").filter(l => l.trim() !== "");
      const recent = noteLines.slice(-4).map(l => clip_(l, 350));
      msg += `📝 <b>Recent History:</b>\n• ${recent.map(escapeHtml).join("\n• ")}\n\n`;
    }
  }

  const compMsg = getCompetitorBriefing(canonical);
  if (compMsg) {
    msg += compMsg.replace(`🕵️ <b>COMPETITOR INTEL BRIEFING: ${escapeHtml(canonical)}</b>\n\n`, `🕵️ <b>Competitor Threat:</b>\n`);
  }
  return msg;
}

// =====================================================================
// --- AI HELPERS (Gemini) ---
// =====================================================================
function callGemini_(prompt) {
  const apiKey = SCRIPT_PROPERTIES.getProperty("GEMINI_API_KEY");
  if (!apiKey) return { ok: false, error: "GEMINI_API_KEY is missing in Script Properties." };
  try {
    const res = UrlFetchApp.fetch(GEMINI_URL, {
      method: "post",
      contentType: "application/json",
      headers: { "x-goog-api-key": apiKey },
      muteHttpExceptions: true,
      payload: JSON.stringify({ contents: [{ parts: [{ text: prompt }] }] })
    });
    const json = JSON.parse(res.getContentText());
    if (json.error) return { ok: false, error: json.error.message || "API error" };
    const cand = json.candidates && json.candidates[0];
    const parts = cand && cand.content && cand.content.parts;
    const text = parts ? parts.map(p => p.text || "").join("") : "";
    if (!text) return { ok: false, error: "AI returned an empty response" + (cand && cand.finishReason ? " (" + cand.finishReason + ")" : "") + "." };
    return { ok: true, text: text };
  } catch (e) {
    return { ok: false, error: String(e) };
  }
}

function aiToTelegramHtml_(raw, max) {
  return escapeHtml(String(raw).slice(0, max)).replace(/\*\*(.+?)\*\*/g, "<b>$1</b>");
}

// =====================================================================
// --- TOP 5 TARGETS & DEADLINE AUDIT ENGINE (/top5) ---
// =====================================================================
function generateTop5Briefing() {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  let summaryData = "";

  const extractDataForAI = (tabName, limit) => {
    const sheet = getSheetFlexible(ss, tabName);
    if (!sheet) return;
    const data = sheet.getDataRange().getValues();
    if (data.length <= 1) return;
    const headers = data[0].map(h => String(h));
    const rows = data.slice(1).slice(-limit);

    const compIdx = headers.findIndex(h => h.includes("Company") || h.includes("Organization"));
    const whyIdx = headers.findIndex(h => h.includes("Signal") || h.includes("Notes") || h.includes("Intel") || h.includes("Remarks") || h.includes("Why"));
    const dateIdx = headers.findIndex(h => h.includes("Close") || h.includes("Date") || h.includes("Events") || h.includes("Timeline"));
    const valIdx = findCol_(headers, ["final", "total"]);

    summaryData += `\n=== SOURCE: ${tabName} ===\n`;
    rows.forEach(r => {
      const comp = compIdx > -1 ? r[compIdx] : "";
      if (!comp) return;
      let why = whyIdx > -1 ? String(r[whyIdx]).slice(0, 300) : "N/A";
      let date = dateIdx > -1 ? r[dateIdx] : "N/A";
      const val = valIdx > -1 ? r[valIdx] : "N/A";
      if (date instanceof Date) date = Utilities.formatDate(date, Session.getScriptTimeZone(), "dd-MMM-yyyy");
      summaryData += `Org: ${comp} | Intel/Status: ${why} | Date/Deadline: ${date} | Value: ${val}\n`;
    });
  };

  extractDataForAI(TABS.LEADS, 15);
  extractDataForAI(TABS.PIPELINE, 15);
  extractDataForAI(TABS.DEALS, 15);

  if (!summaryData.replace(/=== SOURCE: [^\n]*===/g, "").trim()) return "⚠️ No Leads / Pipeline / Deals data found to analyse.";

  const prompt = `You are an elite B2B Sales Director. Analyze the provided CRM data and generate a strictly formatted action plan.

Do exactly this:
1. "TOP 5 TO CLOSE (Pipeline)": Select the 5 best targets from the Pipeline source. State the company, why they are a priority, and the immediate next step to win the deal.
2. "TOP 5 TO HUNT (Leads)": Select the 5 best targets from the Leads source. State the company, the intelligence/signal driving the urgency, and the specific outreach angle.
3. "CRITICAL DEADLINES": Scan the Pipeline and Deals sources. List ANY company that has a stated closing date, tender deadline, or expiry. If none exist, state "No immediate deadlines detected."

Keep it highly strategic, punchy, and sales-focused. Do not invent data.

CRM DATA:
${summaryData}`;

  const ai = callGemini_(prompt);
  if (!ai.ok) return "❌ <b>AI Error:</b> " + escapeHtml(ai.error);
  return "🎯 <b>AI SALES BRIEFING: TOP 5 & DEADLINES</b>\n\n" + aiToTelegramHtml_(ai.text, 3500);
}

// =====================================================================
// --- ON-DEMAND AI BRIEFING (/briefing) ---
// =====================================================================
function generateAIBriefing() {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  let summary = "";
  [TABS.INBOX, TABS.REVIEW, TABS.LEADS, TABS.PIPELINE].forEach(tab => {
    const sheet = getSheetFlexible(ss, tab);
    if (!sheet) return;
    const data = sheet.getDataRange().getValues();
    if (data.length <= 1) return;
    const h = data[0].map(x => String(x));
    const compIdx = h.findIndex(x => x.includes("Company") || x.includes("Organization"));
    const whyIdx = h.findIndex(x => x.includes("Signal") || x.includes("Why") || x.includes("Notes") || x.includes("Intel"));
    if (compIdx === -1) return;
    summary += `\n=== ${tab} ===\n`;
    data.slice(1).slice(-10).forEach(r => {
      if (r[compIdx]) summary += `${r[compIdx]} | ${whyIdx > -1 ? String(r[whyIdx]).slice(0, 300) : ""}\n`;
    });
  });

  if (!summary) return "⚠️ No recent leads found to analyse.";

  const prompt = "You are a B2B sales director. From the CRM data below give a punchy briefing: 1) the 3 hottest new signals and why, 2) one action for today. Do not invent data.\n" + summary;
  const ai = callGemini_(prompt);
  if (!ai.ok) return "❌ <b>AI Error:</b> " + escapeHtml(ai.error);
  return "🧠 <b>AI BRIEFING</b>\n\n" + aiToTelegramHtml_(ai.text, 3500);
}

// =====================================================================
// --- EXECUTIVE DASHBOARD CALCULATOR (/dashboard) ---
// =====================================================================
function generateDashboardReport() {
  const ss = SpreadsheetApp.getActiveSpreadsheet();

  const dealsSheet = getSheetFlexible(ss, TABS.DEALS);
  let totalRevenue = 0, lineItems = 0;
  const dealIds = {};
  if (dealsSheet && dealsSheet.getLastRow() > 1) {
    const data = dealsSheet.getDataRange().getValues();
    const valCol = findCol_(data[0], ["final", "total"]);
    const idCol = data[0].findIndex(h => String(h).toLowerCase().includes("deal id"));
    if (valCol > -1) {
      for (let i = 1; i < data.length; i++) {
        const val = toNumber_(data[i][valCol]);
        if (!val) continue;
        totalRevenue += val;
        lineItems++;
        if (idCol > -1 && data[i][idCol]) dealIds[String(data[i][idCol])] = true;
      }
    }
  }
  const dealCount = Object.keys(dealIds).length;

  const countRows = (sheet) => sheet ? Math.max(0, sheet.getLastRow() - 1) : 0;
  const pipeCount = countRows(getSheetFlexible(ss, TABS.PIPELINE));
  const monitorCount = countRows(getSheetFlexible(ss, TABS.MONITOR));
  const intelCount = countRows(getSheetFlexible(ss, COMPETITOR_TAB));

  let msg = `📊 <b>RADAR EXECUTIVE DASHBOARD</b>\n\n`;
  msg += `💵 <b>Total Booked (incl. GST):</b> ₹${inr_(totalRevenue)} (${dealCount ? dealCount + " deals, " : ""}${lineItems} line items)\n`;
  msg += `💼 <b>Active Pipeline:</b> ${pipeCount} leads in play\n`;
  msg += `👁️ <b>Mega-Projects Monitored:</b> ${monitorCount} ABM targets\n`;
  msg += `🕵️ <b>Competitor Bids Tracked:</b> ${intelCount} historical records`;
  return msg;
}

function testUnicodeLookup() {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  const results = [];

  ["📥 Inbox", "⚠️ Needs Review", "🎯 Leads", "💼 Pipeline", "👁️ Monitoring"].forEach(tab => {
    const sheet = getSheetFlexible(ss, tab);
    if (!sheet) return;
    const row = findRowByCompanyName(sheet, "मेसर्स शर्मा एंड संस");
    if (row > 0) results.push(`${tab} row ${row}`);
  });

  const demo = "Tata & Sons (P) Ltd.";
  const token = cbToken_(demo);
  const inbox = getSheetFlexible(ss, TABS.INBOX);
  const hit   = findRowByCompanyName(inbox, token);

  safeAlert(
    "Unicode name hits: " + (results.join(", ") || "none in sheet (expected if not present)") +
    "\nHash token " + token + " → row " + hit
  );
}

function diagnoseQualify() {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  const out = [];

  // 1. Check triggers
  const triggers = ScriptApp.getProjectTriggers();
  const editTriggers = triggers.filter(t => t.getHandlerFunction() === "onSheetEdit");
  out.push("onSheetEdit triggers installed: " + editTriggers.length);
  if (editTriggers.length === 0) {
    out.push("  ❌ NO TRIGGER — run menu 'Install Triggers'");
  }

  // 2. Check Inbox structure
  const inbox = getSheetFlexible(ss, TABS.INBOX);
  if (!inbox) {
    out.push("❌ Inbox tab not found");
  } else {
    const headers = inbox.getRange(1, 1, 1, inbox.getLastColumn()).getValues()[0].map(h => String(h));
    out.push("\nInbox tab: \"" + inbox.getName() + "\"");
    out.push("Columns (" + headers.length + "):");
    headers.forEach((h, i) => out.push(`  ${i + 1}. "${h}"`));

    const actionCol = headers.findIndex(h =>
      h.includes("Action") || h.includes("Move To") ||
      h.includes("Status") || h.includes("Reason")) + 1;
    out.push("\nAction column index: " + actionCol);
    if (actionCol === 0) {
      out.push("  ❌ NO MATCHING COLUMN — need header with 'Action'/'Status'/'Move To'/'Reason'");
    } else {
      out.push("  Matched header: \"" + headers[actionCol - 1] + "\"");
    }

    // 3. Check Leads tab exists
    const leads = getSheetFlexible(ss, TABS.LEADS);
    out.push("\nLeads tab: " + (leads ? "✅ exists (" + leads.getName() + ")" : "❌ MISSING"));
  }

  safeAlert(out.join("\n"));
}

// ═════════════════════════════════════════════════════════════════════
// FAKE ENTITY CLEANUP
// Removes hallucinated/aggregate/SEO-phrase entries from the CRM.
// Previews before deleting. Idempotent. Uses normName_() for matching,
// so case/punctuation differences are treated as the same entity.
// ═════════════════════════════════════════════════════════════════════

const FAKE_ENTITIES_TIER1 = [
  // ── Hallucinations from job-posting / search-result filler ────────
  "Yrs Any",
  "3D Concept Analysis And Development India",
  "Unknown Construction Company And Building Contractors",
  "Anonymized Architecture And Interior Design Firm",
  "Automotive Industries Manufacturer Subscriber Only Details",
  "Commercial Real Estate Developer Smergers Listing",

  // ── Aggregate labels (describe many companies, not one) ───────────
  "Architecture Engineering And Construction Firms",
  "Government And Public Sector Infrastructure Bodies India",
  "Government Depts Public Sector India",
  "Multiple Cad Companies In India",
  "Software Tenders India",
  "Indiamart Revit Service Providers",
  "Commercial Property India",

  // ── Country / policy labels (not legal entities) ──────────────────
  "Government Of India",
  "Government Of India Smart Cities Mission Viksit Bharat Initiative",

  // ── SEO phrases, not company names ────────────────────────────────
  "Bim Modeling Services India",
  "Cad Drafting Services",
  "Revit Bim Services",
  "Autocad India",
  "Autocad Drafting India",
  "Autodesk Reseller",

  // ── Training-category labels ──────────────────────────────────────
  "Cad Training Institute",
  "Design Studio Cad It Training Institute",
  "Design Studio Cad It Training Institute India",

  // ── Aggregator site name ──────────────────────────────────────────
  "Projects In India"
];

// Tier 2 — generic but might be real. Uncomment one at a time after
// a harvest run if it keeps appearing as a PRECHECK skip.
const FAKE_ENTITIES_TIER2 = [
  // "Bim Solutions",
  // "Enterprise Technology Solutions",
  // "Indbank Global Support Services",
  // "Accent Engineering Design Services",
];


function cleanupFakeEntitiesDryRun() {
  // Preview-only: shows what would be deleted, never deletes.
  const ui = SpreadsheetApp.getUi();
  const ss = SpreadsheetApp.getActiveSpreadsheet();

  const names = FAKE_ENTITIES_TIER1.concat(FAKE_ENTITIES_TIER2);
  const fakeSet = new Set(names.map(normName_));

  const tabsToScan = [
    TABS.INBOX, TABS.REVIEW, TABS.LEADS, TABS.PIPELINE,
    TABS.MONITOR, TABS.ARCHIVE, TABS.TRASH, TABS.CONTACTS
  ];

  const lines = [];
  let total = 0;

  for (const tabName of tabsToScan) {
    const sheet = getSheetFlexible(ss, tabName);
    if (!sheet || sheet.getLastRow() <= 1) continue;
    const data = sheet.getDataRange().getValues();
    const orgCol = orgColIndex_(data[0].map(h => String(h)));
    if (orgCol === -1) continue;

    let count = 0;
    for (let i = 1; i < data.length; i++) {
      if (fakeSet.has(normName_(String(data[i][orgCol] || "").trim()))) count++;
    }
    if (count > 0) { lines.push(`${tabName}: ${count}`); total += count; }
  }

  ui.alert("📋 Dry run",
    total === 0
      ? "No matches. Nothing would be deleted."
      : `Would delete ${total} row(s):\n\n  • ` + lines.join("\n  • "),
    ui.ButtonSet.OK);
}


function cleanupFakeEntities() {
  // Deletes after confirmation dialog.
  const ui = SpreadsheetApp.getUi();
  const ss = SpreadsheetApp.getActiveSpreadsheet();

  const names = FAKE_ENTITIES_TIER1.concat(FAKE_ENTITIES_TIER2);
  const fakeSet = new Set(names.map(normName_));

  const tabsToScan = [
    TABS.INBOX, TABS.REVIEW, TABS.LEADS, TABS.PIPELINE,
    TABS.MONITOR, TABS.ARCHIVE, TABS.TRASH, TABS.CONTACTS
  ];

  const plan = [];
  let totalRows = 0;

  for (const tabName of tabsToScan) {
    const sheet = getSheetFlexible(ss, tabName);
    if (!sheet || sheet.getLastRow() <= 1) continue;

    const data = sheet.getDataRange().getValues();
    const headers = data[0].map(h => String(h));
    const orgCol = orgColIndex_(headers);
    if (orgCol === -1) continue;

    const rowsToDelete = [];
    for (let i = 1; i < data.length; i++) {
      const org = String(data[i][orgCol] || "").trim();
      if (!org) continue;
      if (fakeSet.has(normName_(org))) {
        rowsToDelete.push({ row: i + 1, org: org });
      }
    }

    if (rowsToDelete.length > 0) {
      plan.push({ sheet: sheet, tabName: tabName, rows: rowsToDelete });
      totalRows += rowsToDelete.length;
    }
  }

  if (totalRows === 0) {
    ui.alert("✅ Nothing to delete",
      "No rows in any tab match the fake-entity list.",
      ui.ButtonSet.OK);
    return;
  }

  const previewLines = plan.map(p => {
    const sample = p.rows.slice(0, 5).map(r => `    row ${r.row}: ${r.org}`).join("\n");
    const more = p.rows.length > 5 ? `\n    … and ${p.rows.length - 5} more` : "";
    return `  • ${p.tabName}: ${p.rows.length} row(s)\n${sample}${more}`;
  }).join("\n\n");

  const response = ui.alert(
    `⚠️ Delete ${totalRows} row(s) from ${plan.length} tab(s)?`,
    previewLines + "\n\nThis cannot be undone (but you have the backup copy). Proceed?",
    ui.ButtonSet.YES_NO
  );

  if (response !== ui.Button.YES) {
    Logger.log("Fake-entity cleanup cancelled by user");
    return;
  }

  let deleted = 0;
  const summary = [];
  for (const p of plan) {
    const rowNums = p.rows.map(r => r.row).sort((a, b) => b - a);
    let tabDeleted = 0;
    for (const rowNum of rowNums) {
      try {
        p.sheet.deleteRow(rowNum);
        deleted++;
        tabDeleted++;
      } catch (e) {
        Logger.log(`Failed to delete row ${rowNum} in ${p.tabName}: ${e}`);
      }
    }
    summary.push(`${p.tabName}: ${tabDeleted}`);
  }

  ui.alert(
    `✅ Deleted ${deleted} row(s)`,
    summary.join("\n") +
    "\n\nNext radar.py run will reload the refreshed CRM " +
    "(expect CompanyDedup count to drop by ~30-40).",
    ui.ButtonSet.OK
  );

  Logger.log(`Cleaned ${deleted} fake-entity rows: ${summary.join(", ")}`);
}
