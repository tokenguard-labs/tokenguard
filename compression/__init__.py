"""Context optimizer - owned by Person 2. See compression/README.md for the contract.

The router imports exactly these two functions from this package. Their signatures
are unchanged; the implementation lives in ./adapter.py and ./context_engine/.
"""
from .adapter import OPTIMIZER_NAME, optimize_context, redact_text

__all__ = ["optimize_context", "redact_text", "OPTIMIZER_NAME"]
