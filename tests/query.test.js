const test = require("node:test");
const assert = require("node:assert/strict");
const { buildSenderQuery } = require("../src/lib/query.js");

test("buildSenderQuery combines fromAddress and subject", () => {
  const sender = { name: "PG&E", fromAddress: "billpay.pge.com", subject: "Your PG&E Energy Statement is Ready to View" };
  assert.equal(
    buildSenderQuery(sender, 14),
    'from:billpay.pge.com subject:"Your PG&E Energy Statement is Ready to View" newer_than:14d',
  );
});

test("buildSenderQuery works with fromAddress only", () => {
  const sender = { name: "PG&E", fromAddress: "billpay.pge.com" };
  assert.equal(buildSenderQuery(sender, 14), "from:billpay.pge.com newer_than:14d");
});

test("buildSenderQuery works with subject only", () => {
  const sender = { name: "Trash", subject: "Payment Notification-SAN LUIS" };
  assert.equal(
    buildSenderQuery(sender, 30),
    'subject:"Payment Notification-SAN LUIS" newer_than:30d',
  );
});

test("buildSenderQuery rejects a sender with neither", () => {
  assert.throws(() => buildSenderQuery({ name: "Nobody" }, 14));
});
