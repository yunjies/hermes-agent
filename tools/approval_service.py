"""Runtime Approval Service.

This module owns approval request state, policy checks, audit events, preflight
validation, and producer callback results. It deliberately does not apply
producer artifacts.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from hermes_constants import get_hermes_home


APPROVAL_STATUSES = {"pending", "approved", "rejected", "expired", "revoked", "escalated"}
TERMINAL_STATUSES = {"rejected", "expired", "revoked"}
NON_TERMINAL_STATUSES = {"pending", "approved", "escalated"}
CALLBACK_RESULTS = {"not_started", "applied", "apply_failed", "skipped"}
ACTOR_TYPES = {"user", "profile", "system", "runtime_auto"}
MODES = {"disabled", "shadow", "enforce"}

_PRODUCER_RESOLVERS: Dict[str, Callable[[str], Optional[Dict[str, Any]]]] = {}


class ApprovalError(RuntimeError):
    pass


def _now() -> float:
    return time.time()


def _iso(ts: Optional[float] = None) -> str:
    from datetime import datetime, timezone

    return datetime.fromtimestamp(_now() if ts is None else ts, timezone.utc).isoformat()


def current_mode() -> str:
    raw = os.environ.get("HERMES_APPROVAL_SERVICE_MODE", "shadow").strip().lower()
    return raw if raw in MODES else "shadow"


def _approval_dir() -> Path:
    return get_hermes_home() / "approvals"


def _requests_path() -> Path:
    return _approval_dir() / "requests.json"


def _audit_path() -> Path:
    return _approval_dir() / "audit.jsonl"


def _examples_path() -> Path:
    return _approval_dir() / "decision_examples.jsonl"


def _lock_dir() -> Path:
    return _approval_dir() / ".lock"


def _current_profile_id() -> str:
    try:
        from hermes_cli.profiles import get_active_profile_name

        profile = get_active_profile_name()
    except Exception:
        profile = os.environ.get("HERMES_PROFILE")
    profile = (profile or "").strip()
    return profile or "default"


@contextmanager
def _store_lock(timeout: float = 5.0):
    base = _approval_dir()
    base.mkdir(parents=True, exist_ok=True)
    lock = _lock_dir()
    deadline = _now() + timeout
    acquired = False
    while not acquired:
        try:
            os.mkdir(lock)
            acquired = True
        except FileExistsError:
            if _now() >= deadline:
                raise ApprovalError("approval store lock timeout")
            time.sleep(0.02)
    try:
        yield
    finally:
        try:
            os.rmdir(lock)
        except OSError:
            pass


def _atomic_write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def _append_jsonl(path: Path, record: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


def _load_requests() -> List[Dict[str, Any]]:
    path = _requests_path()
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        backup = path.with_suffix(f".corrupt.{int(_now())}.json")
        try:
            shutil.copy2(path, backup)
        except Exception:
            pass
        return []


def _save_requests(requests: List[Dict[str, Any]]) -> None:
    _atomic_write_json(_requests_path(), requests)
    _write_indexes(requests)


def _write_indexes(requests: List[Dict[str, Any]]) -> None:
    idx = _approval_dir() / "indexes"
    by_status: Dict[str, List[str]] = {}
    by_artifact: Dict[str, List[str]] = {}
    by_owner: Dict[str, List[str]] = {}
    for req in requests:
        rid = req.get("id")
        if not rid:
            continue
        by_status.setdefault(str(req.get("status", "")), []).append(rid)
        artifact_id = str(req.get("target", {}).get("artifact_id", ""))
        if artifact_id:
            by_artifact.setdefault(artifact_id, []).append(rid)
        owner = str(req.get("owner", {}).get("profile_id", ""))
        if owner:
            by_owner.setdefault(owner, []).append(rid)
    _atomic_write_json(idx / "by_status.json", by_status)
    _atomic_write_json(idx / "by_artifact.json", by_artifact)
    _atomic_write_json(idx / "by_owner.json", by_owner)


def stable_hash(value: Any) -> str:
    blob = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def register_producer_resolver(name: str, resolver: Callable[[str], Optional[Dict[str, Any]]]) -> None:
    _PRODUCER_RESOLVERS[name] = resolver


def resolve_artifact(artifact_id: str) -> Optional[Dict[str, Any]]:
    for resolver in list(_PRODUCER_RESOLVERS.values()):
        found = resolver(artifact_id)
        if found:
            return found
    return None


def _actor(actor_type: str = "user", actor_id: str = "user", display_name: Optional[str] = None) -> Dict[str, Any]:
    if actor_type not in ACTOR_TYPES:
        actor_type = "system"
    return {"type": actor_type, "id": actor_id, "display_name": display_name}


def _audit_event(
    event_type: str,
    request: Dict[str, Any],
    *,
    actor: Optional[Dict[str, Any]] = None,
    decision: Optional[Dict[str, Any]] = None,
    constraints_checked: Optional[Dict[str, str]] = None,
    callback: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    target = request.get("target", {})
    return {
        "event_id": f"evt_{uuid.uuid4().hex[:12]}",
        "request_id": request.get("id"),
        "event_type": event_type,
        "actor": actor or _actor("system", "approval_service"),
        "request_snapshot": {
            "request_type": _request_type_label(request),
            "artifact_id": target.get("artifact_id"),
            "artifact_hash": target.get("artifact_hash"),
            "owner": request.get("owner", {}).get("profile_id"),
            "risk_level": request.get("risk", {}).get("level"),
        },
        "decision": decision or {
            "result": request.get("decision", {}).get("result"),
            "policy_rule": request.get("decision", {}).get("policy_rule"),
            "reason": request.get("decision", {}).get("reason"),
        },
        "constraints_checked": constraints_checked or {},
        "callback": callback or {
            "producer": request.get("callback", {}).get("producer"),
            "operation": request.get("callback", {}).get("operation"),
            "result": request.get("callback_result", {}).get("result"),
        },
        "timestamp": _iso(),
    }


def _request_type_label(req: Dict[str, Any]) -> str:
    rt = req.get("request_type", {})
    parts = [rt.get("domain"), rt.get("operation"), rt.get("subtype")]
    return ".".join(str(p) for p in parts if p)


def _default_request(
    *,
    request_type: Dict[str, Any],
    source: Optional[Dict[str, Any]],
    owner: Optional[Dict[str, Any]],
    target: Dict[str, Any],
    risk: Optional[Dict[str, Any]],
    evidence: Optional[Dict[str, Any]],
    callback: Dict[str, Any],
    expires_at: Optional[float] = None,
) -> Dict[str, Any]:
    ts = _iso()
    return {
        "id": f"req_{uuid.uuid4().hex[:12]}",
        "request_type": request_type,
        "source": source or {"profile_id": None, "session_id": None, "requested_by": "system", "request_message_id": None},
        "owner": owner or {"profile_id": None, "scope": None},
        "target": target,
        "risk": {
            "level": (risk or {}).get("level", "L1"),
            "external_side_effect": bool((risk or {}).get("external_side_effect", False)),
            "secret_access": bool((risk or {}).get("secret_access", False)),
            "deletion": bool((risk or {}).get("deletion", False)),
            "cost_or_purchase": bool((risk or {}).get("cost_or_purchase", False)),
            "cross_scope": bool((risk or {}).get("cross_scope", False)),
            "authority_change": bool((risk or {}).get("authority_change", False)),
        },
        "evidence": evidence or {"reason": "", "diff_ref": None, "source_refs": [], "verification_plan": None, "rollback_plan": None},
        "policy": {"selected_rule": None, "required_approver": None, "allowed_decision_paths": []},
        "callback": callback,
        "status": "pending",
        "decision": {"result": None, "decided_by": None, "reason": None, "policy_rule": None, "decided_at": None},
        "callback_result": {"result": "not_started", "reason": None, "applied_ref": None, "recorded_at": None},
        "timestamps": {"created_at": ts, "updated_at": ts, "expires_at": _iso(expires_at) if expires_at else None},
    }


def ensure_request_for_artifact(
    *,
    request_type: Dict[str, Any],
    source: Optional[Dict[str, Any]],
    owner: Optional[Dict[str, Any]],
    target: Dict[str, Any],
    risk: Optional[Dict[str, Any]],
    evidence: Optional[Dict[str, Any]],
    callback: Dict[str, Any],
    expires_at: Optional[float] = None,
) -> Dict[str, Any]:
    if current_mode() == "disabled":
        raise ApprovalError("approval service disabled")
    artifact_id = str(target.get("artifact_id") or "")
    artifact_hash = str(target.get("artifact_hash") or "")
    if not artifact_id or not artifact_hash:
        raise ApprovalError("target.artifact_id and target.artifact_hash are required")

    with _store_lock():
        requests = _load_requests()
        for req in requests:
            req_target = req.get("target", {})
            if (
                req_target.get("artifact_id") == artifact_id
                and req_target.get("artifact_hash") == artifact_hash
                and req.get("status") in NON_TERMINAL_STATUSES
                and not _is_expired(req)
            ):
                return req
        req = _default_request(
            request_type=request_type,
            source=source,
            owner=owner,
            target=target,
            risk=risk,
            evidence=evidence,
            callback=callback,
            expires_at=expires_at,
        )
        constraints, selected = evaluate_policy(req)
        req["policy"] = selected
        if any(v == "fail" for v in constraints.values()):
            req["status"] = "escalated"
            req["decision"]["result"] = "escalated"
            req["decision"]["policy_rule"] = selected.get("selected_rule")
            req["decision"]["reason"] = "Policy gates require escalation."
        requests.append(req)
        _save_requests(requests)
        _append_jsonl(_audit_path(), _audit_event("request_created", req))
        _append_jsonl(_audit_path(), _audit_event("policy_evaluated", req, constraints_checked=constraints))
        return req


def evaluate_policy(req: Dict[str, Any]) -> Tuple[Dict[str, str], Dict[str, Any]]:
    risk = req.get("risk", {})
    evidence = req.get("evidence", {})
    owner = req.get("owner", {})
    level = str(risk.get("level", "L1")).upper()
    try:
        risk_num = int(level.lstrip("L"))
    except ValueError:
        risk_num = 1
    constraints = {
        "owner": "pass" if owner.get("profile_id") else "fail",
        "scope": "pass",
        "risk": "pass",
        "rollback": "skipped",
        "verification": "pass" if evidence.get("verification_plan") else "fail",
        "expiry": "pass" if not _is_expired(req) else "fail",
        "artifact_hash": "pass" if req.get("target", {}).get("artifact_hash") else "fail",
    }
    if any(bool(risk.get(k)) for k in ("deletion", "secret_access", "cost_or_purchase", "authority_change", "external_side_effect")) or risk_num >= 4:
        constraints["risk"] = "fail"
    if risk_num >= 3:
        constraints["rollback"] = "pass" if evidence.get("rollback_plan") else "fail"
    rule = "manual_approval_required" if any(v == "fail" for v in constraints.values()) else "explicit_user_approval"
    return constraints, {
        "selected_rule": rule,
        "required_approver": "user",
        "allowed_decision_paths": ["user"],
    }


def list_requests(status: Optional[str] = None) -> List[Dict[str, Any]]:
    reqs = _load_requests()
    if status:
        reqs = [r for r in reqs if r.get("status") == status]
    return sorted(reqs, key=lambda r: r.get("timestamps", {}).get("created_at", ""))


def get_request(request_id: str) -> Optional[Dict[str, Any]]:
    for req in _load_requests():
        if req.get("id") == request_id:
            return req
    return None


def _update_request(
    request_id: str,
    updater: Callable[[Dict[str, Any]], Tuple[str, Dict[str, Any]]],
    *,
    actor: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    with _store_lock():
        requests = _load_requests()
        for idx, req in enumerate(requests):
            if req.get("id") == request_id:
                event_type, updated = updater(req)
                updated.setdefault("timestamps", {})["updated_at"] = _iso()
                requests[idx] = updated
                _save_requests(requests)
                _append_jsonl(_audit_path(), _audit_event(event_type, updated, actor=actor))
                return updated
    raise ApprovalError(f"approval request not found: {request_id}")


def decide_request(
    request_id: str,
    result: str,
    *,
    reason: str = "",
    actor_type: str = "user",
    actor_id: str = "user",
    display_name: Optional[str] = None,
) -> Dict[str, Any]:
    if result not in {"approved", "rejected"}:
        raise ApprovalError("result must be approved or rejected")

    def _up(req: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
        if _is_expired(req):
            req["status"] = "expired"
            return "request_expired", req
        if req.get("status") in {"expired", "revoked"}:
            raise ApprovalError(f"cannot decide request in status {req.get('status')}")
        if req.get("status") == "rejected":
            raise ApprovalError("rejected requests must be resubmitted as a new request")
        if req.get("status") == "approved" and result == "rejected":
            raise ApprovalError("approved requests must be revoked, not rejected")
        req["status"] = result
        req["decision"] = {
            "result": result,
            "decided_by": _actor(actor_type, actor_id, display_name),
            "reason": reason,
            "policy_rule": "explicit_user_approval" if result == "approved" else "explicit_user_rejection",
            "decided_at": _iso(),
        }
        return "decision_recorded", req

    decision_actor = _actor(actor_type, actor_id, display_name)
    updated = _update_request(request_id, _up, actor=decision_actor)
    if actor_type in {"user", "profile"} and result in {"approved", "rejected"}:
        _append_decision_example(updated)
    return updated


def revoke_request(request_id: str, *, reason: str = "", actor_type: str = "user", actor_id: str = "user") -> Dict[str, Any]:
    def _up(req: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
        if req.get("status") != "approved":
            raise ApprovalError("only approved requests can be revoked")
        req["status"] = "revoked"
        req["decision"]["reason"] = reason or req.get("decision", {}).get("reason")
        return "request_revoked", req

    return _update_request(request_id, _up, actor=_actor(actor_type, actor_id))


def expire_request(request_id: str) -> Dict[str, Any]:
    def _up(req: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
        req["status"] = "expired"
        return "request_expired", req

    return _update_request(request_id, _up)


def _is_expired(req: Dict[str, Any]) -> bool:
    expires_at = req.get("timestamps", {}).get("expires_at")
    if not expires_at:
        return False
    try:
        from datetime import datetime

        return datetime.fromisoformat(expires_at).timestamp() <= _now()
    except Exception:
        return False


def preflight(request_id: str, *, artifact_hash: str, producer: str, executor: Optional[str] = None) -> Dict[str, Any]:
    req = get_request(request_id)
    failed: List[str] = []
    if not req:
        return {"result": "fail", "request_id": request_id, "reason": "request not found", "failed_gates": ["request"]}
    if req.get("status") != "approved":
        failed.append("status")
    if _is_expired(req):
        failed.append("expiry")
    if req.get("status") == "revoked":
        failed.append("revoked")
    if req.get("target", {}).get("artifact_hash") != artifact_hash:
        failed.append("artifact_hash")
    if req.get("callback", {}).get("producer") != producer:
        failed.append("producer")
    if executor and executor != producer:
        failed.append("executor")
    result = "fail" if failed else "pass"
    event_type = "preflight_failed" if failed else "preflight_passed"
    _append_jsonl(_audit_path(), _audit_event(event_type, req, constraints_checked={k: "fail" for k in failed}))
    return {
        "result": result,
        "request_id": request_id,
        "reason": ", ".join(failed) if failed else None,
        "failed_gates": failed,
    }


def record_callback_result(
    request_id: str,
    *,
    result: str,
    reason: str = "",
    applied_ref: Optional[str] = None,
    producer: Optional[str] = None,
) -> Dict[str, Any]:
    if result not in CALLBACK_RESULTS - {"not_started"}:
        raise ApprovalError("invalid callback result")

    def _up(req: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
        if producer and req.get("callback", {}).get("producer") != producer:
            return "callback_rejected", req
        req["callback_result"] = {
            "result": result,
            "reason": reason,
            "applied_ref": applied_ref,
            "recorded_at": _iso(),
        }
        return "callback_recorded", req

    return _update_request(request_id, _up)


def format_request(req: Dict[str, Any]) -> str:
    target = req.get("target", {})
    decision = req.get("decision", {})
    return "\n".join([
        f"Request: {req.get('id')}",
        f"Status: {req.get('status')}",
        f"Type: {_request_type_label(req)}",
        f"Target: {target.get('artifact_id')} ({target.get('summary', '')})",
        f"Owner: {req.get('owner', {}).get('profile_id') or 'unknown'}",
        f"Policy: {req.get('policy', {}).get('selected_rule') or 'none'}",
        f"Decision: {decision.get('result') or 'pending'}",
        f"Callback: {req.get('callback_result', {}).get('result')}",
    ])


def format_decision_result(req: Dict[str, Any], *, action: str) -> str:
    decision = req.get("decision", {})
    actor = decision.get("decided_by") or {}
    target = req.get("target", {})
    result = "APPROVED" if req.get("status") == "approved" else "REJECTED" if req.get("status") == "rejected" else req.get("status", "").upper()
    return "\n".join([
        f"Approval result: {result}",
        f"Request: {req.get('id')}",
        f"Type: {_request_type_label(req)}",
        f"Target: {target.get('artifact_id')}",
        f"Decision path: {actor.get('type') or action}",
        f"Decision by: {actor.get('id') or 'unknown'}",
        f"Policy: {decision.get('policy_rule') or req.get('policy', {}).get('selected_rule') or 'none'}",
        f"Reason: {decision.get('reason') or ''}",
        f"Result: {req.get('status')}, waiting for producer callback.",
    ])


def _redact(value: Any) -> Any:
    if isinstance(value, str):
        redacted = re.sub(r"(?i)(api[_-]?key|token|secret|password|credential)[=:][^\s,]+", r"\1=<redacted>", value)
        redacted = re.sub(r"(?i)(sk-[A-Za-z0-9_-]{12,})", "<redacted-key>", redacted)
        return redacted
    if isinstance(value, list):
        return [_redact(v) for v in value]
    if isinstance(value, dict):
        return {k: _redact(v) for k, v in value.items()}
    return value


def _append_decision_example(req: Dict[str, Any]) -> None:
    decision = req.get("decision", {})
    if decision.get("result") not in {"approved", "rejected"}:
        return
    example = {
        "request_id": req.get("id"),
        "request_type": req.get("request_type", {}),
        "target": {
            "artifact_type": req.get("target", {}).get("artifact_type"),
            "summary": req.get("target", {}).get("summary"),
            "affected_paths": req.get("target", {}).get("affected_paths", []),
        },
        "owner": req.get("owner", {}),
        "risk": req.get("risk", {}),
        "decision": {
            "result": decision.get("result"),
            "decided_by": decision.get("decided_by"),
            "reason": decision.get("reason"),
            "decided_at": decision.get("decided_at"),
        },
        "policy": {
            "rule": decision.get("policy_rule"),
            "decision_path": (decision.get("decided_by") or {}).get("type"),
        },
        "outcome": {
            "callback_result": req.get("callback_result", {}).get("result", "not_started"),
            "verification_result": "unknown",
            "rollback_used": False,
        },
        "tags": [],
    }
    _append_jsonl(_examples_path(), _redact(example))


def list_decision_examples() -> List[Dict[str, Any]]:
    path = _examples_path()
    if not path.exists():
        return []
    rows: List[Dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except Exception:
            continue
    return rows


def get_decision_example(request_id: str) -> Optional[Dict[str, Any]]:
    for row in list_decision_examples():
        if row.get("request_id") == request_id:
            return row
    return None


def request_for_write_pending(subsystem: str, rec: Dict[str, Any]) -> Dict[str, Any]:
    artifact_id = f"{subsystem}:{rec.get('id')}"
    payload = rec.get("payload", {})
    artifact_hash = stable_hash({"subsystem": subsystem, "id": rec.get("id"), "payload": payload, "summary": rec.get("summary")})
    risk_level = "L2" if subsystem == "skills" else "L1"
    profile_id = _current_profile_id()
    return ensure_request_for_artifact(
        request_type={"domain": "write", "operation": "accept", "subtype": subsystem},
        source={"profile_id": profile_id, "session_id": None, "requested_by": "user", "request_message_id": None},
        owner={"profile_id": profile_id, "scope": f"{subsystem}.write_pending"},
        target={
            "ref": f"pending/{subsystem}/{rec.get('id')}.json",
            "artifact_id": artifact_id,
            "artifact_type": f"{subsystem}_pending_write",
            "artifact_hash": artifact_hash,
            "summary": rec.get("summary", ""),
            "affected_paths": [],
        },
        risk={"level": risk_level},
        evidence={
            "reason": rec.get("summary", ""),
            "diff_ref": None,
            "source_refs": [f"pending/{subsystem}/{rec.get('id')}.json"],
            "verification_plan": "Producer reports apply result through callback_result.",
            "rollback_plan": None,
        },
        callback={"producer": "write_approval", "operation": f"{subsystem}.apply", "target_id": rec.get("id"), "idempotency_key": artifact_hash},
    )


def resolve_request_identifier(identifier: str) -> Optional[Dict[str, Any]]:
    """Resolve a request id or known producer artifact id to an approval request.

    Resolution never records a decision. It may create or reuse a request for a
    known producer artifact so an explicit approval command can decide it.
    """
    ident = (identifier or "").strip()
    if not ident:
        return None
    if ident.startswith("req_"):
        return get_request(ident)

    # Write-approval artifacts use memory:<pending_id> / skills:<pending_id>.
    candidates: List[Tuple[str, str]] = []
    if ":" in ident:
        subsystem, pending_id = ident.split(":", 1)
        if subsystem in {"memory", "skills"} and pending_id:
            candidates.append((subsystem, pending_id))
    else:
        candidates.extend([("memory", ident), ("skills", ident)])

    matches: List[Tuple[str, Dict[str, Any]]] = []
    try:
        from tools import write_approval as wa
        for subsystem, pending_id in candidates:
            rec = wa.get_pending(subsystem, pending_id)
            if rec:
                matches.append((subsystem, rec))
    except Exception:
        return None

    if len(matches) != 1:
        return None
    subsystem, rec = matches[0]
    return request_for_write_pending(subsystem, rec)
