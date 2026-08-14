"""Operator items 1–12 for the Hermes evidence-integrity / tool-claim guard."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "hermes-patches"))

from keepapp.evidence_integrity import (  # noqa: E402
    EvidenceState,
    FailureClass,
    begin_turn,
    classify_tool_result,
    extract_turn_evidence_from_messages,
    get_current_turn_evidence,
    guard_user_visible_text,
    record_tool_attempt,
    record_tool_result,
    redact_secrets,
)
from keepapp.install_hook import _patch_gateway_run, _patch_stream_consumer  # noqa: E402


TIMEOUT_LIE = "Connect timeout, please try again later."
CONNECTION_LIE = "Connection failed, the trading server is unreachable."


def test_01_no_tool_call_cannot_emit_timeout():
    begin_turn()
    out = guard_user_visible_text(TIMEOUT_LIE)
    assert "Connect timeout" not in out
    assert EvidenceState.TOOL_CALL_NOT_ATTEMPTED.value in out


def test_02_no_tool_call_cannot_emit_connection_failure():
    begin_turn()
    out = guard_user_visible_text(CONNECTION_LIE)
    assert "Connection failed" not in out
    assert EvidenceState.TOOL_CALL_NOT_ATTEMPTED.value in out


def test_03_proven_timeout_may_emit_timeout():
    begin_turn()
    record_tool_result("mcp_trading_getPrice", {"ok": False, "error": "Read timeout after 30s"})
    out = guard_user_visible_text(TIMEOUT_LIE)
    assert out == TIMEOUT_LIE


def test_04_proven_connection_error_may_emit_connection_failure():
    begin_turn()
    record_tool_result("mcp_trading_getServerHealth", {"error": "ECONNRESET"})
    out = guard_user_visible_text("Connection failed: ECONNRESET from the tool.")
    assert "Connection failed" in out


def test_05_generic_exception_cannot_be_upgraded_to_timeout():
    begin_turn()
    status, klass, _ = classify_tool_result({"error": "boom: unexpected executor exception"})
    assert status is EvidenceState.TOOL_CALL_FAILED_PROVEN
    assert klass is FailureClass.GENERIC
    record_tool_result("some_tool", {"error": "boom: unexpected executor exception"})
    out = guard_user_visible_text(TIMEOUT_LIE)
    assert TIMEOUT_LIE not in out
    assert EvidenceState.TOOL_CALL_FAILED_PROVEN.value in out
    assert "timeout" not in out.split(":")[0].lower() or "not the proven class" in out


def test_06_stale_previous_error_cannot_contaminate_later_response():
    begin_turn()
    record_tool_result("mcp_trading_getPrice", {"error": "Read timeout after 30s"})
    assert TIMEOUT_LIE == guard_user_visible_text(TIMEOUT_LIE)
    begin_turn()  # new turn resets evidence
    out = guard_user_visible_text(TIMEOUT_LIE)
    assert TIMEOUT_LIE not in out
    assert EvidenceState.TOOL_CALL_NOT_ATTEMPTED.value in out


def test_07_successful_call_retains_normal_response():
    begin_turn()
    record_tool_result("mcp_trading_getPrice", {"ok": True, "bid": 1.08, "ask": 1.0801})
    text = "EURUSD bid 1.08000 ask 1.08010 via Remote MCP."
    assert guard_user_visible_text(text) == text


def test_08_retry_success_reports_success_not_stale_first_attempt_failure():
    begin_turn()
    record_tool_result("mcp_trading_getPrice", {"error": "Read timeout after 30s"}, invocation_id="a1")
    record_tool_result("mcp_trading_getPrice", {"ok": True, "bid": 1.08}, invocation_id="a2")
    # Mixed: timeout is still proven from first attempt — a later success must
    # not let a *new* turn keep that timeout. Same-turn: timeout remains proven.
    assert guard_user_visible_text(TIMEOUT_LIE) == TIMEOUT_LIE
    begin_turn()
    record_tool_result("mcp_trading_getPrice", {"ok": True, "bid": 1.08}, invocation_id="a2")
    text = "Quote ready: EURUSD 1.08"
    assert guard_user_visible_text(text) == text
    assert TIMEOUT_LIE not in guard_user_visible_text(TIMEOUT_LIE)


def test_09_multiple_calls_preserve_per_call_provenance():
    begin_turn()
    record_tool_result("alpha", {"ok": True}, invocation_id="id-alpha")
    record_tool_result("beta", {"error": "ECONNRESET"}, invocation_id="id-beta")
    ev = get_current_turn_evidence()
    by_id = {r.invocation_id: r for r in ev.records}
    assert by_id["id-alpha"].status is EvidenceState.TOOL_CALL_SUCCEEDED
    assert by_id["id-beta"].status is EvidenceState.TOOL_CALL_FAILED_PROVEN
    assert by_id["id-beta"].error_class is FailureClass.CONNECTION
    assert by_id["id-alpha"].error_class is None
    public = [r.to_public_dict() for r in ev.records]
    assert {p["invocation_id"] for p in public} == {"id-alpha", "id-beta"}


def test_10_telegram_rendering_preserves_evidence_state():
    """Sanitize hook is the Telegram finalize path; replacement must keep the state token."""
    begin_turn()
    guarded = guard_user_visible_text(TIMEOUT_LIE)
    ns = SimpleNamespace(
        _normalize_empty_agent_response=lambda agent_result, response, *, history_len=0: response,
        _sanitize_gateway_final_response=lambda platform, text: text,
    )
    _patch_gateway_run(ns)
    rendered = ns._sanitize_gateway_final_response("telegram", TIMEOUT_LIE)
    assert EvidenceState.TOOL_CALL_NOT_ATTEMPTED.value in rendered
    assert guarded == rendered


def test_11_unsupported_model_generated_operational_claim_is_blocked():
    begin_turn()
    messages = [
        {"role": "user", "content": "Investigate 18091"},
        {"role": "assistant", "content": TIMEOUT_LIE, "tool_calls": None},
    ]
    ev = extract_turn_evidence_from_messages(messages)
    assert ev.overall_state() is EvidenceState.TOOL_CALL_NOT_ATTEMPTED
    out = guard_user_visible_text(TIMEOUT_LIE, ev)
    assert TIMEOUT_LIE not in out
    assert "NOT_ATTEMPTED" in out


def test_12_secrets_are_not_exposed_in_provenance():
    begin_turn()
    rec = record_tool_result(
        "mcp_trading_getConfigDump",
        {
            "error": "401 unauthorized bearer sk-live-SUPERSECRETTOKEN api_key=abc123",
            "authorization": "Bearer sk-live-SUPERSECRETTOKEN",
        },
        invocation_id="sec-1",
    )
    public = rec.to_public_dict()
    blob = str(public)
    assert "SUPERSECRETTOKEN" not in blob
    assert "abc123" not in blob
    assert "[REDACTED]" in redact_secrets("Authorization: Bearer sk-live-SUPERSECRETTOKEN")


def test_successful_tools_cannot_claim_timeout_aug14_case_b():
    """08:04 UTC incident: tools succeeded, model still emitted the 40-char lie."""
    messages = [
        {"role": "user", "content": "forensic 18082"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "function": {"name": "mcp__trading__getBrokerPositionProvenance"}}
        ]},
        {"role": "tool", "name": "mcp__trading__getBrokerPositionProvenance",
         "tool_call_id": "c1", "content": "<untrusted_tool_result> {\"ok\": true} </untrusted_tool_result>"},
        {"role": "assistant", "content": TIMEOUT_LIE},
    ]
    ev = extract_turn_evidence_from_messages(messages)
    assert ev.overall_state() is EvidenceState.TOOL_CALL_SUCCEEDED
    out = guard_user_visible_text(TIMEOUT_LIE, ev)
    assert TIMEOUT_LIE not in out
    assert EvidenceState.TOOL_CALL_SUCCEEDED.value in out


def test_inference_after_proven_event_must_be_labeled():
    begin_turn()
    record_tool_result("http_get", {"error": "ECONNRESET"})
    labeled = "INFERENCE: This may indicate a transient network interruption."
    assert guard_user_visible_text(labeled) == labeled
    unlabeled_timeout = "The provider timed out."
    out = guard_user_visible_text(unlabeled_timeout)
    assert unlabeled_timeout not in out


def test_stream_finalize_hook_blocks_timeout():
    begin_turn()

    class Consumer:
        async def _send_or_edit(self, text, *, finalize=False, is_turn_final=True):
            return text

    ns = SimpleNamespace(GatewayStreamConsumer=Consumer)
    _patch_stream_consumer(ns)
    import asyncio

    result = asyncio.run(Consumer()._send_or_edit(TIMEOUT_LIE, finalize=True))
    assert EvidenceState.TOOL_CALL_NOT_ATTEMPTED.value in result


def test_normalize_hook_uses_messages_and_blocks_no_call():
    ns = SimpleNamespace(
        _normalize_empty_agent_response=lambda agent_result, response, *, history_len=0: response,
        _sanitize_gateway_final_response=lambda platform, text: text,
    )
    _patch_gateway_run(ns)
    out = ns._normalize_empty_agent_response(
        {"messages": [{"role": "user", "content": "hi"}]},
        TIMEOUT_LIE,
    )
    assert EvidenceState.TOOL_CALL_NOT_ATTEMPTED.value in out


def test_attempt_without_result_is_not_proven():
    begin_turn()
    record_tool_attempt("mcp_trading_getPrice", invocation_id="pending")
    out = guard_user_visible_text(TIMEOUT_LIE)
    assert EvidenceState.TOOL_CALL_ATTEMPTED.value in out or EvidenceState.TOOL_RESULT_NOT_PROVEN.value in out
    assert TIMEOUT_LIE not in out


def test_malformed_result_is_malformed_not_timeout():
    begin_turn()
    status, klass, _ = classify_tool_result("{\"ok\": true, \"bid\":")
    assert status is EvidenceState.TOOL_CALL_FAILED_PROVEN
    assert klass is FailureClass.MALFORMED_RESPONSE
    record_tool_result("parser", "{\"ok\": true, \"bid\":")
    assert TIMEOUT_LIE not in guard_user_visible_text(TIMEOUT_LIE)
