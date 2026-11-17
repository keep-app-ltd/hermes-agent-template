# Keep-app evidence-integrity hook

Structural guard so Hermes cannot assert a timeout / connection / MCP / broker
failure on Telegram unless current-turn tool evidence proves that class.

## Why not prompt-only

Volume `SOUL.md` already forbids paraphrasing `Connect timeout, please try again later.`
The 2026-08-14 Telegram session still emitted that exact 40-character string with
`tool_calls=None`, then admitted the fabrication. Prompt-only is insufficient.

## Hook (not `send_message_tool`, not full `base.py`)

Assistant replies are finalized in:

- `gateway.run._sanitize_gateway_final_response`
- `gateway.run._normalize_empty_agent_response` (has `agent_result['messages']`)
- `gateway.stream_consumer.GatewayStreamConsumer._send_or_edit` (finalize)

`tools/send_message_tool.py` is the optional cross-channel send engine, not the
assistant-reply path. Do **not** overlay `gateway/platforms/base.py` onto 0.23+.

This package is copied to `/opt/hermes-agent/keepapp` and activated via
`sitecustomize.py` (import hook). Monkey-patches only the four small functions
above plus `make_tool_result_message` / `TurnSummaryCollector.begin`.
