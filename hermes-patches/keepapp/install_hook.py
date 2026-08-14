"""Install the claim guard on Hermes 0.20 without overlaying gateway/platforms/base.py.

Hook points (smallest robust):
1. ``gateway.run._normalize_empty_agent_response`` — has ``agent_result['messages']``.
2. ``gateway.run._sanitize_gateway_final_response`` — last sanitizer before chat send.
3. ``gateway.stream_consumer.GatewayStreamConsumer._send_or_edit`` — streamed finalize.
4. ``agent.tool_dispatch_helpers.make_tool_result_message`` — per-call provenance.
5. ``agent.turn_summary.TurnSummaryCollector.begin`` — reset so stale errors cannot leak.

``send_message_tool`` is not the assistant-reply path; Telegram replies go through
the gateway sanitize / stream-consumer send. This hook is the structural filter.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("keepapp.install_hook")

_PATCHED: set[str] = set()


def install_import_hook() -> None:
    """Patch already-imported modules and wrap future imports."""
    import sys

    _try_patch_loaded(sys.modules)
    if getattr(sys, "_keepapp_claim_guard_meta", None):
        return

    class _KeepappFinder:
        def find_module(self, fullname: str, path=None):  # pragma: no cover - import protocol
            return None

        def find_spec(self, fullname, path, target=None):
            return None

    # Wrap builtins import so we patch as soon as the target modules load.
    import builtins

    if getattr(builtins, "_keepapp_original_import", None) is None:
        original_import = builtins.__import__
        builtins._keepapp_original_import = original_import  # type: ignore[attr-defined]

        def _import(name, globals=None, locals=None, fromlist=(), level=0):
            module = original_import(name, globals, locals, fromlist, level)
            try:
                _patch_if_target(name, module, sys.modules)
            except Exception:
                logger.debug("keepapp import hook patch skipped for %s", name, exc_info=True)
            return module

        builtins.__import__ = _import  # type: ignore[assignment]
    sys._keepapp_claim_guard_meta = True  # type: ignore[attr-defined]


def _try_patch_loaded(modules: dict[str, Any]) -> None:
    for name in (
        "gateway.run",
        "gateway.stream_consumer",
        "agent.tool_dispatch_helpers",
        "agent.turn_summary",
    ):
        mod = modules.get(name)
        if mod is not None:
            _patch_module(name, mod)


def _patch_if_target(imported_name: str, module: Any, modules: dict[str, Any]) -> None:
    targets = {
        "gateway.run",
        "gateway.stream_consumer",
        "agent.tool_dispatch_helpers",
        "agent.turn_summary",
    }
    if imported_name in targets:
        _patch_module(imported_name, module)
        return
    # from gateway import run  /  import gateway.run as x
    for name in targets:
        loaded = modules.get(name)
        if loaded is not None:
            _patch_module(name, loaded)


def _patch_module(name: str, module: Any) -> None:
    if name in _PATCHED:
        return
    # Mark first so a re-entrant import during patching cannot wrap twice.
    _PATCHED.add(name)
    if name == "gateway.run":
        _patch_gateway_run(module)
    elif name == "gateway.stream_consumer":
        _patch_stream_consumer(module)
    elif name == "agent.tool_dispatch_helpers":
        _patch_tool_dispatch(module)
    elif name == "agent.turn_summary":
        _patch_turn_summary(module)
    logger.info("keepapp evidence-integrity hook installed on %s", name)


def _patch_gateway_run(module: Any) -> None:
    from keepapp.evidence_integrity import (
        extract_turn_evidence_from_messages,
        get_current_turn_evidence,
        guard_user_visible_text,
    )

    original_normalize = getattr(module, "_normalize_empty_agent_response", None)
    original_sanitize = getattr(module, "_sanitize_gateway_final_response", None)
    if original_normalize is None or original_sanitize is None:
        return

    def _normalize_empty_agent_response(agent_result, response, *, history_len=0):
        messages = []
        if isinstance(agent_result, dict):
            messages = agent_result.get("messages") or []
        extract_turn_evidence_from_messages(messages)
        if response:
            return guard_user_visible_text(str(response), get_current_turn_evidence())
        return original_normalize(agent_result, response, history_len=history_len)

    def _sanitize_gateway_final_response(platform, text):
        sanitized = original_sanitize(platform, text)
        return guard_user_visible_text(sanitized, get_current_turn_evidence())

    module._normalize_empty_agent_response = _normalize_empty_agent_response
    module._sanitize_gateway_final_response = _sanitize_gateway_final_response
    try:
        import gateway.stream_consumer as stream_consumer

        _patch_module("gateway.stream_consumer", stream_consumer)
    except Exception:
        logger.debug("stream_consumer not ready for keepapp hook", exc_info=True)


def _patch_stream_consumer(module: Any) -> None:
    from keepapp.evidence_integrity import get_current_turn_evidence, guard_user_visible_text

    consumer_cls = getattr(module, "GatewayStreamConsumer", None)
    if consumer_cls is None:
        return
    original = getattr(consumer_cls, "_send_or_edit", None)
    if original is None:
        return

    async def _send_or_edit(self, text, *, finalize=False, is_turn_final=True):
        if finalize or is_turn_final:
            text = guard_user_visible_text(text, get_current_turn_evidence())
        return await original(self, text, finalize=finalize, is_turn_final=is_turn_final)

    consumer_cls._send_or_edit = _send_or_edit


def _patch_tool_dispatch(module: Any) -> None:
    from keepapp.evidence_integrity import record_tool_result

    original = getattr(module, "make_tool_result_message", None)
    if original is None:
        return

    def make_tool_result_message(name, content, tool_call_id, *, effect_disposition=None):
        try:
            record_tool_result(
                str(name or "unknown"),
                content,
                invocation_id=str(tool_call_id or ""),
                source="make_tool_result_message",
            )
        except Exception:
            logger.debug("keepapp record_tool_result failed", exc_info=True)
        return original(
            name, content, tool_call_id, effect_disposition=effect_disposition
        )

    module.make_tool_result_message = make_tool_result_message


def _patch_turn_summary(module: Any) -> None:
    from keepapp.evidence_integrity import begin_turn

    collector = getattr(module, "TurnSummaryCollector", None)
    if collector is None:
        return
    original = getattr(collector, "begin", None)
    if original is None:
        return

    def begin(self):
        begin_turn()
        return original(self)

    collector.begin = begin
