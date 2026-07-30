"""Minimal HTML -> plaintext, good enough for regex matching against bill emails.

Deliberately duplicated (it's ~10 lines) as a small JS function in src/Parser.gs,
since Apps Script can't import this Python module.
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
    return text.strip()
