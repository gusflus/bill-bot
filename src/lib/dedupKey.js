// Pure key-building logic - no GAS globals, isomorphic with Node (see tests/).

function billerIdFromName(name) {
  return String(name)
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "");
}

function buildDedupKey(billerId, year, month) {
  var mm = month < 10 ? "0" + month : String(month);
  return "BILL_" + billerId + "_" + year + "_" + mm;
}

if (typeof module !== "undefined") {
  module.exports = { billerIdFromName: billerIdFromName, buildDedupKey: buildDedupKey };
}
