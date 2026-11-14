"""知识条目分批排期工具。

根据 front matter 中声明的先决条目、可选替代条目和预计导入耗时，
在"每晚一个固定入库时段、入库后下一晚才能被引用"的约束下，
求出指定专题可最早交付的分批清单。
"""

from .model import Entry, EntryCatalog, ScheduleError
from .plan import BatchPlan, PlanResult, build_plan

__all__ = [
    "Entry",
    "EntryCatalog",
    "ScheduleError",
    "BatchPlan",
    "PlanResult",
    "build_plan",
]
