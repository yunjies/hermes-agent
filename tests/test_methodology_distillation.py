from agent.methodology_distillation import (
    apply_methodology_distillation_pending,
    classify_methodology_distillation_signal,
    scan_methodology_distillation_signals,
)


def test_scan_methodology_distillation_signals_for_methodology_decision():
    messages = [
        {
            "role": "user",
            "content": (
                "\u8fd9\u7c7b\u95ee\u9898\u5e94\u8be5\u5148\u660e\u786e\u5de5\u4f5c\u6d41\uff0c"
                "\u518d\u505a\u9a8c\u8bc1\u548c\u56de\u6eda\u8bbe\u8ba1\u3002"
            ),
        },
        {"role": "assistant", "content": "\u5df2\u8bb0\u5f55\u3002"},
    ]

    signals = scan_methodology_distillation_signals(messages)

    assert signals
    assert signals[0]["id"] == "methodology_decision"
    assert signals[0]["outputs"] == ["methodology_delta"]


def test_scan_methodology_distillation_ignores_tool_noise_by_default():
    messages = [
        {
            "role": "tool",
            "content": (
                '{"success": true, "results": ["\u5de5\u4f5c\u6d41", '
                '"\u8fd9\u7c7b\u95ee\u9898", "\u9a8c\u8bc1"]}'
            ),
        },
        {"role": "assistant", "content": "\u5df2\u5b8c\u6210\u672c\u6b21\u68c0\u67e5\u3002"},
    ]

    signals = scan_methodology_distillation_signals(messages)

    assert signals == []


def test_classify_troubleshooting_signal_routes_to_skill():
    signal = {
        "evidence": [{
            "role": "user",
            "quote": (
                "\u8bf7\u5c06\u4ee5\u4e0b\u5185\u5bb9\u6574\u7406\u5e76\u6c89\u6dc0\u4e3a "
                "Hermes framework-maintainer \u7684\u8fd0\u7ef4\u77e5\u8bc6\u4e0e"
                "\u6545\u969c\u5904\u7406\u89c4\u8303\u3002Provider authentication failed."
            ),
        }],
    }

    review = classify_methodology_distillation_signal(signal)

    assert review["destination"] == "skill"


def test_classify_governance_boundary_signal_routes_to_methodology():
    signal = {
        "evidence": [{
            "role": "user",
            "quote": (
                "\u4ece\u6cbb\u7406/\u5b89\u5168/\u6267\u884c\u8fb9\u754c\u89d2\u5ea6"
                "\u5ba1\u6838\u662f\u5426\u4f1a\u9690\u5f0f\u8d8a\u6743\uff0c"
                "\u5e76\u8f93\u51fa\u95e8\u7981\u89c4\u5219\u548c\u7ea2\u7ebf\u3002"
            ),
        }],
    }

    review = classify_methodology_distillation_signal(signal)

    assert review["destination"] == "methodology"


def test_apply_methodology_distillation_pending_generates_methodology_agents(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    proposal = {
        "type": "methodology_delta",
        "section": "Default Judgments",
        "text": "Prefer profile-local AGENTS.methodology.md for generated methodology learning.",
        "confidence": "high",
        "evidence": [{"role": "user", "quote": "\u8fd9\u4e2a\u65b9\u6848\u91c7\u7528"}],
        "conflicts_with_soul": False,
    }

    result = apply_methodology_distillation_pending({"action": "apply", "proposal": proposal})

    assert result["success"] is True
    distilled = tmp_path / "AGENTS.methodology.md"
    assert distilled.exists()
    text = distilled.read_text(encoding="utf-8")
    assert "SOUL.md > manual AGENTS/project context > AGENTS.methodology.md" in text
    assert "Prefer profile-local AGENTS.methodology.md" in text
