// Small in-memory Apps Script harness. Nothing is written to Google Sheets.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

class Sheet {
  constructor(name, headers, rows = []) {
    this.name = name;
    this.values = [headers, ...rows];
  }
  getName() { return this.name; }
  getDataRange() { return {getValues: () => this.values.map(r => [...r])}; }
  getLastRow() { return this.values.length; }
  getLastColumn() { return this.values[0].length; }
  getRange(row, col, rows = 1, cols = 1) {
    return {
      getValues: () => this.values.slice(row - 1, row - 1 + rows)
        .map(r => r.slice(col - 1, col - 1 + cols)),
      getValue: () => this.values[row - 1][col - 1],
      setValue: (v) => { this.values[row - 1][col - 1] = v; },
    };
  }
  appendRow(row) { this.values.push([...row]); }
}

const buyerHeaders = ["Date", "Event", "Role", "Industry", "State", "City",
  "Organization", "Solution", "Why", "Source URL", "Usage", "Notes", "Lead ID"];
const partnerHeaders = ["ID", "Date", "Organization", "City", "State",
  "Solution", "Source URL", "Notes", "Contact", "Phone", "Other"];
const buyers = new Sheet("📥 Inbox", buyerHeaders);
const trash = new Sheet("🗑 AI_Trash", buyerHeaders, [[
  "", "", "", "", "", "", "Apex Engineering Limited", "", "old rejection", "https://old.example", "", "", "",
]]);
const partners = new Sheet("🤝 Partners & Suppliers", partnerHeaders);
const byName = new Map([[buyers.name, buyers], [trash.name, trash], [partners.name, partners]]);
const book = {getSheetByName: name => byName.get(name) || null, getSheets: () => [...byName.values()]};
const ctx = vm.createContext({
  PropertiesService: {getScriptProperties: () => ({getProperty: k => k === "WEBHOOK_SECRET" ? "test" : ""})},
  SpreadsheetApp: {getActiveSpreadsheet: () => book},
  Utilities: {formatDate: () => "05-Nov-2026"},
  Session: {getScriptTimeZone: () => "Asia/Kolkata"},
  ContentService: {
    MimeType: {JSON: "json"},
    createTextOutput: str => ({
      setMimeType() {return this;},
      getContent() {return str;},
    }),
  },
  Logger: {log: () => {}},
  console,
});

const scriptPath = path.join(__dirname, "..", "Code.gs");
vm.runInContext(fs.readFileSync(scriptPath, "utf8"), ctx, {filename: "Code.gs"});
const send = (target, org, url, row) => {
  const response = ctx.handleHarvesterPost_({
    action: "insert_lead", secret: "test", target_sheet: target,
    company_name: org, source_url: url, signal_brief: "New buying signal",
    row_data: row,
  });
  return JSON.parse(response.getContent());
};

const row1 = ["2026-11-05", "Purchase", "BUYER", "Engineering", "UP",
  "Noida", "Apex Engineering Limited", "Tools", "New opportunity",
  "https://new.example/event1", "", "", "id1"];
const first = send(buyers.name, "Apex Engineering Ltd", "https://new.example/event1", row1);
assert.equal(first.status, "success", "Trash must not suppress a new buyer");
assert.equal(buyers.values.length, 2);

const second = send(buyers.name, "Apex Engineering Limited",
  "https://new.example/event2", row1);
assert.equal(second.status, "signal_updated", "second company event must be preserved");
const third = send(buyers.name, "Apex Engineering Limited",
  "https://new.example/event2", row1);
assert.equal(third.status, "duplicate", "same URL should not append twice");

const partnerRow = ["p1", "2026-11-05", "Apex Engineering Limited", "", "", "Tools",
  "https://vendor.example", "", "", "", ""];
const seller = send(partners.name, "Apex Engineering Limited", "https://vendor.example", partnerRow);
assert.equal(seller.status, "success", "seller role must not merge into buyer");
assert.equal(partners.values.length, 2);
console.log("Apps Script offline regression tests passed.");
