import json


def _artifact_request(svc, artifact_id="artifact-1", content="v1"):
    artifact_hash = svc.stable_hash({"content": content})
    return svc.ensure_request_for_artifact(
        request_type={"domain": "write", "operation": "accept", "subtype": "test"},
        source={"profile_id": None, "session_id": "s1", "requested_by": "user", "request_message_id": None},
        owner={"profile_id": "owner", "scope": "test"},
        target={
            "ref": f"pending/test/{artifact_id}.json",
            "artifact_id": artifact_id,
            "artifact_type": "test_artifact",
            "artifact_hash": artifact_hash,
            "summary": "test artifact",
            "affected_paths": [],
        },
        risk={"level": "L1"},
        evidence={
            "reason": "test",
            "diff_ref": None,
            "source_refs": [],
            "verification_plan": "callback result",
            "rollback_plan": None,
        },
        callback={"producer": "test_producer", "operation": "apply", "target_id": artifact_id, "idempotency_key": artifact_hash},
    )


def test_ensure_reuses_non_terminal_same_hash(monkeypatch):
    monkeypatch.setenv("HERMES_APPROVAL_SERVICE_MODE", "shadow")
    from tools import approval_service as svc

    first = _artifact_request(svc)
    second = _artifact_request(svc)

    assert first["id"] == second["id"]
    assert first["status"] == "pending"


def test_decision_callback_and_preflight_are_separate(monkeypatch):
    monkeypatch.setenv("HERMES_APPROVAL_SERVICE_MODE", "shadow")
    from tools import approval_service as svc

    req = _artifact_request(svc)
    approved = svc.decide_request(req["id"], "approved", reason="reviewed")
    assert approved["status"] == "approved"

    preflight = svc.preflight(
        req["id"],
        artifact_hash=req["target"]["artifact_hash"],
        producer="test_producer",
        executor="test_producer",
    )
    assert preflight["result"] == "pass"

    updated = svc.record_callback_result(
        req["id"],
        result="apply_failed",
        reason="producer failed",
        applied_ref="pending/test/artifact-1.json",
        producer="test_producer",
    )
    assert updated["status"] == "approved"
    assert updated["callback_result"]["result"] == "apply_failed"


def test_preflight_fails_on_changed_hash(monkeypatch):
    monkeypatch.setenv("HERMES_APPROVAL_SERVICE_MODE", "shadow")
    from tools import approval_service as svc

    req = _artifact_request(svc)
    svc.decide_request(req["id"], "approved", reason="reviewed")
    preflight = svc.preflight(
        req["id"],
        artifact_hash=svc.stable_hash({"content": "v2"}),
        producer="test_producer",
        executor="test_producer",
    )

    assert preflight["result"] == "fail"
    assert "artifact_hash" in preflight["failed_gates"]


def test_approval_command_approve_and_examples(monkeypatch):
    monkeypatch.setenv("HERMES_APPROVAL_SERVICE_MODE", "shadow")
    from hermes_cli.approval_service_commands import handle_approval_command
    from tools import approval_service as svc

    req = _artifact_request(svc, artifact_id="artifact-commands")
    out = handle_approval_command(["approve", req["id"], "--reason", "looks good"])

    assert "Approval result: APPROVED" in out
    assert req["id"] in out
    examples = handle_approval_command(["examples", "list"])
    assert req["id"] in examples


def test_write_pending_approval_records_request_and_callback(monkeypatch):
    monkeypatch.setenv("HERMES_APPROVAL_SERVICE_MODE", "enforce")
    from hermes_cli.write_approval_commands import handle_pending_subcommand
    from tools import write_approval as wa
    from tools.memory_tool import MemoryStore
    from tools import approval_service as svc

    store = MemoryStore()
    store.load_from_disk()
    rec = wa.stage_write(
        "memory",
        {"action": "add", "target": "user", "content": "approval service entry"},
        summary="add approval service entry",
        origin="foreground",
    )

    out = handle_pending_subcommand(wa.MEMORY, ["approve", rec["id"]], memory_store=store)

    assert "Approved 1" in out
    assert wa.pending_count("memory") == 0
    requests = svc.list_requests()
    assert len(requests) == 1
    assert requests[0]["status"] == "approved"
    assert requests[0]["callback_result"]["result"] == "applied"
    assert "approval service entry" in store.user_entries[0]


def test_approval_command_resolves_write_artifact_id(monkeypatch):
    monkeypatch.setenv("HERMES_APPROVAL_SERVICE_MODE", "shadow")
    from hermes_cli.approval_service_commands import handle_approval_command
    from tools import write_approval as wa
    from tools import approval_service as svc

    rec = wa.stage_write(
        "memory",
        {"action": "add", "target": "user", "content": "approval artifact id"},
        summary="approval artifact id",
        origin="foreground",
    )

    out = handle_approval_command(["approve", f"memory:{rec['id']}", "--reason", "artifact id"])

    assert "Approval result: APPROVED" in out
    requests = svc.list_requests()
    assert len(requests) == 1
    assert requests[0]["target"]["artifact_id"] == f"memory:{rec['id']}"
    assert requests[0]["status"] == "approved"


def test_audit_has_user_actor_for_decision(monkeypatch):
    monkeypatch.setenv("HERMES_APPROVAL_SERVICE_MODE", "shadow")
    from tools import approval_service as svc
    from hermes_constants import get_hermes_home

    req = _artifact_request(svc, artifact_id="artifact-audit")
    svc.decide_request(req["id"], "approved", reason="manual")

    audit_path = get_hermes_home() / "approvals" / "audit.jsonl"
    events = [json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines()]
    decision_events = [e for e in events if e["event_type"] == "decision_recorded"]
    assert decision_events
    assert decision_events[-1]["actor"]["type"] == "user"


def test_write_pending_request_uses_active_profile_owner(monkeypatch):
    monkeypatch.setenv("HERMES_APPROVAL_SERVICE_MODE", "shadow")
    monkeypatch.setattr("hermes_cli.profiles.get_active_profile_name", lambda: "framework-maintainer")
    from tools import approval_service as svc
    from tools import write_approval as wa

    rec = wa.stage_write(
        "skills",
        {"action": "upsert", "name": "example", "content": "body"},
        summary="profile-owned skill write",
        origin="foreground",
    )

    req = svc.request_for_write_pending("skills", rec)

    assert req["source"]["profile_id"] == "framework-maintainer"
    assert req["owner"]["profile_id"] == "framework-maintainer"
    assert req["owner"]["scope"] == "skills.write_pending"
