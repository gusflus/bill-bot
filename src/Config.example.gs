// Copy this to src/Config.gs and fill in your own values.
// src/Config.gs is gitignored - it's your household's settings, not
// something that belongs in a shared/public repo.

const Config = {
  ROOMMATE_COUNT: 6,

  // Names must match keys registered in Notifiers/NotifierRegistry.gs.
  // Multiple notifiers can be active at once.
  ACTIVE_NOTIFIERS: ['discord'],

  // How far back to search on each run. Wide enough to not miss a bill if a
  // trigger run fails, narrow enough to not re-scan your whole inbox.
  LOOKBACK_DAYS: 14,

  // Gmail label applied to threads once processed, used for dedup instead of
  // a growing Script Properties list.
  PROCESSED_LABEL_NAME: 'Bill-Bot/Processed',

  // Venmo username that should receive each roommate's share. Set this to
  // your own Venmo @handle.
  VENMO_USERNAME: '',
};
