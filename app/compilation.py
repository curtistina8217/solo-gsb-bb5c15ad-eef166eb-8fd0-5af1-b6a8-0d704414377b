"""片段合辑：按用户给定的互不重叠半开时间窗拼接修订 cue。

窗口语义为 [start, end)（秒，起点非负、终点大于起点），按用户指定顺序处理；
每条 cue 与各窗口取半开交集、裁到窗口边界，窗口长度作为该段在新时间轴上的
基准偏移依次累加，因此窗内静音间隙保留、窗口之间的间隙被丢弃，跨窗口的 cue
被拆成多条（文本相同），不产生零时长 cue。
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from .subtitles import Cue

MAX_WINDOWS = 200
_EPS = 1e-6
# 输出时间码以毫秒为最小单位，裁后不足半毫秒的片段落到同一毫秒刻度，
# 视为零时长不生成
_MIN_MS = 0.0005


@dataclass
class Window:
    start: float
    end: float


@dataclass
class CompiledCue:
    """合辑时间轴上的一条 cue。source_seq 为来源修订 cue 的 0 基序号。"""
    start: float
    end: float
    text: str
    source_seq: int
    window_index: int


def normalize_windows(raw: list[dict]) -> list[Window]:
    """校验并归一窗口配置：非负起点、终点大于起点、互不重叠（允许相切）。"""
    if len(raw) > MAX_WINDOWS:
        raise ValueError(f"窗口数量过多（上限 {MAX_WINDOWS} 个）")
    windows: list[Window] = []
    for i, w in enumerate(raw):
        label = f"第 {i + 1} 个窗口"
        try:
            start = float(w["start"])
            end = float(w["end"])
        except (TypeError, ValueError, KeyError):
            raise ValueError(f"{label}的时间端点必须是数字（秒）")
        if not math.isfinite(start) or not math.isfinite(end):
            raise ValueError(f"{label}的时间端点必须是有限数字（秒）")
        if start < -_EPS:
            raise ValueError(f"{label}起点不能为负（{start:g}s）")
        if start < 0:
            start = 0.0
        if end <= start + _EPS:
            raise ValueError(
                f"{label}终点（{end:g}s）必须大于起点（{start:g}s）")
        windows.append(Window(start=start, end=end))

    # 半开窗口允许相切：[a,b) 与 [b,c) 不重叠
    for i in range(len(windows)):
        for j in range(i + 1, len(windows)):
            a, b = windows[i], windows[j]
            if a.start < b.end - _EPS and b.start < a.end - _EPS:
                raise ValueError(
                    f"第 {i + 1} 个窗口与第 {j + 1} 个窗口存在重叠，"
                    "窗口之间只能相切、不能相交")
    return windows


def _round_ms(value: float) -> float:
    return round(value + 0.0, 3)


def compile_cues(cues: list[Cue], windows: list[Window]) -> list[CompiledCue]:
    """从已保存的修订 cue 生成合辑时间轴。

    窗口按用户指定顺序处理，基准偏移按窗口长度累加；窗内 cue 间的静音间隙
    原样保留，窗口之间不再插入任何时长；跨窗口 cue 拆成多条。修订阶段会给
    cue 末端加毫秒级余量，相邻修订 cue 可能因此轻微重叠，这里在同一窗口内
    把后一条 cue 的起点钳到前一条的裁后终点，保证合辑时间码单调。
    """
    compiled: list[CompiledCue] = []
    base = 0.0
    for wi, win in enumerate(windows):
        length = win.end - win.start
        last_end: float | None = None
        for seq, cue in enumerate(cues):
            s = max(cue.start, win.start)
            e = min(cue.end, win.end)
            if last_end is not None and s < last_end:
                s = last_end  # 修订余量导致的相邻重叠：钳到上一条裁后终点
            if e - s < _MIN_MS:
                continue
            ns = _round_ms(base + s - win.start)
            ne = _round_ms(base + e - win.start)
            if ne <= ns:  # 裁剪到毫秒后零时长：不生成该 cue
                continue
            compiled.append(CompiledCue(
                start=ns, end=ne, text=cue.text,
                source_seq=seq, window_index=wi))
            last_end = e
        base += length
    return compiled


def window_offsets(windows: list[Window]) -> list[float]:
    """各窗口在合辑时间轴上的起点（窗口长度累加）。"""
    offsets: list[float] = []
    total = 0.0
    for w in windows:
        offsets.append(_round_ms(total))
        total += w.end - w.start
    return offsets


def total_duration(windows: list[Window]) -> float:
    return _round_ms(sum(w.end - w.start for w in windows))


def build_vtt(compiled: list[CompiledCue],
              notes: dict[int, list[str]] | None = None) -> str:
    """导出合辑 WebVTT，保留来源修订 cue 对应的校验 NOTE。"""
    from .subtitles import write_vtt
    cue_list = [Cue(start=c.start, end=c.end, text=c.text) for c in compiled]
    cue_notes = {pos: notes.get(c.source_seq, [])
                 for pos, c in enumerate(compiled)} if notes else None
    return write_vtt(cue_list, notes=cue_notes)
