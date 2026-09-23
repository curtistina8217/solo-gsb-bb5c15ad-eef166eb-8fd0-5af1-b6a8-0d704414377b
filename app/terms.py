"""术语审校引擎：按语言区分的术语表与跨 cue 不一致检测。

检测维度（均相对于术语表中的规范词）：
- 禁止替代词：用户显式禁止的写法（全半角不敏感、大小写敏感的精确匹配）；
- 大小写差异：与规范词仅大小写不同（webvtt vs WebVTT）；
- 全半角差异：与规范词仅全角/半角不同（ＷｅｂＶＴＴ vs WebVTT）；
- 标点差异：与规范词仅空格/连字符/间隔号等连接标点不同（Web VTT vs WebVTT）；
- 近似拼写：与规范词编辑距离在阈值内的疑似笔误（WebVTs vs WebVTT）。

术语按语言区分：仅当 cue 的检测语言与术语语言一致（或术语为 all 通用）时，
该术语才参与此 cue 的检测。检测在修订后 cue 上进行，finding 记录原文偏移，
用户逐项确认接受后，可在导出时把原文批量替换为规范词。
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

from .subtitles import Cue, detect_language

CATEGORY_LABELS = {
    "forbidden": "禁止替代词",
    "case": "大小写不一致",
    "width": "全半角不一致",
    "punct": "标点不一致",
    "spelling": "近似拼写",
}
STATUS_LABELS = {
    "pending": "待确认",
    "accepted": "已接受",
    "dismissed": "已忽略",
}

# 标点层归一时去除的连接/停顿字符（空白、连字符、间隔号、句点类）
_STRIP_CHARS = set(" \t\r\n\f\v-_.·・•˙‐‑‒–—―")
# 归一文本中的拉丁 token（近似拼写候选）
_LATIN_TOKEN_RE = re.compile(r"[a-z0-9à-ÿ]+")
_LATIN_CHAR_RE = re.compile(r"[A-Za-z0-9À-ÿ]")
# 归一文本中的 CJK / 假名 / 谚文连续段
_CJK_RUN_RE = re.compile(r"[㐀-䶿一-鿿豈-﫿぀-ヿ가-힯]+")


@dataclass
class TermEntry:
    id: int
    language: str               # zh / en / ... / all
    canonical: str              # 规范词
    variants: list[str] = field(default_factory=list)   # 允许变体
    forbidden: list[str] = field(default_factory=list)  # 禁止替代词
    note: str = ""


@dataclass
class Finding:
    term_id: int
    language: str               # 术语语言
    canonical: str
    cue_seq: int                # 修订后 cue 序号（0 基）
    source_cues: list[int]      # 来源原 cue 序号（0 基）
    start_off: int              # 在 cue 文本中的字符偏移
    end_off: int
    matched: str                # 字幕中的实际写法
    suggestion: str             # 建议替换（恒为规范词）
    category: str
    reason: str
    context: str                # 所在 cue 全文


@dataclass
class _CueIndex:
    """一个 cue 文本的多层归一视图，每层都保留到原文的索引映射。"""
    raw: str
    norm_w: str                 # 仅全半角归一（保留大小写）：禁止替代词层
    map_w: list[int]
    norm1: str                  # 全半角 + 大小写归一：规范词/变体层
    map1: list[int]
    norm2: str                  # 再去除连接标点：标点差异与近似拼写层
    map2: list[int]
    latin_tokens: list[tuple[int, int]]
    cjk_runs: list[tuple[int, int]]

    @classmethod
    def build(cls, raw: str) -> "_CueIndex":
        norm_w, map_w = _normalize(raw, casefold=False, strip=False)
        norm1, map1 = _normalize(raw, casefold=True, strip=False)
        norm2, map2 = _normalize(raw, casefold=True, strip=True)
        return cls(
            raw=raw,
            norm_w=norm_w, map_w=map_w,
            norm1=norm1, map1=map1,
            norm2=norm2, map2=map2,
            latin_tokens=[(m.start(), m.end())
                          for m in _LATIN_TOKEN_RE.finditer(norm2)],
            cjk_runs=[(m.start(), m.end())
                      for m in _CJK_RUN_RE.finditer(norm2)],
        )


def _normalize(raw: str, casefold: bool, strip: bool) -> tuple[str, list[int]]:
    """逐字符 NFKC（可选 casefold / 去连接标点），返回归一串与到原文的索引映射。"""
    chars: list[str] = []
    idx_map: list[int] = []
    for i, ch in enumerate(raw):
        n = unicodedata.normalize("NFKC", ch)
        if casefold:
            n = n.casefold()
        for c in n:
            if strip and c in _STRIP_CHARS:
                continue
            chars.append(c)
            idx_map.append(i)
    return "".join(chars), idx_map


def _norm_form(form: str, casefold: bool, strip: bool) -> str:
    return _normalize(form, casefold, strip)[0]


def _lev_leq(a: str, b: str, limit: int) -> int | None:
    """Levenshtein 距离；超过 limit 提前剪枝返回 None。"""
    la, lb = len(a), len(b)
    if abs(la - lb) > limit:
        return None
    prev = list(range(lb + 1))
    for i in range(1, la + 1):
        ai = a[i - 1]
        cur = [i] + [0] * lb
        row_min = i
        for j in range(1, lb + 1):
            v = min(prev[j] + 1, cur[j - 1] + 1,
                    prev[j - 1] + (0 if ai == b[j - 1] else 1))
            cur[j] = v
            if v < row_min:
                row_min = v
        if row_min > limit:
            return None
        prev = cur
    dist = prev[lb]
    return dist if dist <= limit else None


def _fuzzy_limit(length: int) -> int:
    """近似拼写阈值：归一长度 < 4 不做模糊匹配，避免短词大面积误报。"""
    if length < 4:
        return 0
    return 1 if length < 8 else 2


def _pure_extension(a: str, b: str, dist: int) -> bool:
    """差异是否仅为纯前后缀延伸（短词是长词的子串）。

    这类差异通常是复数/派生词（WebVTTX、人工智能化）而非笔误，不告警。
    """
    if dist == 0 or abs(len(a) - len(b)) != dist:
        return False
    short, long_ = (a, b) if len(a) < len(b) else (b, a)
    return short in long_


@dataclass
class _Cand:
    start: int                  # 原文偏移
    end: int
    priority: int               # 小者优先：禁止0 合规占用1 大小写/全半角2 标点3 拼写4
    category: str               # 空串 = 合规占用（规范词原形 / 允许变体）
    matched: str
    reason: str = ""


def _latin_bounded(s: str, i: int, j: int) -> bool:
    """拉丁术语的词边界：两侧不得仍是拉丁字母/数字（相邻 CJK 视为天然边界）。"""
    if i > 0 and _LATIN_CHAR_RE.match(s[i - 1]):
        return False
    if j < len(s) and _LATIN_CHAR_RE.match(s[j]):
        return False
    return True


def _find_all(hay: str, needle: str, latin_bound: bool):
    start = 0
    while needle:
        i = hay.find(needle, start)
        if i < 0:
            return
        j = i + len(needle)
        if not latin_bound or _latin_bounded(hay, i, j):
            yield i, j
        start = i + 1


def _spelling_cand(idx: _CueIndex, s: int, e: int, can: str,
                   dist: int) -> _Cand:
    m = idx.raw[s:e]
    return _Cand(
        s, e, 4, "spelling", m,
        f"「{m}」与规范词「{can}」拼写相近（编辑距离 {dist}），"
        "疑似笔误，建议确认后统一。")


def _match_term(idx: _CueIndex, term: TermEntry) -> list[_Cand]:
    can = term.canonical
    n2_can = _norm_form(can, casefold=True, strip=True)
    latin = bool(n2_can) and bool(_LATIN_TOKEN_RE.fullmatch(n2_can))
    forms = [(can, False)] + [(v, True) for v in term.variants]
    cands: list[_Cand] = []

    # 1) 禁止替代词：全半角归一（保留大小写）层精确匹配
    for form in term.forbidden:
        nw = _norm_form(form, casefold=False, strip=False)
        if not nw:
            continue
        for i, j in _find_all(idx.norm_w, nw, latin):
            s, e = idx.map_w[i], idx.map_w[j - 1] + 1
            m = idx.raw[s:e]
            cands.append(_Cand(
                s, e, 0, "forbidden", m,
                f"「{m}」是术语表中禁止的替代词，应统一为规范词「{can}」。"))

    # 2) 规范词 / 允许变体：全半角 + 大小写归一层
    for form, is_variant in forms:
        n1 = _norm_form(form, casefold=True, strip=False)
        if not n1:
            continue
        for i, j in _find_all(idx.norm1, n1, latin):
            s, e = idx.map1[i], idx.map1[j - 1] + 1
            m = idx.raw[s:e]
            if is_variant or m == can:
                cands.append(_Cand(s, e, 1, "", m))
            elif unicodedata.normalize("NFKC", m) != m:
                cands.append(_Cand(
                    s, e, 2, "width", m,
                    f"「{m}」含全角字符，与规范词「{can}」的全半角不一致，"
                    "建议统一为规范词。"))
            else:
                cands.append(_Cand(
                    s, e, 2, "case", m,
                    f"「{m}」与规范词「{can}」仅大小写不一致，"
                    "建议统一为规范词。"))

    # 3) 标点层：去连接标点后再匹配（归一后过短则跳过，防止误报）
    for form, is_variant in forms:
        n2 = _norm_form(form, casefold=True, strip=True)
        if len(n2) < 2:
            continue
        for i, j in _find_all(idx.norm2, n2, latin):
            s, e = idx.map2[i], idx.map2[j - 1] + 1
            m = idx.raw[s:e]
            if is_variant:
                cands.append(_Cand(s, e, 1, "", m))
            else:
                cands.append(_Cand(
                    s, e, 3, "punct", m,
                    f"「{m}」与规范词「{can}」仅标点/空格不一致，"
                    "建议统一为规范词。"))

    # 4) 近似拼写：仅针对规范词，在去标点层上做编辑距离匹配
    limit = _fuzzy_limit(len(n2_can))
    if limit:
        if latin:
            for ts, te in idx.latin_tokens:
                tok = idx.norm2[ts:te]
                if abs(len(tok) - len(n2_can)) > limit:
                    continue
                if len(set(tok) & set(n2_can)) < len(n2_can) - limit:
                    continue
                d = _lev_leq(tok, n2_can, limit)
                if d and not _pure_extension(tok, n2_can, d):
                    s, e = idx.map2[ts], idx.map2[te - 1] + 1
                    cands.append(_spelling_cand(idx, s, e, can, d))
        else:
            for rs, re_ in idx.cjk_runs:
                run = idx.norm2[rs:re_]
                for wlen in range(max(1, len(n2_can) - limit),
                                  len(n2_can) + limit + 1):
                    for k in range(0, len(run) - wlen + 1):
                        win = run[k:k + wlen]
                        if len(set(win) & set(n2_can)) < len(n2_can) - limit:
                            continue
                        d = _lev_leq(win, n2_can, limit)
                        if d and not _pure_extension(win, n2_can, d):
                            s = idx.map2[rs + k]
                            e = idx.map2[rs + k + wlen - 1] + 1
                            cands.append(_spelling_cand(idx, s, e, can, d))
    return cands


def check_cues(cues: list[Cue], spans: list[list[int]],
               terms: list[TermEntry]) -> list[Finding]:
    """对修订后 cue 列表做跨 cue 术语一致性检测，返回疑似不一致项。"""
    findings: list[Finding] = []
    if not terms:
        return findings

    for ci, cue in enumerate(cues):
        lang = detect_language(cue.text)
        applicable = [t for t in terms
                      if t.language == "all" or t.language == lang]
        if not applicable:
            continue

        idx = _CueIndex.build(cue.text)
        cands: list[tuple[TermEntry, _Cand]] = []
        for t in applicable:
            cands.extend((t, c) for c in _match_term(idx, t))

        # 重叠解决：同一起点上优先级高者胜出，跨度长者优先；不与已选区间重叠
        cands.sort(key=lambda tc: (tc[1].start, tc[1].priority,
                                   -(tc[1].end - tc[1].start)))
        last_end = -1
        for t, c in cands:
            if c.start < last_end:
                continue
            last_end = c.end
            if not c.category:
                continue  # 合规占用（规范词原形 / 允许变体），只用于排除误报
            findings.append(Finding(
                term_id=t.id,
                language=t.language,
                canonical=t.canonical,
                cue_seq=ci,
                source_cues=list(spans[ci]) if ci < len(spans) else [],
                start_off=c.start,
                end_off=c.end,
                matched=c.matched,
                suggestion=t.canonical,
                category=c.category,
                reason=c.reason,
                context=cue.text,
            ))
    return findings
