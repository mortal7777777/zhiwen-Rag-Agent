from app.agent.tool_contracts import ToolContract
from app.agent.tool_preflight import validate_call


def test_missing_target():
    contract = ToolContract(mutating=True, target_arg="order_id")
    assert validate_call(contract, "x", {}) is not None
    assert validate_call(contract, "x", {"order_id": "#W1"}) is None


def test_enum_values():
    contract = ToolContract(
        mutating=True,
        target_arg="order_id",
        enum_args={"reason": frozenset({"a", "b"})},
    )
    assert validate_call(
        contract, "x", {"order_id": "#W1", "reason": "c"}
    ) is not None
    assert validate_call(
        contract, "x", {"order_id": "#W1", "reason": "a"}
    ) is None


def test_noop_pairs():
    contract = ToolContract(
        mutating=True,
        target_arg="order_id",
        compare_pairs=(("item_ids", "new_item_ids"),),
    )
    assert validate_call(
        contract,
        "x",
        {"order_id": "#W1", "item_ids": ["a"], "new_item_ids": ["a"]},
    ) is not None
    assert validate_call(
        contract,
        "x",
        {"order_id": "#W1", "item_ids": ["a", "b"], "new_item_ids": ["a"]},
    ) is not None
    assert validate_call(
        contract,
        "x",
        {"order_id": "#W1", "item_ids": ["a"], "new_item_ids": ["b"]},
    ) is None


def test_transfer_needs_explicit_request():
    contract = ToolContract(requires_consent=True)
    assert (
        validate_call(
            contract,
            "transfer_to_human_agents",
            {},
            last_user_text="I want to speak to a human",
        )
        is None
    )
    assert (
        validate_call(
            contract,
            "transfer_to_human_agents",
            {},
            last_user_text="please help",
        )
        is not None
    )
    assert (
        validate_call(
            contract,
            "transfer_to_human_agents",
            {},
            last_user_text="please help",
            check_consent=False,
        )
        is None
    )
    # 提到过去联系过的 representative 不等于要求转人工
    assert (
        validate_call(
            contract,
            "transfer_to_human_agents",
            {},
            last_user_text="the customer support representative I spoke with earlier said so",
        )
        is not None
    )
