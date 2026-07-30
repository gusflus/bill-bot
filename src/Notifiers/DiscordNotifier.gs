// Posts to a Discord channel via an incoming webhook. Create one under
// Channel Settings > Integrations > Webhooks, then store the URL in
// Script Properties as DISCORD_WEBHOOK_URL (Project Settings > Script
// Properties in the Apps Script editor) - never commit it to source.

const DiscordNotifier = {
  send: function (text) {
    const webhookUrl = PropertiesService.getScriptProperties().getProperty('DISCORD_WEBHOOK_URL');
    if (!webhookUrl) {
      throw new Error('DISCORD_WEBHOOK_URL is not set in Script Properties.');
    }

    UrlFetchApp.fetch(webhookUrl, {
      method: 'post',
      contentType: 'application/json',
      payload: JSON.stringify({ content: text }),
    });
  },
};
