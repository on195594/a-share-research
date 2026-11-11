"""Explicit framework routing and portfolio metadata."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class FrameworkRouting:
    key: str
    portfolio_label: str
    industry_keywords: tuple[str, ...]
    stop_loss_pct: tuple[float, float] | None


FRAMEWORK_ROUTING = {
    "A": FrameworkRouting("A", "A通用", (), None),
    "B": FrameworkRouting("B", "B银行", ("银行",), (0.88, 0.82)),
    "C": FrameworkRouting(
        "C",
        "C资源",
        ("煤炭", "石油", "天然气", "有色金属", "铜", "钢铁", "采矿"),
        (0.82, 0.75),
    ),
    "D": FrameworkRouting(
        "D",
        "D公用",
        ("水电", "水力发电", "电网", "水务", "燃气", "高速", "公用事业"),
        (0.88, 0.82),
    ),
    "E": FrameworkRouting("E", "E消费", ("白酒", "消费", "食品", "零售", "饮料"), None),
    "F": FrameworkRouting(
        "F",
        "F科技",
        ("互联网", "软件", "科技", "半导体", "游戏", "通信"),
        (0.80, 0.72),
    ),
}
