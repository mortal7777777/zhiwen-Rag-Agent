from app.agent.tool_contracts import ToolContract
from app.agent.tool_verification import augment_mutation


class FakeTool:
    def __init__(self, result):
        self.name = "get_order_details"
        self.result = result
        self.calls = []

    def invoke(self, args):
        self.calls.append(dict(args))
        return self.result


def test_readback_runs_once():
    tool = FakeTool({"status": "pending (item modified)"})
    contract = ToolContract(
        mutating=True,
        target_arg="order_id",
        readback_tool="get_order_details",
        readback_arg="order_id",
        verified_fields=("status",),
        status_keywords=("item modified",),
    )
    entry = augment_mutation(
        name="modify_pending_order_items",
        args={"order_id": "#W1"},
        result={"summary": "ok"},
        tools_by_name={"get_order_details": tool},
        contract=contract,
        is_sensitive=lambda name, args: False,
    )
    assert entry["verified"]["ok"] is True
    assert tool.calls == [{"order_id": "#W1"}]


def test_failed_mutation_has_no_entry():
    entry = augment_mutation(
        name="x",
        args={"order_id": "#W1"},
        result={"error": "boom"},
        tools_by_name={},
        contract=ToolContract(mutating=True, target_arg="order_id"),
    )
    assert entry is None


def test_readback_error_keeps_mutation_result():
    class Boom:
        name = "get_order_details"

        def invoke(self, args):
            raise RuntimeError("timeout")

    contract = ToolContract(
        mutating=True,
        target_arg="order_id",
        readback_tool="get_order_details",
        readback_arg="order_id",
    )
    entry = augment_mutation(
        name="x",
        args={"order_id": "#W1"},
        result={"summary": "ok"},
        tools_by_name={"get_order_details": Boom()},
        contract=contract,
        is_sensitive=lambda name, args: False,
    )
    assert entry["verified"]["ok"] is False
    assert "timeout" in entry["verified"]["error"]


def test_readback_skipped_for_guarded_tool():
    tool = FakeTool({})
    contract = ToolContract(
        mutating=True,
        target_arg="order_id",
        readback_tool="get_order_details",
        readback_arg="order_id",
    )
    entry = augment_mutation(
        name="x",
        args={"order_id": "#W1"},
        result={"summary": "ok"},
        tools_by_name={"get_order_details": tool},
        contract=contract,
        is_sensitive=lambda name, args: True,
    )
    assert entry["verified"]["reason"] == "readback_sensitive"
    assert tool.calls == []


def test_compact_limits_size():
    import json

    from app.agent.tool_verification import compact_verified

    blob = {"ok": True, "status": "confirmed", "big": "x" * 5000}
    out = compact_verified(blob, 900)
    assert len(json.dumps(out, ensure_ascii=False)) <= 900
    assert out.get("ok") is True
