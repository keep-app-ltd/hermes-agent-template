"""Keep-app Hermes overlays that must not require overlaying gateway/platforms/base.py."""

from keepapp.evidence_integrity import (
    EvidenceState,
    FailureClass,
    ToolEvidence,
    TurnEvidence,
    begin_turn,
    classify_tool_result,
    extract_turn_evidence_from_messages,
    get_current_turn_evidence,
    guard_user_visible_text,
    record_tool_attempt,
    record_tool_result,
)

__all__ = [
    "EvidenceState",
    "FailureClass",
    "ToolEvidence",
    "TurnEvidence",
    "begin_turn",
    "classify_tool_result",
    "extract_turn_evidence_from_messages",
    "get_current_turn_evidence",
    "guard_user_visible_text",
    "record_tool_attempt",
    "record_tool_result",
]
