"""Token counting. Uses tiktoken when available, falls back to a chars/4 estimate."""

try:
    import tiktoken

    _enc = tiktoken.get_encoding("cl100k_base")
except Exception:  # offline / encoding not cached
    _enc = None


def count_tokens(text: str) -> int:
    if not text:
        return 0
    if _enc is not None:
        return len(_enc.encode(text, disallowed_special=()))
    return max(1, len(text) // 4)
