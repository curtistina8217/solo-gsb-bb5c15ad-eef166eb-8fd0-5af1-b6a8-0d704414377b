"""片段合辑：按用户给定的互不重叠半开时间窗 [start, end) 裁切修订 cue，

按窗口顺序拼接为一条新时间轴。规则：

- 窗口端点以秒计，起点非负、终点严格大于起点，窗口两两不重叠（允许相接）；
- 每个窗口取与修订 cue 时间轴的交集，裁到窗口边界；
- 新时间轴基准按窗口长度逐窗累加（即第 k 窗在新轴上的起点为前面所有窗口
  长度之和），窗内的静音间隙原样保留；
- 跨多个窗口（或被窗口边界切开）的 cue 拆成多条，各自独立映射；
- 交集为零时长（或取整后退化为零时长）的条目不生成。
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from .subtitles import Cue

MAX_WINDOWS = 100
_EPS = 1e-9


@dataclass
class CompiledCue:
    """合辑中的一条 cue（可能由同一条修订 cue 拆分而来）。"""
    seq: int            # 在合辑新时间轴上的序号（0 起）
    start: float        # 合辑时间码
    end: float
    text: str
    origin_seq: int     # 来源修订 cue 序号（0 起）


def normalize_windows(raw: list[dict]) -> list[tuple[float, float]]:
    """校验并规整窗口配置，失败时抛出 ValueError（信息可直接展示）。"""
    if not isinstance(raw, list):
        raise ValueError("窗口配置必须是数组")
    if len(raw) > MAX_WINDOWS:
        raise ValueError(f"窗口数量过多（上限 {MAX_WINDOWS} 个）")

    windows: list[tuple[float, float]] = []
    for idx, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ValueError(f"第 {idx + 1} 个窗口格式不正确")
        try:
            start = float(item.get("start"))
            end = float(item.get("end"))
        except (TypeError, ValueError):
            raise ValueError(f"第 {idx + 1} 个窗口的端点必须是数字")
        if not math.isfinite(start) or not math.isfinite(end):
            raise ValueError(f"第 {idx + 1} 个窗口的端点必须是有限数值")
        if start < 0:
            raise ValueError(f"第 {idx + 1} 个窗口起点不能为负")
        if end <= start:
            raise ValueError(f"第 {idx + 1} 个窗口终点必须大于起点")
        windows.append((start, end))

    # 用户在页面上按指定顺序排列，保存前同样要求互不重叠；
    # 排序仅用于重叠检测，不改变用户给定顺序
    ordered = sorted(windows)
    for (s1, e1), (s2, e2) in zip(ordered, ordered[1:]):
        if s2 < e1 - _EPS:
            raise ValueError(
                f"窗口 [{s1:g}, {e1:g}) 与 [{s2:g}, {e2:g}) 存在重叠")
    return windows


def _round3(value: float) -> float:
    return round(value + 0.0, 3)


def compile_cues(cues: list[Cue],
                 windows: list[tuple[float, float]]) -> list[CompiledCue]:
    """按窗口裁切修订 cue 并依次拼接。

    时间映射：原始时间 t（落在第 k 窗内）映射为
    ``sum(len(window_i), i < k) + (t - window_k.start)``。
    由于基准只累加窗口长度，窗内静音间隙保留、窗外内容丢弃。
    """
    compiled: list[CompiledCue] = []
    # 第 k 窗在新轴上的起点 = 前面所有窗口长度之和
    offsets: list[float] = []
    total = 0.0
    for start, end in windows:
        offsets.append(total)
        total += end - start

    for wi, (w_start, w_end) in enumerate(windows):
        base = offsets[wi]
        for origin, cue in enumerate(cues):
            s = max(cue.start, w_start)
            e = min(cue.end, w_end)
            if e - s <= _EPS:
                continue  # 与窗口无正长度交集
            ns = _round3(base + s - w_start)
            ne = _round3(base + e - w_start)
            if ne <= ns:
                # 正长度交集不足 1ms，按 VTT 毫秒精度取整后退化为零时长：
                # 不生成该条（顺延会越出本窗口在新轴上的分配区间）
                continue
            compiled.append(CompiledCue(
                seq=len(compiled), start=ns, end=ne,
                text=cue.text, origin_seq=origin))
    return compiled
