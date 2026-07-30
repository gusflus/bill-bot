// Venmo has no programmatic charge API for personal accounts anymore, so the
// best we can do is a pre-filled link: whoever taps it gets Venmo open with
// a payment to Config.VENMO_USERNAME for the right amount, ready to send.

function buildVenmoLink(amountPerPerson, note) {
  if (!Config.VENMO_USERNAME) return null;

  const params = {
    txn: 'pay',
    audience: 'private',
    recipients: Config.VENMO_USERNAME,
    amount: amountPerPerson.toFixed(2),
    note: note,
  };

  const query = Object.keys(params)
    .map((key) => encodeURIComponent(key) + '=' + encodeURIComponent(params[key]))
    .join('&');

  return 'https://venmo.com/?' + query;
}
