// Builds the Gmail search query for one configured sender. Pulled out as its own
// pure function so both processNewBills() and scanInbox() build the exact same query
// and it's testable outside Apps Script.
function buildSenderQuery(sender, lookbackDays) {
  var parts = [];
  if (sender.fromAddress) {
    parts.push("from:" + sender.fromAddress);
  }
  if (sender.subject) {
    parts.push('subject:"' + sender.subject + '"');
  }
  if (parts.length === 0) {
    throw new Error(
      'sender "' + sender.name + '" needs a fromAddress or a subject',
    );
  }
  parts.push("newer_than:" + lookbackDays + "d");
  return parts.join(" ");
}

if (typeof module !== "undefined") {
  module.exports = { buildSenderQuery: buildSenderQuery };
}
