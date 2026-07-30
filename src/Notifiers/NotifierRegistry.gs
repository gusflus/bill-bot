// Every notifier implements { send(text) }. Add a new one (Slack, SMS, ...)
// by writing a new file with that shape and registering it here - nothing
// else in the project needs to change.

const NotifierRegistry = {
  discord: DiscordNotifier,
};

function getActiveNotifiers_() {
  return Config.ACTIVE_NOTIFIERS.map((name) => {
    const notifier = NotifierRegistry[name];
    if (!notifier) {
      throw new Error('Unknown notifier in Config.ACTIVE_NOTIFIERS: ' + name);
    }
    return notifier;
  });
}

function notifyAll(text) {
  getActiveNotifiers_().forEach((notifier) => notifier.send(text));
}
