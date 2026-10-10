"""Shared data contract between the router (Person 1) and the context optimizer (Person 2).

Changing a field here is a breaking change for both sides - agree on it first.
"""
from dataclasses import dataclass, field
from typing import List


@dataclass
class ContextResult:
    context: str                  # the exact text sent to the cloud with the prompt (already redacted)
    original_tokens: int          # tokens of ALL code/repo before optimization (cloud-only baseline, excludes prompt)
    optimized_tokens: int         # tokens of `context`; must be <= token_budget
    files_selected: List[str] = field(default_factory=list)       # repo-relative paths included in `context`
    secrets_redacted: int = 0                                     # how many secrets were masked
    redaction_details: List[str] = field(default_factory=list)    # e.g. "AWS access key in config.py" (never the secret itself)
    optimizer: str = "fallback"   # who produced this result; Person 2 sets e.g. "person2-v1"
