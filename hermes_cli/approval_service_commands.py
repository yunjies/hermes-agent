"""Shared slash-command handler for Approval Service."""

from __future__ import annotations

from typing import List, Optional

from tools import approval_service as svc


def handle_approval_command(args: List[str]) -> str:
    if not args:
        return _help()
    sub = args[0].lower()
    rest = args[1:]

    try:
        if sub == "list":
            status = None
            if len(rest) >= 2 and rest[0] == "--status":
                status = rest[1]
            return _list(status)
        if sub == "mode":
            return f"HERMES_APPROVAL_SERVICE_MODE={svc.current_mode()}"
        if sub in {"show", "status"}:
            if not rest:
                return f"Usage: /approval {sub} <request_id>"
            req = svc.get_request(rest[0])
            return svc.format_request(req) if req else f"No approval request with id '{rest[0]}'."
        if sub in {"approve", "reject", "deny"}:
            if not rest:
                return f"Usage: /approval {sub} <request_id> [--reason <reason>]"
            req = svc.resolve_request_identifier(rest[0])
            if not req:
                return f"No approval request or known producer artifact with id '{rest[0]}'."
            reason = _parse_reason(rest[1:])
            result = "rejected" if sub in {"reject", "deny"} else "approved"
            req = svc.decide_request(req["id"], result, reason=reason, actor_type="user", actor_id="user")
            return svc.format_decision_result(req, action=sub)
        if sub == "revoke":
            if not rest:
                return "Usage: /approval revoke <request_id> [--reason <reason>]"
            req = svc.revoke_request(rest[0], reason=_parse_reason(rest[1:]))
            return svc.format_decision_result(req, action="revoke")
        if sub == "expire":
            if not rest:
                return "Usage: /approval expire <request_id>"
            req = svc.expire_request(rest[0])
            return svc.format_decision_result(req, action="expire")
        if sub == "examples":
            return _examples(rest)
    except Exception as exc:
        return f"Approval Service error: {exc}"

    return _help()


def _parse_reason(args: List[str]) -> str:
    if not args:
        return ""
    if args[0] == "--reason":
        return " ".join(args[1:]).strip()
    return " ".join(args).strip()


def _list(status: Optional[str]) -> str:
    rows = svc.list_requests(status)
    if not rows:
        suffix = f" with status {status}" if status else ""
        return f"No approval requests{suffix}."
    lines = [f"Approval requests ({len(rows)}):"]
    for req in rows:
        target = req.get("target", {})
        lines.append(
            f"  {req.get('id')}  {req.get('status')}  "
            f"{svc._request_type_label(req)}  {target.get('artifact_id')}  {target.get('summary', '')}"
        )
    return "\n".join(lines)


def _examples(args: List[str]) -> str:
    if not args or args[0].lower() == "list":
        rows = svc.list_decision_examples()
        if not rows:
            return "No approval decision examples."
        lines = [f"Approval decision examples ({len(rows)}):"]
        for row in rows:
            decision = row.get("decision", {})
            target = row.get("target", {})
            lines.append(f"  {row.get('request_id')}  {decision.get('result')}  {target.get('summary', '')}")
        return "\n".join(lines)
    if args[0].lower() == "show" and len(args) > 1:
        row = svc.get_decision_example(args[1])
        if not row:
            return f"No approval decision example for request '{args[1]}'."
        import json

        return json.dumps(row, ensure_ascii=False, indent=2, sort_keys=True)
    return "Usage: /approval examples [list|show <request_id>]"


def _help() -> str:
    return (
        "Usage: /approval <list|show|status|approve|reject|revoke|expire|examples|mode>\n"
        "Examples:\n"
        "  /approval list --status pending\n"
        "  /approval show req_...\n"
        "  /approval approve req_... --reason reviewed\n"
        "  /approval examples list\n"
        "  /approval mode"
    )
