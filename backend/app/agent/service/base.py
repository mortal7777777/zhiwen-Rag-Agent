"""服务共享基类：图执行参数推导（预算↔步数）与线程配置。

被 RunMixin / ResumeMixin 共用，所以单独放一层，避免两个 mixin 互相依赖。"""

from __future__ import annotations

from ...runtime_config import effective_or


class ServiceBaseMixin:
    def _recursion_limit(self) -> int:
        """按当前生效的工具预算推导图步数上限，避免"改了预算却先撞图步数"。

        一次 agent→tools 迭代 = 2 个 superstep，另加 prepare/dispatch/subagent/
        merge/finalize 的余量。设置页允许把 `agent_task_max_iterations` 调到 500，
        而 config 的 `agent_recursion_limit` 固定 110（≈50 次迭代）——不推导就会
        出现"预算还有一大半，图先报 Recursion limit"的假失败。
        """
        try:
            budget = max(
                int(effective_or(self.settings, "agent_max_iterations", 20) or 20),
                int(
                    effective_or(self.settings, "agent_task_max_iterations", 50)
                    or 50
                ),
            )
        except (TypeError, ValueError):
            budget = 50
        configured = int(getattr(self.settings, "agent_recursion_limit", 110) or 110)
        # 余量 10 与 config.py 的推导口径一致（50×2 + 10 = 110），默认配置下不改变现有行为
        return max(configured, budget * 2 + 10)

    def _thread_config(self, conversation_id: int) -> dict:
        return {
            "recursion_limit": self._recursion_limit(),
            "configurable": {"thread_id": f"conv:{conversation_id}"},
        }
