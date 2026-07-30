#!/usr/bin/env python3
"""Interactive setup for a new bill sender.

Usage:
    python setup/wizard.py --sender billpay.pge.com

Fetches recent sample emails from that sender, tries a battery of candidate
patterns against each, shows what each one extracted, and lets you pick the
one that's actually right (or type your own). The result is appended to
senders.config.json, which build/generate_senders_config.py then turns into
src/SendersConfig.gs for the Apps Script bot to use.
"""
import argparse
import base64
import json
import os
import sys

from gmail_auth import get_gmail_service
from html_to_text import html_to_text
from regex_candidates import all_candidates, try_candidate, find_all_dollar_amounts

SENDERS_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "senders.config.json")
SAMPLE_COUNT = 3


def fetch_sample_bodies(service, sender_address, max_results=SAMPLE_COUNT):
    result = service.users().messages().list(
        userId="me", q=f"from:{sender_address}", maxResults=max_results
    ).execute()
    message_refs = result.get("messages", [])
    if not message_refs:
        raise SystemExit(f"No emails found from '{sender_address}'.")

    bodies = []
    for ref in message_refs:
        msg = service.users().messages().get(userId="me", id=ref["id"], format="full").execute()
        bodies.append(_extract_plaintext(msg["payload"]))
    return bodies


def _extract_plaintext(payload):
    html_parts = []
    plain_parts = []
    _walk_parts(payload, html_parts, plain_parts)
    if html_parts:
        return html_to_text("\n".join(html_parts))
    return "\n".join(plain_parts)


def _walk_parts(part, html_parts, plain_parts):
    mime_type = part.get("mimeType", "")
    body_data = part.get("body", {}).get("data")
    if body_data:
        decoded = base64.urlsafe_b64decode(body_data + "==").decode("utf-8", errors="replace")
        if mime_type == "text/html":
            html_parts.append(decoded)
        elif mime_type == "text/plain":
            plain_parts.append(decoded)
    for sub_part in part.get("parts", []) or []:
        _walk_parts(sub_part, html_parts, plain_parts)


def evaluate_candidates(bodies):
    """Returns [{candidate, per_sample: [result_or_None, ...]}, ...]"""
    rows = []
    for candidate in all_candidates():
        per_sample = [try_candidate(candidate, body) for body in bodies]
        rows.append({"candidate": candidate, "per_sample": per_sample})
    return rows


def print_candidate_table(rows, sample_count):
    print(f"\nTried {len(rows)} candidate patterns against {sample_count} sample email(s):\n")
    for i, row in enumerate(rows, start=1):
        label = row["candidate"]["label"]
        results = row["per_sample"]
        hits = sum(1 for r in results if r is not None)
        summary_bits = []
        for r in results:
            summary_bits.append(f"${r['amount_text']}" if r else "no match")
        consistency = f"{hits}/{sample_count} samples matched"
        print(f"  [{i}] {label:32s} {', '.join(summary_bits):30s} ({consistency})")
        if hits and results[0]:
            print(f"       context: \"...{results[0]['context']}...\"")
    print(f"  [c] Enter a custom regex")
    print(f"  [q] Quit without saving")


def prompt_choice(rows):
    while True:
        choice = input("\nWhich one is correct? ").strip().lower()
        if choice == "q":
            sys.exit(0)
        if choice == "c":
            return "custom"
        if choice.isdigit() and 1 <= int(choice) <= len(rows):
            return rows[int(choice) - 1]["candidate"]
        print("Not a valid choice, try again.")


def prompt_custom_regex(bodies):
    print("\nEnter a regex with one capture group for the amount (digits, e.g. 1,234.56).")
    print('Example: r"Total Owed[:\\s]*\\$?([\\d,]+\\.\\d{2})"')
    import re

    while True:
        pattern = input("Pattern: ").strip()
        try:
            compiled = re.compile(pattern, re.IGNORECASE)
        except re.error as e:
            print(f"Invalid regex: {e}")
            continue
        matches = [compiled.search(body) for body in bodies]
        hits = [m.group(1) if m else None for m in matches]
        print(f"Matches against your {len(bodies)} sample(s): {hits}")
        if input("Use this pattern? [y/N] ").strip().lower() == "y":
            return pattern


def save_sender(sender_address, name, match_type, regex_source=None):
    with open(SENDERS_CONFIG_PATH) as f:
        senders = json.load(f)

    senders = [s for s in senders if s["fromAddress"] != sender_address]
    entry = {"name": name, "fromAddress": sender_address, "matchType": match_type}
    if regex_source is not None:
        entry["regexSource"] = regex_source
    senders.append(entry)

    with open(SENDERS_CONFIG_PATH, "w") as f:
        json.dump(senders, f, indent=2)
        f.write("\n")

    print(f"\nSaved '{name}' ({sender_address}) to {SENDERS_CONFIG_PATH}.")
    print("Run `python build/generate_senders_config.py` then `npx clasp push` to deploy it.")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--sender", required=True, help="From address to configure, e.g. billpay.pge.com")
    parser.add_argument("--name", help="Human-readable name (defaults to the sender address)")
    args = parser.parse_args()

    service = get_gmail_service()
    print(f"Fetching sample emails from {args.sender}...")
    bodies = fetch_sample_bodies(service, args.sender)

    rows = evaluate_candidates(bodies)
    print_candidate_table(rows, len(bodies))
    choice = prompt_choice(rows)

    if choice == "custom":
        regex_source = prompt_custom_regex(bodies)
        match_type = "regex"
    elif choice["id"].startswith("generic-"):
        match_type = choice["id"]
        regex_source = None
    else:
        match_type = "regex"
        regex_source = choice["pattern"]

    name = args.name or args.sender
    save_sender(args.sender, name, match_type, regex_source)


if __name__ == "__main__":
    main()
