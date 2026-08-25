/**
 * Talking to the bill-bot ingest Lambda.
 *
 * Two Script Properties are required (Project Settings > Script Properties):
 *
 *   API_BASE_URL  the Function URL from the IngestUrl stack output
 *   API_SECRET    the shared secret from the SharedSecretArn stack output
 *
 * Neither belongs in source control, which is why they live in Script
 * Properties rather than a config file.
 */

var SECRET_HEADER = 'x-bill-bot-secret';

function getScriptProperty_(name) {
  var value = PropertiesService.getScriptProperties().getProperty(name);
  if (!value) {
    throw new Error(
      name +
        ' is not set in Script Properties. See the README - it comes from a ' +
        '`cdk deploy` output.'
    );
  }
  return value;
}

function apiUrl_(path) {
  // Function URLs come with a trailing slash; avoid a double slash.
  return getScriptProperty_('API_BASE_URL').replace(/\/$/, '') + path;
}

/**
 * Fetch the bill senders to watch.
 *
 * The list lives in DynamoDB, seeded from config.yaml by `cdk deploy`, so
 * adding a biller needs no `clasp push` - only a deploy.
 */
function fetchSenders() {
  var response = UrlFetchApp.fetch(apiUrl_('/senders'), {
    method: 'get',
    headers: buildHeaders_(),
    muteHttpExceptions: true,
  });

  var code = response.getResponseCode();
  if (code !== 200) {
    throw new Error(
      'GET /senders failed with ' + code + ': ' + response.getContentText()
    );
  }
  return JSON.parse(response.getContentText()).senders || [];
}

/**
 * Post one email for processing.
 *
 * Returns the raw response so the caller can label the thread by status class.
 * HTTP errors are muted deliberately: a non-2xx is information we act on, not
 * an exception to escape the loop and abandon the remaining threads.
 */
function postBill(payload) {
  return UrlFetchApp.fetch(apiUrl_('/bills'), {
    method: 'post',
    contentType: 'application/json',
    headers: buildHeaders_(),
    payload: JSON.stringify(payload),
    muteHttpExceptions: true,
  });
}

function buildHeaders_() {
  var headers = {};
  headers[SECRET_HEADER] = getScriptProperty_('API_SECRET');
  return headers;
}
