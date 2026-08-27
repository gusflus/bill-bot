// Pure regex extraction/validation logic - no GAS globals, isomorphic with Node (see
// tests/). Gemini API calls (which do need UrlFetchApp) live in Gemini.gs instead.

var KEYPHRASE_PATTERNS = [
  /total\s+amount\s+due\s*:?\s*\$?\s*([\d,]+\.\d{2})/i,
  /total\s+due\s*:?\s*\$?\s*([\d,]+\.\d{2})/i,
  /amount\s+due\s*:?\s*\$?\s*([\d,]+\.\d{2})/i,
  /balance\s+due\s*:?\s*\$?\s*([\d,]+\.\d{2})/i,
  /total\s+charges\s*:?\s*\$?\s*([\d,]+\.\d{2})/i,
];

// Generic currency-shaped number - exactly 2 decimals, optional '$' and thousands
// commas. Used only as a post-hoc sanity check on Gemini's answer, never to pick the
// amount itself (too easy to grab a previous balance or a late fee).
var GENERIC_AMOUNT_RE = /\$?\s*(\d{1,3}(?:,\d{3})+\.\d{2}|\d+\.\d{2})/g;

function isIgnorableSubject(subject, keywords) {
  var lower = String(subject || "").toLowerCase();
  return (keywords || []).some(function (kw) {
    return lower.indexOf(String(kw).toLowerCase()) !== -1;
  });
}

function toCents(amountString) {
  var normalized = amountString.replace(/,/g, "");
  return Math.round(parseFloat(normalized) * 100);
}

function extractAmountCentsByKeyphrase(text) {
  for (var i = 0; i < KEYPHRASE_PATTERNS.length; i++) {
    var match = KEYPHRASE_PATTERNS[i].exec(text);
    if (match) {
      return toCents(match[1]);
    }
  }
  return null;
}

function allAmountsCents(text) {
  var amounts = [];
  var match;
  GENERIC_AMOUNT_RE.lastIndex = 0;
  while ((match = GENERIC_AMOUNT_RE.exec(text)) !== null) {
    amounts.push(toCents(match[1]));
  }
  return amounts;
}

function amountAppears(text, amountCents) {
  return allAmountsCents(text).indexOf(amountCents) !== -1;
}

if (typeof module !== "undefined") {
  module.exports = {
    isIgnorableSubject: isIgnorableSubject,
    extractAmountCentsByKeyphrase: extractAmountCentsByKeyphrase,
    amountAppears: amountAppears,
    allAmountsCents: allAmountsCents,
    toCents: toCents,
  };
}
