const test = require("node:test");
const assert = require("node:assert/strict");
const { billerIdFromName, buildDedupKey } = require("../src/lib/dedupKey.js");

test("billerIdFromName slugifies punctuation", () => {
  assert.equal(billerIdFromName("PG&E"), "pg-e");
  assert.equal(billerIdFromName("SoCalGas"), "socalgas");
});

test("buildDedupKey zero-pads the month", () => {
  assert.equal(buildDedupKey("pg-e", 2026, 8), "BILL_pg-e_2026_08");
  assert.equal(buildDedupKey("pg-e", 2026, 11), "BILL_pg-e_2026_11");
});
