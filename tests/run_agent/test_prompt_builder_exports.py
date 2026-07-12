import run_agent


def test_run_agent_reexports_system_prompt_helpers():
    assert callable(run_agent.load_methodology_agents_md)
    assert callable(run_agent.load_soul_md)
