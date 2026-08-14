# Changelog

## 2026-08-14 — Evidence-integrity / tool-claim guard

Hermes can no longer deliver `Connect timeout, please try again later.` (or similar operational failure claims) on Telegram unless current-turn tool evidence proves that class.

**Added**
- `hermes-patches/keepapp/evidence_integrity.py` — evidence states + claim guard
- `hermes-patches/keepapp/install_hook.py` — wraps gateway sanitize / stream finalize / tool-result recording
- `hermes-patches/sitecustomize.py` — loads the hook in the gateway process
- `tests/test_evidence_integrity.py` — operator items 1–12

**Why**
- 2026-08-14 Telegram session `20260806_063454_c4affbfb` emitted the 40-character timeout string with `tool_calls=None` (`api_calls=1`), then admitted fabrication. Prompt-only SOUL was already in place and insufficient.
