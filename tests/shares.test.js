const test = require("node:test");
const assert = require("node:assert/strict");
const { allocateCents, buildSplit } = require("../src/lib/shares.js");

test("allocateCents sums exactly to the total for an even split", () => {
  const parts = allocateCents(10000, [1, 1, 1]);
  assert.deepEqual(parts, [3334, 3333, 3333]);
  assert.equal(parts.reduce((a, b) => a + b, 0), 10000);
});

test("allocateCents gives largest remainders their extra cent, ties to earlier index", () => {
  // 100 / 3 = 33.33... each; leftover 1 cent goes to index 0 (all remainders equal)
  const parts = allocateCents(100, [1, 1, 1]);
  assert.deepEqual(parts, [34, 33, 33]);
});

test("allocateCents respects uneven weights", () => {
  const parts = allocateCents(14405, [1, 2, 1]); // SoCalGas-style total, from README example
  assert.equal(parts.reduce((a, b) => a + b, 0), 14405);
  assert.ok(parts[1] > parts[0]);
  assert.ok(parts[1] > parts[2]);
});

test("allocateCents rejects empty weights, non-positive weights, and negative totals", () => {
  assert.throws(() => allocateCents(100, []));
  assert.throws(() => allocateCents(100, [1, 0]));
  assert.throws(() => allocateCents(-1, [1]));
});

test("buildSplit appends the payee's share and returns the absorbed amount", () => {
  const roommates = [
    { label: "Sam", share: 1 },
    { label: "Alex", share: 2 },
    { label: "Jo", share: 1 },
  ];
  const result = buildSplit(1, roommates, 4405); // matches dev/preview.js fixture total
  const rowSum = result.rows.reduce((sum, r) => sum + r.amountCents, 0);
  assert.equal(rowSum + result.payerAmountCents, 4405);
  assert.equal(result.rows.length, 3);
});

test("buildSplit with payee.share 0 gives the payer nothing and roommates the whole bill", () => {
  const roommates = [
    { label: "Sam", share: 1 },
    { label: "Alex", share: 1 },
  ];
  const result = buildSplit(0, roommates, 5000);
  assert.equal(result.payerAmountCents, 0);
  const rowSum = result.rows.reduce((sum, r) => sum + r.amountCents, 0);
  assert.equal(rowSum, 5000);
});

test("buildSplit equalizes equal-share roommates to the higher cent amount", () => {
  // 4-way split (3 roommates + payee, all share 1) of $10.01 doesn't divide evenly -
  // largest-remainder would otherwise leave one roommate a cent below the others.
  const roommates = [
    { label: "Sam", share: 1 },
    { label: "Alex", share: 1 },
    { label: "Jo", share: 1 },
  ];
  const result = buildSplit(1, roommates, 1001);
  const amounts = result.rows.map((r) => r.amountCents);
  assert.deepEqual(amounts, [251, 251, 251]);
  // Every roommate gets at least their fair share; the payee absorbs the shortfall
  // that creates, so nobody but the payee is ever left short.
  const rowSum = amounts.reduce((a, b) => a + b, 0);
  assert.ok(rowSum + result.payerAmountCents >= 1001);
});

test("buildSplit leaves an uneven share distinct, but equalizes within each share group", () => {
  const roommates = [
    { label: "Sam", share: 1 },
    { label: "Alex", share: 2 }, // master bedroom - legitimately owes more
    { label: "Jo", share: 1 },
  ];
  const result = buildSplit(0, roommates, 1002);
  const byLabel = Object.fromEntries(result.rows.map((r) => [r.label, r.amountCents]));
  assert.equal(byLabel.Sam, byLabel.Jo); // same share weight -> same amount
  assert.ok(byLabel.Alex > byLabel.Sam); // double share -> still pays more
});
