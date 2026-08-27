// Plain incoming webhook - a notification only. No bot, no reactions, no message
// editing. The Ledger sheet (Ledger.gs) is the source of truth for who's paid.

function postDiscordNotification_(
  billerName,
  monthLabel,
  totalFormatted,
  rows,
  genericVenmoLink,
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
    return;
  }

  // One "Share" field per distinct amount - just one in the common case where every
  // roommate owes the same amount, but this still shows each amount separately if an
  // uneven share weight ever makes roommates owe different amounts.
  var uniqueAmounts = [];
  rows.forEach(function (row) {
    if (uniqueAmounts.indexOf(row.amountCents) === -1) {
      uniqueAmounts.push(row.amountCents);
    }
  });
  var shareFields =
    uniqueAmounts.length === 1
      ? [{ name: "Share", value: formatCents_(uniqueAmounts[0]) + " each", inline: true }]
      : uniqueAmounts.map(function (amount) {
          var labels = rows
            .filter(function (row) {
              return row.amountCents === amount;
            })
            .map(function (row) {
              return row.label;
            })
            .join(", ");
          return { name: "Share (" + labels + ")", value: formatCents_(amount), inline: true };
        });

  var embed = {
    title: billerName + " bill split",
    color: confidence === "low" ? 15158332 : 3447003,
    fields: [
      { name: "Total", value: totalFormatted, inline: true },
      { name: "Month", value: monthLabel, inline: true },
    ]
      .concat(shareFields)
      .concat([
        { name: "Venmo", value: "[Pay " + CONFIG.payee.label + "](" + genericVenmoLink + ")" },
        { name: "Ledger", value: "[Open the sheet](" + ledgerSheetUrl_() + ")" },
      ]),
    footer: {
      text: confidence === "low" ? "Low confidence - double check the amount" : "bill-bot",
    },
  };

  var response;
  try {
    response = UrlFetchApp.fetch(webhookUrl, {
      method: "post",
      contentType: "application/json",
      payload: JSON.stringify({ embeds: [embed] }),
      muteHttpExceptions: true,
    });
  } catch (e) {
    Logger.log("Discord webhook request failed: %s", e);
    return;
  }

  var code = response.getResponseCode();
  if (code < 200 || code >= 300) {
    Logger.log(
      "Discord webhook returned HTTP %s: %s",
      code,
      response.getContentText()
    );
  }
}
