/**
 * bill-bot - a single self-contained Google Apps Script project.
 *
 * Scans Gmail for configured bill senders, extracts the total (regex keyphrase
 * match first, Gemini 2.5 Flash as a fallback for senders regex doesn't recognize),
 * splits it by configured share weight, records the split in a Google Sheet ledger
 * (which also serves as the dedup record), posts a Discord notification, and trashes
 * the thread.
 *
 * Setup: copy Config.example.gs to Config.gs and edit it, set GEMINI_API_KEY and
 * DISCORD_WEBHOOK_URL as Script Properties, run testConnection, then scanInbox, then
 * setupTrigger once. See README.md.
 */

var TRIGGER_HANDLER = "processNewBills";
var TRIGGER_MINUTES = 30;
var DEFAULT_LOOKBACK_DAYS = 14;

function processNewBills() {
  var processedLabel = getOrCreateLabel_(CONFIG.behavior.processedLabel);
  var errorLabel = getOrCreateLabel_(CONFIG.behavior.errorLabel);
  var lookback = lookbackDays_();
  var stats = { processed: 0, duplicate: 0, ignored: 0, errored: 0 };

  CONFIG.senders.forEach(function (sender) {
    var query = "from:" + sender.fromAddress + " newer_than:" + lookback + "d";
    var threads = GmailApp.search(query);

    threads.forEach(function (thread) {
      if (
        hasLabel_(thread, processedLabel.getName()) ||
        hasLabel_(thread, errorLabel.getName())
      ) {
        return;
      }
      handleThread_(thread, sender, processedLabel, errorLabel, stats);
    });
  });

  Logger.log(
    "Done. processed=%s duplicate=%s ignored=%s errored=%s",
    stats.processed,
    stats.duplicate,
    stats.ignored,
    stats.errored
  );
}

function handleThread_(thread, sender, processedLabel, errorLabel, stats) {
  var messages = thread.getMessages();
  var message = messages[messages.length - 1];
  var subject = message.getSubject();
  var bodyText = message.getPlainBody();
  var receivedAt = message.getDate();

  if (isIgnorableSubject(subject, CONFIG.behavior.ignorableSubjectKeywords)) {
    thread.addLabel(processedLabel);
    thread.moveToTrash();
    stats.ignored++;
    return;
  }

  var year = receivedAt.getFullYear();
  var month = receivedAt.getMonth() + 1;
  var dedupKey = buildDedupKey(billerIdFromName(sender.name), year, month);

  if (ledgerHasDedupKey_(dedupKey)) {
    thread.addLabel(processedLabel);
    thread.moveToTrash();
    stats.duplicate++;
    return;
  }

  var extraction = extractAmount_(subject, bodyText);
  if (!extraction) {
    thread.addLabel(errorLabel);
    Logger.log(
      "Could not extract an amount for %s (thread %s)",
      sender.name,
      thread.getId()
    );
    stats.errored++;
    return;
  }

  var monthLabel = Utilities.formatDate(
    receivedAt,
    CONFIG.timezone || Session.getScriptTimeZone(),
    "MM-yyyy"
  );
  var split = buildSplit(CONFIG.payee.share, CONFIG.roommates, extraction.amountCents);

  var rows = split.rows.map(function (row) {
    var note = sender.name + " split " + monthLabel;
    return {
      dedupKey: dedupKey,
      biller: sender.name,
      month: monthLabel,
      totalCents: extraction.amountCents,
      label: row.label,
      amountCents: row.amountCents,
      venmoLink: buildPayLink(CONFIG.payee.venmoUsername, row.amountCents, note),
      threadId: thread.getId(),
      confidence: extraction.confidence,
    };
  });

  appendLedgerRows_(rows);
  postDiscordNotification_(
    sender.name,
    monthLabel,
    formatCents_(extraction.amountCents),
    rows,
    extraction.confidence
  );

  thread.addLabel(processedLabel);
  thread.moveToTrash();
  stats.processed++;
}

// Regex keyphrase match first; Gemini only runs when that fails. Biller name and
// month don't need Gemini - biller comes from the sender config that matched, month
// comes from when the email arrived.
function extractAmount_(subject, bodyText) {
  var text = subject + "\n" + bodyText;

  var byKeyphrase = extractAmountCentsByKeyphrase(text);
  if (byKeyphrase !== null) {
    return { amountCents: byKeyphrase, confidence: "high" };
  }

  var geminiAmountCents = callGeminiForAmount_(text);
  if (geminiAmountCents === null) {
    return null;
  }

  // Gemini's answer is only trusted at "high" confidence if it also appears
  // literally in the email text - guards against a reconstructed/estimated total.
  var confidence = amountAppears(text, geminiAmountCents) ? "high" : "low";
  return { amountCents: geminiAmountCents, confidence: confidence };
}

function formatCents_(cents) {
  return "$" + (cents / 100).toFixed(2);
}

function lookbackDays_() {
  var override = PropertiesService.getScriptProperties().getProperty("LOOKBACK_DAYS");
  var parsed = parseInt(override, 10);
  return parsed > 0 ? parsed : DEFAULT_LOOKBACK_DAYS;
}

function getOrCreateLabel_(name) {
  return GmailApp.getUserLabelByName(name) || GmailApp.createLabel(name);
}

function hasLabel_(thread, name) {
  return thread.getLabels().some(function (label) {
    return label.getName() === name;
  });
}

/** Install the recurring trigger. Run once from the editor. */
function setupTrigger() {
  ScriptApp.getProjectTriggers()
    .filter(function (trigger) {
      return trigger.getHandlerFunction() === TRIGGER_HANDLER;
    })
    .forEach(function (trigger) {
      ScriptApp.deleteTrigger(trigger);
    });

  ScriptApp.newTrigger(TRIGGER_HANDLER).timeBased().everyMinutes(TRIGGER_MINUTES).create();

  Logger.log("Installed a %s-minute trigger for %s().", TRIGGER_MINUTES, TRIGGER_HANDLER);
}

/**
 * Check the config and secrets without touching Gmail. Run this first after
 * pushing: it proves Config.gs and the Script Properties are set correctly.
 */
function testConnection() {
  Logger.log("Payee: %s (share %s, Venmo @%s)",
    CONFIG.payee.label, CONFIG.payee.share, CONFIG.payee.venmoUsername);
  Logger.log("Roommates: %s", CONFIG.roommates.map(function (r) {
    return r.label + " (share " + r.share + ")";
  }).join(", "));
  Logger.log("Senders: %s", CONFIG.senders.map(function (s) { return s.name; }).join(", "));

  var props = PropertiesService.getScriptProperties();
  ["GEMINI_API_KEY", "DISCORD_WEBHOOK_URL"].forEach(function (name) {
    Logger.log("%s: %s", name, props.getProperty(name) ? "set" : "MISSING");
  });
}

/**
 * Report what processNewBills() would pick up, without doing any of it. Sends
 * nothing to Gemini or Discord and applies no labels, so it's safe to run
 * repeatedly while you work out the right LOOKBACK_DAYS.
 */
function scanInbox() {
  var lookback = lookbackDays_();
  Logger.log(
    "Dry scan: %s sender(s), last %s day(s). Nothing will be sent or labeled.",
    CONFIG.senders.length,
    lookback
  );

  var total = 0;
  var alreadyDone = 0;

  CONFIG.senders.forEach(function (sender) {
    var query = "from:" + sender.fromAddress + " newer_than:" + lookback + "d";
    var threads = GmailApp.search(query);
    Logger.log("");
    Logger.log("%s <%s>: %s thread(s)", sender.name, sender.fromAddress, threads.length);

    threads.forEach(function (thread) {
      var messages = thread.getMessages();
      var message = messages[messages.length - 1];
      var done =
        hasLabel_(thread, CONFIG.behavior.processedLabel) ||
        hasLabel_(thread, CONFIG.behavior.errorLabel);
      if (done) {
        alreadyDone++;
      } else {
        total++;
      }
      Logger.log(
        "  %s  %s  \"%s\"%s",
        done ? "[done]" : "[new] ",
        Utilities.formatDate(message.getDate(), Session.getScriptTimeZone(), "yyyy-MM-dd"),
        message.getSubject(),
        done ? " (already labeled, would be skipped)" : ""
      );
    });
  });

  Logger.log("");
  Logger.log(
    "Would process %s new thread(s); %s already labeled and skipped.",
    total,
    alreadyDone
  );
  if (total === 0 && alreadyDone === 0) {
    Logger.log(
      "Nothing matched. Either widen the window (set a LOOKBACK_DAYS Script " +
        "Property) or check that your senders in Config.gs match the actual From " +
        "addresses on your bills."
    );
  }
}

/**
 * Remove bill-bot labels from every thread it has touched. Useful during a smoke
 * test: labels are one of the two things that stop an email being reprocessed, so
 * clearing them lets you run again. The other is the dedup row in the Ledger sheet -
 * delete the row by hand if you need to replay an email.
 */
function clearLabels() {
  [CONFIG.behavior.processedLabel, CONFIG.behavior.errorLabel].forEach(function (name) {
    var label = GmailApp.getUserLabelByName(name);
    if (!label) {
      Logger.log("%s: no such label", name);
      return;
    }
    var threads = label.getThreads();
    threads.forEach(function (thread) {
      thread.removeLabel(label);
    });
    Logger.log("%s: cleared from %s thread(s)", name, threads.length);
  });
}
