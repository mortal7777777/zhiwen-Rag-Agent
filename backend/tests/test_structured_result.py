"""子代理结构化结果解析（Phase A 6.3）：合法/非法/多围栏/无损回退。"""

from __future__ import annotations

from app.agent.structured_result import parse_structured_result


def test_plain_text_falls_back_losslessly():
    text = "结论：没有发现问题。\n补充说明 [1]。"
    out = parse_structured_result(text)
    assert out["structured"] is False
    assert out["summary"] == text
    assert out["findings"] == []
    assert out["open_questions"] == []


def test_valid_fence_is_parsed_and_normalized():
    text = (
        "审查完成。\n\n```json\n"
        '{"summary":"发现两个问题",'
        '"findings":[{"severity":"major","confidence":1.7,'
        '"path":"backend/app/x.py","line":"42","claim":"空指针",'
        '"evidence":"第 42 行直接解引用","trigger":"传入 None"},'
        '{"severity":"info","claim":"命名不一致","confidence":"0.3"}],'
        '"open_questions":["是否需要兼容旧格式？",""]}\n```'
    )
    out = parse_structured_result(text)
    assert out["structured"] is True
    assert out["summary"] == "发现两个问题"
    assert out["findings"][0] == {
        "severity": "HIGH",
        "confidence": 1.0,
        "path": "backend/app/x.py",
        "line": 42,
        "claim": "空指针",
        "evidence": "第 42 行直接解引用",
        "trigger": "传入 None",
    }
    assert out["findings"][1]["severity"] == "LOW"
    assert out["findings"][1]["confidence"] == 0.3
    assert out["findings"][1]["line"] is None
    assert out["open_questions"] == ["是否需要兼容旧格式？"]


def test_invalid_json_fence_keeps_original_summary():
    text = "正文结论。\n```json\n{不是合法 JSON}\n```"
    out = parse_structured_result(text)
    assert out["structured"] is False
    assert out["summary"] == text


def test_multiple_fences_last_one_wins():
    text = (
        "正文。\n```json\n{\"summary\":\"旧\",\"findings\":[]}\n```\n"
        "补充。\n```json\n{\"summary\":\"新\",\"findings\":["
        '{"severity":"critical","claim":"阻断上线"}],"open_questions":[]}\n```'
    )
    out = parse_structured_result(text)
    assert out["structured"] is True
    assert out["summary"] == "新"
    assert out["findings"][0]["severity"] == "CRITICAL"


def test_finding_without_claim_is_dropped():
    text = (
        "正文。\n```json\n"
        '{"summary":"x","findings":[{"severity":"HIGH"},'
        '{"severity":"LOW","claim":"有效问题"}],"open_questions":[]}\n```'
    )
    out = parse_structured_result(text)
    assert out["structured"] is True
    assert len(out["findings"]) == 1
    assert out["findings"][0]["claim"] == "有效问题"


def test_empty_payload_falls_back():
    text = "正文。\n```json\n{}\n```"
    out = parse_structured_result(text)
    # 围栏外正文作为 summary，仍算结构化成功（无 findings 是合法的）
    assert out["structured"] is True
    assert out["summary"] == "正文。"
    assert out["findings"] == []
