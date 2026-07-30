// Dedup via a Gmail label instead of a growing Script Properties list - it's
// self-limiting, and lets you see what's been processed directly in Gmail.

function getProcessedLabel_() {
  let label = GmailApp.getUserLabelByName(Config.PROCESSED_LABEL_NAME);
  if (!label) {
    label = GmailApp.createLabel(Config.PROCESSED_LABEL_NAME);
  }
  return label;
}

function markThreadProcessed(thread) {
  thread.addLabel(getProcessedLabel_());
}
