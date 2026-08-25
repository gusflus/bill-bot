"""Minimal HTML -> plaintext, good enough for bill-amount extraction.

A fallback only: Apps Script sends ``getPlainBody()``, which is already
plaintext for the overwhelming majority of bill emails. This handles the
senders whose plaintext part is empty or is just "view this in a browser".
"""
import re

_SCRIPT_STYLE_RE = re.compile(r"<(script|style)[\s\S]*?</\1>", re.IGNORECASE)
_BR_RE = re.compile(r"<br\s*/?>", re.IGNORECASE)
_BLOCK_CLOSE_RE = re.compile(r"</(p|td|tr|div|table)>", re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")
_BLANK_LINES_RE = re.compile(r"\n\s*\n+")
_SPACES_RE = re.compile(r"[ \t]+")

_ENTITIES = {
    "&nbsp;": " ",
    "&amp;": "&",
    "&#39;": "'",
    "&rsquo;": "'",
    "&quot;": '"',
}


def html_to_text(html: str) -> str:
    text = _SCRIPT_STYLE_RE.sub(" ", html)
    text = _BR_RE.sub("\n", text)
    text = _BLOCK_CLOSE_RE.sub("\n", text)
    text = _TAG_RE.sub(" ", text)
    for entity, replacement in _ENTITIES.items():
        text = text.replace(entity, replacement)
    text = _SPACES_RE.sub(" ", text)
    text = _BLANK_LINES_RE.sub("\n", text)
    # Strip per line: replacing tags with spaces otherwise leaves every line
    # indented, which wastes prompt tokens and makes logs harder to read.
    return "\n".join(line.strip() for line in text.split("\n")).strip()
