// Plain incoming webhook - a notification only. No bot, no reactions, no message
// editing. The Ledger sheet (Ledger.gs) is the source of truth for who's paid.

function postDiscordNotification_(billerName, monthLabel, totalFormatted, rows, confidence) {
  var webhookUrl = PropertiesService.getScriptProperties().getProperty("DISCORD_WEBHOOK_URL");
  if (!webhookUrl) {
    Logger.log("DISCORD_WEBHOOK_URL is not set; skipping Discord notification.");
    return;
  }

  var roommateFields = rows.map(function (row) {
    return {
      name: row.label,
      value: formatCents_(row.amountCents) + " — [Pay on Venmo](" + row.venmoLink + ")",
      inline: true,
    };
  });

  var embed = {
    title: (confidence === "low" ? "⚠️ " : "⚡ ") + billerName + " bill split",
    color: confidence === "low" ? 15158332 : 3447003,
    fields: [
      { name: "Total", value: totalFormatted, inline: true },
      { name: "Month", value: monthLabel, inline: true },
    ]
      .concat(roommateFields)
      .concat([{ name: "Ledger", value: "[Open the sheet](" + ledgerSheetUrl_() + ")" }]),
    footer: {
      text: confidence === "low" ? "Low confidence - double check the amount" : "bill-bot",
    },
  };

  UrlFetchApp.fetch(webhookUrl, {
    method: "post",
    contentType: "application/json",
    payload: JSON.stringify({ embeds: [embed] }),
    muteHttpExceptions: true,
  });
}
