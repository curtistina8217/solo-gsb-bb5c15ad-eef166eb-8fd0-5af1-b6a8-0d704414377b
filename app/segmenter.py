"""跨 cue 动态规划断句引擎。

流程：
1. 把全部 cue 文本展开为不可再分的「单元」（拉丁字母连续词 / 单个 CJK 字 /
   附着标点），每个单元携带时间、来源 cue 以及其前方是否存在空白；
2. 在相邻单元之间动态规划选取切点。切点若落在不同原 cue 之间，会在相邻 cue 的
   重叠/间隙区间内重分配边界时间（默认取中点）；
3. 无法同时满足每行字数、最小停留、阅读速度的最小区间被标注，并给出原因。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .subtitles import Cue, detect_language

_EPS = 0.001
# 违例权重：超长可由切分结构性修复，权重最高；时间类约束只能靠边界重分配，
# 权重低一档。这样 DP 会先消除所有可消除的超长，再在剩余最小区段上标注
# 客观无法满足的停留/速度约束。
_W_LINE = 1_000_000
_W_TIME = 1_000

_TOKEN_RE = re.compile(
    r"[A-Za-zÀ-ÿ0-9][A-Za-zÀ-ÿ0-9'#\-]*"  # 拉丁词/数字（含 don't / 3D / 1080p）
    r"|[\s]+"
    r"|[^\sA-Za-zÀ-ÿ0-9]"
)
# 汉字 / 假名：相邻时之间不插空格（韩文词汇间本身有空格，不在此列）
_NO_SPACE_RE = re.compile(r"[㐀-䶿一-鿿豈-﫿぀-ヿ]")


@dataclass
class Unit:
    text: str
    length: int                 # 视觉宽度（按字符数）
    center: float               # 在原 cue 时间轴上的中心时刻
    cue_idx: int
    cue_start: float
    cue_end: float
    offset: float               # 单元中心在 cue 内的位置（0..1）
    ws_before: bool = False     # 该单元与前一单元之间在原文中是否有空白


@dataclass
class Issue:
    index: int                  # 修订后 cue 序号
    kind: str                   # line_too_long / dwell_too_short / read_speed
    severity: str
    language: str
    reason: str
    segment: str
    start: float
    end: float
    redistributable: bool


@dataclass
class Result:
    cues: list[Cue]
    issues: list[Issue]
    stats: dict
    # 每个修订 cue 跨越的原 cue 序号（0 基）
    spans: list[list[int]] = field(default_factory=list)
    # 每个修订 cue 的检测语言（按来源原 cue 语言多数决，与问题标注一致）
    cue_languages: list[str] = field(default_factory=list)


def _is_no_space(ch: str) -> bool:
    return bool(_NO_SPACE_RE.match(ch))


def _units_for_cue(cue: Cue, cue_idx: int, cross_cue: bool) -> list[Unit]:
    """把一个 cue 的文本切成单元，先 token 化再把尾随标点并入前一个词。"""
    raw = _TOKEN_RE.findall(cue.text.replace("\n", " "))

    # 合并：标点附到前一个非空白 token 后（句内标点不单独成行）
    tokens: list[str] = []
    pending_ws = False
    ws_flags: list[bool] = []
    for tok in raw:
        if tok.isspace():
            pending_ws = True
            continue
        if (tokens and not tok[0].isalnum()
                and not _is_no_space(tok[0])):
            # 标点附着：与前一个单元合并，沿用其空白标志
            tokens[-1] += tok
            continue
        ws_flags.append(pending_ws)
        pending_ws = False
        tokens.append(tok)

    units: list[Unit] = []
    total = max(1, len(tokens))
    for pos, (tok, ws) in enumerate(zip(tokens, ws_flags)):
        offset = (pos + 0.5) / total
        first = pos == 0
        units.append(Unit(
            text=tok,
            length=len(tok),
            center=cue.start + offset * cue.duration,
            cue_idx=cue_idx,
            cue_start=cue.start,
            cue_end=cue.end,
            offset=offset,
            ws_before=(cross_cue if first else ws),
        ))
    return units


def _separator(left: Unit, right: Unit) -> str:
    """两个单元拼回文本时使用的连接符（汉字/假名相邻不插空格）。"""
    if not right.ws_before:
        return ""
    if _is_no_space(left.text[-1]) and _is_no_space(right.text[0]):
        return ""
    return " "


def _cut_time(left: Unit, right: Unit) -> tuple[float, bool]:
    """计算 left 与 right 之间切点的时间戳。

    返回 (时间, 是否跨 cue 重分配)。
    """
    if left.cue_idx == right.cue_idx:
        # 同一 cue 内：按字符位置插值
        s, e = left.cue_start, left.cue_end
        if s == e:
            return s, False
        cut = s + ((left.offset + right.offset) / 2.0) * (e - s)
        return min(max(cut, s), e), False

    a_s, a_e = left.cue_start, left.cue_end
    b_s, b_e = right.cue_start, right.cue_end

    if a_e < b_s:
        # cue 之间存在间隙：中点分配
        return (a_e + b_s) / 2.0, True
    if a_s == a_e and b_s == b_e:
        return (a_s + b_s) / 2.0, True
    if a_s == a_e:
        return a_s, True
    if b_s == b_e:
        return b_s, True
    # 重叠或首尾相接：在重叠区间内取中点
    lo, hi = max(a_s, b_s), min(a_e, b_e)
    if lo <= hi:
        return (lo + hi) / 2.0, True
    # 理论上不会到达，退回各自边界中点
    return (a_e + b_s) / 2.0, True


def analyze(cues: list[Cue], max_chars: int, min_dwell: float,
            max_cps: float) -> Result:
    cues = [c for c in cues if c.text.strip()]
    if not cues:
        raise ValueError("没有包含文本的字幕条目")
    if max_chars < 1:
        raise ValueError("每行字数必须大于 0")
    if min_dwell <= 0 or max_cps <= 0:
        raise ValueError("最小停留与阅读速度阈值必须为正数")

    # 语言以原 cue 全文检测，切分后的短片段继承其来源 cue 的语言
    cue_langs = [detect_language(c.text) for c in cues]

    # 1. 展开为全局单元流
    units: list[Unit] = []
    for i, cue in enumerate(cues):
        units.extend(_units_for_cue(cue, i, cross_cue=bool(units)))
    n = len(units)

    # 2. 连接符、前缀视觉长度与切点时间
    seps = [""] * (n - 1)   # seps[k] = units[k] 与 units[k+1] 之间的连接符
    for k in range(n - 1):
        seps[k] = _separator(units[k], units[k + 1])

    # pref_len[k] = units[0:k] 拼接后的视觉长度（含内部连接符）
    pref_len = [0] * (n + 1)
    for k in range(1, n + 1):
        pref_len[k] = (pref_len[k - 1] + units[k - 1].length
                       + (len(seps[k - 2]) if k >= 2 else 0))

    cut_ts: list[float] = [0.0] * (n - 1)
    for k in range(n - 1):
        cut_ts[k], _ = _cut_time(units[k], units[k + 1])

    # cue 间存在静音间隙时，切点为强制位置（cue 不能覆盖整段间隙），
    # 但间隙时长仍可在两侧段之间重新分配
    forced: set[int] = set()
    for k in range(n - 1):
        if units[k].cue_idx != units[k + 1].cue_idx:
            if units[k].cue_end < units[k + 1].cue_start:
                forced.add(k + 1)

    t0 = units[0].cue_start
    tn = units[-1].cue_end

    def seg_text(i: int, j: int) -> str:
        parts = [units[i].text]
        for k in range(i + 1, j):
            parts.append(seps[k - 1])
            parts.append(units[k].text)
        return "".join(parts)

    def seg_len(i: int, j: int) -> int:
        """units[i:j] 段的视觉字符数（仅含段内连接符）。"""
        if j <= i:
            return 0
        total = pref_len[j] - pref_len[i]
        if i > 0:
            total -= len(seps[i - 1])  # 减去段首与上一段之间的连接符
        return total

    # 3. 动态规划
    #    dp[k] = 把前 k 个单元切成若干段的最优成本；par[k] = 上一个切点
    m0 = int(min_dwell * max_cps)
    # 搜索窗口上限：正常输入远超需求，仅为防御超长不可分词导致的 O(n^2)
    window_cap = max(max_chars, m0) + 3 * max_chars + 64

    @dataclass
    class _S:
        cost: float

    INF = float("inf")
    dp = [_S(INF)] * (n + 1)
    dp[0] = _S(0.0)
    par = [-1] * (n + 1)

    def seg_cost(i: int, j: int) -> float:
        length = seg_len(i, j)
        st = t0 if i == 0 else cut_ts[i - 1]
        en = tn if j == n else cut_ts[j - 1]
        dur = max(0.0, en - st)
        cost = 0.0

        excess = length - max_chars
        if excess > 0:
            cost += _W_LINE + excess * 100

        need_time = length / max_cps
        for short in (min_dwell - dur, need_time - dur):
            if short > 1e-9:
                cost += _W_TIME + short * 10_000
        # 次级目标：在不违例前提下，优先更少的分段（避免过度碎片化），
        # 其次减少多余停留；额外保留原 cue 边界（除非合并能消除违例）
        cross_cue_merge = len({u.cue_idx for u in units[i:j]}) > 1
        cost += 0.5 + (2.0 if cross_cue_merge else 0.0) \
            + max(0.0, dur - need_time)
        return cost

    for j in range(1, n + 1):
        best = dp[j]
        lo = max(0, j - window_cap)
        for i in range(j - 1, lo - 1, -1):
            # i 与 j 之间不能跨越强制切点（cue 间静音间隙）
            if any(i < f < j for f in forced):
                continue
            prev = dp[i]
            if prev.cost == INF:
                continue
            cand_cost = prev.cost + seg_cost(i, j)
            if cand_cost < best.cost:
                best = _S(cand_cost)
                par[j] = i
        dp[j] = best

    # 4. 回溯切点
    bounds: list[int] = []
    k = n
    while k > 0:
        bounds.append(k)
        k = par[k] if par[k] >= 0 else 0
    bounds.reverse()

    # 5. 组装修订 cue
    revised: list[Cue] = []
    spans: list[list[int]] = []
    revised_langs: list[str] = []
    left = 0
    for b in bounds:
        st = t0 if left == 0 else cut_ts[left - 1]
        en = tn if b == n else cut_ts[b - 1]
        if en < st:
            en = st
        text = seg_text(left, b)
        src = sorted({u.cue_idx for u in units[left:b]})
        # 修订 cue 的语言按来源原 cue 语言多数决；语言以原 cue 全文检测，
        # 切分后的短片段继承其来源 cue 的语言
        src_langs = [cue_langs[s] for s in src]
        revised_langs.append(max(set(src_langs), key=src_langs.count))
        revised.append(Cue(start=round(st, 3), end=round(en + _EPS, 3),
                           text=text))
        spans.append(src)
        left = b

    # 边界重分配统计：对每对相邻且无间隙的原 cue，若原边界被合并消失，
    # 或修订切点时间偏离原边界时间，即计为一次重分配
    bound_set = set(bounds)
    moved = 0
    for k in range(n - 1):
        if units[k].cue_idx == units[k + 1].cue_idx:
            continue
        a_e = units[k].cue_end
        b_s = units[k + 1].cue_start
        if a_e < b_s:
            continue  # 静音间隙：不重分配，仅在中点切开
        p = k + 1  # 单元边位置
        if p not in bound_set:
            moved += 1  # 原边界被合并进修订段
        elif a_e == b_s and abs(cut_ts[k] - a_e) <= 2 * _EPS:
            pass        # 切点与原边界一致，未移动
        else:
            moved += 1  # 重叠区间内取点，或切点偏离原边界

    # 6. 生成问题报告（最少违例区段解释）
    issues: list[Issue] = []
    for idx, cue in enumerate(revised):
        src = spans[idx]
        length = len(cue.text)
        dur = cue.duration
        cps = length / dur if dur > 0 else float("inf")
        lang = revised_langs[idx]

        # 该段跨原 cue（合并）或与跨 cue 段相邻时，
        # 说明 DP 已经探索过跨 cue 借调时间的可能
        can_redistribute = len(src) > 1 or (
            idx > 0 and len(spans[idx - 1]) > 1) or (
            idx + 1 < len(spans) and len(spans[idx + 1]) > 1)

        problems: list[tuple[str, str]] = []
        if length > max_chars:
            over = length - max_chars
            problems.append((
                "line_too_long",
                f"共 {length} 字，超出每行 {max_chars} 字上限 {over} 字；"
                "该区间内含无法拆分的连续词/字符，任何切分都会保留超长片段。",
            ))
        if dur < min_dwell - 1e-9:
            problems.append((
                "dwell_too_short",
                f"停留 {dur:.2f}s，低于最小停留 {min_dwell:.2f}s；"
                f"在约束下该段至少需要 {max(min_dwell, length / max_cps):.2f}s，"
                "相邻 cue 可调整空间不足，无法补足停留时间。",
            ))
        if cps > max_cps + 1e-9:
            problems.append((
                "read_speed",
                f"阅读速度约 {cps:.1f} 字/秒，超过阈值 {max_cps:g} 字/秒；"
                f"{length} 字按阈值需要 {length / max_cps:.2f}s，"
                f"实际仅 {dur:.2f}s，且相邻 cue 边界无可借用的时间。",
            ))

        for kind, reason in problems:
            if can_redistribute:
                reason = reason.replace("。", "")
                reason += "；已尝试在相邻 cue 间重分配边界，但时间预算仍不满足。"
            issues.append(Issue(
                index=idx,
                kind=kind,
                severity="error",
                language=lang,
                reason=reason,
                segment=cue.text,
                start=cue.start,
                end=cue.end,
                redistributable=can_redistribute,
            ))

    # “拆分”：同一原 cue 出现在多个修订 cue 中；“合并”：一个修订 cue 跨多个原 cue
    seen: dict[int, int] = {}
    for s in spans:
        for c in s:
            seen[c] = seen.get(c, 0) + 1
    split_count = sum(1 for v in seen.values() if v > 1)
    merge_count = sum(1 for s in spans if len(s) > 1)

    stats = {
        "original_count": len(cues),
        "revised_count": len(revised),
        "issue_count": len(issues),
        "split_cues": split_count,
        "merged_cues": merge_count,
        "moved_boundaries": moved,
        "languages": sorted({i.language for i in issues}),
    }
    return Result(cues=revised, issues=issues, stats=stats, spans=spans,
                  cue_languages=revised_langs)
