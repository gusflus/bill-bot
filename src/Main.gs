/**
 * The Gmail watcher. This is all that runs on Apps Script now.
 *
 * Search the senders the Lambda tells us about, forward matching emails, and
 * label the thread according to what the Lambda said. No parsing, no amounts,
 * no splitting, no payment links - all of that moved to AWS, where it can be
 * tested.
 *
 * Labelling is driven by response class, and the distinction matters:
 *
 *   2xx  handled (notified, ignored, duplicate)  -> Processed
 *   4xx  will never succeed (unknown sender)     -> Error
 *   5xx  might succeed later (Bedrock throttled) -> leave unlabeled, retry
 *
 * Leaving a 5xx thread unlabeled is the important case: the next run picks it
 * up again, so a transient outage delays a bill instead of losing it.
 *
 * Run setupTrigger() once from the editor to install the recurring trigger.
 */

var TRIGGER_HANDLER = 'processNewBills';
var TRIGGER_MINUTES = 30;

// Kept in step with behavior.lookback_days and the label names in config.yaml.
// Duplicated here because Apps Script can't read the YAML; the README says to
// change both.
var LOOKBACK_DAYS = 14;
var PROCESSED_LABEL = 'Bill-Bot/Processed';
var ERROR_LABEL = 'Bill-Bot/Error';

function processNewBills() {
  var senders = fetchSenders();
  if (!senders.length) {
    Logger.log('No senders configured. Add some to config.yaml and cdk deploy.');
    return;
  }

  var processedLabel = getOrCreateLabel_(PROCESSED_LABEL);
  var errorLabel = getOrCreateLabel_(ERROR_LABEL);
  var stats = { forwarded: 0, skipped: 0, errored: 0, retryLater: 0 };

  senders.forEach(function (sender) {
    var query =
      'from:' + sender.fromAddress + ' newer_than:' + LOOKBACK_DAYS + 'd';

    GmailApp.search(query).forEach(function (thread) {
      if (hasLabel_(thread, PROCESSED_LABEL) || hasLabel_(thread, ERROR_LABEL)) {
        return;
      }
      handleThread_(thread, sender, processedLabel, errorLabel, stats);
    });
  });

  Logger.log(
    'Done. forwarded=%s skipped=%s errored=%s retryLater=%s',
    stats.forwarded,
    stats.skipped,
    stats.errored,
    stats.retryLater
  );
}

function handleThread_(thread, sender, processedLabel, errorLabel, stats) {
  var messages = thread.getMessages();
  var message = messages[messages.length - 1];

  var payload = {
    messageId: message.getId(),
    threadId: thread.getId(),
    from: message.getFrom(),
    subject: message.getSubject(),
    receivedAt: message.getDate().toISOString(),
    bodyText: message.getPlainBody(),
  };

  var response;
  try {
    response = postBill(payload);
  } catch (err) {
    // A network failure or a missing Script Property. Treat as retryable and
    // leave the thread alone.
    Logger.log('Request failed for %s: %s', sender.name, err.message);
    stats.retryLater++;
    return;
  }

  var code = response.getResponseCode();
  var body = response.getContentText();

  if (code >= 200 && code < 300) {
    var action = parseAction_(body);
    Logger.log('%s: %s (%s)', sender.name, action, payload.messageId);
    thread.addLabel(processedLabel);
    if (action === 'new' || action === 'corrected') {
      stats.forwarded++;
    } else {
      stats.skipped++;
    }
    return;
  }

  if (code >= 400 && code < 500) {
    // Permanent: retrying forever would just burn quota every 30 minutes.
    Logger.log('%s: permanent failure %s - %s', sender.name, code, body);
    thread.addLabel(errorLabel);
    stats.errored++;
    return;
  }

  // 5xx and anything unexpected: leave unlabeled so the next run retries.
  Logger.log('%s: retryable failure %s - %s', sender.name, code, body);
  stats.retryLater++;
}

function parseAction_(body) {
  try {
    return JSON.parse(body).action || 'unknown';
  } catch (err) {
    return 'unknown';
  }
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
    'Installed a %s-minute trigger for %s().',
    TRIGGER_MINUTES,
    TRIGGER_HANDLER
  );
}

/**
 * Check the wiring without touching Gmail.
 *
 * Run this first after deploying: it proves the URL and secret are right, which
 * is the step most likely to be wrong.
 */
function testConnection() {
  var senders = fetchSenders();
  Logger.log('Connected. %s sender(s) configured:', senders.length);
  senders.forEach(function (sender) {
    Logger.log('  %s <%s>', sender.name, sender.fromAddress);
  });
}
