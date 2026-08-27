// Copy this file to Config.gs and edit it. Config.gs is gitignored - it holds your
// household's Venmo handle and roommate list. `clasp push` sends whichever one
// actually exists in this directory.
//
// Secrets (GEMINI_API_KEY, DISCORD_WEBHOOK_URL) do NOT go here - set them as Script
// Properties instead (Project Settings -> Script Properties in the Apps Script
// editor), so they never end up in source control even by accident. See README.

var CONFIG = {
  // Used to format bill months and label the Ledger sheet's timestamps.
  timezone: "America/Los_Angeles",

  // You - the person who pays the utility and gets reimbursed.
  //
  // You are NOT a roommate: you already paid the bill, so a notification telling you
  // to pay yourself would be nonsense. But you do carry a 'share', because your
  // portion has to be in the denominator for everyone else's share to come out right.
  // Your share is simply absorbed rather than collected - it never gets a Venmo link
  // or a ledger row of its own.
  payee: {
    // Your Venmo handle, without the leading '@'. Every Venmo link generated for a
    // roommate pays this handle - the link means "whoever opens this pays the amount
    // shown to this recipient," so only the amount changes per roommate, never who
    // it's paid to.
    venmoUsername: "your-venmo-handle",

    // Your name, used on the Ledger sheet and in the Discord notification.
    label: "Gus",

    // Your share of each bill, on the same relative scale as the roommates below.
    // Set 0 if you don't take a share and the roommates cover the whole bill.
    share: 1,
  },

  // Everyone who owes a share of the bill. Do not list yourself here - you go in
  // 'payee' above.
  //
  // 'share' is a relative weight, not a percentage - it is normalized against the sum
  // of all shares (the payee's included), so the numbers never have to add up to
  // anything in particular. All 1 means an even split. A 2 pays double a 1.
  //
  // Cents are allocated by largest remainder across everyone (payee included), so the
  // roommate amounts plus the payee's absorbed share always sum to the bill total
  // exactly.
  roommates: [
    { label: "Sam", share: 1 },
    { label: "Alex", share: 2 }, // master bedroom
    { label: "Jo", share: 1 },
  ],

  // The bill senders to watch. 'fromAddress' is matched by Gmail search, so a bare
  // domain works and catches every address at that domain.
  senders: [
    { name: "PG&E", fromAddress: "billpay.pge.com" },
    { name: "SoCalGas", fromAddress: "socalgas.com" },
    { name: "WaterSewer", fromAddress: "merchanttransact.com" },
    { name: "Spectrum", fromAddress: "spectrumemails.com" },
  ],

  behavior: {
    // How far back Gmail is searched on each run. Wide enough to survive a failed
    // run, narrow enough not to rescan the whole mailbox. Can be overridden without a
    // code push via a LOOKBACK_DAYS Script Property.
    lookbackDays: 14,

    // Applied to a Gmail thread once it's fully handled (a new bill, a duplicate, or
    // an ignorable notice) - all three get trashed after labeling. A thread that
    // fails extraction entirely gets errorLabel instead and is left in the inbox.
    processedLabel: "Bill-Bot/Processed",
    errorLabel: "Bill-Bot/Error",

    // Subjects containing any of these (case-insensitive) are skipped before
    // extraction even runs - payment receipts and autopay confirmations, not bills.
    ignorableSubjectKeywords: [
      "payment received",
      "thank you for your payment",
      "auto-pay scheduled",
      "payment confirmation",
    ],
  },
};

if (typeof module !== "undefined") {
  module.exports = { CONFIG: CONFIG };
}
