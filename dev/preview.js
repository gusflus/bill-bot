#!/usr/bin/env node
// Runs a saved bill fixture through the same regex extraction, split, and Venmo
// link logic the Apps Script project uses (src/lib/*.js), fully offline - no Gemini
// call, no deployed script, no Google account needed. Confirms the amounts
// reconcile before you ever push real code.
//
// Usage:
//   node dev/preview.js tests/fixtures/pge.txt --biller "PG&E" --subject "Your PG&E energy statement is ready"
//
// Reads roommates/payee from Config.gs if it exists (falls back to Config.example.gs
// so this works before you've set up your own household).

const fs = require("node:fs");
const path = require("node:path");

const { extractAmountCentsByKeyphrase } = require("../src/lib/extractRegex.js");
const { buildSplit } = require("../src/lib/shares.js");
const { buildPayLink } = require("../src/lib/venmo.js");

function loadConfig() {
  const real = path.join(__dirname, "..", "src", "Config.gs");
  const example = path.join(__dirname, "..", "src", "Config.example.gs");
  const configPath = fs.existsSync(real) ? real : example;
  const source = fs.readFileSync(configPath, "utf8");
  // Config.*.gs is a plain `var CONFIG = {...}; if (typeof module ...)` file - strip
  // the GAS-only wrapper and eval the object literal directly rather than requiring
  // callers to keep a parallel JSON file in sync.
  const moduleShim = { exports: {} };
  const fn = new Function("module", "exports", source);
  fn(moduleShim, moduleShim.exports);
  return moduleShim.exports.CONFIG;
}

function formatCents(cents) {
  return "$" + (cents / 100).toFixed(2);
}

function main() {
  const args = process.argv.slice(2);
  const fixturePath = args[0];
  if (!fixturePath) {
    console.error("usage: node dev/preview.js <fixture-file> [--biller NAME] [--subject TEXT]");
    process.exit(1);
  }

  const billerFlagIndex = args.indexOf("--biller");
  const subjectFlagIndex = args.indexOf("--subject");
  const biller = billerFlagIndex !== -1 ? args[billerFlagIndex + 1] : "Test Biller";
  const subject = subjectFlagIndex !== -1 ? args[subjectFlagIndex + 1] : "";

  const body = fs.readFileSync(fixturePath, "utf8");
  const text = subject + "\n" + body;

  const amountCents = extractAmountCentsByKeyphrase(text);
  if (amountCents === null) {
    console.log(
      "No regex keyphrase matched - this bill would fall through to the Gemini " +
        "fallback in production. preview.js only exercises the offline regex path."
    );
    process.exit(1);
  }

  const config = loadConfig();
  const split = buildSplit(config.payee.share, config.roommates, amountCents);
  const month = "preview";

  console.log(`BILL  ${biller} ${month}`);
  console.log(`      total ${(amountCents / 100).toFixed(2)}`);
  console.log(`      ${config.payee.label} absorbs ${(split.payerAmountCents / 100).toFixed(2)} (payee.share ${config.payee.share})`);
  console.log("");

  let rowSum = 0;
  split.rows.forEach((row) => {
    rowSum += row.amountCents;
    const note = `${biller} split ${month}`;
    const link = buildPayLink(config.payee.venmoUsername, row.amountCents, note);
    console.log(`  ${row.label.padEnd(10)} ${formatCents(row.amountCents)}`);
    console.log(`  ${" ".repeat(10)} ${link}`);
  });

  console.log("");
  console.log(`roommates in config : ${config.roommates.length}`);
  console.log(`sum of shares       : ${(rowSum / 100).toFixed(2)}`);
  console.log(`payer absorbs       : ${(split.payerAmountCents / 100).toFixed(2)}`);
  console.log(`total accounted for : ${((rowSum + split.payerAmountCents) / 100).toFixed(2)}   (bill was ${(amountCents / 100).toFixed(2)})`);
  console.log("");

  const accounted = rowSum + split.payerAmountCents;
  if (accounted < amountCents) {
    console.error("MISMATCH - shares plus payer amount fall short of the bill total.");
    process.exit(1);
  }

  if (accounted > amountCents) {
    console.log(
      `OK - every roommate has a link. Equal-share roommates are bumped up to the ` +
        `same cent amount, so this run overcollects by ${formatCents(accounted - amountCents)} ` +
        `(the payee keeps the difference) rather than leaving anyone a cent short.`
    );
  } else {
    console.log("OK - every roommate has a link, and the amounts reconcile to the bill total exactly.");
  }
}

main();
