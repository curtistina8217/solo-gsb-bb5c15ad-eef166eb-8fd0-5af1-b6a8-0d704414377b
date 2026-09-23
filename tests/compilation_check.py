"""片段合辑纯逻辑校验（仅需标准库）：

    python3 tests/compilation_check.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.subtitles import Cue, write_vtt  # noqa: E402
from app.compilation import compile_cues, normalize_windows  # noqa: E402


def check(label, cond):
    if not cond:
        raise AssertionError(label)
    print(f"ok - {label}")


# --- 校验规则 ---
def expect_error(raw, needle):
    try:
        normalize_windows(raw)
    except ValueError as exc:
        check(f"拒绝非法配置「{needle}」", needle in str(exc))
    else:
        raise AssertionError(f"应拒绝：{raw}")


expect_error([{"start": -1, "end": 2}], "起点不能为负")
expect_error([{"start": 1, "end": 1}], "终点必须大于起点")
expect_error([{"start": 0, "end": 2}, {"start": 1.5, "end": 3}], "存在重叠")
expect_error([{"start": 0, "end": float("nan")}], "有限数值")
expect_error([{"start": "x", "end": 2}], "数字")
check("空窗口集合法", normalize_windows([]) == [])
check("相接窗口合法",
      normalize_windows([{"start": 0, "end": 2}, {"start": 2, "end": 5}])
      == [(0.0, 2.0), (2.0, 5.0)])
# 重叠检测对乱序输入同样生效
expect_error([{"start": 1, "end": 3}, {"start": 0, "end": 1.5}], "存在重叠")

# --- 拼接规则 ---
cues = [
    Cue(0.0, 4.0, "A"),
    Cue(5.0, 9.0, "B"),
]

# 窗口按用户顺序；基准按窗口长度累加；窗外内容丢弃
# 窗 [6,8) 裁 B→[0,2)；窗 [1,3) 裁 A[1,3)→base 2+偏移=[2,4)
cc = compile_cues(cues, [(6.0, 8.0), (1.0, 3.0)])
check("乱序窗口保持用户顺序", [(c.origin_seq, c.start, c.end) for c in cc]
      == [(1, 0.0, 2.0), (0, 2.0, 4.0)])

# 跨窗口 cue 拆分；窗内静音间隙保留
# A 跨窗 [1,2.5)/[3,7) 拆两段：[0,1.5)、[1.5,2.5)；
# B[5,7) 在第二窗内偏移 2、base=1.5 → [3.5,5.5)，与 A 第二段间隔 1s
cc = compile_cues(cues, [(1.0, 2.5), (3.0, 7.0)])
check("跨窗口 cue 拆分且保留窗内间隙",
      [(c.origin_seq, c.start, c.end) for c in cc]
      == [(0, 0.0, 1.5), (0, 1.5, 2.5), (1, 3.5, 5.5)])
gap = cc[2].start - cc[1].end
check("新轴上 A 与 B 间保留 1s 静音间隙", abs(gap - 1.0) < 1e-9)

# 窗口只覆盖静音间隙 → 无 cue（不报错）
cc = compile_cues(cues, [(4.2, 4.8)])
check("空窗口交集不产生 cue", cc == [])

# 边界相接：半开区间，仅端点相接时交集为零时长，不生成 cue
# A.end=4 与窗口起点 4 相接；B.start=5 与窗口终点 5 相接
check("两侧都仅端点相接时无 cue", compile_cues(cues, [(4.0, 5.0)]) == [])
# 同样，B 在窗口 [1,8) 内取 [5,8)；A 取 [1,4)（右端点 4 是 A 自身结尾）
cc = compile_cues(cues, [(1.0, 8.0)])
check("窗内裁切保留 cue 自身边界",
      [(c.origin_seq, round(c.start, 3), round(c.end, 3)) for c in cc]
      == [(0, 0.0, 3.0), (1, 4.0, 7.0)])

# 不足 1ms 的正长度交集取整后丢弃，不生成零时长 cue
cc = compile_cues([Cue(0.0, 0.0004, "X")], [(0.0, 1.0)])
check("亚毫秒片段不生成零时长 cue", cc == [])

# 正常片段保留毫秒精度
cc = compile_cues([Cue(0.0004, 0.002, "X")], [(0.0, 1.0)])
check("正长度片段保留", len(cc) == 1 and cc[0].end > cc[0].start)

# --- 导出：空合辑仍是有效 WebVTT，NOTE 随 cue 位置附着 ---
empty_vtt = write_vtt([])
check("空合辑导出为有效空 WebVTT", empty_vtt.strip() == "WEBVTT")

cc = compile_cues(cues, [(1.0, 3.0), (6.0, 8.0)])
out = [Cue(c.start, c.end, c.text) for c in cc]
# 假设原 cue 0 带有校验 NOTE
notes = {i: (["[校验问题/line_too_long] 原因"] if c.origin_seq == 0 else [])
         for i, c in enumerate(cc)}
vtt = write_vtt(out, notes=notes)
check("合辑 VTT 含两条 cue", vtt.count("-->") == 2)
# 时间码为重映射后的合辑码（窗 1 的 B 在前，窗 2 的 A 在后）
check("合辑时间码按窗口长度累加",
      "00:00:00.000 --> 00:00:02.000" in vtt
      and "00:00:02.000 --> 00:00:04.000" in vtt)
# NOTE 块紧贴在来自原 cue 0 的片段（第一窗内）之前
blocks = vtt.rstrip("\n").split("\n\n")
note_idx = next(i for i, b in enumerate(blocks)
                if b.startswith("NOTE [校验问题/line_too_long]"))
check("仅原 cue 0 的片段带校验 NOTE", vtt.count("NOTE ") == 1)
check("NOTE 紧邻来自原 cue 0 的片段",
      blocks[note_idx + 1] == "00:00:00.000 --> 00:00:02.000\nA")
check("来自原 cue 1 的片段无 NOTE",
      blocks[note_idx + 2] == "00:00:02.000 --> 00:00:04.000\nB")

print("COMPILATION_CHECK_OK")


if __name__ == "__main__":
    pass
