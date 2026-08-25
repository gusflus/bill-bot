# Test fixtures

These bill email bodies are **synthetic**, not anonymized real mail. They were
written to match the shape each sender's mail is known to have, taken from the
`matchType` that the old `senders.config.json` had settled on for it:

| Fixture | Old matchType | What that implied |
|---|---|---|
| `pge.txt` | `generic-first` | Total is the first dollar figure and carries no adjacent text label — in the real mail it's rendered as an image |
| `socalgas.txt` | `generic-last` | Total is the last dollar figure, after usage and rate detail |
| `watersewer.txt` | `generic-last` | Total is the last dollar figure |
| `spectrum.txt` | `generic-first` | Total is the first dollar figure |

Plus two cases that exist to prove specific behavior rather than to mimic a sender:

| Fixture | Purpose |
|---|---|
| `tricky_largest_is_not_total.txt` | The largest figure in the email is a previous balance, so "pick the biggest number" gets it wrong. Shows why Haiku beats a positional heuristic. |
| `html_only.txt` | Plaintext part is just "view in browser"; the amount only exists in HTML markup. Exercises the `html_to_text` fallback. |
| `upcoming_no_due_date.txt` | A "your bill is ready soon" notice with no due date, which is the email that arrives *before* the real bill and must not double-charge anyone. |

If you want these to reflect your actual mail, replace the contents with real
bodies and strip account numbers first. The tests assert on the amounts written
in each file, so update `tests/test_extract.py` to match if you change them.
