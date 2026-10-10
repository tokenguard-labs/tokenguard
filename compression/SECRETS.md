# Person 2 — Secret Patterns Covered

Used by `compression.redact_text` (prompt) and inside `optimize_context` (code + repo).
Everything is redacted **before** token counting, and `redaction_details` never contains
the secret value.

| Rule | Catches | Example |
|---|---|---|
| `assigned_secret` | quoted values assigned to key-like names; handles `SCREAMING_SNAKE` (`DB_PASSWORD`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `API_KEY`, `TOKEN`, `CONNECTION_STRING`…) | `DB_PASSWORD = "hunter2"` → `DB_PASSWORD = "[REDACTED]"` |
| `openai_key` | OpenAI-style keys | `sk-…` |
| `github_pat` | GitHub personal access tokens | `ghp_…` |
| `aws_access_key` | AWS access key IDs | `AKIA…` |
| `private_key` | PEM private key headers | `-----BEGIN … PRIVATE KEY-----` |
| `connection_string` | DB/broker URLs with credentials | `postgres://user:pass@host/db` |

## What we intentionally do **not** redact

- Bare identifiers without a value (e.g. `token = get_token()`), to avoid destroying context.
- Package imports named `secrets` / `token`, etc.

## Verification

```bash
python -m compression.check_contract      # 20/20, including `redact_text`
```
