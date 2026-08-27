// Pure link-building logic - no GAS globals, isomorphic with Node (see tests/).

function buildPayLink(payeeVenmoUsername, amountCents, note) {
  if (!payeeVenmoUsername) {
    throw new Error("payeeVenmoUsername is required to build a Venmo link");
  }
  var amount = (amountCents / 100).toFixed(2);
  var params = [
    "txn=pay",
    "audience=private",
    "recipients=" + encodeURIComponent(payeeVenmoUsername),
    "amount=" + encodeURIComponent(amount),
    "note=" + encodeURIComponent(note),
  ];
  return "https://venmo.com/?" + params.join("&");
}

// A single link with no pre-filled amount, safe to send to every roommate as-is -
// they type in their own amount (shown separately) before sending. Used for the
// Discord notification, where one link is simpler than a near-identical one per
// roommate; the Ledger sheet still stores each roommate's exact buildPayLink().
function buildGenericPayLink(payeeVenmoUsername, note) {
  if (!payeeVenmoUsername) {
    throw new Error("payeeVenmoUsername is required to build a Venmo link");
  }
  var params = [
    "txn=pay",
    "audience=private",
    "recipients=" + encodeURIComponent(payeeVenmoUsername),
    "note=" + encodeURIComponent(note),
  ];
  return "https://venmo.com/?" + params.join("&");
}

if (typeof module !== "undefined") {
  module.exports = { buildPayLink: buildPayLink, buildGenericPayLink: buildGenericPayLink };
}
