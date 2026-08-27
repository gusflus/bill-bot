const test = require("node:test");
const assert = require("node:assert/strict");
const {
  isIgnorableSubject,
  extractAmountCentsByKeyphrase,
  amountAppears,
} = require("../src/lib/extractRegex.js");

test("isIgnorableSubject matches configured keywords case-insensitively", () => {
  const keywords = ["payment received", "auto-pay scheduled"];
  assert.ok(isIgnorableSubject("Payment Received - Thank You", keywords));
  assert.ok(isIgnorableSubject("Your Auto-Pay Scheduled for Aug 20", keywords));
  assert.ok(!isIgnorableSubject("Your PG&E energy statement is ready", keywords));
});

test("extractAmountCentsByKeyphrase finds a labeled total", () => {
  const text = "Account summary\nTotal Amount Due: $142.53\nDue September 08, 2026";
  assert.equal(extractAmountCentsByKeyphrase(text), 14253);
});

test("extractAmountCentsByKeyphrase handles thousands separators", () => {
  const text = "Balance Due: $1,204.09";
  assert.equal(extractAmountCentsByKeyphrase(text), 120409);
});

test("extractAmountCentsByKeyphrase returns null when no keyphrase matches", () => {
  const text = "Your PG&E energy statement is ready.\n\n142.53\n\nStatement date: August 18, 2026";
  assert.equal(extractAmountCentsByKeyphrase(text), null);
});

test("amountAppears confirms a Gemini-reported amount is literally in the text", () => {
  const text = "Gas and electric charges.\n\n142.53\n\nStatement date: August 18, 2026";
  assert.ok(amountAppears(text, 14253));
  assert.ok(!amountAppears(text, 99999));
});
