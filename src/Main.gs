// Entry point. setupTrigger() installs the recurring trigger (run once from
// the Apps Script editor); processNewBills() is what that trigger calls.

function processNewBills() {
  const label = getProcessedLabel_();

  SENDERS.forEach(function (sender) {
    const query = 'from:' + sender.fromAddress + ' newer_than:' + Config.LOOKBACK_DAYS + 'd';
    const threads = GmailApp.search(query);

    threads.forEach(function (thread) {
      if (threadHasLabel_(thread, label)) return;
      processThread_(thread, sender);
    });
  });
}

function threadHasLabel_(thread, label) {
  return thread.getLabels().some(function (l) {
    return l.getName() === label.getName();
  });
}

function processThread_(thread, sender) {
  const messages = thread.getMessages();
  const latestMessage = messages[messages.length - 1];
  const bodyText = latestMessage.getPlainBody();

  const amount = extractAmount(sender, bodyText);
  if (amount === null) {
    Logger.log('No amount found for sender %s in thread %s, skipping.', sender.name, thread.getId());
    markThreadProcessed(thread);
    return;
  }

  const perPerson = Math.round((amount / Config.ROOMMATE_COUNT) * 100) / 100;
  const venmoLink = buildVenmoLink(perPerson, sender.name + ' bill split');

  notifyAll(formatMessage_(sender, amount, perPerson, venmoLink));
  markThreadProcessed(thread);
}

function formatMessage_(sender, totalAmount, perPerson, venmoLink) {
  const lines = [
    '💡 New bill from ' + sender.name + ': $' + totalAmount.toFixed(2),
    'Split ' + Config.ROOMMATE_COUNT + ' ways: $' + perPerson.toFixed(2) + ' each',
  ];
  if (venmoLink) {
    lines.push('Pay your share: ' + venmoLink);
  }
  return lines.join('\n');
}

function setupTrigger() {
  ScriptApp.getProjectTriggers()
    .filter(function (t) {
      return t.getHandlerFunction() === 'processNewBills';
    })
    .forEach(function (t) {
      ScriptApp.deleteTrigger(t);
    });

  ScriptApp.newTrigger('processNewBills').timeBased().everyMinutes(30).create();

  Logger.log('Installed a 30-minute trigger for processNewBills().');
}
