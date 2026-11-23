"""工具契约注册表：核心契约、first-wins、快照隔离、τ² WRITE 覆盖。"""

from __future__ import annotations

import pytest

from app.agent.tool_contracts import (
    ToolContract,
    contracts_snapshot,
    get_contract,
    register_contracts,
    reset_contracts,
)


def setup_function() -> None:
    reset_contracts()


def teardown_function() -> None:
    reset_contracts()


def test_core_contracts_present():
    assert get_contract("write_file").readback_tool == "read_file"
    assert get_contract("edit_file").mutating is True
    assert get_contract("delete_file").mutating is True
    # bash 不做自动回读
    assert get_contract("bash") is None


def test_register_coerces_and_first_wins():
    register_contracts(
        {
            "x": {
                "mutating": True,
                "target_arg": "order_id",
                "compare_pairs": [["item_ids", "new_item_ids"]],
                "enum_args": {"reason": {"a", "b"}},
            }
        },
        source="test",
    )
    contract = get_contract("x")
    assert contract.compare_pairs == (("item_ids", "new_item_ids"),)
    assert contract.enum_args["reason"] == frozenset({"a", "b"})

    # 同名冲突：first-wins，不覆盖
    register_contracts({"x": ToolContract(mutating=False)}, source="later")
    assert get_contract("x").mutating is True


def test_core_file_tools_do_not_require_path_echo():
    """写/编辑/删除的答复不要求回显路径；真正的验证走 readback。"""
    for name in ("write_file", "edit_file", "delete_file"):
        contract = get_contract(name)
        assert contract is not None
        assert contract.answer_id_args == ()


def test_snapshot_is_a_copy():
    snapshot = contracts_snapshot()
    snapshot.pop("write_file", None)
    assert get_contract("write_file") is not None


def test_tau2_write_tools_have_contracts():
    # 该桥接文件是本地用户工具（.gitignore 忽略），干净检出没有它
    pytest.importorskip("app.user_tools.tau2_bridge")
    from app.user_tools.tau2_bridge import TOOL_CONTRACTS

    expected = {
        "cancel_pending_order",
        "modify_pending_order_address",
        "modify_pending_order_items",
        "modify_pending_order_payment",
        "exchange_delivered_order_items",
        "return_delivered_order_items",
        "modify_user_address",
        "book_reservation",
        "cancel_reservation",
        "update_reservation_flights",
        "update_reservation_baggages",
        "update_reservation_passengers",
        "send_certificate",
        "transfer_to_human_agents",
    }
    assert expected <= set(TOOL_CONTRACTS)
    for name, raw in TOOL_CONTRACTS.items():
        if raw.get("mutating"):
            assert raw.get("readback_tool"), f"{name} 缺少 readback_tool"
        if name == "transfer_to_human_agents":
            assert raw.get("requires_consent") is True
