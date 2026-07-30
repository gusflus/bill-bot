# bill-bot

Watches Gmail for utility bill emails, splits the amount among your roommates,
and posts the split (with a Venmo request link) to a group chat.

The bot itself runs on **Google Apps Script** - free, no server, and it uses
Apps Script's own built-in Gmail access so it needs no OAuth setup of its own.
The **setup wizard** (for teaching it a new sender's bill format) is a local
Python script.

## How it works

- `SENDERS` (`src/SendersConfig.gs`, generated - see below) lists the bill
  senders to watch and how to pull the dollar amount out of each one's email.
- Every 30 minutes, `processNewBills()` checks each sender for new emails,
  extracts the amount, divides it by `Config.ROOMMATE_COUNT`, and posts the
  result through every notifier listed in `Config.ACTIVE_NOTIFIERS`
  (`src/Notifiers/`) - Discord to start, more can be added later as new files
  implementing `{ send(text) }`.
- Processed threads get a `Bill-Bot/Processed` Gmail label so nothing is ever
  double-charged.

## One-time setup

### 1. Create the Apps Script project

```
npm install          # installs clasp, the Apps Script CLI
npx clasp login
npx clasp create --title "bill-bot" --type standalone --rootDir src
cp .clasp.json.example .clasp.json   # then paste the scriptId clasp just gave you
npx clasp push
```

### 2. Configure secrets

In the Apps Script editor (`npx clasp open`) → Project Settings → Script
Properties, add:

| Property | Value |
|---|---|
| `DISCORD_WEBHOOK_URL` | A Discord webhook URL (Channel Settings → Integrations → Webhooks) |

### 3. Configure `src/Config.gs`

Set `ROOMMATE_COUNT` and `VENMO_USERNAME` (your own Venmo @handle - it's who
gets paid). Both are safe to commit; they're not secrets.

### 4. Install the trigger

In the Apps Script editor, run `setupTrigger` once (select it from the
function dropdown, click Run). This installs the recurring 30-minute trigger
that calls `processNewBills`. The first time you run anything, Google will
prompt you to authorize the script's Gmail access - that's expected.

### 5. Add your bill senders

This is the part that needs real sample emails, so it runs locally with
Python rather than inside Apps Script.

```
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp senders.config.example.json senders.config.json
```

`senders.config.json` and the `src/SendersConfig.gs` generated from it are
gitignored - they're your personal bill-sender list, not something that
belongs in a shared repo.

You'll need a Google Cloud OAuth client for the wizard to read your Gmail
(read-only) while picking a pattern - this is separate from the bot's own
Apps Script access:

1. In [Google Cloud Console](https://console.cloud.google.com/), create a
   project (or reuse one), enable the **Gmail API**, and create an OAuth
   client ID of type **Desktop app**.
2. Download it as `credentials.json` into the repo root.

Then, for each utility sender:

```
python setup/wizard.py --sender billpay.pge.com --name "PG&E"
```

The wizard fetches a few recent emails from that address, tries a battery of
common bill-amount patterns against them, shows you what each one caught
(and how consistently across samples), and lets you pick the right one or
type a custom regex. It saves the result to `senders.config.json`.

After adding a sender:

```
python build/generate_senders_config.py   # senders.config.json -> src/SendersConfig.gs
npx clasp push
```

### 6. Test it

In the Apps Script editor, run `processNewBills` manually and check your
Discord channel. Run it again immediately after - it should not post a
second time for the same bill (that's the `Bill-Bot/Processed` label at
work).

## Adding a notifier

Add a file under `src/Notifiers/` exporting `{ send(text) }` (see
`DiscordNotifier.gs`), register it by name in `NotifierRegistry.gs`, and add
that name to `Config.ACTIVE_NOTIFIERS`. Multiple notifiers can be active at
once.

## Notes

- **Venmo**: there's no programmatic charge API for personal Venmo accounts
  anymore, so notifications include a pre-filled `venmo.com/?txn=pay&...`
  link instead - tapping it opens Venmo ready to send, but nothing is
  charged automatically.
- **Rounding**: `amount / ROOMMATE_COUNT` is rounded to the cent per person;
  the total collected may be off by a few cents from the actual bill. Not
  worth solving for a household split.
