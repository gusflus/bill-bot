// Extracts a dollar amount from an email body using the sender's configured
// matchType (set by setup/wizard.py, see senders.config.json / SendersConfig.gs).
//
// 'regex'            - sender.regexSource applied to the body, group 1 is the amount.
// 'generic-first'     - first "$X.XX" found anywhere in the body.
// 'generic-last'      - last "$X.XX" found anywhere in the body.
// 'generic-largest'   - largest "$X.XX" found anywhere in the body.

const ALL_AMOUNTS_PATTERN = /\$\s*([\d,]+\.\d{2})/g;

function findAllDollarAmounts_(text) {
  const amounts = [];
  let match;
  ALL_AMOUNTS_PATTERN.lastIndex = 0;
  while ((match = ALL_AMOUNTS_PATTERN.exec(text)) !== null) {
    amounts.push(parseFloat(match[1].replace(/,/g, '')));
  }
  return amounts;
}

function extractAmount(sender, bodyText) {
  if (sender.matchType === 'regex') {
    const pattern = new RegExp(sender.regexSource, 'i');
    const match = bodyText.match(pattern);
    if (!match) return null;
    return parseFloat(match[1].replace(/,/g, ''));
  }

  const amounts = findAllDollarAmounts_(bodyText);
  if (amounts.length === 0) return null;

  if (sender.matchType === 'generic-first') return amounts[0];
  if (sender.matchType === 'generic-last') return amounts[amounts.length - 1];
  if (sender.matchType === 'generic-largest') return Math.max.apply(null, amounts);

  throw new Error('Unknown matchType: ' + sender.matchType);
}
