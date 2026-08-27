/**
 * bill-bot - a single self-contained Google Apps Script project.
 *
 * Scans Gmail for configured bill senders, extracts the total (regex keyphrase
 * match first, Gemini 2.5 Flash-Lite as a fallback for senders regex doesn't recognize),
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
  var stats = { processed: 0, duplicate: 0, ignored: 0, errored: 0, retryLater: 0 };

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
    "Done. processed=%s duplicate=%s ignored=%s errored=%s retryLater=%s",
    stats.processed,
    stats.duplicate,
    stats.ignored,
    stats.errored,
    stats.retryLater,
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
  if (extraction === undefined) {
    // Transient failure (Gemini overloaded, a network blip) - leave unlabeled so the
    // next scheduled run just tries again automatically, no manual step needed.
    Logger.log(
      "Temporary failure extracting an amount for %s (thread %s) - will retry next run",
      sender.name,
      thread.getId(),
    );
    stats.retryLater++;
    return;
  }
  if (extraction === null) {
    thread.addLabel(errorLabel);
    Logger.log(
      "Could not extract an amount for %s (thread %s)",
      sender.name,
      thread.getId(),
    );
    stats.errored++;
    return;
  }

  var monthLabel = Utilities.formatDate(
    receivedAt,
    CONFIG.timezone || Session.getScriptTimeZone(),
    "MM-yyyy",
  );
  var split = buildSplit(
    CONFIG.payee.share,
    CONFIG.roommates,
    extraction.amountCents,
  );

  var note = sender.name + " split " + monthLabel;
  var rows = split.rows.map(function (row) {
    return {
      dedupKey: dedupKey,
      biller: sender.name,
      month: monthLabel,
      totalCents: extraction.amountCents,
      label: row.label,
      amountCents: row.amountCents,
      venmoLink: buildPayLink(
        CONFIG.payee.venmoUsername,
        row.amountCents,
        note,
      ),
      threadId: thread.getId(),
      confidence: extraction.confidence,
    };
  });

  // Discord goes out before anything is recorded: it's an extra hop through
  // Cloudflare and more prone to a transient failure than Gmail/Sheets calls to
  // Google's own APIs. If it fails, bail out with nothing written or labeled, so the
  // next scheduled run retries this thread from scratch rather than silently missing
  // the notification for an already-recorded bill.
  var genericVenmoLink = buildGenericPayLink(CONFIG.payee.venmoUsername, note);
  var discordOk = postDiscordNotification_(
    sender.name,
    monthLabel,
    formatCents_(extraction.amountCents),
    rows,
    genericVenmoLink,
    extraction.confidence,
  );
  if (!discordOk) {
    Logger.log(
      "Discord notification failed for %s (thread %s) - will retry next run",
      sender.name,
      thread.getId(),
    );
    stats.retryLater++;
    return;
  }

  appendLedgerRows_(rows);
  thread.addLabel(processedLabel);
  thread.moveToTrash();
  stats.processed++;
}

// Regex keyphrase match first; Gemini only runs when that fails. Biller name and
// month don't need Gemini - biller comes from the sender config that matched, month
// comes from when the email arrived.
//
// Returns { amountCents, confidence } on success, null if extraction has genuinely
// failed (flag for a human), or undefined if Gemini's failure looks transient (leave
// the thread alone, it'll be retried automatically next scheduled run).
function extractAmount_(subject, bodyText) {
  var text = subject + "\n" + bodyText;

  var byKeyphrase = extractAmountCentsByKeyphrase(text);
  if (byKeyphrase !== null) {
    return { amountCents: byKeyphrase, confidence: "high" };
  }

  var geminiAmountCents = callGeminiForAmount_(text);
  if (geminiAmountCents === undefined) {
    return undefined;
  }
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
  var override =
    PropertiesService.getScriptProperties().getProperty("LOOKBACK_DAYS");
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

  ScriptApp.newTrigger(TRIGGER_HANDLER)
    .timeBased()
    .everyMinutes(TRIGGER_MINUTES)
    .create();

  Logger.log(
    "Installed a %s-minute trigger for %s().",
    TRIGGER_MINUTES,
    TRIGGER_HANDLER,
  );
}

/**
 * Check the config and secrets without touching Gmail. Run this first after
 * pushing: it proves Config.gs is set up correctly.
 */
function testConnection() {
  Logger.log(
    "Payee: %s (share %s, Venmo @%s)",
    CONFIG.payee.label,
    CONFIG.payee.share,
    CONFIG.payee.venmoUsername,
  );
  Logger.log(
    "Roommates: %s",
    CONFIG.roommates
      .map(function (r) {
        return r.label + " (share " + r.share + ")";
      })
      .join(", "),
  );
  Logger.log(
    "Senders: %s",
    CONFIG.senders
      .map(function (s) {
        return s.name;
      })
      .join(", "),
  );

  var props = PropertiesService.getScriptProperties();
  var secretSources = {
    "Gemini API key":
      CONFIG.secrets && CONFIG.secrets.geminiApiKey
        ? "Config.gs"
        : props.getProperty("GEMINI_API_KEY")
          ? "Script Property"
          : null,
    "Discord webhook URL":
      CONFIG.secrets && CONFIG.secrets.discordWebhookUrl
        ? "Config.gs"
        : props.getProperty("DISCORD_WEBHOOK_URL")
          ? "Script Property"
          : null,
  };
  Object.keys(secretSources).forEach(function (name) {
    Logger.log("%s: %s", name, secretSources[name] || "MISSING");
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
    lookback,
  );

  var total = 0;
  var alreadyDone = 0;

  CONFIG.senders.forEach(function (sender) {
    var query = "from:" + sender.fromAddress + " newer_than:" + lookback + "d";
    var threads = GmailApp.search(query);
    Logger.log("");
    Logger.log(
      "%s <%s>: %s thread(s)",
      sender.name,
      sender.fromAddress,
      threads.length,
    );

    threads.forEach(function (thread) {
      var messages = thread.getMessages();
      var message = messages[messages.length - 1];
      var errored = hasLabel_(thread, CONFIG.behavior.errorLabel);
      var processed = hasLabel_(thread, CONFIG.behavior.processedLabel);
      var done = processed || errored;
      if (done) {
        alreadyDone++;
      } else {
        total++;
      }
      var tag = "[new] ";
      var note = "";
      if (errored) {
        tag = "[error]";
        note =
          " (extraction failed last time - still in your inbox, no ledger row." +
          " Fix the cause, then clearLabels() to retry)";
      } else if (processed) {
        tag = "[done] ";
        note = " (already processed and trashed)";
      }
      Logger.log(
        '  %s  %s  "%s"%s',
        tag,
        Utilities.formatDate(
          message.getDate(),
          Session.getScriptTimeZone(),
          "yyyy-MM-dd",
        ),
        message.getSubject(),
        note,
      );
    });
  });

  Logger.log("");
  Logger.log(
    "Would process %s new thread(s); %s already labeled and skipped.",
    total,
    alreadyDone,
  );
  if (total === 0 && alreadyDone === 0) {
    Logger.log(
      "Nothing matched. Either widen the window (set a LOOKBACK_DAYS Script " +
        "Property) or check that your senders in Config.gs match the actual From " +
        "addresses on your bills.",
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
  [CONFIG.behavior.processedLabel, CONFIG.behavior.errorLabel].forEach(
    function (name) {
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
    },
  );
}
