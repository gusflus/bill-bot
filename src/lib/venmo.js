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

if (typeof module !== "undefined") {
  module.exports = { buildPayLink: buildPayLink };
}
