// Pure split logic - no GAS globals referenced, so this file runs unmodified both
// inside Apps Script (pushed via clasp) and under Node (required by tests/).

function allocateCents(totalCents, weights) {
  if (!weights || weights.length === 0) {
    throw new Error("weights cannot be empty");
  }
  weights.forEach(function (w) {
    if (!(w > 0)) {
      throw new Error("every weight must be > 0, got " + JSON.stringify(weights));
    }
  });
  if (totalCents < 0) {
    throw new Error("totalCents cannot be negative, got " + totalCents);
  }

  var weightSum = weights.reduce(function (a, b) {
    return a + b;
  }, 0);
  var exact = weights.map(function (w) {
    return (totalCents * w) / weightSum;
  });
  var floors = exact.map(Math.floor);
  var floorSum = floors.reduce(function (a, b) {
    return a + b;
  }, 0);
  var leftover = totalCents - floorSum;

  var order = weights.map(function (_, i) {
    return i;
  });
  order.sort(function (a, b) {
    var remA = exact[a] - floors[a];
    var remB = exact[b] - floors[b];
    if (remA !== remB) return remB - remA; // largest remainder first
    return a - b; // tie: earlier config entry wins
  });

  for (var k = 0; k < leftover; k++) {
    floors[order[k]] += 1;
  }

  return floors;
}

// Splits totalCents across roommates (list of { label, share }), with the payee's
// own share appended to the same allocation so everyone's cents come from one
// largest-remainder pass. The payee's resulting amount is never a payable row - it's
// what they absorb, returned separately as payerAmountCents.
//
// Largest-remainder allocation can leave two roommates on the identical share weight
// a cent apart (e.g. $11.67 vs $11.66), which reads as a mistake even though it
// reconciles exactly. So within each group of roommates who share the same weight,
// everyone is bumped up to the group's largest allocated cent amount - a roommate
// never pays less than their fair share, at most a cent or two more, and the payee
// simply ends up with the tiny overage. Roommates on a different weight (an uneven
// split) are unaffected and still legitimately owe a different amount.
function buildSplit(payeeShare, roommates, totalCents) {
  var weights = roommates.map(function (r) {
    return r.share;
  });

  var payerAmountCents = 0;
  var roommateAmounts;

  if (payeeShare > 0) {
    var parts = allocateCents(totalCents, weights.concat([payeeShare]));
    payerAmountCents = parts[parts.length - 1];
    roommateAmounts = parts.slice(0, parts.length - 1);
  } else {
    roommateAmounts = allocateCents(totalCents, weights);
  }

  var maxByShare = {};
  roommates.forEach(function (r, i) {
    if (maxByShare[r.share] === undefined || roommateAmounts[i] > maxByShare[r.share]) {
      maxByShare[r.share] = roommateAmounts[i];
    }
  });

  var rows = roommates.map(function (r) {
    return { label: r.label, amountCents: maxByShare[r.share] };
  });

  return { rows: rows, payerAmountCents: payerAmountCents };
}

if (typeof module !== "undefined") {
  module.exports = { allocateCents: allocateCents, buildSplit: buildSplit };
}
