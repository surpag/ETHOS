"""Run 终态的语义投影：把 end_reason（诊断）换算成 outcome（语义）。

为什么要分开：

- `end_reason` 回答「为什么停下来」，取值会随实现增长——轮次耗尽、上下文预算、
  输出被 length 截断、内容过滤、以后可能还有新的外力打断。
- `outcome` 回答「任务完成了吗」，取值有限，且**默认值必须是保守的那个**。

消费方（UI、会话列表、任何判「还能不能继续」的地方）一律只看 outcome。
如果让消费方去枚举 end_reason，后端每加一种终止原因，所有消费方都会静默漏掉它，
表现为「任务没做完，界面却说做完了」。这里把判定收敛成一个函数，新增原因不需要
改任何消费方。

三态而非两态（与 completion_evidence 同一条纪律）：

| end_reason      | outcome      | 含义                              |
| --------------- | ------------ | --------------------------------- |
| None            | None         | 未知（该字段引入前的旧行）          |
| "completed"     | "complete"   | 模型主动收尾                       |
| 其余任何字符串   | "incomplete" | 外力打断 / 资源耗尽，任务未完成      |

把 None 当 incomplete 等于给历史消息盖一个「没做完」的章，是凭空指控；
把 None 当 complete 则会掩盖真实的未完成。所以未知就是未知，不下结论。
"""

from __future__ import annotations

OUTCOME_COMPLETE = "complete"
OUTCOME_INCOMPLETE = "incomplete"

#: 唯一的「完成」原因：模型自己决定收尾。
END_REASON_COMPLETED = "completed"


def derive_outcome(end_reason: str | None) -> str | None:
    """把终止原因投影成任务语义；None 原样返回（未知）。

    判定刻意写成「只承认 completed」，而不是枚举未完成的原因：
    未知原因、以及将来新增的任何外力打断都自动归为 incomplete。
    宁可多提示一次「任务未完成」，也不能把半成品显示成已完成。
    """
    if end_reason is None:
        return None
    return OUTCOME_COMPLETE if end_reason == END_REASON_COMPLETED else OUTCOME_INCOMPLETE
