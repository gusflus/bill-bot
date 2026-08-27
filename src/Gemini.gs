// Fallback extraction when no regex keyphrase matches. Only called for senders whose
// bill format the regex patterns in lib/extractRegex.js don't recognize.
//
// Returns a positive number of cents on success, null if the failure is permanent
// (bad API key, malformed response - retrying won't help, so the caller should flag
// the thread for a human), or undefined if the failure looks transient (Gemini
// overloaded, a network blip - the caller should leave the thread unlabeled instead,
// so the next scheduled run just tries again automatically).

// HTTP codes worth a short in-run retry before giving up as transient.
var GEMINI_RETRYABLE_CODES = [429, 500, 502, 503, 504];
var GEMINI_MAX_ATTEMPTS = 3;
var GEMINI_RETRY_DELAYS_MS = [2000, 5000]; // between attempts 1->2 and 2->3

function callGeminiForAmount_(text) {
  var apiKey =
    (CONFIG.secrets && CONFIG.secrets.geminiApiKey) ||
    PropertiesService.getScriptProperties().getProperty("GEMINI_API_KEY");
  if (!apiKey) {
    Logger.log(
      "No Gemini API key set (CONFIG.secrets.geminiApiKey or GEMINI_API_KEY " +
        "Script Property); cannot fall back to Gemini extraction.",
    );
    return null;
  }

  var url =
    "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.6-flash:generateContent?key=" +
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
              "balance, a minimum payment, or a late fee.\n\nEmail:\n\n" +
              text,
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

  var response = null;
  var transientFailure = false;

  for (var attempt = 0; attempt < GEMINI_MAX_ATTEMPTS; attempt++) {
    if (attempt > 0) {
      Utilities.sleep(GEMINI_RETRY_DELAYS_MS[attempt - 1]);
    }

    try {
      response = UrlFetchApp.fetch(url, {
        method: "post",
        contentType: "application/json",
        payload: JSON.stringify(payload),
        muteHttpExceptions: true,
      });
    } catch (e) {
      // A thrown exception from UrlFetchApp itself (DNS/connection failure) is
      // always transient - never the email's fault.
      Logger.log(
        "Gemini request failed (attempt %s/%s): %s",
        attempt + 1,
        GEMINI_MAX_ATTEMPTS,
        e,
      );
      transientFailure = true;
      response = null;
      continue;
    }

    var code = response.getResponseCode();
    if (code === 200) {
      transientFailure = false;
      break;
    }

    transientFailure = GEMINI_RETRYABLE_CODES.indexOf(code) !== -1;
    Logger.log(
      "Gemini returned HTTP %s (attempt %s/%s): %s",
      code,
      attempt + 1,
      GEMINI_MAX_ATTEMPTS,
      response.getContentText(),
    );
    if (!transientFailure) {
      // A 4xx other than 429 (bad API key, bad request) won't fix itself by retrying.
      return null;
    }
  }

  if (!response || response.getResponseCode() !== 200) {
    return transientFailure ? undefined : null;
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
