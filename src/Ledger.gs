// Google Sheet used as both the dedup record and the payment ledger. Auto-created on
// first run; its ID is stashed in the LEDGER_SHEET_ID Script Property so later runs
// reuse the same sheet instead of creating a new one every time.

function getOrCreateLedgerSheet_() {
  var props = PropertiesService.getScriptProperties();
  var sheetId = props.getProperty("LEDGER_SHEET_ID");
  var spreadsheet = null;

  if (sheetId) {
    try {
      spreadsheet = SpreadsheetApp.openById(sheetId);
    } catch (e) {
      spreadsheet = null;
    }
  }

  if (!spreadsheet) {
    spreadsheet = SpreadsheetApp.create("bill-bot ledger");
    props.setProperty("LEDGER_SHEET_ID", spreadsheet.getId());
  }

  var sheet = spreadsheet.getSheetByName("Ledger") || spreadsheet.getSheets()[0];
  if (sheet.getName() !== "Ledger") sheet.setName("Ledger");
  if (sheet.getLastRow() === 0) {
    sheet.appendRow([
      "DedupKey",
      "Biller",
      "Month",
      "Total",
      "RoommateLabel",
      "ShareAmount",
      "VenmoLink",
      "Paid",
      "ProcessedAt",
      "GmailThreadId",
      "Confidence",
    ]);
    sheet.setFrozenRows(1);
  }
  return sheet;
}

function ledgerSheetUrl_() {
  return getOrCreateLedgerSheet_().getParent().getUrl();
}

function ledgerHasDedupKey_(dedupKey) {
  var sheet = getOrCreateLedgerSheet_();
  var lastRow = sheet.getLastRow();
  if (lastRow < 2) return false;
  var keys = sheet.getRange(2, 1, lastRow - 1, 1).getValues();
  return keys.some(function (row) {
    return row[0] === dedupKey;
  });
}

// rows: [{ dedupKey, biller, month, totalCents, label, amountCents, venmoLink,
//          threadId, confidence }]
function appendLedgerRows_(rows) {
  var sheet = getOrCreateLedgerSheet_();
  var now = new Date();
  var startRow = sheet.getLastRow() + 1;

  rows.forEach(function (row) {
    sheet.appendRow([
      row.dedupKey,
      row.biller,
      row.month,
      formatCents_(row.totalCents),
      row.label,
      formatCents_(row.amountCents),
      row.venmoLink,
      false,
      now,
      row.threadId,
      row.confidence,
    ]);
  });

  var paidColumn = 8;
  sheet.getRange(startRow, paidColumn, rows.length, 1).insertCheckboxes();
}
