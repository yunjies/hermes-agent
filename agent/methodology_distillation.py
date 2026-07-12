"""Methodology distillation for profile-local AGENTS.methodology.md.

This module is intentionally parallel to, not merged into, the existing
memory/skill background review. Memory records facts, skills record task
procedures, and methodology distillation proposals record learned behavior defaults
for future turns. Active turns only capture compact signals; proposal generation
runs at a session boundary such as reset, expiry, or close.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import re
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)


DEFAULT_METHODOLOGY_DISTILLATION_CONFIG: Dict[str, Any] = {
    "enabled": True,
    "approval": "pending",
    "roles": ["user"],
    "focus": [
        "reusable_workflows",
        "methodology_rules",
        "validation_and_rollback",
        "ownership_boundaries",
        "anti_patterns",
        "engineering_gaps",
    ],
    "ignore": [
        "business_facts",
        "product_feature_requests",
        "already_landed_implementation_details",
        "raw_tool_outputs",
        "raw_search_or_wiki_dumps",
        "transient_environment_errors",
        "one_off_task_details",
    ],
    "model": {},
    "signals": [],
}


DEFAULT_SIGNALS: List[Dict[str, Any]] = [
    {
        "id": "methodology_decision",
        "phrases": [
            "\u5de5\u4f5c\u6d41",
            "\u65b9\u6cd5\u8bba",
            "\u9ed8\u8ba4\u505a\u6cd5",
            "\u8fd9\u7c7b\u95ee\u9898",
            "\u8fd9\u7c7b\u60c5\u51b5",
            "\u4ee5\u540e\u9047\u5230",
            "workflow",
            "methodology",
            "default practice",
        ],
        "outputs": ["methodology_delta"],
        "confidence": "high",
    },
    {
        "id": "methodology_correction",
        "phrases": [
            "\u4e0d\u8981\u76f4\u63a5",
            "\u4e0d\u80fd\u76f4\u63a5",
            "\u9700\u8981\u5148",
            "\u5148\u505a",
            "\u518d\u505a",
            "\u5e94\u8be5\u5148",
            "\u5e94\u8be5\u9ed8\u8ba4",
            "\u9ed8\u8ba4\u5e94\u8be5",
            "should first",
            "do not directly",
            "don't directly",
            "first validate",
            "validate before",
        ],
        "outputs": ["methodology_delta", "quality_incident"],
        "confidence": "high",
    },
    {
        "id": "workflow_rule",
        "phrases": [
            "\u6d41\u7a0b",
            "\u6b65\u9aa4",
            "\u539f\u5219",
            "\u89c4\u5219",
            "\u539f\u5219\u4e0a",
            "\u9a8c\u8bc1\u6807\u51c6",
            "\u9a8c\u8bc1",
            "\u56de\u6eda",
            "workflow rule",
            "validation criteria",
            "rollback plan",
        ],
        "outputs": ["methodology_delta"],
        "confidence": "medium",
    },
    {
        "id": "engineering_methodology_gap",
        "phrases": [
            "\u53ef\u81ea\u52a8\u4fee\u590d",
            "\u53ef\u4ee5\u81ea\u52a8\u4fee",
            "\u4e0d\u80fd\u81ea\u52a8\u4fee",
            "\u5efa task",
            "\u521b\u5efa task",
            "\u5de5\u7a0b\u5316",
            "\u7f3a\u9a8c\u8bc1",
            "\u7f3a\u5c11\u9a8c\u8bc1",
            "\u7f3a rollback",
            "\u7f3a\u56de\u6eda",
            "\u7f3a owner",
            "\u8fb9\u754c",
            "create a task",
            "owner",
            "boundary",
        ],
        "outputs": ["engineering_gap"],
        "confidence": "medium",
    },
]

SECTION_ORDER = [
    "Default Judgments",
    "Anti-patterns",
    "Learning Priorities",
    "Escalation",
    "Engineering Gaps",
    "Quality Incidents",
]


def load_methodology_distillation_config() -> Dict[str, Any]:
    """Return merged ``distillation.methodology`` config for the active profile."""
    cfg = dict(DEFAULT_METHODOLOGY_DISTILLATION_CONFIG)
    try:
        from hermes_cli.config import load_config

        full = load_config()
        dist = full.get("distillation", {}) if isinstance(full, dict) else {}
        session_cfg = {}
        if isinstance(dist, dict):
            session_cfg = dist.get("methodology", {}) or dist.get("methodology_distillation", {})
        if isinstance(session_cfg, dict):
            for key, value in session_cfg.items():
                if key in {"focus", "ignore", "signals", "roles"} and not isinstance(value, list):
                    continue
                if key == "model" and not isinstance(value, dict):
                    continue
                cfg[key] = value
    except Exception:
        pass
    return cfg


def methodology_distillation_interval() -> int:
    cfg = load_methodology_distillation_config()
    if not _truthy(cfg.get("enabled", True)):
        return 0
    return 1


def evaluate_methodology_distillation_trigger(
    agent: Any,
    messages_snapshot: List[Dict[str, Any]],
    *,
    periodic_due: bool = False,
) -> Tuple[bool, List[Dict[str, Any]]]:
    """Return whether methodology distillation should run and the compact signals."""
    cfg = load_methodology_distillation_config()
    if not _truthy(cfg.get("enabled", True)):
        return False, []
    signals = scan_methodology_distillation_signals(messages_snapshot, cfg)
    if not signals:
        return False, []
    # Periodic due lets medium-confidence signals through; explicit high
    # confidence signals run immediately.
    if periodic_due or any(s.get("confidence") == "high" for s in signals):
        return True, signals
    return False, []


def record_methodology_distillation_signals(
    agent: Any,
    messages_snapshot: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Capture cheap methodology-learning signals during an active session."""
    cfg = load_methodology_distillation_config()
    if not _truthy(cfg.get("enabled", True)):
        return []
    signals = scan_methodology_distillation_signals(messages_snapshot, cfg)
    if not signals:
        return []
    existing = getattr(agent, "_methodology_distillation_signals", None)
    if not isinstance(existing, list):
        existing = []
    seen = {
        _short_hash(json.dumps(item, ensure_ascii=False, sort_keys=True))
        for item in existing
        if isinstance(item, dict)
    }
    for signal in signals:
        key = _short_hash(json.dumps(signal, ensure_ascii=False, sort_keys=True))
        if key not in seen:
            existing.append(signal)
            seen.add(key)
    agent._methodology_distillation_signals = existing[-80:]
    return signals


def finalize_methodology_distillation(
    agent: Any,
    messages_snapshot: List[Dict[str, Any]],
    *,
    session_id: Optional[str] = None,
    reason: str = "session_boundary",
) -> bool:
    """Spawn proposal generation after a session is no longer active."""
    cfg = load_methodology_distillation_config()
    if not _truthy(cfg.get("enabled", True)):
        return False
    signals = list(getattr(agent, "_methodology_distillation_signals", []) or [])
    for signal in scan_methodology_distillation_signals(messages_snapshot, cfg, recent_messages=200):
        key = _short_hash(json.dumps(signal, ensure_ascii=False, sort_keys=True))
        if all(_short_hash(json.dumps(s, ensure_ascii=False, sort_keys=True)) != key for s in signals):
            signals.append(signal)
    if not signals:
        return False
    envelope_signal = {
        "id": "session_boundary",
        "phrase": reason,
        "outputs": ["methodology_delta"],
        "confidence": "medium",
        "source": "lifecycle",
        "session_id": session_id or getattr(agent, "session_id", "") or "",
        "evidence": [],
    }
    signals.append(envelope_signal)
    target = spawn_methodology_distillation_thread(agent, list(messages_snapshot or []), signals[-100:])
    try:
        import threading

        thread = threading.Thread(target=target, daemon=True, name="methodology-distillation")
        thread.start()
        agent._methodology_distillation_signals = []
        return True
    except Exception:
        logger.debug("methodology distillation spawn failed", exc_info=True)
        return False


def run_history_distillation(
    *,
    profile: str = "current",
    stage: bool = False,
    max_sessions: int = 50,
    max_signals_per_profile: int = 80,
) -> str:
    """Scan historical profile sessions and optionally stage pending proposals."""
    homes = _resolve_history_profile_homes(profile)
    if not homes:
        return f"No profile state.db found for profile={profile!r}."

    lines = [
        "# Methodology Distillation History",
        "",
        f"Mode: {'stage pending proposals' if stage else 'dry-run'}",
        f"Profiles: {len(homes)}",
        "",
    ]
    total_sessions = 0
    total_signals = 0
    total_staged = 0

    for profile_name, home in homes:
        sessions = _load_history_sessions(home, max_sessions=max_sessions)
        profile_signals: List[Dict[str, Any]] = []
        signal_counts: Dict[str, int] = {}
        route_counts: Dict[str, int] = {}
        for session in sessions:
            signals = scan_methodology_distillation_signals(
                session.get("messages", []),
                recent_messages=200,
            )
            if not signals:
                continue
            for signal in signals:
                signal["session_id"] = session.get("session_id") or ""
                signal["profile"] = profile_name
                sid = str(signal.get("id") or "signal")
                signal_counts[sid] = signal_counts.get(sid, 0) + 1
                review = classify_methodology_distillation_signal(signal)
                signal["review"] = review
                route = str(review.get("destination") or "methodology")
                route_counts[route] = route_counts.get(route, 0) + 1
                profile_signals.append(signal)

        if len(profile_signals) > max_signals_per_profile:
            profile_signals = profile_signals[-max_signals_per_profile:]

        staged = 0
        if stage and profile_signals:
            methodology_signals = [
                signal for signal in profile_signals
                if (signal.get("review") or {}).get("destination") == "methodology"
            ]
            proposals = fallback_methodology_distillation_proposals(methodology_signals)
            staged, _applied = _with_hermes_home(home, persist_methodology_distillation_proposals, proposals)

        total_sessions += len(sessions)
        total_signals += len(profile_signals)
        total_staged += staged
        lines.append(f"## {profile_name}")
        lines.append(f"- sessions scanned: {len(sessions)}")
        lines.append(f"- signals found: {len(profile_signals)}")
        if signal_counts:
            compact = ", ".join(f"{k}={v}" for k, v in sorted(signal_counts.items()))
            lines.append(f"- signal types: {compact}")
        if route_counts:
            compact = ", ".join(f"{k}={v}" for k, v in sorted(route_counts.items()))
            lines.append(f"- review routes: {compact}")
        if stage:
            lines.append(f"- pending proposals staged: {staged}")
        lines.append("")

    lines.extend([
        "## Total",
        f"- sessions scanned: {total_sessions}",
        f"- signals found: {total_signals}",
    ])
    if stage:
        lines.append(f"- pending proposals staged: {total_staged}")
        lines.append("")
        lines.append("Review with /distill pending and /distill diff <id> before approving.")
    else:
        lines.append("")
        lines.append("Run /distill history --stage to create pending proposals.")
    return "\n".join(lines).rstrip()


def run_history_distillation_from_args(args: List[str]) -> str:
    """Parse ``/distill history`` args and run the historical scanner."""
    profile = "current"
    stage = False
    max_sessions = 50
    i = 0
    while i < len(args):
        arg = args[i]
        if arg in {"--stage", "stage"}:
            stage = True
        elif arg in {"--dry-run", "dry-run", "dryrun"}:
            stage = False
        elif arg in {"--profile", "-p"} and i + 1 < len(args):
            profile = args[i + 1]
            i += 1
        elif arg.startswith("--profile="):
            profile = arg.split("=", 1)[1] or "current"
        elif arg in {"--limit", "--max-sessions"} and i + 1 < len(args):
            max_sessions = _safe_int(args[i + 1], max_sessions)
            i += 1
        elif arg.startswith("--limit=") or arg.startswith("--max-sessions="):
            max_sessions = _safe_int(arg.split("=", 1)[1], max_sessions)
        else:
            return (
                "Usage: /distill history [--dry-run|--stage] "
                "[--profile current|all|<name>] [--limit N]"
            )
        i += 1
    return run_history_distillation(
        profile=profile,
        stage=stage,
        max_sessions=max_sessions,
    )


def scan_methodology_distillation_signals(
    messages_snapshot: List[Dict[str, Any]],
    cfg: Optional[Dict[str, Any]] = None,
    *,
    recent_messages: int = 18,
) -> List[Dict[str, Any]]:
    """Deterministically extract high-value methodology distillation signals."""
    cfg = cfg or load_methodology_distillation_config()
    candidates = list(DEFAULT_SIGNALS)
    for item in cfg.get("signals", []) or []:
        if not isinstance(item, dict):
            continue
        phrases = item.get("phrases") or item.get("user_phrases") or []
        if isinstance(phrases, str):
            phrases = [phrases]
        if not phrases:
            continue
        candidates.append({
            "id": str(item.get("id") or "profile_signal"),
            "phrases": [str(p) for p in phrases if str(p).strip()],
            "outputs": item.get("outputs") or item.get("produce") or ["methodology_delta"],
            "confidence": str(item.get("confidence") or "high"),
            "source": "profile",
        })

    allowed_roles = _configured_roles(cfg)
    recent = []
    for msg in (messages_snapshot or [])[-recent_messages:]:
        if not isinstance(msg, dict):
            continue
        role = str(msg.get("role") or "")
        if role not in allowed_roles:
            continue
        text = _message_text(msg)
        if _looks_like_noise(text, role):
            continue
        recent.append((msg, text))

    matches: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for spec in candidates:
        for phrase in spec.get("phrases", []):
            phrase = phrase.strip()
            if not phrase:
                continue
            for msg, text in recent:
                if phrase not in text:
                    continue
                key = f"{spec.get('id')}:{phrase}:{_short_hash(text)}"
                if key in seen:
                    continue
                seen.add(key)
                matches.append({
                    "id": spec.get("id") or "signal",
                    "phrase": phrase,
                    "outputs": _as_list(spec.get("outputs")) or ["methodology_delta"],
                    "confidence": spec.get("confidence") or "medium",
                    "source": spec.get("source") or "default",
                    "evidence": [{
                        "role": msg.get("role") or "",
                        "quote": _clip(text, 700),
                    }],
                })
                break
    return matches


def spawn_methodology_distillation_thread(
    agent: Any,
    messages_snapshot: List[Dict[str, Any]],
    signals: List[Dict[str, Any]],
):
    """Build a daemon-thread target for methodology distillation proposal generation."""

    def _target() -> None:
        _run_methodology_distillation(agent, messages_snapshot, signals)

    return _target


def _run_methodology_distillation(
    agent: Any,
    messages_snapshot: List[Dict[str, Any]],
    signals: List[Dict[str, Any]],
) -> None:
    if not signals:
        return
    cfg = load_methodology_distillation_config()
    review_agent = None
    try:
        prompt = build_methodology_distillation_prompt(signals, cfg)
        runtime = _resolve_methodology_distillation_runtime(agent, cfg)

        from run_agent import AIAgent

        reasoning_config = _resolve_reasoning_config(cfg, runtime)
        review_agent = AIAgent(
            model=runtime.get("model") or agent.model,
            provider=runtime.get("provider") or agent.provider,
            api_key=runtime.get("api_key") or None,
            base_url=runtime.get("base_url") or None,
            api_mode=runtime.get("api_mode"),
            credential_pool=getattr(agent, "_credential_pool", None),
            max_iterations=2,
            quiet_mode=True,
            platform=getattr(agent, "platform", None) or "methodology_distillation",
            enabled_toolsets=[],
            skip_context_files=True,
            skip_memory=True,
            reasoning_config=reasoning_config,
        )
        review_agent._memory_nudge_interval = 0
        review_agent._skill_nudge_interval = 0
        review_agent._methodology_distillation_interval = 0
        review_agent.suppress_status_output = True
        review_agent._end_session_on_close = False

        with open(os.devnull, "w", encoding="utf-8") as devnull, \
             contextlib.redirect_stdout(devnull), \
             contextlib.redirect_stderr(devnull):
            result = review_agent.run_conversation(user_message=prompt, conversation_history=[])

        final = ""
        if isinstance(result, dict):
            final = str(result.get("final_response") or "")
        proposals = parse_methodology_distillation_model_output(final)
        if not proposals:
            proposals = fallback_methodology_distillation_proposals(signals)

        staged, applied = persist_methodology_distillation_proposals(proposals, cfg)
        if staged or applied:
            summary = []
            if staged:
                summary.append(f"staged {staged}")
            if applied:
                summary.append(f"applied {applied}")
            msg = "Methodology distillation: " + ", ".join(summary) + " proposal(s)"
            agent._safe_print(f"  🧬 {msg}")
            cb = getattr(agent, "background_review_callback", None)
            if cb:
                try:
                    cb(f"🧬 {msg}")
                except Exception:
                    pass
    except Exception as exc:
        logger.warning("Methodology distillation failed: %s", exc, exc_info=True)
        try:
            agent._emit_auxiliary_failure("methodology distillation", exc)
        except Exception:
            pass
    finally:
        if review_agent is not None:
            try:
                review_agent.close()
            except Exception:
                pass


def build_methodology_distillation_prompt(signals: List[Dict[str, Any]], cfg: Dict[str, Any]) -> str:
    envelope = {
        "signals": signals,
        "focus": cfg.get("focus") or [],
        "ignore": cfg.get("ignore") or [],
    }
    return (
        "You are the Hermes methodology distiller. Convert compact evidence from "
        "an inactive or expired session into profile-local methodology proposals.\n\n"
        "Rules:\n"
        "- Return JSON only. No markdown fence.\n"
        "- Do not rewrite SOUL.md.\n"
        "- Propose reusable workflow/methodology rules only.\n"
        "- Skip business directions, product ideas, already-landed feature detail, raw tool output, search/wiki dumps, and one-off task detail.\n"
        "- If the evidence is a concrete troubleshooting procedure, command recipe, API usage guide, or operational runbook, route it to skill and emit no methodology proposal except a very short abstract principle.\n"
        "- If the evidence is about identity, values, personality, delegation authority, or enduring human preference, route it to SOUL.md and emit no methodology proposal.\n"
        "- If the evidence is a wiki/page/document structure request, route it to docs and emit no methodology proposal unless it defines a reusable operating workflow.\n"
        "- If the evidence is business/domain content such as trading strategy, media wishlist, NAS/qB endpoint facts, or one-off task status, route it to drop/skill/docs and emit no methodology proposal.\n"
        "- Skip generic user preference unless it changes how future work should be routed, validated, sequenced, rolled back, or owned.\n"
        "- If the evidence reveals missing automation/control, use engineering_gap.\n"
        "- If the evidence reveals an agent mistake or anti-pattern, use quality_incident.\n"
        "- Keep each proposal text short and directly actionable.\n"
        "- SOUL.md and explicit user instructions always outrank these proposals.\n\n"
        "JSON schema:\n"
        "{\n"
        "  \"proposals\": [\n"
        "    {\n"
        "      \"type\": \"methodology_delta|engineering_gap|quality_incident\",\n"
        "      \"section\": \"Default Judgments|Anti-patterns|Learning Priorities|Escalation|Engineering Gaps|Quality Incidents\",\n"
        "      \"text\": \"short durable rule or gap\",\n"
        "      \"confidence\": \"low|medium|high\",\n"
        "      \"evidence\": [{\"role\": \"user|assistant|tool\", \"quote\": \"...\"}],\n"
        "      \"conflicts_with_soul\": false\n"
        "    }\n"
        "  ]\n"
        "}\n\n"
        "Envelope:\n"
        f"{json.dumps(envelope, ensure_ascii=False, indent=2)}"
    )


def parse_methodology_distillation_model_output(text: str) -> List[Dict[str, Any]]:
    raw = (text or "").strip()
    if not raw:
        return []
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
    try:
        parsed = json.loads(raw)
    except Exception:
        match = re.search(r"\{.*\}", raw, flags=re.DOTALL)
        if not match:
            return []
        try:
            parsed = json.loads(match.group(0))
        except Exception:
            return []
    proposals = parsed.get("proposals") if isinstance(parsed, dict) else parsed
    if not isinstance(proposals, list):
        return []
    return normalize_methodology_distillation_proposals(proposals)


def classify_methodology_distillation_signal(signal: Dict[str, Any]) -> Dict[str, str]:
    """Classify a candidate signal before staging methodology proposals.

    The scanner intentionally errs on recall. This review step prevents concrete
    skills, SOUL-level preferences, docs work, and business facts from leaking
    into generated AGENTS.methodology.md.
    """
    evidence = signal.get("evidence") or []
    quote = ""
    if evidence and isinstance(evidence[0], dict):
        quote = str(evidence[0].get("quote") or "")
    text = quote.strip()
    lower = text.lower()

    if _has_any(text, (
        "\u4eba\u683c",
        "\u884c\u4e3a\u51c6\u5219",
        "\u6027\u683c",
        "\u4ef7\u503c\u89c2",
        "\u6743\u9650\u7684\u5ba1\u6279",
        "\u5168\u6743\u4ee3\u7406",
        "soul.md",
        "persona",
    )):
        return {"destination": "soul", "reason": "identity, values, or enduring authority belongs in SOUL-level context"}

    if _has_any(text, (
        "\u6700\u8fd1\u4e00\u6b21\u6267\u884c\u65e5\u5fd7",
        "token\u6d88\u8017",
        "\u7b80\u5355\u6536\u96c6\u4e00\u4e0b\u8fd0\u884c\u72b6\u6001",
        "\u5148\u7b97\u4e00\u4e2a\u6e05\u5355",
        "\u5148\u5217\u4e00\u4e2a\u6e05\u5355",
    )):
        return {"destination": "drop", "reason": "one-off inspection or inventory request is not methodology"}

    if len(text) < 120 and _has_any(text, (
        "cron\u662f\u6309\u7167workflow\u53bb\u5199",
        "workflow\u5199\u9519",
    )):
        return {"destination": "drop", "reason": "short fragment lacks enough context for durable methodology"}

    if _has_any(text, (
        "\u6545\u969c\u6392\u67e5",
        "\u6545\u969c\u5904\u7406\u89c4\u8303",
        "\u62a5\u9519",
        "\u6392\u67e5",
        "\u8bf7\u5c06\u4ee5\u4e0b\u5185\u5bb9\u6574\u7406\u5e76\u6c89\u6dc0",
        "\u7f16\u5199\u7b2c\u4e00\u4e2a\u7cfb\u5217\u7684skills",
        "troubleshooting",
        "runbook",
        "provider authentication failed",
        "unknown provider",
        "models_cache.json",
        "skill.md",
        "skills",
    )):
        return {"destination": "skill", "reason": "concrete troubleshooting or executable procedure belongs in a skill"}

    if _has_any(text, (
        "\u521b\u5efa workflow contract",
        "\u66f4\u65b0 workflow page",
        "\u6587\u6863\u7ed3\u6784",
        "\u9875\u9762",
        "\u77e5\u8bc6\u5e93",
        "archive final operating report",
        "update workflow page",
        "wiki governance",
    )) and not _has_any(text, (
        "\u81ea\u68c0\u4f18\u5316\u6d41\u7a0b",
        "workflow/methodology",
    )):
        return {"destination": "docs", "reason": "document or wiki structure work belongs in docs"}

    if _has_any(text, (
        "\u4ea4\u6613\u7b56\u7565",
        "\u7b56\u7565\u7684\u771f\u5b9e\u8bb0\u5f55",
        "\u91cf\u5316",
        "media wishlist",
        "wishlist",
        "qb",
        "nas ip",
        "container ip",
        "cyclonejoker",
        "adl goal",
        "kanban board",
        "completed_task_ids",
        "status: ready",
    )):
        return {"destination": "drop", "reason": "business/domain fact or one-off task state is not methodology"}

    if _has_any(text, (
        "\u8fa9\u8bc1\u5ba1\u6838",
        "\u95e8\u7981\u89c4\u5219",
        "\u7ea2\u7ebf",
        "\u9690\u5f0f\u8d8a\u6743",
        "\u8bef\u542f\u52a8",
        "\u81ea\u68c0\u4f18\u5316\u6d41\u7a0b",
        "\u547d\u4e2d\u7387",
        "\u4e0d\u662f\u7ed3\u6784\u7684\u95ee\u9898",
        "\u975e\u7ed3\u6784\u6027",
        "\u53ef\u56de\u6eda",
        "\u56de\u6eda",
        "validation criteria",
        "rollback",
    )):
        return {"destination": "methodology", "reason": "reusable workflow, validation, boundary, or rollback rule"}

    if len(text) < 90 and _has_any(lower, ("create ", "archive ", "review ", "update ")):
        return {"destination": "drop", "reason": "short task title is not durable methodology"}

    return {"destination": "methodology", "reason": "candidate appears reusable enough for methodology review"}


def fallback_methodology_distillation_proposals(signals: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    proposals: List[Dict[str, Any]] = []
    for signal in signals:
        output = (_as_list(signal.get("outputs")) or ["methodology_delta"])[0]
        evidence = signal.get("evidence") or []
        quote = ""
        if evidence and isinstance(evidence[0], dict):
            quote = str(evidence[0].get("quote") or "")
        proposals.append({
            "type": output,
            "section": _default_section(output),
            "text": _fallback_text(output, quote),
            "confidence": signal.get("confidence") or "medium",
            "evidence": evidence,
            "conflicts_with_soul": False,
        })
    return normalize_methodology_distillation_proposals(proposals)


def persist_methodology_distillation_proposals(
    proposals: List[Dict[str, Any]],
    cfg: Optional[Dict[str, Any]] = None,
) -> Tuple[int, int]:
    """Stage or apply normalized proposals. Returns ``(staged, applied)``."""
    cfg = cfg or load_methodology_distillation_config()
    approval = str(cfg.get("approval") or "pending").strip().lower()
    staged = 0
    applied = 0
    for proposal in normalize_methodology_distillation_proposals(proposals):
        if _proposal_exists(proposal):
            continue
        if approval == "auto" and not _methodology_distillation_write_approval_enabled():
            result = apply_methodology_distillation_pending({"action": "apply", "proposal": proposal})
            if result.get("success"):
                applied += 1
            continue
        if stage_methodology_distillation_proposal(proposal):
            staged += 1
    return staged, applied


def stage_methodology_distillation_proposal(proposal: Dict[str, Any]) -> bool:
    try:
        from tools import write_approval as wa

        record = wa.stage_write(
            wa.METHODOLOGY_DISTILLATION,
            {"action": "apply", "proposal": proposal},
            summary=methodology_distillation_gist(proposal),
            origin="background_review",
        )
        return bool(record.get("id"))
    except Exception:
        logger.warning("Failed to stage methodology distillation proposal", exc_info=True)
        return False


def apply_methodology_distillation_pending(payload: Dict[str, Any]) -> Dict[str, Any]:
    proposal = payload.get("proposal") if isinstance(payload, dict) else None
    if not isinstance(proposal, dict):
        return {"success": False, "error": "methodology distillation pending payload has no proposal"}
    proposal = normalize_methodology_distillation_proposals([proposal])[0]
    proposal["accepted_at"] = time.time()
    base = _methodology_distillation_dir("accepted")
    base.mkdir(parents=True, exist_ok=True)
    path = base / f"{proposal['proposal_hash']}.json"
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(proposal, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    regenerate_methodology_agents()
    return {"success": True, "proposal_hash": proposal["proposal_hash"]}


def reject_methodology_distillation_pending(record: Dict[str, Any]) -> bool:
    try:
        payload = record.get("payload", {}) if isinstance(record, dict) else {}
        proposal = payload.get("proposal", {})
        if isinstance(proposal, dict):
            proposal = normalize_methodology_distillation_proposals([proposal])[0]
        else:
            proposal = {"raw": payload}
        proposal["rejected_at"] = time.time()
        proposal["pending_id"] = record.get("id")
        base = _methodology_distillation_dir("rejected")
        base.mkdir(parents=True, exist_ok=True)
        name = proposal.get("proposal_hash") or record.get("id") or _short_hash(json.dumps(payload, sort_keys=True))
        path = base / f"{name}.json"
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(proposal, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)
        return True
    except Exception:
        logger.warning("Failed to archive rejected methodology distillation proposal", exc_info=True)
        return False


def regenerate_methodology_agents() -> Path:
    proposals = _load_accepted_proposals()
    sections: Dict[str, List[str]] = {name: [] for name in SECTION_ORDER}
    for proposal in proposals[-80:]:
        section = proposal.get("section") or _default_section(proposal.get("type"))
        if section not in sections:
            section = "Default Judgments"
        text = str(proposal.get("text") or "").strip()
        if not text:
            continue
        line = f"- {text}"
        if line not in sections[section]:
            sections[section].append(line)

    lines = [
        "# AGENTS.methodology.md",
        "",
        "Generated by Hermes methodology distillation. Do not edit this file manually.",
        "Edit SOUL.md or reject/correct methodology distillation proposals when the learning is wrong.",
        "",
        "Priority: SOUL.md > manual AGENTS/project context > AGENTS.methodology.md.",
        "If these methodology notes conflict with SOUL.md or explicit user instructions, ignore them.",
        "",
    ]
    for section in SECTION_ORDER:
        entries = sections.get(section) or []
        if not entries:
            continue
        lines.extend([f"## {section}", "", *entries, ""])

    path = get_hermes_home() / "AGENTS.methodology.md"
    tmp = path.with_suffix(".methodology.md.tmp")
    tmp.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return path


def methodology_distillation_pending_diff(record: Dict[str, Any]) -> str:
    payload = record.get("payload", {}) if isinstance(record, dict) else {}
    proposal = payload.get("proposal", payload)
    return json.dumps(proposal, ensure_ascii=False, indent=2)


def methodology_distillation_gist(proposal: Dict[str, Any]) -> str:
    typ = proposal.get("type") or "methodology_delta"
    text = str(proposal.get("text") or "").strip().replace("\n", " ")
    return f"{typ}: {_clip(text, 140)}"


def normalize_methodology_distillation_proposals(proposals: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    normalized: List[Dict[str, Any]] = []
    for item in proposals or []:
        if not isinstance(item, dict):
            continue
        typ = str(item.get("type") or "methodology_delta").strip()
        if typ not in {"methodology_delta", "engineering_gap", "quality_incident"}:
            typ = "methodology_delta"
        section = str(item.get("section") or _default_section(typ)).strip()
        if section not in SECTION_ORDER:
            section = _default_section(typ)
        text = str(item.get("text") or item.get("content") or "").strip()
        if not text:
            continue
        evidence = item.get("evidence") if isinstance(item.get("evidence"), list) else []
        clean = {
            "type": typ,
            "section": section,
            "text": _clip(text.replace("\r\n", "\n"), 500),
            "confidence": str(item.get("confidence") or "medium"),
            "evidence": _clean_evidence(evidence),
            "conflicts_with_soul": bool(item.get("conflicts_with_soul", False)),
        }
        clean["proposal_hash"] = _proposal_hash(clean)
        normalized.append(clean)
    return normalized


def _resolve_methodology_distillation_runtime(agent: Any, cfg: Dict[str, Any]) -> Dict[str, Any]:
    parent_runtime = agent._current_main_runtime()
    parent_api_mode = parent_runtime.get("api_mode") or None
    if parent_api_mode == "codex_app_server":
        parent_api_mode = "codex_responses"
    parent = {
        "provider": agent.provider,
        "model": agent.model,
        "api_key": parent_runtime.get("api_key") or None,
        "base_url": parent_runtime.get("base_url") or None,
        "api_mode": parent_api_mode,
    }

    model_cfg = cfg.get("model") if isinstance(cfg.get("model"), dict) else {}
    candidates = [model_cfg]
    try:
        from hermes_cli.config import load_config

        full = load_config()
        aux = full.get("auxiliary", {}) if isinstance(full, dict) else {}
        if isinstance(aux, dict):
            methodology_distillation_aux = aux.get("methodology_distillation", {})
            bg_aux = aux.get("background_review", {})
            if isinstance(methodology_distillation_aux, dict):
                candidates.append(methodology_distillation_aux)
            if isinstance(bg_aux, dict):
                candidates.append(bg_aux)
    except Exception:
        pass

    for candidate in candidates:
        provider = str(candidate.get("provider", "")).strip() if isinstance(candidate, dict) else ""
        model = str(candidate.get("model", "")).strip() if isinstance(candidate, dict) else ""
        if not provider or provider == "auto" or not model:
            continue
        try:
            from hermes_cli.runtime_provider import resolve_runtime_provider

            rp = resolve_runtime_provider(
                requested=provider,
                target_model=model,
                explicit_api_key=str(candidate.get("api_key", "")).strip() or None,
                explicit_base_url=str(candidate.get("base_url", "")).strip() or None,
            )
            return {
                "provider": rp.get("provider") or provider,
                "model": model,
                "api_key": rp.get("api_key"),
                "base_url": rp.get("base_url"),
                "api_mode": rp.get("api_mode"),
                "reasoning": candidate.get("reasoning"),
            }
        except Exception:
            logger.debug("methodology distillation runtime resolution failed", exc_info=True)
    return parent


def _resolve_reasoning_config(cfg: Dict[str, Any], runtime: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    model_cfg = cfg.get("model") if isinstance(cfg.get("model"), dict) else {}
    raw = runtime.get("reasoning") or model_cfg.get("reasoning") or cfg.get("reasoning")
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        return {"effort": raw.strip()}
    return None


def _methodology_distillation_write_approval_enabled() -> bool:
    try:
        from tools import write_approval as wa

        return wa.write_approval_enabled(wa.METHODOLOGY_DISTILLATION)
    except Exception:
        return True


def _proposal_exists(proposal: Dict[str, Any]) -> bool:
    proposal_hash = proposal.get("proposal_hash")
    if not proposal_hash:
        return False
    if (_methodology_distillation_dir("accepted") / f"{proposal_hash}.json").exists():
        return True
    try:
        from tools import write_approval as wa

        for record in wa.list_pending(wa.METHODOLOGY_DISTILLATION):
            payload = record.get("payload", {})
            existing = payload.get("proposal", {}) if isinstance(payload, dict) else {}
            if existing.get("proposal_hash") == proposal_hash:
                return True
    except Exception:
        pass
    return False


def _load_accepted_proposals() -> List[Dict[str, Any]]:
    base = _methodology_distillation_dir("accepted")
    if not base.exists():
        return []
    records: List[Dict[str, Any]] = []
    for path in base.glob("*.json"):
        try:
            item = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(item, dict):
                records.append(item)
        except Exception:
            logger.warning("Skipping unreadable methodology distillation record: %s", path)
    records.sort(key=lambda r: r.get("accepted_at", 0))
    return records


def _methodology_distillation_dir(name: str) -> Path:
    return get_hermes_home() / "distillation" / "methodology_distillation" / name


def _resolve_history_profile_homes(profile: str) -> List[Tuple[str, Path]]:
    current = get_hermes_home()
    profile = (profile or "current").strip()
    profiles_root = current.parent if current.parent.name == "profiles" else current / "profiles"
    if profile in {"current", "."}:
        return [(current.name, current)] if (current / "state.db").exists() else []
    if profile in {"all", "*"}:
        homes = []
        root_home = profiles_root.parent if profiles_root.name == "profiles" else current
        if (root_home / "state.db").exists():
            homes.append(("default", root_home))
        if not profiles_root.exists():
            return homes
        for home in sorted(profiles_root.iterdir()):
            if home.is_dir() and (home / "state.db").exists():
                homes.append((home.name, home))
        return homes
    if profile == "default":
        root_home = profiles_root.parent if profiles_root.name == "profiles" else current
        return [("default", root_home)] if (root_home / "state.db").exists() else []
    home = profiles_root / profile
    return [(profile, home)] if (home / "state.db").exists() else []


def _load_history_sessions(profile_home: Path, *, max_sessions: int) -> List[Dict[str, Any]]:
    db_path = profile_home / "state.db"
    if not db_path.exists():
        return []
    limit = max(1, int(max_sessions or 50))
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        session_cols = {
            row[1] for row in con.execute("pragma table_info(sessions)").fetchall()
        }
        order_col = "ended_at" if "ended_at" in session_cols else "started_at"
        rows = con.execute(
            f"select id from sessions order by {order_col} desc, id desc limit ?",
            (limit,),
        ).fetchall()
        sessions = []
        for (session_id,) in rows:
            messages = _load_history_messages(con, str(session_id))
            if messages:
                sessions.append({"session_id": str(session_id), "messages": messages})
        return sessions
    finally:
        con.close()


def _load_history_messages(con: sqlite3.Connection, session_id: str) -> List[Dict[str, Any]]:
    rows = con.execute(
        "select role, content from messages where session_id = ? order by id asc",
        (session_id,),
    ).fetchall()
    messages: List[Dict[str, Any]] = []
    for role, content in rows:
        role = str(role or "")
        if role not in {"user", "assistant", "tool"}:
            continue
        text = _history_content_text(content)
        if text:
            messages.append({"role": role, "content": text})
    return messages


def _history_content_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        raw = content.strip()
    else:
        raw = str(content).strip()
    if not raw:
        return ""
    if raw[:1] in {"[", "{"}:
        try:
            parsed = json.loads(raw)
        except Exception:
            return raw
        if isinstance(parsed, str):
            return parsed.strip()
        if isinstance(parsed, list):
            parts = []
            for item in parsed:
                if isinstance(item, dict):
                    parts.append(str(item.get("text") or item.get("content") or ""))
                elif isinstance(item, str):
                    parts.append(item)
            return " ".join(p for p in parts if p).strip()
        if isinstance(parsed, dict):
            return str(parsed.get("text") or parsed.get("content") or raw).strip()
    return raw


def _with_hermes_home(home: Path, fn, *args, **kwargs):
    old = os.environ.get("HERMES_HOME")
    os.environ["HERMES_HOME"] = str(home)
    try:
        return fn(*args, **kwargs)
    finally:
        if old is None:
            os.environ.pop("HERMES_HOME", None)
        else:
            os.environ["HERMES_HOME"] = old


def _message_text(message: Dict[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                parts.append(str(item.get("text") or item.get("content") or ""))
        return " ".join(p for p in parts if p).strip()
    return ""


def _configured_roles(cfg: Dict[str, Any]) -> set[str]:
    raw = cfg.get("roles")
    roles = _as_list(raw) if raw is not None else ["user"]
    clean = {role for role in roles if role in {"user", "assistant", "tool"}}
    return clean or {"user"}


def _looks_like_noise(text: str, role: str) -> bool:
    text = (text or "").strip()
    if not text:
        return True
    lower = text.lower()
    if any(
        marker in text
        for marker in (
            "[IMPORTANT:",
            "[CONTEXT COMPACTION",
            "[ASYNC DELEGATION",
            "You are executing exactly one Hermes workflow node.",
        )
    ):
        return True
    if "Workflow:" in text and "Run:" in text and "Node:" in text and "Profile:" in text:
        return True
    if text[:1] in {"{", "["} and any(
        key in lower
        for key in (
            '"success"',
            '"results"',
            '"query"',
            '"matches_text"',
            '"total_count"',
            '"tool_uses"',
            '"stdout"',
            '"stderr"',
            '"exit_code"',
        )
    ):
        return True
    if any(marker in lower for marker in ("matches_format", "path-grouped", "ref_id", "turn0search")):
        return True
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(lines) >= 8:
        numbered = sum(1 for line in lines[:20] if re.match(r"^\d+\|", line))
        fileish = sum(1 for line in lines[:20] if re.search(r"\.(py|md|json|ya?ml|txt)(:\d+)?", line))
        if numbered >= 5 or fileish >= 8:
            return True
    if len(text) > 5000 and not any(
        phrase in text
        for phrase in (
            "\u5de5\u4f5c\u6d41",
            "\u65b9\u6cd5\u8bba",
            "\u8fd9\u7c7b\u95ee\u9898",
            "\u4ee5\u540e\u9047\u5230",
            "workflow",
            "methodology",
        )
    ):
        return True
    return False


def _has_any(text: str, needles: Iterable[str]) -> bool:
    lower = text.lower()
    for needle in needles:
        needle = str(needle)
        if not needle:
            continue
        haystack = lower if needle.isascii() else text
        probe = needle.lower() if needle.isascii() else needle
        if probe in haystack:
            return True
    return False


def _clean_evidence(evidence: Iterable[Dict[str, Any]]) -> List[Dict[str, str]]:
    clean: List[Dict[str, str]] = []
    for item in evidence or []:
        if not isinstance(item, dict):
            continue
        quote = str(item.get("quote") or "").strip()
        if not quote:
            continue
        clean.append({
            "role": str(item.get("role") or ""),
            "quote": _clip(quote, 700),
        })
    return clean[:4]


def _fallback_text(output: str, quote: str) -> str:
    quote = _clip((quote or "").replace("\n", " "), 220)
    if output == "engineering_gap":
        return f"Review this session for a reusable automation, validation, ownership, or rollback gap: {quote}"
    if output == "quality_incident":
        return f"Avoid repeating the reusable workflow anti-pattern corrected in this session: {quote}"
    return f"Apply this reusable workflow or methodology rule: {quote}"


def _default_section(output: Optional[str]) -> str:
    if output == "engineering_gap":
        return "Engineering Gaps"
    if output == "quality_incident":
        return "Quality Incidents"
    return "Default Judgments"


def _proposal_hash(proposal: Dict[str, Any]) -> str:
    payload = {
        "type": proposal.get("type"),
        "section": proposal.get("section"),
        "text": proposal.get("text"),
    }
    return _short_hash(json.dumps(payload, ensure_ascii=False, sort_keys=True))


def _short_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="ignore")).hexdigest()[:12]


def _clip(text: str, limit: int) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "..."


def _as_list(value: Any) -> List[str]:
    if isinstance(value, list):
        return [str(v) for v in value if str(v).strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() not in {"0", "false", "no", "off", "disabled"}
    return bool(value)


def _safe_int(value: Any, default: int) -> int:
    try:
        parsed = int(value)
    except Exception:
        return default
    return parsed if parsed > 0 else default


__all__ = [
    "apply_methodology_distillation_pending",
    "evaluate_methodology_distillation_trigger",
    "finalize_methodology_distillation",
    "load_methodology_distillation_config",
    "record_methodology_distillation_signals",
    "run_history_distillation",
    "run_history_distillation_from_args",
    "methodology_distillation_interval",
    "methodology_distillation_pending_diff",
    "reject_methodology_distillation_pending",
    "regenerate_methodology_agents",
    "classify_methodology_distillation_signal",
    "scan_methodology_distillation_signals",
    "spawn_methodology_distillation_thread",
]
