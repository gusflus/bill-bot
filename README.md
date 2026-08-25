# bill-bot

Watches Gmail for utility bill emails, splits each bill among your roommates by
weight, and texts everyone their share with a pre-filled Venmo link. Roommates
reply `PAID` to mark themselves settled.

Bedrock Claude Haiku reads each bill email, so there is **no setup wizard and no
per-sender configuration**. The only list you maintain is which email addresses
your bills come from.

Runs about **$2.30/month**, almost entirely the phone number rental.

## How it works

```
Gmail ──(every 30 min)──> Apps Script watcher
                                │  GET /senders     (what to search for)
                                │  POST /bills      (one email)
                                ▼
                          Ingest Lambda (Function URL)
                                │
                          Claude Haiku reads the email
                                │  amount, due date, bill type
                          regex sweep confirms the amount is really in the text
                                │
                          DynamoDB dedups on provider + month
                                │
                          weighted split, exact cents
                                ▼
                    AWS End User Messaging ──> each roommate's phone
                                                     │  "PAID" / "STATUS"
                                                     ▼
                                              SNS ──> Inbound Lambda
                                                          │
                                                    payment ledger
```

Apps Script does nothing but search, forward, and label. Everything else runs in
AWS where it can be tested — the suite is 511 tests and needs no AWS account.

### Extraction: Haiku reads, regex checks

Haiku returns the amount, due date, and a classification (`bill_due`,
`bill_upcoming`, `payment_confirmation`, `other`). A generic dollar-amount sweep
then confirms that the number it reported actually appears in the email.

That ordering is deliberate. There is no per-sender regex to extract with, so the
regex became the *validator*, guarding against a model reconstructing a total
instead of reading one. A validation miss lowers confidence but does not reject
the bill — some senders render the total as an image, and a false rejection would
silently lose a real bill.

### Dedup: Haiku classifies, DynamoDB decides

Bills are keyed `BILL#<provider>#<YYYY-MM>` and written with a conditional put,
so duplicate or concurrent runs cannot notify twice. The common "your bill is
coming soon" then "your bill is due" pair resolves like this:

| Second email says | Result |
|---|---|
| Same amount | Ignored; a due date is filled in if it supplies one |
| Different amount | The `bill_due` figure wins, record updated, correction texted |
| Same Gmail message id | Replay, ignored |

Two subtler cases the tests cover: a pair straddling a month boundary (soon Aug
31, due Sep 1) is recognized as one bill, and a flat-rate sender billing the
identical amount every month still gets a bill every month.

Dedup is deliberately deterministic rather than an LLM judgment call, so a re-run
can never disagree with the first run.

### Splitting

Shares are relative weights: all `1` is an even split, a `2` pays double a `1`.
They normalize against their own sum, so they never have to add up to anything.
Cents are allocated by largest remainder, so the amounts always sum to the bill
total exactly.

**You are not a receiver.** You paid the utility, so a text asking you to pay
yourself would be nonsense. But you carry a `payee.share` on the same scale as
everyone else, because your portion has to be in the denominator or the receivers
would be overcharged to cover a share nobody owes. Your portion is absorbed
rather than collected: you get no ledger row, and the bill record stores
`payer_amount` so `sum(ledger) + payer_amount` still reconciles to the total. Set
`payee.share: 0` if the receivers cover the whole bill.

Optionally, `payee.notify_on_payment: true` texts you each time someone marks
themselves paid, so you know money is coming without having to ask.

## Setup

### 1. Prerequisites

- An AWS account with credentials configured (`aws sts get-caller-identity`
  should succeed)
- [uv](https://docs.astral.sh/uv/getting-started/installation/) and Node
- Bedrock model access for Claude Haiku, enabled in your chosen region
  (Bedrock console → Model access)

```bash
uv sync
npm install
```

### 2. Configure

```bash
cp config.example.yaml config.yaml
```

Edit `config.yaml`: your roommates and their share weights, your Venmo handle,
and the addresses your bills come from. `config.example.yaml` documents every
setting inline. `config.yaml` is gitignored — it holds phone numbers.

`dry_run: true` is the default and should stay on for now.

### 3. Deploy

```bash
npx cdk bootstrap   # first time in this account/region only
npx cdk deploy
```

Deploy prints an SMS segment estimate per message template, warning if one is
expensive enough to be worth shortening:

```
SMS segment estimates (sample values, per recipient):
  bill       ~2 segment(s)  ~$0.02
  status     ~1 segment(s)  ~$0.01
  A bill notification to all 6 receivers costs roughly $0.12.
```

Note two stack outputs — Apps Script needs both:

```bash
npx cdk deploy --outputs-file outputs.json
# IngestUrl        -> the Function URL
# SharedSecretArn  -> read the value with:
aws secretsmanager get-secret-value --secret-id <arn> \
  --query SecretString --output text
```

### 4. Install the Gmail watcher

```bash
npx clasp login
npx clasp create --title "bill-bot" --type standalone --rootDir src
npx clasp push
```

In the Apps Script editor (`npx clasp open`) → Project Settings → Script
Properties, add:

| Property | Value |
|---|---|
| `API_BASE_URL` | the `IngestUrl` output |
| `API_SECRET` | the secret value you read above |

Then run `testConnection` from the function dropdown. It should log your
configured senders — that proves the URL and secret are right, which is the step
most likely to be wrong. Google will prompt for Gmail authorization the first
time.

Finally run `setupTrigger` once to install the 30-minute schedule.

### 5. Watch it in dry run

Run `processNewBills` manually, then read the ingest Lambda's CloudWatch logs.
You will see the exact text each roommate would receive:

```
[dry_run] would text +15551230001:
    SoCalGas bill received: $142.53 total.
    Your share, Gus: $23.76 - due Sep 11
    Pay: https://venmo.com/?txn=pay&...
```

**Stay here for a billing cycle.** Because there is no wizard that made you
eyeball a pattern up front, this is where you confirm Haiku is reading your
senders correctly. Check that the amounts match the real bills.

### 6. Get a phone number, then go live

US SMS requires a dedicated number — there are no shared origination identities,
and AWS Notify (the only no-number path) can only send pre-approved verification
templates, not custom text.

1. In the AWS End User Messaging console, request a **toll-free number** ($2/mo).
2. Submit **toll-free verification**. It asks for a business name, website, and
   opt-in description; a household bill splitter is an awkward fit, so expect to
   explain the use case. Verification is free but not instant.
3. Set an **SMS spend limit** deliberately. The account default monthly threshold
   is $1.00, uncomfortably close to real spend of about $0.30 — one buggy loop
   either blows through it or gets silently blocked. Five dollars is a sensible
   cap.
4. While verification is pending you can send to numbers added to the
   **sandbox verified destination** list (up to 10 — enough for a household).

Then put the number's ID in `config.yaml`, flip the flag, and redeploy:

```yaml
behavior:
  dry_run: false
messaging:
  origination_number_id: phone-abc123def456
```

```bash
npx cdk deploy
```

### 7. Enable replies

Two-way SMS needs one manual wiring step, because the phone number is
deliberately not a CloudFormation resource — a stack update that replaced or
released a number you waited on verification for would mean starting over.

Take the `InboundTopicArn` stack output and set it as the number's **two-way SMS**
destination in the End User Messaging console. Then text `PAID` from a roommate's
phone; the ledger flips and an acknowledgement comes back with the tally.
`STATUS` returns the tally on demand.

## Changing things later

| Change | What to run |
|---|---|
| Add or remove a bill sender | edit `config.yaml`, `npx cdk deploy` |
| Change a roommate or their share | edit `config.yaml`, `npx cdk deploy` |
| Reword a message | edit `config.yaml`, `npx cdk deploy` |
| Change the Gmail label names or lookback | edit `config.yaml` **and** `src/Main.gs`, then `npx cdk deploy` and `npx clasp push` |

Only the last one needs a `clasp push`. Sender addresses live in DynamoDB and
Apps Script fetches them at runtime, so adding a biller never touches the
watcher. The label names and lookback window are duplicated in `Main.gs` because
Apps Script can't read the YAML.

Removing a roommate from `config.yaml` prunes their config row on the next
deploy. Existing bills and payment history are untouched — deploys never write to
bill data.

## Optional: unpaid reminders

Off by default. Enable in `config.yaml`:

```yaml
reminders:
  enabled: true
  after_days: 3     # wait this long after the bill before the first nudge
  repeat_days: 3    # then at most once every this many days
  hour_utc: 17
```

Only people who haven't paid are ever contacted.

## Costs

| Item | Cost |
|---|---|
| Toll-free number | $2.00/month |
| SMS | ~$0.01 per message, so ~$0.30/month for ~30 |
| Bedrock Haiku | a fraction of a cent per email |
| Lambda, DynamoDB, SNS | within free tier at this volume |
| **Total** | **~$2.30/month** |

Confirm current rates on the [AWS End User Messaging pricing
page](https://aws.amazon.com/end-user-messaging/pricing/) rather than trusting
these figures.

MMS and RCS are deliberately unsupported: MMS costs 3× per message for media this
bot never sends, and RCS costs $500 up front plus $200/month in agent
maintenance.

## Security notes

Worth reading rather than skimming.

- **The Function URL is publicly reachable** with auth type `NONE`. The shared
  secret header is the only control. It's generated by Secrets Manager and
  compared with `hmac.compare_digest`, and the handler rejects oversized bodies
  before doing any work — but the Lambdas are plain, with no concurrency
  reservation, so nothing caps how much Bedrock spend a leaked URL could drive.
  If that matters to you, set an AWS Budgets alert on Bedrock. Rotate the secret
  by updating it in Secrets Manager and in Script Properties.
- **Email bodies are sent to Bedrock.** Utility bills contain account numbers and
  service addresses. Bodies are truncated before the call and bill records carry
  a TTL (`behavior.record_ttl_days`, default 400 days).
- **`PAID` is self-reported.** There is no payment API to verify against for
  personal Venmo or Zelle, so anyone texting from a roommate's number can mark
  that roommate paid. Fine for a household; not a payment system.
- **`config.yaml` is gitignored** because it holds phone numbers.
- IAM is scoped throughout: Bedrock to the one configured model, DynamoDB to the
  one table, SMS to the one origination number.
- The DynamoDB table is `RETAIN` on delete, so `cdk destroy` will not take the
  payment ledger with it.

### If you used an earlier version of this project

The Python setup wizard is gone, and with it the Google Cloud OAuth client it
needed. You can clean up:

```bash
rm -f credentials.json .setup-token.json
```

Then revoke that OAuth client in the [Google Cloud
Console](https://console.cloud.google.com/apis/credentials). The bot's Gmail
access now comes only from Apps Script's built-in authorization.

## Notes

- **Venmo**: there is no programmatic charge API for personal accounts, so
  messages carry a pre-filled `venmo.com/?txn=pay&...` link. Tapping it opens
  Venmo with the amount, recipient, and comment ready to send; nothing is charged
  automatically. The comment comes from `messages.venmo_note` and defaults to
  `"{biller} bill split"`, so it lands in both parties' Venmo history naming the
  utility. A longer note makes the link longer, which can push a text into
  another billable segment — `cdk deploy` reports the segment count.
- **Zelle**: publishes no deep-link or request URL scheme at all, so the Zelle
  line can only state the handle and amount as text. Omit `payee.zelle_contact`
  to leave it out.
- **Low confidence**: when Haiku's amount can't be found in the email text, the
  bill is still sent, and the low confidence is recorded on the bill record and in
  the Lambda logs. Set `behavior.on_low_confidence: hold` to record the bill and
  text nobody instead — but note that a held bill is a silently missed bill unless
  you're watching.

## Development

```bash
uv run pytest              # 511 tests, no AWS account needed
npx cdk synth              # validate infrastructure
npx cdk diff               # what a deploy would change
```

Layout:

```
app.py                  CDK entry point, prints segment cost estimates
config.example.yaml     every setting, documented
infra/
  config.py             config loader and all validation
  bill_bot_stack.py     composes the constructs
  constructs/           state_table, ingest_api, messaging
lambdas/
  shared/python/        extract, validate, shares, render, store, sender
  ingest/               Function URL handler
  inbound/              PAID / STATUS replies
  reminders/            optional nudges
  seed_config/          config -> DynamoDB custom resource
src/                    Apps Script watcher (JS, required by the platform)
tests/                  including synthetic bill fixtures
```

Shared modules sit under `lambdas/shared/python/` rather than `lambdas/shared/`
because that is what makes them importable as a Lambda layer — layer content only
reaches `sys.path` when it lives under `python/`. Flattening it breaks every
Lambda at runtime while the unit tests keep passing, so there's a test guarding
the structure.

Extraction can be run against a saved email body without deploying:

```bash
cd lambdas/shared/python
uv run python -m extract ../../../tests/fixtures/pge.txt
uv run python -m extract ../../../tests/fixtures/pge.txt --offline  # regex only
```
