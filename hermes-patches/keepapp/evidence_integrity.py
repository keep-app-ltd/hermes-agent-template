"""Deterministic evidence-integrity / tool-claim guard.

Blocks user-visible operational failure claims unless current-turn tool
evidence proves that failure class. Prompt-only mitigation is insufficient;
this module is the structural chokepoint.
"""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
from contextvars import ContextVar
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable

logger = logging.getLogger("keepapp.evidence_integrity")

_SECRET_RE = re.compile(
    r"(?i)(bearer\s+[a-z0-9._\-+=/]+|api[_-]?key\s*[:=]\s*\S+|token\s*[:=]\s*\S+|"
    r"authorization\s*[:=]\s*\S+|sk-[a-z0-9]{10,}|xox[baprs]-[a-z0-9-]+)"
)


class EvidenceState(str, Enum):
    TOOL_CALL_NOT_ATTEMPTED = "TOOL_CALL_NOT_ATTEMPTED"
    TOOL_CALL_ATTEMPTED = "TOOL_CALL_ATTEMPTED"
    TOOL_CALL_SUCCEEDED = "TOOL_CALL_SUCCEEDED"
    TOOL_CALL_FAILED_PROVEN = "TOOL_CALL_FAILED_PROVEN"
    TOOL_RESULT_NOT_PROVEN = "TOOL_RESULT_NOT_PROVEN"


class FailureClass(str, Enum):
    TIMEOUT = "timeout"
    CONNECTION = "connection"
    AUTHENTICATION = "authentication"
    PERMISSION = "permission"
    RATE_LIMIT = "rate_limit"
    TOOL_UNAVAILABLE = "tool_unavailable"
    MALFORMED_RESPONSE = "malformed_response"
    UPSTREAM = "upstream"
    DATABASE = "database"
    MCP = "mcp"
    BROKER = "broker"
    TELEGRAM = "telegram"
    RAILWAY = "railway"
    API = "api"
    GENERIC = "generic"


CLAIM_TO_CLASS: dict[str, FailureClass] = {
    "timeout": FailureClass.TIMEOUT,
    "connection": FailureClass.CONNECTION,
    "authentication": FailureClass.AUTHENTICATION,
    "permission": FailureClass.PERMISSION,
    "rate_limit": FailureClass.RATE_LIMIT,
    "database": FailureClass.DATABASE,
    "mcp": FailureClass.MCP,
    "broker": FailureClass.BROKER,
    "telegram": FailureClass.TELEGRAM,
    "railway": FailureClass.RAILWAY,
    "api": FailureClass.API,
    "tool_unavailable": FailureClass.TOOL_UNAVAILABLE,
    "no_data": FailureClass.UPSTREAM,
}

# Assertion-oriented. Explanatory "a timeout is…" in a long report is handled
# by claim-only vs mixed-message replacement, not by ignoring the word.
_CLAIM_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("timeout", re.compile(
        r"(?i)\b(connect(?:ion)?\s+timeout|timed?\s+out|timeout(?:ed)?)\b"
    )),
    ("connection", re.compile(
        r"(?i)\b(connection\s+failed|disconnected|unreachable|not\s+responding|"
        r"connect(?:ion)?\s+(?:error|refused|reset)|econnreset|econnrefused)\b"
    )),
    ("rate_limit", re.compile(r"(?i)\b(rate[- ]?limit(?:ed)?|too many requests|http\s*429)\b")),
    ("authentication", re.compile(
        r"(?i)\b(authentication\s+failed|auth(?:entication)?\s+(?:error|denied)|unauthorized|http\s*401)\b"
    )),
    ("permission", re.compile(r"(?i)\b(permission\s+denied|forbidden|http\s*403)\b")),
    ("database", re.compile(
        r"(?i)\b(database\s+(?:unavailable|down|unreachable|error)|postgres(?:ql)?\s+(?:unavailable|down))\b"
    )),
    ("mcp", re.compile(
        r"(?i)\b(mcp\s+(?:unavailable|disconnected|unreachable|down|error|failed)|"
        r"trading\s+server\s+(?:unreachable|unavailable|down|offline))\b"
    )),
    ("broker", re.compile(r"(?i)\b(broker\s+(?:rejected|unavailable|error|failed|timeout))\b")),
    ("telegram", re.compile(r"(?i)\b(telegram\s+(?:failed|unavailable|error|timeout|disconnected))\b")),
    ("railway", re.compile(
        r"(?i)\b((?:railway|repository|service)\s+(?:unavailable|down|unreachable|failed))\b"
    )),
    ("api", re.compile(r"(?i)\b(api\s+(?:returned\s+an\s+error|error|failed|unavailable))\b")),
    ("tool_unavailable", re.compile(r"(?i)\b(tool\s+(?:unavailable|not\s+found|missing))\b")),
    ("no_data", re.compile(r"(?i)\b(tool\s+returned\s+no\s+data|returned\s+no\s+data)\b")),
)

_EXACT_UNSAFE_FALLBACKS = (
    "connect timeout, please try again later.",
    "connect timeout, please try again later",
)

_INFERENCE_PREFIX_RE = re.compile(
    r"(?i)^\s*(inference|hypothesis|this may|this might|this could|possibly|perhaps)\b"
)

_QUOTE_OR_CODE_RE = re.compile(r"```.*?```|`[^`]+`|\"[^\"]+\"|'[^']+'", re.DOTALL)


@dataclass
class ToolEvidence:
    tool_name: str
    invocation_id: str
    attempted_at: float
    status: EvidenceState
    error_class: FailureClass | None = None
    source: str = "current_turn"
    error_excerpt: str = ""

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "tool_name": self.tool_name,
            "invocation_id": self.invocation_id,
            "attempted_at": self.attempted_at,
            "status": self.status.value,
            "error_class": self.error_class.value if self.error_class else None,
            "source": self.source,
            "error_excerpt": redact_secrets(self.error_excerpt)[:240],
        }


@dataclass
class TurnEvidence:
    turn_id: str
    records: list[ToolEvidence] = field(default_factory=list)

    def proven_classes(self) -> set[FailureClass]:
        return {
            rec.error_class
            for rec in self.records
            if rec.status is EvidenceState.TOOL_CALL_FAILED_PROVEN and rec.error_class
        }

    def overall_state(self) -> EvidenceState:
        if not self.records:
            return EvidenceState.TOOL_CALL_NOT_ATTEMPTED
        if any(r.status is EvidenceState.TOOL_CALL_FAILED_PROVEN for r in self.records):
            return EvidenceState.TOOL_CALL_FAILED_PROVEN
        if any(r.status is EvidenceState.TOOL_RESULT_NOT_PROVEN for r in self.records):
            return EvidenceState.TOOL_RESULT_NOT_PROVEN
        if any(r.status is EvidenceState.TOOL_CALL_ATTEMPTED for r in self.records) and not any(
            r.status in {EvidenceState.TOOL_CALL_SUCCEEDED, EvidenceState.TOOL_CALL_FAILED_PROVEN}
            for r in self.records
        ):
            return EvidenceState.TOOL_CALL_ATTEMPTED
        if all(r.status is EvidenceState.TOOL_CALL_SUCCEEDED for r in self.records):
            return EvidenceState.TOOL_CALL_SUCCEEDED
        if any(r.status is EvidenceState.TOOL_CALL_SUCCEEDED for r in self.records):
            return EvidenceState.TOOL_CALL_SUCCEEDED
        return EvidenceState.TOOL_RESULT_NOT_PROVEN


_CURRENT_TURN: ContextVar[TurnEvidence | None] = ContextVar(
    "keepapp_current_turn_evidence", default=None
)


def redact_secrets(text: Any) -> str:
    raw = "" if text is None else str(text)
    return _SECRET_RE.sub("[REDACTED]", raw)


def begin_turn(turn_id: str | None = None) -> TurnEvidence:
    evidence = TurnEvidence(turn_id=turn_id or uuid.uuid4().hex)
    _CURRENT_TURN.set(evidence)
    return evidence


def get_current_turn_evidence() -> TurnEvidence:
    evidence = _CURRENT_TURN.get()
    if evidence is None:
        evidence = TurnEvidence(turn_id="unbound")
        _CURRENT_TURN.set(evidence)
    return evidence


def record_tool_attempt(
    tool_name: str,
    *,
    invocation_id: str | None = None,
    attempted_at: float | None = None,
    source: str = "current_turn",
) -> ToolEvidence:
    evidence = get_current_turn_evidence()
    rec = ToolEvidence(
        tool_name=str(tool_name or "unknown"),
        invocation_id=invocation_id or uuid.uuid4().hex,
        attempted_at=attempted_at or time.time(),
        status=EvidenceState.TOOL_CALL_ATTEMPTED,
        source=source,
    )
    evidence.records.append(rec)
    return rec


def record_tool_result(
    tool_name: str,
    result: Any,
    *,
    invocation_id: str | None = None,
    is_error: bool | None = None,
    attempted_at: float | None = None,
    source: str = "current_turn",
) -> ToolEvidence:
    status, error_class, excerpt = classify_tool_result(result, is_error=is_error)
    evidence = get_current_turn_evidence()
    inv = invocation_id or uuid.uuid4().hex
    for rec in reversed(evidence.records):
        if rec.invocation_id == inv or (
            rec.tool_name == tool_name and rec.status is EvidenceState.TOOL_CALL_ATTEMPTED
        ):
            rec.status = status
            rec.error_class = error_class
            rec.error_excerpt = excerpt
            rec.source = source
            return rec
    rec = ToolEvidence(
        tool_name=str(tool_name or "unknown"),
        invocation_id=inv,
        attempted_at=attempted_at or time.time(),
        status=status,
        error_class=error_class,
        source=source,
        error_excerpt=excerpt,
    )
    evidence.records.append(rec)
    return rec


def classify_tool_result(
    result: Any, *, is_error: bool | None = None
) -> tuple[EvidenceState, FailureClass | None, str]:
    """Classify a tool payload. Never upgrades a generic exception to timeout."""
    excerpt = redact_secrets(_result_excerpt(result))
    lowered = excerpt.lower()

    if _is_malformed(result):
        return EvidenceState.TOOL_CALL_FAILED_PROVEN, FailureClass.MALFORMED_RESPONSE, excerpt

    explicit_error = _explicit_error_text(result)
    if is_error is True or explicit_error:
        class_ = _classify_failure_text(explicit_error or excerpt)
        if class_ is None:
            class_ = FailureClass.GENERIC
        return EvidenceState.TOOL_CALL_FAILED_PROVEN, class_, excerpt

    if _looks_successful(result):
        return EvidenceState.TOOL_CALL_SUCCEEDED, None, excerpt

    if not excerpt.strip() or excerpt.strip() in {"{}", "[]", "null", "none"}:
        return EvidenceState.TOOL_RESULT_NOT_PROVEN, None, excerpt

    # Untrusted MCP wrappers with substantial body and no error → success.
    if "untrusted_tool_result" in lowered and "error" not in lowered:
        return EvidenceState.TOOL_CALL_SUCCEEDED, None, excerpt

    if _classify_failure_text(excerpt):
        # Failure language without a structured error is not a proven class.
        return EvidenceState.TOOL_RESULT_NOT_PROVEN, None, excerpt

    return EvidenceState.TOOL_CALL_SUCCEEDED, None, excerpt


def _result_excerpt(result: Any) -> str:
    if result is None:
        return ""
    if isinstance(result, (dict, list)):
        try:
            return json.dumps(result, default=str)[:2000]
        except Exception:
            return str(result)[:2000]
    return str(result)[:2000]


def _explicit_error_text(result: Any) -> str:
    if isinstance(result, dict):
        for key in ("error", "errorCode", "error_code", "kind"):
            val = result.get(key)
            if val and str(val).lower() not in {"ok", "success", "none", "null"}:
                return str(val)
        if result.get("ok") is False or result.get("success") is False:
            return str(result.get("message") or result.get("error") or result)
    if isinstance(result, str):
        stripped = result.strip()
        if stripped.startswith("{") and ("\"error\"" in stripped or "'error'" in stripped):
            try:
                parsed = json.loads(stripped)
            except Exception:
                parsed = None
            if isinstance(parsed, dict):
                return _explicit_error_text(parsed)
        if re.search(r"(?i)\b(error|exception|traceback|failed)\b", stripped) and len(stripped) < 800:
            if re.search(r"(?i)(\"ok\"\s*:\s*true|success\":\s*true)", stripped):
                return ""
            if stripped.lower().startswith("error") or "traceback" in stripped.lower():
                return stripped
    return ""


def _looks_successful(result: Any) -> bool:
    if isinstance(result, dict):
        if result.get("ok") is True or result.get("success") is True:
            return True
        if result.get("error") or result.get("ok") is False or result.get("success") is False:
            return False
    text = _result_excerpt(result).lower()
    if "\"ok\": true" in text or "\"success\": true" in text:
        return True
    return False


def _is_malformed(result: Any) -> bool:
    if isinstance(result, str):
        stripped = result.strip()
        if stripped.startswith("{") and not stripped.endswith("}"):
            return True
        if stripped.startswith("[") and not stripped.endswith("]"):
            return True
    return False


def _classify_failure_text(text: str) -> FailureClass | None:
    t = (text or "").lower()
    if not t:
        return None
    if re.search(r"\b(timeout|timed out|connecttimeout|deadline exceeded)\b", t):
        return FailureClass.TIMEOUT
    if re.search(r"\b(econnreset|econnrefused|connection refused|connection reset|disconnected|unreachable)\b", t):
        return FailureClass.CONNECTION
    if re.search(r"\b(401|unauthorized|authentication failed|invalid.?token|invalid.?key)\b", t):
        return FailureClass.AUTHENTICATION
    if re.search(r"\b(403|permission denied|forbidden)\b", t):
        return FailureClass.PERMISSION
    if re.search(r"\b(429|rate.?limit|too many requests)\b", t):
        return FailureClass.RATE_LIMIT
    if re.search(r"\b(tool unavailable|not found|unknown tool)\b", t):
        return FailureClass.TOOL_UNAVAILABLE
    if re.search(r"\b(json|parse|malformed|invalid response)\b", t) and "error" in t:
        return FailureClass.MALFORMED_RESPONSE
    if re.search(r"\b(database|postgres|sqlite)\b", t) and re.search(r"\b(unavailable|down|error)\b", t):
        return FailureClass.DATABASE
    if re.search(r"\bmcp\b", t) and re.search(r"\b(unavailable|disconnected|error|failed)\b", t):
        return FailureClass.MCP
    if re.search(r"\bbroker\b", t) and re.search(r"\b(reject|unavailable|error|failed)\b", t):
        return FailureClass.BROKER
    if re.search(r"\btelegram\b", t) and re.search(r"\b(fail|unavailable|error|timeout)\b", t):
        return FailureClass.TELEGRAM
    if re.search(r"\brailway\b", t) and re.search(r"\b(unavailable|down|error|failed)\b", t):
        return FailureClass.RAILWAY
    if re.search(r"\b(http\s*5\d\d|upstream|502|503|504)\b", t):
        return FailureClass.UPSTREAM
    if re.search(r"\bapi\b", t) and re.search(r"\b(error|failed|unavailable)\b", t):
        return FailureClass.API
    return None


def detect_operational_claims(text: str) -> list[str]:
    """Return claim keys asserted as current-turn fact (not quoted / not inference)."""
    if not text:
        return []
    stripped = text.strip()
    if stripped.lower() in _EXACT_UNSAFE_FALLBACKS:
        return ["timeout"]
    masked = _QUOTE_OR_CODE_RE.sub(" ", stripped)
    claims: list[str] = []
    for key, pattern in _CLAIM_PATTERNS:
        for match in pattern.finditer(masked):
            sentence = _sentence_at(masked, match.start())
            if _INFERENCE_PREFIX_RE.search(sentence):
                continue
            if key not in claims:
                claims.append(key)
    return claims


def _sentence_at(text: str, index: int) -> str:
    start = text.rfind(".", 0, index)
    start = 0 if start < 0 else start + 1
    end = text.find(".", index)
    end = len(text) if end < 0 else end + 1
    return text[start:end].strip()


def _honest_replacement(evidence: TurnEvidence, claims: Iterable[str]) -> str:
    state = evidence.overall_state()
    claim_list = ", ".join(sorted(set(claims))) or "operational failure"
    if state is EvidenceState.TOOL_CALL_NOT_ATTEMPTED:
        return (
            f"{EvidenceState.TOOL_CALL_NOT_ATTEMPTED.value}: no tool was invoked this turn. "
            f"A {claim_list} is not proven. I will not invent a cause."
        )
    if state is EvidenceState.TOOL_CALL_ATTEMPTED:
        return (
            f"{EvidenceState.TOOL_CALL_ATTEMPTED.value}: a tool was invoked but no result is "
            f"available yet. A {claim_list} is {EvidenceState.TOOL_RESULT_NOT_PROVEN.value}."
        )
    if state is EvidenceState.TOOL_RESULT_NOT_PROVEN:
        return (
            f"{EvidenceState.TOOL_RESULT_NOT_PROVEN.value}: a tool result exists but no proven "
            f"failure class matches {claim_list}. I will not invent a cause."
        )
    if state is EvidenceState.TOOL_CALL_SUCCEEDED:
        return (
            f"{EvidenceState.TOOL_CALL_SUCCEEDED.value}: current-turn tool evidence does not "
            f"prove {claim_list}."
        )
    proven = ",".join(sorted(c.value for c in evidence.proven_classes())) or "generic"
    return (
        f"{EvidenceState.TOOL_CALL_FAILED_PROVEN.value}:{proven}. "
        f"The claimed {claim_list} is not the proven class. I will not upgrade the cause."
    )


def _is_claim_only_message(text: str, claims: list[str]) -> bool:
    compact = re.sub(r"\s+", " ", (text or "").strip())
    if compact.lower() in _EXACT_UNSAFE_FALLBACKS:
        return True
    if len(compact) <= 160 and claims:
        return True
    return False


def guard_user_visible_text(
    text: str,
    evidence: TurnEvidence | None = None,
    *,
    messages: list[Any] | None = None,
) -> str:
    """Replace unsupported operational claims before Telegram/gateway delivery."""
    if not text:
        return text
    if messages is not None:
        evidence = extract_turn_evidence_from_messages(messages)
    if evidence is None:
        evidence = get_current_turn_evidence()

    claims = detect_operational_claims(text)
    if not claims:
        return text

    proven = evidence.proven_classes()
    unsupported = [
        claim for claim in claims
        if CLAIM_TO_CLASS[claim] not in proven
    ]
    if not unsupported:
        return text

    replacement = _honest_replacement(evidence, unsupported)
    if _is_claim_only_message(text, unsupported):
        logger.info(
            "blocked unsupported operational claim state=%s claims=%s",
            evidence.overall_state().value,
            unsupported,
        )
        return replacement

    # Mixed message: drop unsupported claim sentences, keep the rest.
    kept: list[str] = []
    for sentence in re.split(r"(?<=[.!?])\s+", text.strip()):
        sentence_claims = detect_operational_claims(sentence)
        if any(c in unsupported for c in sentence_claims) and not _INFERENCE_PREFIX_RE.search(sentence):
            continue
        kept.append(sentence)
    body = " ".join(kept).strip()
    if not body:
        return replacement
    return f"{body}\n\n{replacement}"


def extract_turn_evidence_from_messages(
    messages: list[Any] | None,
    *,
    replace_current: bool = True,
) -> TurnEvidence:
    """Build current-turn evidence from the suffix after the last user message."""
    evidence = TurnEvidence(turn_id=uuid.uuid4().hex)
    if not messages:
        if replace_current:
            _CURRENT_TURN.set(evidence)
        return evidence

    start = 0
    for i, msg in enumerate(messages):
        if _msg_role(msg) == "user":
            start = i + 1
    turn_msgs = messages[start:]

    for msg in turn_msgs:
        role = _msg_role(msg)
        if role == "assistant":
            for call in _msg_tool_calls(msg):
                name = _call_name(call)
                inv = _call_id(call)
                record = ToolEvidence(
                    tool_name=name,
                    invocation_id=inv,
                    attempted_at=time.time(),
                    status=EvidenceState.TOOL_CALL_ATTEMPTED,
                    source="assistant.tool_calls",
                )
                evidence.records.append(record)
        elif role == "tool":
            name = str(
                (msg.get("tool_name") if isinstance(msg, dict) else getattr(msg, "tool_name", None))
                or (msg.get("name") if isinstance(msg, dict) else getattr(msg, "name", None))
                or "unknown"
            )
            inv = str(
                (msg.get("tool_call_id") if isinstance(msg, dict) else getattr(msg, "tool_call_id", None))
                or uuid.uuid4().hex
            )
            content = msg.get("content") if isinstance(msg, dict) else getattr(msg, "content", "")
            status, error_class, excerpt = classify_tool_result(content)
            updated = False
            for rec in evidence.records:
                if rec.invocation_id == inv or (
                    rec.tool_name == name and rec.status is EvidenceState.TOOL_CALL_ATTEMPTED
                ):
                    rec.status = status
                    rec.error_class = error_class
                    rec.error_excerpt = excerpt
                    rec.source = "tool.result"
                    updated = True
                    break
            if not updated:
                evidence.records.append(
                    ToolEvidence(
                        tool_name=name,
                        invocation_id=inv,
                        attempted_at=time.time(),
                        status=status,
                        error_class=error_class,
                        source="tool.result",
                        error_excerpt=excerpt,
                    )
                )

    if replace_current:
        _CURRENT_TURN.set(evidence)
    return evidence


def _msg_role(msg: Any) -> str:
    if isinstance(msg, dict):
        return str(msg.get("role") or "")
    return str(getattr(msg, "role", "") or "")


def _msg_tool_calls(msg: Any) -> list[Any]:
    if isinstance(msg, dict):
        calls = msg.get("tool_calls") or []
    else:
        calls = getattr(msg, "tool_calls", None) or []
    return list(calls) if isinstance(calls, list) else []


def _call_name(call: Any) -> str:
    if isinstance(call, dict):
        fn = call.get("function") or {}
        if isinstance(fn, dict) and fn.get("name"):
            return str(fn["name"])
        return str(call.get("name") or "unknown")
    fn = getattr(call, "function", None)
    if fn is not None and getattr(fn, "name", None):
        return str(fn.name)
    return str(getattr(call, "name", None) or "unknown")


def _call_id(call: Any) -> str:
    if isinstance(call, dict):
        return str(call.get("id") or call.get("call_id") or uuid.uuid4().hex)
    return str(getattr(call, "id", None) or uuid.uuid4().hex)
