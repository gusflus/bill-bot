// Fallback extraction when no regex keyphrase matches. Only called for senders whose
// bill format the regex patterns in lib/extractRegex.js don't recognize.

function callGeminiForAmount_(text) {
  var apiKey =
    (CONFIG.secrets && CONFIG.secrets.geminiApiKey) ||
    PropertiesService.getScriptProperties().getProperty("GEMINI_API_KEY");
  if (!apiKey) {
    Logger.log(
      "No Gemini API key set (CONFIG.secrets.geminiApiKey or GEMINI_API_KEY " +
        "Script Property); cannot fall back to Gemini extraction."
    );
    return null;
  }

  var url =
    "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key=" +
    encodeURIComponent(apiKey);

  var payload = {
    contents: [
      {
        parts: [
          {
            text:
              "Read this utility bill email and report the total amount the customer " +
              "must pay now, as a plain number of dollars and cents (e.g. 142.53). Do " +
              "not calculate, estimate, or guess - copy the figure as printed. If " +
              "several figures appear, choose the total due now, not a previous " +
              "balance, a minimum payment, or a late fee.\n\nEmail:\n\n" + text,
          },
        ],
      },
    ],
    generationConfig: {
      responseMimeType: "application/json",
      responseSchema: {
        type: "OBJECT",
        properties: { total_amount: { type: "NUMBER" } },
        required: ["total_amount"],
      },
    },
  };

  var response;
  try {
    response = UrlFetchApp.fetch(url, {
      method: "post",
      contentType: "application/json",
      payload: JSON.stringify(payload),
      muteHttpExceptions: true,
    });
  } catch (e) {
    Logger.log("Gemini request failed: %s", e);
    return null;
  }

  if (response.getResponseCode() !== 200) {
    Logger.log("Gemini returned HTTP %s: %s", response.getResponseCode(), response.getContentText());
    return null;
  }

  try {
    var body = JSON.parse(response.getContentText());
    var content = body.candidates[0].content.parts[0].text;
    var parsed = JSON.parse(content);
    if (typeof parsed.total_amount !== "number") return null;
    return Math.round(parsed.total_amount * 100);
  } catch (e) {
    Logger.log("Could not parse Gemini response: %s", e);
    return null;
  }
}
