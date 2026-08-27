const test = require("node:test");
const assert = require("node:assert/strict");
const { buildPayLink } = require("../src/lib/venmo.js");

test("buildPayLink always targets the payee, regardless of who opens it", () => {
  const link = buildPayLink("gus-flusser", 4405, "PG&E split 08-2026");
  assert.match(link, /^https:\/\/venmo\.com\/\?/);
  assert.match(link, /txn=pay/);
  assert.match(link, /recipients=gus-flusser/);
  assert.match(link, /amount=44\.05/);
});

test("buildPayLink URL-encodes the note", () => {
  const link = buildPayLink("gus-flusser", 100, "PG&E split 08-2026");
  assert.match(link, /note=PG%26E%20split%2008-2026/);
});

test("buildPayLink requires a payee username", () => {
  assert.throws(() => buildPayLink("", 100, "note"));
});
