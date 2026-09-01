# bill-bot

Watches Gmail for utility bill emails and automatically splits them among your roommates by configurable weight. Posts Discord notifications with pre-filled Venmo links, and tracks payments in a Google Sheet. Runs entirely on Google Apps Script—no server, no deploy complexity, no cost beyond occasional Gemini API calls.

## Setup

1. **Prerequisites**
   - Google account with Gmail and Drive
   - [Node.js](https://nodejs.org/)
   - [Gemini API key](https://aistudio.google.com/apikey) (free tier works)
   - Discord webhook URL (Server Settings → Integrations → Webhooks → New Webhook →
     copy the URL). Point it at a channel only you can see for DM-like behavior — no
     bot, no bot token, no extra setup: create a private channel in any server you're
     in (or a throwaway server with just you in it)

2. **Install and configure**

   ```bash
   npm install
   cp src/Config.example.gs src/Config.gs
   ```

   Edit `src/Config.gs` with your roommates, share weights, Venmo handle, bill sender
   addresses, and your Gemini API key + Discord webhook URL (in the `secrets` block at
   the top). `Config.gs` is gitignored, so none of this leaves your machine — just
   don't paste the file's contents somewhere public. Leave a secret blank to fall back
   to a `GEMINI_API_KEY` / `DISCORD_WEBHOOK_URL` Script Property instead, if you'd
   rather keep keys out of the file.

3. **Deploy to Apps Script**

   ```bash
   npx clasp login
   npx clasp create --title "bill-bot" --type standalone --rootDir src
   cp src/.clasp.json .clasp.json
   npx clasp push
   ```

4. **Test**
   Run `testConnection` in the Apps Script editor to verify access.

   ```bash
   npx clasp open
   ```

5. **Activate**
   Run `setupTrigger` in the Apps Script editor (click on the `Main.gs` file, at the top select `setupTrigger.gs` from the dropdown next to the `Debug` button, then click `Run`) to start the once-every-30-minute scan. Once the popup appears, click "Review Permissions" and authorize the script to access your Gmail and Drive. If you get a "Google hasn’t verified this app" warning, click "Advanced" → "Go to bill-bot (unsafe)" to proceed. Then select all the permissions and click "Allow".
