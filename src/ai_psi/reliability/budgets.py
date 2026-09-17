"""认知预算的实时记账（任务书 §13.1、ADR-0008）。

🔴 **本模块是"循环永不超预算"这条硬指标的落点。**

超预算回合率是任务书 §16.1 的关键硬指标之一，要求为 **0**。
让这条指标成立的做法不是"事后统计然后报警"，而是**在花钱之前先问够不够**：

* 每一次模型调用前调用 :meth:`BudgetTracker.spend_model_call`；
* 编排器在跑**可选模块**之前用 :meth:`BudgetTracker.can_afford` 连同
  **尾部保留额度**一起判断——保证无论如何都留得下判断合成与回答渲染。

因此超预算是**不可能事件**，而不是"应当避免的事"。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from ai_psi.domain.cognitive_rounds import CognitiveBudget
from ai_psi.domain.exceptions import BudgetExhaustedError

__all__ = ["MANDATORY_TAIL_CALLS", "BudgetSnapshot", "BudgetTracker"]

#: 回合尾部的**强制**模型调用数：判断合成 + 回答渲染。
#:
#: 编排器为它保留额度。没有这个保留，"前面的可选模块花光了钱"
#: 就会变成"回合拿不出结论"——那比少做一次分析糟糕得多。
MANDATORY_TAIL_CALLS: Final[int] = 2


@dataclass(frozen=True, slots=True)
class BudgetSnapshot:
    """某一时刻的预算快照，用于事件负载与诊断。"""

    max_model_calls: int
    model_calls_used: int
    max_metacognitive_loops: int
    metacognitive_loops_used: int

    @property
    def remaining_model_calls(self) -> int:
        """剩余模型调用次数。"""
        return self.max_model_calls - self.model_calls_used

    @property
    def remaining_metacognitive_loops(self) -> int:
        """剩余元认知循环轮数。"""
        return self.max_metacognitive_loops - self.metacognitive_loops_used


class BudgetTracker:
    """一次认知回合内的预算记账器。

    **每个回合一个实例。** 跨回合复用会让上一个回合的消耗计入下一个回合。
    """

    def __init__(
        self,
        budget: CognitiveBudget,
        *,
        tail_reserve: int = MANDATORY_TAIL_CALLS,
    ) -> None:
        """初始化。

        Args:
            budget: 回合预算。
            tail_reserve: 为强制尾部保留的调用数。
        """
        self._budget = budget
        self._tail_reserve = max(0, tail_reserve)
        self._model_calls_used = 0
        self._metacognitive_loops_used = 0

    # ------------------------------------------------------------------
    # 只读视图
    # ------------------------------------------------------------------

    @property
    def budget(self) -> CognitiveBudget:
        """原始预算配置。"""
        return self._budget

    @property
    def model_calls_used(self) -> int:
        """已消耗的模型调用数。"""
        return self._model_calls_used

    @property
    def metacognitive_loops_used(self) -> int:
        """已执行的元认知循环轮数。"""
        return self._metacognitive_loops_used

    @property
    def remaining_model_calls(self) -> int:
        """剩余模型调用次数（**含**尾保留）。"""
        return self._budget.max_model_calls - self._model_calls_used

    @property
    def remaining_metacognitive_loops(self) -> int:
        """剩余元认知循环轮数。"""
        return self._budget.max_metacognitive_loops - self._metacognitive_loops_used

    @property
    def tail_reserve(self) -> int:
        """尾部保留额度。"""
        return self._tail_reserve

    def set_tail_reserve(self, reserve: int) -> None:
        """调整尾部保留额度。

        保留额度**随回合推进而收缩**：分析阶段要同时留出判断合成、
        元认知与回答渲染三次调用；判断一旦产出，就只剩回答渲染需要保留。
        固定不变会让后面几步被无谓地判为"预算不足"——
        那是把保守当成安全，代价是白白丢掉一次元认知复核。

        Args:
            reserve: 新的保留额度（负数按 0 处理）。
        """
        self._tail_reserve = max(0, reserve)

    def snapshot(self) -> BudgetSnapshot:
        """返回当前快照。"""
        return BudgetSnapshot(
            max_model_calls=self._budget.max_model_calls,
            model_calls_used=self._model_calls_used,
            max_metacognitive_loops=self._budget.max_metacognitive_loops,
            metacognitive_loops_used=self._metacognitive_loops_used,
        )

    # ------------------------------------------------------------------
    # 判断与消耗
    # ------------------------------------------------------------------

    def can_afford(self, calls: int = 1, *, keep_tail_reserve: bool = True) -> bool:
        """是否负担得起 ``calls`` 次调用。

        Args:
            calls: 期望消耗的调用数。
            keep_tail_reserve: 是否要求消耗后仍留有尾部保留额度。
                **可选模块必须保持为 ``True``**；只有强制尾部模块
                （判断合成、回答渲染）才传 ``False``。

        Returns:
            负担得起返回 ``True``。
        """
        if calls < 0:
            msg = "calls 不得为负"
            raise ValueError(msg)
        required = calls + (self._tail_reserve if keep_tail_reserve else 0)
        return self.remaining_model_calls >= required

    def spend_model_call(self, *, task_name: str) -> None:
        """消耗一次模型调用额度。

        🔴 **在调用 Provider 之前调用本方法**——先记账再花钱，
        顺序反了就会出现"已经花掉但记账失败"的窗口。

        Args:
            task_name: 消耗额度的任务名，用于错误上下文。

        Raises:
            BudgetExhaustedError: 额度不足。
        """
        if self.remaining_model_calls <= 0:
            msg = (
                f"模型调用预算耗尽：上限 {self._budget.max_model_calls}，"
                f"已用 {self._model_calls_used}（任务 {task_name}）"
            )
            raise BudgetExhaustedError(
                msg,
                budget_name="max_model_calls",
                limit=self._budget.max_model_calls,
                used=self._model_calls_used,
                context={"task_name": task_name},
            )
        self._model_calls_used += 1

    def rebase(self, budget: CognitiveBudget) -> BudgetTracker:
        """换用新的预算配置，**保留已消耗的计数**。

        深度路由在回合开始之后才定级，因此预算需要从初始上限下调到
        该深度的真实预算。重新构造一个记账器而不搬运计数，
        会让新预算看起来"一分钱都没花"——那是超预算的经典成因。

        Args:
            budget: 新的预算配置。

        Returns:
            保留已消耗计数的新记账器。
        """
        rebased = BudgetTracker(budget, tail_reserve=self._tail_reserve)
        rebased._model_calls_used = self._model_calls_used
        rebased._metacognitive_loops_used = self._metacognitive_loops_used
        return rebased

    def start_metacognitive_loop(self) -> None:
        """开始一轮元认知循环。

        Raises:
            BudgetExhaustedError: 循环轮数已用完。
        """
        if self.remaining_metacognitive_loops <= 0:
            msg = (
                f"元认知循环次数已达上限 {self._budget.max_metacognitive_loops}。"
                "注意：超限**不判失败**，而是强制 STOP 进入合成（ADR-0008）"
            )
            raise BudgetExhaustedError(
                msg,
                budget_name="max_metacognitive_loops",
                limit=self._budget.max_metacognitive_loops,
                used=self._metacognitive_loops_used,
            )
        self._metacognitive_loops_used += 1
