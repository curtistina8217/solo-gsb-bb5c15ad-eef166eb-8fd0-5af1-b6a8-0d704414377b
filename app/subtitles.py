"""WebVTT / SRT 解析、WebVTT 导出与语言检测。"""
from __future__ import annotations

import re
from dataclasses import dataclass

_TS_RE = re.compile(r"(?:(\d{1,3}):)?(\d{1,2}):(\d{2})[.,](\d{1,3})")
_TAG_RE = re.compile(r"<[^>]+>")


@dataclass
class Cue:
    start: float
    end: float
    text: str

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)


def parse_timestamp(value: str) -> float | None:
    """把 HH:MM:SS.mmm / MM:SS.mmm / SRT 逗号格式解析为秒。"""
    m = _TS_RE.search(value)
    if not m:
        return None
    hours = int(m.group(1) or 0)
    minutes = int(m.group(2))
    seconds = int(m.group(3))
    frac = m.group(4).ljust(3, "0")
    return hours * 3600 + minutes * 60 + seconds + int(frac) / 1000.0


def format_timestamp(seconds: float) -> str:
    """格式化为 WebVTT 时间码 HH:MM:SS.mmm。"""
    if seconds < 0:
        seconds = 0.0
    total_ms = int(round(seconds * 1000))
    hours, rem = divmod(total_ms, 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    secs, ms = divmod(rem, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}.{ms:03d}"


def _clean_text(lines: list[str]) -> str:
    cleaned = []
    for line in lines:
        line = _TAG_RE.sub("", line).strip()
        if line:
            cleaned.append(line)
    return "\n".join(cleaned)


def _parse_timing(line: str) -> tuple[float, float] | None:
    if "-->" not in line:
        return None
    left, right = line.split("-->", 1)
    start = parse_timestamp(left)
    end = parse_timestamp(right.strip().split()[0] if right.strip() else "")
    if start is None or end is None:
        return None
    return start, end


def parse_subtitle(content: str) -> list[Cue]:
    """解析 WebVTT 或 SRT 内容，返回 cue 列表。解析失败抛出 ValueError。"""
    if not content or not content.strip():
        raise ValueError("文件内容为空")

    text = content.lstrip("﻿").replace("\r\n", "\n").replace("\r", "\n")
    is_vtt = text.lstrip().upper().startswith("WEBVTT")

    cues: list[Cue] = []
    for block in re.split(r"\n[ \t]*\n", text.strip("\n")):
        lines = [ln for ln in block.split("\n") if ln.strip() != ""]
        if not lines:
            continue

        timing_idx = next((i for i, ln in enumerate(lines) if "-->" in ln), -1)
        if timing_idx < 0:
            continue

        head = lines[0].upper()
        if is_vtt and (head.startswith("NOTE") or head.startswith("STYLE")
                       or head.startswith("REGION")):
            continue

        timing = _parse_timing(lines[timing_idx])
        if timing is None:
            continue
        start, end = timing
        if end < start:
            end = start

        body = _clean_text(lines[timing_idx + 1:])
        if not body:
            continue
        cues.append(Cue(start=start, end=end, text=body))

    if not cues:
        raise ValueError("未能从文件中解析出任何字幕条目（请确认是 WebVTT 或 SRT 格式）")
    return cues


def write_vtt(cues: list[Cue], notes: dict[int, list[str]] | None = None) -> str:
    """把 cue 列表导出为 WebVTT 文本。notes 可在指定 cue 前插入 NOTE 说明。"""
    notes = notes or {}
    out = ["WEBVTT", ""]
    for idx, cue in enumerate(cues):
        for note in notes.get(idx, []):
            for line in note.splitlines():
                out.append(f"NOTE {line}")
            out.append("")  # NOTE 块必须以空行结束
        out.append(f"{format_timestamp(cue.start)} --> {format_timestamp(cue.end)}")
        out.extend(cue.text.splitlines() or [""])
        out.append("")
    return "\n".join(out).rstrip("\n") + "\n"


# --- 语言检测（基于文字系统与常用词的轻量启发式） ---

_HIRAGANA = range(0x3040, 0x309F + 1)
_KATAKANA = range(0x30A0, 0x30FF + 1)
_HANGUL = range(0xAC00, 0xD7AF + 1)
_CJK = (range(0x3400, 0x4DBF + 1), range(0x4E00, 0x9FFF + 1),
        range(0xF900, 0xFAFF + 1))

_LANG_HINTS = {
    "en": {"the", "and", "you", "that", "with", "for", "are", "this",
           "is", "it", "not", "but", "have", "was", "will"},
    "es": {"que", "los", "las", "una", "para", "como", "pero", "está"},
    "fr": {"les", "que", "dans", "pour", "une", "nous", "vous", "c'est"},
    "de": {"der", "die", "und", "nicht", "ist", "ein", "auch", "sich"},
}


def _is_cjk(ch: str) -> bool:
    o = ord(ch)
    return any(o in r for r in _CJK)


def detect_language(text: str) -> str:
    ja = ko = cjk = latin = 0
    for ch in text:
        o = ord(ch)
        if o in _HIRAGANA or o in _KATAKANA:
            ja += 1
        elif o in _HANGUL:
            ko += 1
        elif _is_cjk(ch):
            cjk += 1
        elif "A" <= ch.upper() <= "Z" or 0x00C0 <= o <= 0x024F:
            latin += 1

    if ja and ja >= cjk:
        return "ja"
    if ko and ko > cjk:
        return "ko"
    if cjk and cjk >= latin:
        return "zh"
    if latin:
        words = set(re.findall(r"[a-zÀ-ÿ']+", text.lower()))
        best, best_hits = "other", 0
        for lang, hints in _LANG_HINTS.items():
            hits = len(words & hints)
            if hits > best_hits:
                best, best_hits = lang, hits
        return best if best_hits else "other"
    return "unknown"


LANGUAGE_NAMES = {
    "zh": "中文",
    "en": "英语",
    "ja": "日语",
    "ko": "韩语",
    "es": "西班牙语",
    "fr": "法语",
    "de": "德语",
    "other": "其他拉丁字母",
    "unknown": "未知",
}
