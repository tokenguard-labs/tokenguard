"""Query term extraction.

The corpus stopword list in `context_engine.selector` is tuned for ranking *code bodies* and
removes words like `test`, `unit`, `error`, `code`, `file`. That is wrong for a *query*: a request
about "the test cases" or "the docs" would lose its anchor term and drift to unrelated files.

So queries use their own, much smaller stopword list: English function words plus a few generic
intent verbs. Domain nouns (`test`, `docs`, `case`, `evaluate`, `pool`, ...) are kept.
"""

from __future__ import annotations

from typing import Set

from ..context_engine.selector import _SPLIT, _WORDS

# Function words + generic intent verbs only. No code/domain nouns.
QUERY_STOPWORDS = {
    "the", "a", "an", "and", "or", "but", "if", "then", "else", "for", "of", "to", "in", "on",
    "at", "by", "with", "from", "as", "is", "are", "was", "were", "be", "been", "being", "do",
    "does", "did", "how", "what", "why", "when", "where", "which", "who", "whom", "this", "that",
    "these", "those", "my", "our", "your", "its", "can", "could", "should", "would", "will",
    "not", "no", "yes", "please", "here", "there", "about", "into", "over", "after", "before",
    "you", "we", "they", "them", "his", "her", "their", "any", "all", "some", "more", "most",
    "just", "also", "than", "so", "up", "out", "off", "own", "each", "few", "other", "such",
    # generic intent verbs (not domain nouns)
    "explain", "describe", "tell", "show", "help", "want", "need", "use", "used", "using",
    "make", "write", "give", "get", "let", "know", "think", "work", "works", "working",
}


def query_terms(query: str) -> Set[str]:
    terms: Set[str] = set()
    for raw in _SPLIT.split(query or ""):
        if not raw:
            continue
        for part in _WORDS.findall(raw):
            low = part.lower()
            if len(low) >= 2 and low not in QUERY_STOPWORDS and not low.isdigit():
                terms.add(low)
        low = raw.lower()
        if len(low) >= 2 and low not in QUERY_STOPWORDS and not low.isdigit():
            terms.add(low)
    return terms
