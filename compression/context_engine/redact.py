"""Stage 7: high-precision secret detection and redaction.

Only assigned/quoted values and known vendor token formats are redacted, so
ordinary code like ``token = getToken()`` is left untouched.
"""

from __future__ import annotations

import re

from .models import Redaction

# (rule name, compiled regex). Group "val" is replaced when present.
# NOTE: no trailing \b, so SCREAMING_SNAKE names like DB_PASSWORD / ACCESS_KEY_ID match.
_ASSIGNMENT = re.compile(
    r"(?i)(?<![A-Za-z])"
    r"(?:api[_-]?key|secret|token|password|passwd|access[_-]?key|connection[_-]?string)"
    r"[A-Za-z0-9_]*\s*[:=]\s*(?P<q>[\"'])(?P<val>[^\"']+)(?P=q)"
)

_CONNECTION_STRING = re.compile(
    r"(?i)\b(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis|amqp|mssql)://[^\s\"']+"
)

RULES = [
    ("assigned_secret", _ASSIGNMENT),
    ("openai_key", re.compile(r"sk-[A-Za-z0-9_\-]{16,}")),
    ("github_pat", re.compile(r"ghp_[A-Za-z0-9]{20,}")),
    ("aws_access_key", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("private_key", re.compile(r"-----BEGIN[^-]*PRIVATE KEY-----")),
    ("connection_string", _CONNECTION_STRING),
]

_FILE_HEADER = "// file: "


def _redact_line(line: str) -> tuple:
    new = line
    hits: list = []
    for name, rx in RULES:
        if name == "assigned_secret":

            def _sub(m, _rx=rx):
                val = m.group("val")
                if val == "[REDACTED]":
                    return m.group(0)
                return m.group(0).replace(val, "[REDACTED]")

            updated = rx.sub(_sub, new)
        else:
            updated = rx.sub("[REDACTED]", new)

        if updated != new:
            hits.append(name)
            new = updated
    return new, hits


def redact(text: str, counter=None) -> tuple:
    """Return ``(redacted_text, list[Redaction])``.

    Line numbers are relative to the original source file (derived from the
    ``// file:`` headers emitted during assembly). Previews are already redacted.
    """
    out_lines: list = []
    redactions: list = []
    current_file = "<unknown>"
    header_index = 0

    for index, line in enumerate(text.split("\n"), start=1):
        if line.startswith(_FILE_HEADER):
            current_file = line[len(_FILE_HEADER):].strip()
            header_index = index
            out_lines.append(line)
            continue

        new_line, hits = _redact_line(line)
        if hits:
            file_line = index - header_index
            for rule in hits:
                redactions.append(
                    Redaction(
                        file=current_file,
                        line=file_line,
                        rule=rule,
                        preview=new_line.strip()[:120],
                    )
                )
        out_lines.append(new_line)

    return "\n".join(out_lines), redactions
