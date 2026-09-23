"""独立验证 app/terms.py（仅依赖标准库 + app.subtitles）。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.subtitles import Cue, parse_subtitle
from app.terms import TermEntry, check_cues

term_vtt = (
    "WEBVTT\n\n"
    "00:00:01.000 --> 00:00:03.000\n本字幕采用 WebVTT 格式发布\n\n"
    "00:00:03.000 --> 00:00:05.000\nwebvtt 可以直接在浏览器播放\n\n"
    "00:00:05.000 --> 00:00:07.000\n全角写法 ＷｅｂＶＴＴ 不统一\n\n"
    "00:00:07.000 --> 00:00:09.000\n也会写成 Web VTT 的形式\n\n"
    "00:00:09.000 --> 00:00:11.000\n偶见笔误 WebVTs 需修正\n\n"
    "00:00:11.000 --> 00:00:13.000\n请在账号设置里绑定邮箱\n\n"
    "00:00:13.000 --> 00:00:15.000\n帐号安全同样也很重要\n\n"
    "00:00:15.000 --> 00:00:17.000\n人工智能改变译制流程\n\n"
    "00:00:17.000 --> 00:00:19.000\n人工知能译法需要统一\n"
)

cues = parse_subtitle(term_vtt)
spans = [[i] for i in range(len(cues))]
terms = [
    TermEntry(id=1, language="all", canonical="WebVTT",
              forbidden=["webvtt"]),
    TermEntry(id=2, language="zh", canonical="账号",
              forbidden=["帐号"]),
    TermEntry(id=3, language="zh", canonical="人工智能"),
]

findings = check_cues(cues, spans, terms)
for f in findings:
    assert f.context[f.start_off:f.end_off] == f.matched
    print(f"cue {f.cue_seq + 1} [{f.category}] "
          f"{f.matched!r} -> {f.suggestion!r}")

from collections import Counter

cnt = Counter(f.category for f in findings)
assert dict(cnt) == {"forbidden": 2, "width": 1, "punct": 1,
                     "spelling": 2}, cnt
assert {f.matched for f in findings} == {
    "webvtt", "帐号", "ＷｅｂＶＴＴ", "Web VTT", "WebVTs", "人工知能"}

# 允许变体不产生发现（含大小写/空格变体）
ok = check_cues(
    [Cue(0, 1, "WEB-VTT 与 web vtt 都是允许的")], [[0]],
    [TermEntry(id=1, language="all", canonical="WebVTT",
               variants=["WEB-VTT", "web vtt"])])
assert ok == [], ok

# 英文 cue 中不应命中中文术语；通用术语对所有语言生效
en = check_cues([Cue(0, 1, "I love WebVTT")], [[0]],
                [terms[1], terms[0]])
assert [(f.category, f.matched) for f in en] == []

# 英文术语在英文 cue 中的大小写发现
encase = check_cues([Cue(0, 1, "I love webvtt")], [[0]], [terms[0]])
assert len(encase) == 1 and encase[0].category == "forbidden"
enlow = check_cues(
    [Cue(0, 1, "the new WEbvtt format")], [[0]],
    [TermEntry(id=9, language="en", canonical="WebVTT")])
assert len(enlow) == 1 and enlow[0].category == "case", enlow

# 短词不做模糊匹配（避免误报）
short = check_cues([Cue(0, 1, "AID ALD AD AI")], [[0]],
                   [TermEntry(id=8, language="en", canonical="AI")])
assert short == [], short

# 拉丁词边界：紧贴 CJK 应匹配；纯后缀延伸（WebVTTX）视为派生词不告警
edge = check_cues(
    [Cue(0, 1, "使用WebVTT格式与 WebVTTX 格式")], [[0]],
    [terms[0]])
assert edge == [], [f.matched for f in edge]
edge2 = check_cues(
    [Cue(0, 1, "使用webvtt格式")], [[0]],
    [terms[0]])
assert [f.matched for f in edge2] == ["webvtt"]

# 应用替换（模拟导出）
by_cue = {}
for f in findings:
    by_cue.setdefault(f.cue_seq, []).append(f)
replaced = [c.text for c in cues]
for seq, items in by_cue.items():
    t = replaced[seq]
    for f in sorted(items, key=lambda x: x.start_off, reverse=True):
        assert t[f.start_off:f.end_off] == f.matched
        t = t[:f.start_off] + f.suggestion + t[f.end_off:]
    replaced[seq] = t
out = "\n".join(replaced)
assert "webvtt" not in out and "帐号" not in out and "人工知能" not in out
assert out.count("WebVTT") == 5
assert out.count("账号") == 2
assert out.count("人工智能") == 2
print("TERMS_ENGINE_OK")
