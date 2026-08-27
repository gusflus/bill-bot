// Plain incoming webhook - a notification only. No bot, no reactions, no message
// editing. Called *before* the ledger row/label/trash - Discord (an extra hop through
// Cloudflare) is more likely to hiccup than Gmail/Sheets calls to Google's own APIs,
// so a failure here should hold up recording the bill at all, not just the ping. The
// caller retries the whole thread next scheduled run if this returns false.
//
// Returns true if it's safe to proceed (message sent, or no webhook configured at
// all - that's a deliberate choice, not a failure, and must never block bills from
// being recorded). Returns false only when a webhook IS configured but every attempt
// to reach it failed.
var DISCORD_MAX_ATTEMPTS = 2;
var DISCORD_RETRY_DELAY_MS = 4000;

function postDiscordNotification_(
  billerName,
  monthLabel,
  totalFormatted,
  rows,
  confidence
) {
  var webhookUrl =
    (CONFIG.secrets && CONFIG.secrets.discordWebhookUrl) ||
    PropertiesService.getScriptProperties().getProperty("DISCORD_WEBHOOK_URL");
  if (!webhookUrl) {
    Logger.log(
      "No Discord webhook URL set (CONFIG.secrets.discordWebhookUrl or " +
        "DISCORD_WEBHOOK_URL Script Property); skipping Discord notification."
    );
    return true;
  }

  // One "Share" field per distinct amount, each with its own pre-filled Venmo link -
  // just one field in the common case where every roommate owes the same amount, but
  // this still shows each amount (and its own correct link) separately if an uneven
  // share weight ever makes roommates owe different amounts. Every row in a group has
  // an identical venmoLink already (same amount + same note), so the first is reused.
  var uniqueAmounts = [];
  rows.forEach(function (row) {
    if (uniqueAmounts.indexOf(row.amountCents) === -1) {
      uniqueAmounts.push(row.amountCents);
    }
  });
  var shareFields = uniqueAmounts.map(function (amount) {
    var matching = rows.filter(function (row) {
      return row.amountCents === amount;
    });
    var name = uniqueAmounts.length === 1 ? "Share" : "Share (" + matching.map(function (row) {
      return row.label;
    }).join(", ") + ")";
    return {
      name: name,
      value: formatCents_(amount) + " — [Pay on Venmo](" + matching[0].venmoLink + ")",
      inline: true,
    };
  });

  var embed = {
    title: billerName + " bill split",
    color: confidence === "low" ? 15158332 : 3447003,
    fields: [
      { name: "Total", value: totalFormatted, inline: true },
      { name: "Month", value: monthLabel, inline: true },
    ]
      .concat(shareFields)
      .concat([{ name: "Ledger", value: "[Open the sheet](" + ledgerSheetUrl_() + ")" }]),
    footer: {
      text: confidence === "low" ? "Low confidence - double check the amount" : "bill-bot",
    },
  };

  var payload = JSON.stringify({ embeds: [embed] });

  for (var attempt = 0; attempt < DISCORD_MAX_ATTEMPTS; attempt++) {
    if (attempt > 0) {
      Utilities.sleep(DISCORD_RETRY_DELAY_MS);
    }

    var response;
    try {
      response = UrlFetchApp.fetch(webhookUrl, {
        method: "post",
        contentType: "application/json",
        payload: payload,
        muteHttpExceptions: true,
      });
    } catch (e) {
      Logger.log(
        "Discord webhook request failed (attempt %s/%s): %s",
        attempt + 1,
        DISCORD_MAX_ATTEMPTS,
        e
      );
      continue;
    }

    var code = response.getResponseCode();
    if (code >= 200 && code < 300) {
      return true;
    }

    Logger.log(
      "Discord webhook returned HTTP %s (attempt %s/%s): %s",
      code,
      attempt + 1,
      DISCORD_MAX_ATTEMPTS,
      response.getContentText()
    );
  }

  return false;
}
