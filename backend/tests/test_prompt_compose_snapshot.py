"""系统提示词组合级字节快照（2026-10-29 版本化改造基线）。

锁的是 `compose_system_prompt` 在固定入参矩阵下的 SHA-256，以及所有可版本化
片段的默认文本。任何字符变化都会失败——这是有意的：静态核心进入消息链并参与
DeepSeek 前缀缓存键，改提示词=历史会话缓存失效，必须显式更新快照并在提交
说明里注明。节点级 `nodes/prompts.py` 的快照见 `test_node_prompts.py`。
"""

from __future__ import annotations

import hashlib

from app.agent.prompts import (
    DEFAULT_SYSTEM_PROMPT,
    GLOBAL_SAFETY_NOTE,
    KB_INVENTORY_GUIDE,
    PLAN_MODE_BLOCK,
    PRESET_TEMPLATES,
    SKILL_SAFETY_NOTE,
    SKILLS_USAGE_GUIDE,
    TODOS_RULES_BLOCK,
    TOOL_INSTRUCTIONS,
    compose_system_prompt,
    default_fragments,
    find_mid_sentence_breaks,
    lint_fragment_content,
    lint_template_content,
    normalize_mid_sentence_breaks,
    validate_fragment_content,
)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


CASES = {
    "default": {},
    "kb": {"use_knowledge_base": True},
    "web": {"use_web_search": True},
    "coding": {"advanced_tools": True},
    "plan": {"plan_only": True},
    "todos": {"todos_text": "1. a\n2. b"},
    "skills": {"skills_catalog": "<available_skills>\n- demo\n</available_skills>"},
    "kb_docs": {"use_knowledge_base": True, "kb_documents": ["a.pdf", "b.md"]},
    "all_dynamic": {
        "use_knowledge_base": True,
        "use_web_search": True,
        "advanced_tools": True,
        "plan_only": True,
        "todos_text": "1. a\n2. b",
        "skills_catalog": "<available_skills>\n- demo\n</available_skills>",
        "kb_documents": ["a.pdf", "b.md"],
    },
}

# 2026-11-21 派发策略调整（简单并行查询优先同轮工具，dispatch 兜底）
# 与子代理模式说明后的输出哈希。静态核心进入消息链并参与前缀缓存键，
# 本次改动会让历史会话下一轮重建一次前缀缓存（有意为之）。
EXPECTED = {
    ("default", "full"): "fe88ab608dad858f461fb13e7240352c4b856bcef2ff7a0d1a3b4a92f152f577",
    ("default", "static"): "fe88ab608dad858f461fb13e7240352c4b856bcef2ff7a0d1a3b4a92f152f577",
    ("default", "dynamic"): "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
    ("kb", "full"): "65705ad9f943e5694cf1805c0be2ed12c7c406f64e06df08928ac7b559e0b6df",
    ("kb", "static"): "65705ad9f943e5694cf1805c0be2ed12c7c406f64e06df08928ac7b559e0b6df",
    ("kb", "dynamic"): "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
    ("web", "full"): "dc06d4d2dc33e22f78e598829b0950a950d35ebf7616e5d06ba150e670c94eb1",
    ("web", "static"): "dc06d4d2dc33e22f78e598829b0950a950d35ebf7616e5d06ba150e670c94eb1",
    ("web", "dynamic"): "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
    ("coding", "full"): "71bdc5281cd1f8f57302db82c92478c6861768db8029c3f05e29f61c33869503",
    ("coding", "static"): "71bdc5281cd1f8f57302db82c92478c6861768db8029c3f05e29f61c33869503",
    ("coding", "dynamic"): "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
    ("plan", "full"): "1fb005ed9d4a8a791fa84b34c2bf04b2ca8f3938ab9f901c9f014068f62a6a54",
    ("plan", "static"): "1fb005ed9d4a8a791fa84b34c2bf04b2ca8f3938ab9f901c9f014068f62a6a54",
    ("plan", "dynamic"): "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
    ("todos", "full"): "a442afc909827bc164deb5da00785dfd913800a0ca687884393bb62442e1578f",
    ("todos", "static"): "fe88ab608dad858f461fb13e7240352c4b856bcef2ff7a0d1a3b4a92f152f577",
    ("todos", "dynamic"): "da416e086083320d8fa341da0fad7e78606b9a72383a7af05a2b6e71c5eba135",
    ("skills", "full"): "e23ede118d554f5d04dffd720bb9bd15c83cda702903db816211dd676f395b17",
    ("skills", "static"): "fe88ab608dad858f461fb13e7240352c4b856bcef2ff7a0d1a3b4a92f152f577",
    ("skills", "dynamic"): "73a05f4940ee3997068284eaaf317f9e5f908a4f9fc1582f1eff799419699d63",
    ("kb_docs", "full"): "22a0db7da6a3015c179ddafc1d81757488850951b10b79c0c3b695a76557fcde",
    ("kb_docs", "static"): "65705ad9f943e5694cf1805c0be2ed12c7c406f64e06df08928ac7b559e0b6df",
    ("kb_docs", "dynamic"): "fe8b5081c5ff160f10a0952ce6ba7e2cf24904a53c9ff9b03fab6c7c65c4bdf5",
    ("all_dynamic", "full"): "2f94b77a20c7d5ec34a1e3315ebebc3e0f52cdc56ad81e92ae9429dd90eb9792",
    ("all_dynamic", "static"): "847a0cf597ecfd268537fea3d3bee7eb9409158e41599aeadc0c0b796cc6bfa7",
    ("all_dynamic", "dynamic"): "3020c307cb74586423a3ab2f03271a2d1ed8b7cb224bda6f59cd97a6302617d1",
}

PRESET_HASHES = [
    "8dd271ad96a1b8a135a51ad37421daec796264e6cb35298f1376401d1adf305c",
    "e677ab1cf5377602a62fe3d98cd1f02c3f3a6c900acedc70c1d5c0401c36f5ff",
    "5a9a86f25283e4842d9206b06ee526d75af9c0e4697a9fada99ad69e2cd97ad3",
    "5b3fc5ff59c2cf594ca81b7c3d9a4eb42269047245542ca0a5eff00553be600f",
    "791622aa57a9f8a614fff0308afa5c5dfbf8dbe25944e676ff5f980c64d6db71",
    "65e5b55b1c949e504bb6811fcb6da70170220ac6ee0f4fb053b2b2ed2128b95c",
    "808f8b459def72e41b246bd6fd04140508ec69c451e3757ff8bcfb905b219f40",
    "7785345635c9fd2a9d184aa1b90b937a90189e67d38f9738eb3530d066715b5c",
    "541ed59b65745442acd450408629e4a8ebb999cb7edd01046b02d7bf6f38f47d",
    "bd67017d57745786c84553a56d46bc6f050e9cc64997fddd5949fcd315ad2949",
    "c57bfea0258af9d263519c19c0e0e81be02b2fadd281053693e6f31f457bd80a",
    "3ec86992a537e31dfacd50147cf33bcdfbbe76a1798b455d4d929d3875530460",
]


def test_默认文本与片段快照():
    assert _sha(DEFAULT_SYSTEM_PROMPT) == (
        "8dd271ad96a1b8a135a51ad37421daec796264e6cb35298f1376401d1adf305c"
    )
    assert [_sha(t["content"]) for t in PRESET_TEMPLATES] == PRESET_HASHES
    assert _sha(TOOL_INSTRUCTIONS["knowledge"]) == (
        "2558e81804fa066a763fafb7e9ced68e59a55f3575531060e2877537ab36e800"
    )
    assert _sha(TOOL_INSTRUCTIONS["web"]) == (
        "8b20a1f6928b79849ca5d83ef4828c2653420102e0f37fd884c72b00ca346dcf"
    )
    assert _sha(TOOL_INSTRUCTIONS["coding"]) == (
        "0a61da9bfc1437abe4ca8e2f886bcacacb89b03e386af2aaf94e17b0d55d9fed"
    )
    assert _sha(SKILL_SAFETY_NOTE) == (
        "b420127079995465748ebd85dc339af3311b525685a4eda0120bc3f973445183"
    )
    assert _sha(PLAN_MODE_BLOCK) == (
        "5c74425d98f8181c8cf4e1b0d47bb6155e70785b3730a584183921afb46c6df3"
    )
    assert _sha(TODOS_RULES_BLOCK) == (
        "256d01db379c47632b08d0110aff9ff0ddb3df68a617aee6daa4e8249bf6d31d"
    )
    assert _sha(SKILLS_USAGE_GUIDE) == (
        "b96f6b9b4910b5b7efb58797b05b6d1e78def8553e19db3a27a345fa933d339e"
    )
    assert _sha(KB_INVENTORY_GUIDE) == (
        "c9ee59b272d33778965229224eec5776163942427c0417a322cad47587e2fde2"
    )
    assert _sha(GLOBAL_SAFETY_NOTE) == (
        "6c6a914e0611c675d9bdea5a26136202ff7dd0c82d96d6099461c532f85c5cf9"
    )


def test_组合输出字节稳定():
    for (name, mode), expected in EXPECTED.items():
        kwargs = dict(CASES[name])
        if mode == "static":
            kwargs["static_only"] = True
        elif mode == "dynamic":
            kwargs["dynamic_only"] = True
        assert _sha(compose_system_prompt(**kwargs)) == expected, (name, mode)


def test_默认片段与缺省一致():
    for name, kwargs in CASES.items():
        for extra in ({}, {"static_only": True}, {"dynamic_only": True}):
            plain = compose_system_prompt(**kwargs, **extra)
            overridden = compose_system_prompt(
                **kwargs, **extra, fragments=default_fragments()
            )
            assert plain == overridden, (name, extra)


def test_片段覆盖只影响对应段():
    base = compose_system_prompt(use_knowledge_base=True)
    changed = compose_system_prompt(
        use_knowledge_base=True,
        fragments={"tool.knowledge": "自定义知识库规则"},
    )
    assert "自定义知识库规则" in changed
    assert TOOL_INSTRUCTIONS["knowledge"] not in changed
    assert base != changed


def test_片段占位符校验():
    import pytest

    with pytest.raises(ValueError):
        validate_fragment_content("skills.usage", "缺少占位符")
    with pytest.raises(ValueError):
        validate_fragment_content(
            "skills.usage", "{{SKILLS_CATALOG}} 和 {{SKILLS_CATALOG}}"
        )
    with pytest.raises(ValueError):
        validate_fragment_content("tool.knowledge", "  ")
    with pytest.raises(ValueError):
        validate_fragment_content("unknown", "x")
    validate_fragment_content("skills.usage", "目录：{{SKILLS_CATALOG}}")
    validate_fragment_content("tool.knowledge", "任意规则")


def test_片段与模板_lint_提示():
    warnings = lint_fragment_content("plan.mode", "只写文字计划，不调用工具")
    assert any("exit_plan_mode" in w for w in warnings)

    warnings = lint_fragment_content(
        "skills.usage", "目录：{{SKILLS_CATALOG}}，加载用 {{UNKNOWN}}"
    )
    assert any("skill_view" in w for w in warnings)
    assert any("{{UNKNOWN}}" in w for w in warnings)

    warnings = lint_fragment_content(
        "skills.usage", "用 skill_view 加载：{{SKILLS_CATALOG}}"
    )
    assert warnings == []

    warnings = lint_template_content("角色：{{NAME}}")
    assert any("{{NAME}}" in w for w in warnings)


def test_全局约束始终进入静态核心():
    assert "全局约束" in compose_system_prompt()
    assert "全局约束" in compose_system_prompt(use_knowledge_base=True)
    assert "全局约束" not in compose_system_prompt(dynamic_only=True)


def test_默认提示词没有句中硬换行():
    texts = {
        "default": DEFAULT_SYSTEM_PROMPT,
        "global_safety": GLOBAL_SAFETY_NOTE,
        "skill_safety": SKILL_SAFETY_NOTE,
        "plan": PLAN_MODE_BLOCK,
        "todos": TODOS_RULES_BLOCK,
        "skills": SKILLS_USAGE_GUIDE,
        "kb": KB_INVENTORY_GUIDE,
    }
    texts.update({f"tool.{key}": text for key, text in TOOL_INSTRUCTIONS.items()})
    texts.update(
        {f"preset.{tpl['name']}": tpl["content"] for tpl in PRESET_TEMPLATES}
    )
    offenders = {
        name: find_mid_sentence_breaks(text)
        for name, text in texts.items()
        if find_mid_sentence_breaks(text)
    }
    assert not offenders, offenders


def test_lint_提示句中硬换行():
    warnings = lint_fragment_content(
        "tool.knowledge", "- 一句话被拆成两行，\n  第二行是续句。"
    )
    assert any("句中硬换行" in item for item in warnings)


def test_合并句中硬换行():
    text = "- 规则一，\n  续行；\n- 规则二\n\n规则三：\n  续行。"
    normalized = normalize_mid_sentence_breaks(text)
    assert normalized == "- 规则一，续行；\n- 规则二\n\n规则三：续行。"
    assert find_mid_sentence_breaks(normalized) == []
    assert normalize_mid_sentence_breaks(normalized) == normalized
