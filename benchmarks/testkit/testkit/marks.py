"""Status icons for terminal output, with a plain-text fallback.

Icons are used when stdout is UTF-8 and neither TESTKIT_PLAIN nor NO_COLOR is set. JSON output never
uses them.
"""

import os
import sys
import unicodedata

ICONS = {"ok": "✅", "pass": "✅", "fail": "❌", "warn": "🟡", "missing": "🟡", "skip": "⚪"}


def fancy(stream=None):
    if os.environ.get("TESTKIT_PLAIN") or "NO_COLOR" in os.environ:
        return False
    enc = getattr(stream or sys.stdout, "encoding", None) or ""
    return enc.lower().replace("-", "").replace("_", "") in ("utf8", "utf8sig")


def icon(status, use=None):
    """Icon of a status (ok/pass/fail/warn/missing/skip, any case), or "" without icons."""
    if use is None:
        use = fancy()
    return ICONS.get(str(status).lower(), "") if use else ""


def labeled(status, use=None):
    """"✅ PASS" with icons, "PASS" without."""
    mark = icon(status, use)
    return "{} {}".format(mark, status) if mark else str(status)


def width(text):
    """Terminal columns of text: wide (emoji, CJK) characters take two."""
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in text)


def pad(text, columns):
    return text + " " * max(0, columns - width(text))
