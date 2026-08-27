# bill-bot

Watches Gmail for utility bill emails and automatically splits them among your roommates by configurable weight. Posts Discord notifications with pre-filled Venmo links, and tracks payments in a Google Sheet. Runs entirely on Google Apps Script—no server, no deploy complexity, no cost beyond occasional Gemini API calls.

## Setup

1. **Prerequisites**
   - Google account with Gmail and Drive
   - [Node.js](https://nodejs.org/)
   - [Gemini API key](https://aistudio.google.com/apikey) (free tier works)
   - Discord webhook URL (Server Settings → Integrations → Webhooks)

2. **Install and configure**
   ```bash
   npm install
   cp src/Config.example.gs src/Config.gs
   ```
   Edit `src/Config.gs` with your roommates, share weights, Venmo handle, and bill sender addresses.

3. **Deploy to Apps Script**
   ```bash
   npx clasp login
   npx clasp create --title "bill-bot" --type standalone --rootDir src
   npx clasp push
   ```
   In the Apps Script editor (`npx clasp open`) → Project Settings → Script Properties, add:
   - `GEMINI_API_KEY`: your API key
   - `DISCORD_WEBHOOK_URL`: your webhook URL

4. **Test**
   Run `testConnection` in the Apps Script editor to verify access.
   ```bash
   npx clasp open
   ```

5. **Activate**
   Run `setupTrigger` in the Apps Script editor to start the 30-minute scan. The script creates a ledger sheet on its first run.

## Development

```bash
npm test                                               # run tests
node dev/preview.js <fixture> --biller X --subject Y  # offline extraction check
npx clasp push                                         # deploy to Apps Script
```
