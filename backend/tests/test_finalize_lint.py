from types import SimpleNamespace

from app.agent.nodes import finalize as finalize_module
from app.agent.nodes.finalize import _finalize_node
from _node_rt import runtime_from_state


class Bus:
    def __init__(self):
        self.events = []

    def emit(self, event, data):
        self.events.append((event, data))


class Repo:
    def __init__(self):
        self.messages = []

    def add_message(self, db, conv_id, role, content, **kwargs):
        self.messages.append((role, content))

    def create_agent_run(self, *args, **kwargs):
        return SimpleNamespace(id=1)


def test_lint_block_is_persisted_and_emitted_before_done(monkeypatch):
    repo = Repo()
    monkeypatch.setattr(finalize_module, "repo", repo)
    monkeypatch.setattr(finalize_module, "write_trace", lambda *a, **k: None)

    bus = Bus()
    state = {
        "service": SimpleNamespace(
            settings=SimpleNamespace(
                trajectory_compress_enabled=False,
                checkpoint_keep_per_thread=20,
            ),
            checkpoint_saver=None,
        ),
        "bus": bus,
        "db": SimpleNamespace(rollback=lambda: None),
        "runtime": {
            "final_text": "Done.",
            "status": "ok",
            "conv_id": 1,
            "mutation_ledger": [
                {
                    "ok": True,
                    "tool": "t",
                    "target": "A",
                    "claims": [
                        {
                            "kind": "id",
                            "value": "ABC123",
                            "label": "id",
                            "tool": "t",
                            "target": "A",
                        }
                    ],
                }
            ],
        },
        "sources": [],
        "tool_trace": [],
        "question": "q",
        "messages": [],
        "plan_steps": [],
        "todos": [],
        "plan_done_count": 0,
        "memory_hits": [],
        "subagent_round_count": 0,
        "persist_start": 0,
    }

    _finalize_node(state, runtime_from_state(state))

    assert state["runtime"]["patches"]["answer_lint"] == 1
    assistant = [c for role, c in repo.messages if role == "assistant"][-1]
    assert "ABC123" in assistant
    names = [e for e, _ in bus.events]
    assert "token" in names and "done" in names
    assert names.index("token") < names.index("done")
    token_text = [d for e, d in bus.events if e == "token"][0]
    assert "ABC123" in token_text
