"""字幕断句校验台 — FastAPI 入口。"""
from __future__ import annotations

import csv
import io
import json
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import database as db
from . import terms as terms_mod
from . import compilation as comp_mod
from .segmenter import analyze
from .subtitles import (LANGUAGE_NAMES, Cue, format_timestamp, parse_subtitle,
                        write_vtt)

STATIC_DIR = Path(__file__).parent / "static"

# 术语语言：除检测语言外支持「通用」（对全部 cue 生效）
TERM_LANGUAGES = {"all": "通用", **LANGUAGE_NAMES}


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    yield


app = FastAPI(title="字幕断句校验台", version="1.0.0", lifespan=lifespan)


def _issue_dict(iss) -> dict:
    return {
        "cue_seq": iss.index,
        "kind": iss.kind,
        "severity": iss.severity,
        "language": iss.language,
        "language_name": LANGUAGE_NAMES.get(iss.language, iss.language),
        "reason": iss.reason,
        "segment": iss.segment,
        "start": round(iss.start, 3),
        "end": round(iss.end, 3),
        "redistributable": iss.redistributable,
    }


def _result_payload(sid: int, filename: str, fmt: str, params: dict,
                    result) -> dict:
    return {
        "id": sid,
        "filename": filename,
        "format": fmt,
        "params": params,
        "stats": result.stats,
        "cues": [
            {
                "seq": i,
                "start": round(c.start, 3),
                "end": round(c.end, 3),
                "duration": round(c.duration, 3),
                "length": len(c.text),
                "cps": round(len(c.text) / c.duration, 1)
                if c.duration > 0 else None,
                "text": c.text,
                "sources": [s + 1 for s in spans],
                "redistributed": len(spans) > 1,
            }
            for i, (c, spans) in enumerate(zip(result.cues, result.spans))
        ],
        "issues": [_issue_dict(i) for i in result.issues],
        "language_names": LANGUAGE_NAMES,
    }


def _build_notes(issues) -> dict[int, list[str]]:
    """按修订 cue 序号汇总校验问题，生成 write_vtt 需要的 NOTE 标注。"""
    notes: dict[int, list[str]] = {}
    for iss in sorted(issues, key=lambda i: i["cue_seq"]):
        notes.setdefault(iss["cue_seq"], []).append(
            f"[校验问题/{iss['kind']}] {iss['reason']}")
    return notes


# --- 片段合辑 ---

class CompilationIn(BaseModel):
    windows: list[dict]


def _compilation_payload(sid: int,
                         windows: list[tuple[float, float]],
                         record: dict | None = None) -> dict:
    """按窗口配置生成合辑结果：新时间轴上的 cue、来源序号与校验 NOTE。"""
    if record is None:
        record = db.get_artifact(sid)
    cues = [Cue(start=c["start_sec"], end=c["end_sec"], text=c["text"])
            for c in record["cues"]]
    compiled = comp_mod.compile_cues(cues, windows)
    notes_by_origin = _build_notes(record["issues"])
    return {
        "windows": [
            {"start": round(s, 3), "end": round(e, 3), "length": round(e - s, 3)}
            for s, e in windows
        ],
        "cues": [
            {
                "seq": cc.seq,
                "start": round(cc.start, 3),
                "end": round(cc.end, 3),
                "duration": round(cc.end - cc.start, 3),
                "text": cc.text,
                "origin_seq": cc.origin_seq,
                "notes": notes_by_origin.get(cc.origin_seq, []),
            }
            for cc in compiled
        ],
        "timeline_length": round(sum(e - s for s, e in windows), 3),
    }


@app.put("/api/submissions/{sid}/compilation")
async def api_save_compilation(sid: int, payload: CompilationIn):
    """保存（覆盖）该任务的片段合辑窗口配置，并返回最新合辑结果。"""
    if not db.submission_exists(sid):
        raise HTTPException(status_code=404, detail="记录不存在")
    try:
        windows = comp_mod.normalize_windows(payload.windows)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    db.save_compilation(sid, windows)
    return _compilation_payload(sid, windows)


@app.get("/api/submissions/{sid}/compilation")
async def api_get_compilation(sid: int):
    record = db.get_artifact(sid)
    if record is None:
        raise HTTPException(status_code=404, detail="记录不存在")
    windows = db.get_compilation(sid)
    if windows is None:
        raise HTTPException(status_code=404, detail="该任务尚未配置片段合辑")
    return _compilation_payload(sid, windows, record)


@app.get("/api/submissions/{sid}/compilation-export")
async def api_compilation_export(sid: int):
    """导出独立合辑 WebVTT；空窗口集（已保存）返回结构有效的空 WebVTT。"""
    record = db.get_artifact(sid)
    if record is None:
        raise HTTPException(status_code=404, detail="记录不存在")
    windows = db.get_compilation(sid)
    if windows is None:
        raise HTTPException(status_code=404, detail="该任务尚未配置片段合辑")

    payload = _compilation_payload(sid, windows, record)
    out_cues = [Cue(start=c["start"], end=c["end"], text=c["text"])
                for c in payload["cues"]]
    notes = {i: list(c["notes"]) for i, c in enumerate(payload["cues"])}
    vtt = write_vtt(out_cues, notes=notes)

    base = record["submission"]["filename"].rsplit(".", 1)[0]
    filename = f"{base}.compilation.vtt"
    return Response(
        content=vtt,
        media_type="text/vtt; charset=utf-8",
        headers={"Content-Disposition":
                 f'attachment; filename="{filename}"'},
    )


@app.post("/api/analyze")
async def api_analyze(
    file: UploadFile = File(...),
    max_chars: int = Form(18),
    min_dwell: float = Form(0.8),
    max_cps: float = Form(12.0),
):
    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="上传文件为空")
    try:
        content = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            content = raw.decode("latin-1")
        except UnicodeDecodeError:
            raise HTTPException(status_code=400,
                                detail="无法识别文件编码，请使用 UTF-8")

    name = file.filename or "subtitles"
    fmt = "srt" if name.lower().endswith(".srt") else "vtt"

    try:
        cues = parse_subtitle(content)
        result = analyze(cues, max_chars=max_chars, min_dwell=min_dwell,
                         max_cps=max_cps)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    notes = _build_notes([
        {"cue_seq": iss.index, "kind": iss.kind, "reason": iss.reason}
        for iss in result.issues
    ])
    revised_vtt = write_vtt(result.cues, notes=notes)
    sid = db.save_submission(name, fmt, max_chars, min_dwell, max_cps,
                             result, revised_vtt)

    # 术语审校：术语表非空时，对修订后 cue 做跨 cue 一致性检测
    term_entries = _term_entries()
    term_findings = terms_mod.check_cues(result.cues, result.spans,
                                         term_entries)
    if term_findings:
        db.replace_term_findings(sid, term_findings)

    payload = _result_payload(sid, name, fmt,
                              {"max_chars": max_chars, "min_dwell": min_dwell,
                               "max_cps": max_cps}, result)
    payload["term_findings"] = len(term_findings)
    return payload


@app.get("/api/submissions")
async def api_submissions():
    items = db.list_submissions()
    for it in items:
        it["stats"] = json.loads(it.get("stats_json") or "{}")
        it.pop("stats_json", None)
    return {"items": items, "language_names": LANGUAGE_NAMES}


@app.get("/api/submissions/{sid}")
async def api_get_submission(sid: int, language: str | None = None):
    record = db.get_artifact(sid)
    if record is None:
        raise HTTPException(status_code=404, detail="记录不存在")
    sub = record["submission"]
    issues = record["issues"]
    if language:
        issues = [i for i in issues if i["language"] == language]
    payload = {
        "id": sub["id"],
        "filename": sub["filename"],
        "format": sub["format"],
        "params": {
            "max_chars": sub["max_chars"],
            "min_dwell": sub["min_dwell"],
            "max_cps": sub["max_cps"],
        },
        "stats": json.loads(sub["stats_json"] or "{}"),
        "cues": [
            {
                "seq": c["seq"],
                "start": round(c["start_sec"], 3),
                "end": round(c["end_sec"], 3),
                "duration": round(c["end_sec"] - c["start_sec"], 3),
                "length": len(c["text"]),
                "cps": round(len(c["text"])
                             / max(1e-9, c["end_sec"] - c["start_sec"]), 1),
                "text": c["text"],
                "sources": [s + 1 for s in json.loads(c["source_indices"])],
                "redistributed": len(json.loads(c["source_indices"])) > 1,
            }
            for c in record["cues"]
        ],
        "issues": [
            {
                "cue_seq": i["cue_seq"],
                "kind": i["kind"],
                "severity": i["severity"],
                "language": i["language"],
                "language_name": LANGUAGE_NAMES.get(i["language"],
                                                    i["language"]),
                "reason": i["reason"],
                "segment": i["segment"],
                "start": round(i["start_sec"], 3),
                "end": round(i["end_sec"], 3),
                "redistributable": bool(i["redistributable"]),
            }
            for i in issues
        ],
        "language_names": LANGUAGE_NAMES,
    }
    return payload


@app.get("/api/submissions/{sid}/export")
async def api_export(sid: int, language: str | None = None):
    record = db.get_artifact(sid)
    if record is None:
        raise HTTPException(status_code=404, detail="记录不存在")

    base = record["submission"]["filename"].rsplit(".", 1)[0]
    filename = f"{base}.revised.vtt"

    if not language:
        # 未选择语言：导出完整修订 WebVTT（沿用分析时持久化的内容）
        vtt = record["revised_vtt"]
    else:
        # 结果页选择了语言：只输出检测语言匹配的修订 cue，保持原顺序、
        # 时间码与文本，并仅重建这些 cue 对应的校验 NOTE 标注
        cue_rows = [c for c in record["cues"] if c["language"] == language]
        cues = [Cue(start=c["start_sec"], end=c["end_sec"], text=c["text"])
                for c in cue_rows]
        all_notes = _build_notes(record["issues"])
        notes = {
            pos: all_notes.get(c["seq"], [])
            for pos, c in enumerate(cue_rows)
        }
        vtt = write_vtt(cues, notes=notes)

    return Response(
        content=vtt,
        media_type="text/vtt; charset=utf-8",
        headers={"Content-Disposition":
                 f'attachment; filename="{filename}"'},
    )


# --- 术语审校 ---

class TermIn(BaseModel):
    language: str = "all"
    canonical: str
    variants: list[str] = []
    forbidden: list[str] = []
    note: str = ""


class FindingStatusIn(BaseModel):
    ids: list[int]
    status: str


def _term_entries() -> list[terms_mod.TermEntry]:
    return [terms_mod.TermEntry(
        id=t["id"], language=t["language"], canonical=t["canonical"],
        variants=t["variants"], forbidden=t["forbidden"], note=t["note"])
        for t in db.list_terms()]


def _clean_str_list(values: list[str], label: str) -> list[str]:
    out: list[str] = []
    for v in values:
        v = (v or "").strip()
        if not v:
            continue
        if len(v) > 100:
            raise HTTPException(status_code=400,
                                detail=f"{label}「{v[:20]}…」超过 100 字符上限")
        if v not in out:
            out.append(v)
    if len(out) > 50:
        raise HTTPException(status_code=400,
                            detail=f"{label}数量过多（上限 50 条）")
    return out


def _validate_term(payload: TermIn) -> dict:
    language = (payload.language or "all").strip()
    if language not in TERM_LANGUAGES:
        raise HTTPException(status_code=400,
                            detail=f"未知术语语言：{language}")
    canonical = payload.canonical.strip()
    if not canonical:
        raise HTTPException(status_code=400, detail="规范词不能为空")
    if len(canonical) > 100:
        raise HTTPException(status_code=400, detail="规范词超过 100 字符上限")
    if not any(ch.isalnum() for ch in canonical):
        raise HTTPException(status_code=400,
                            detail="规范词需至少包含一个字母或文字")
    variants = [v for v in _clean_str_list(payload.variants, "允许变体")
                if v != canonical]
    forbidden = _clean_str_list(payload.forbidden, "禁止替代词")
    if canonical in forbidden:
        raise HTTPException(status_code=400,
                            detail="禁止替代词不能与规范词相同")
    conflict = set(variants) & set(forbidden)
    if conflict:
        raise HTTPException(
            status_code=400,
            detail=f"「{'、'.join(sorted(conflict))}」同时出现在允许变体与"
                   "禁止替代词中")
    return {
        "language": language,
        "canonical": canonical,
        "variants": variants,
        "forbidden": forbidden,
        "note": payload.note.strip()[:200],
    }


def _finding_dict(row: dict) -> dict:
    return {
        "id": row["id"],
        "term_id": row["term_id"],
        "language": row["language"],
        "language_name": TERM_LANGUAGES.get(row["language"], row["language"]),
        "canonical": row["canonical"],
        "cue_seq": row["cue_seq"],
        "source_cues": [s + 1 for s in json.loads(row["source_cues"] or "[]")],
        "start_off": row["start_off"],
        "end_off": row["end_off"],
        "matched": row["matched"],
        "suggestion": row["suggestion"],
        "category": row["category"],
        "category_label": terms_mod.CATEGORY_LABELS.get(row["category"],
                                                        row["category"]),
        "reason": row["reason"],
        "context": row["context"],
        "cue_start": row.get("cue_start"),
        "cue_end": row.get("cue_end"),
        "status": row["status"],
        "status_label": terms_mod.STATUS_LABELS.get(row["status"],
                                                    row["status"]),
    }


@app.get("/api/terms")
async def api_list_terms(language: str | None = None):
    return {"items": db.list_terms(language), "languages": TERM_LANGUAGES}


@app.post("/api/terms", status_code=201)
async def api_create_term(payload: TermIn):
    data = _validate_term(payload)
    tid = db.create_term(**data)
    return {"id": tid}


@app.put("/api/terms/{tid}")
async def api_update_term(tid: int, payload: TermIn):
    data = _validate_term(payload)
    if not db.update_term(tid, **data):
        raise HTTPException(status_code=404, detail="术语不存在")
    return {"ok": True}


@app.delete("/api/terms/{tid}")
async def api_delete_term(tid: int):
    if not db.delete_term(tid):
        raise HTTPException(status_code=404, detail="术语不存在")
    return {"ok": True}


@app.get("/api/submissions/{sid}/term-findings")
async def api_term_findings(sid: int):
    if not db.submission_exists(sid):
        raise HTTPException(status_code=404, detail="记录不存在")
    return {"items": [_finding_dict(r) for r in db.list_term_findings(sid)]}


@app.post("/api/submissions/{sid}/term-check")
async def api_term_check(sid: int):
    """按当前术语表重新检测（会重置全部确认为「待确认」）。"""
    record = db.get_artifact(sid)
    if record is None:
        raise HTTPException(status_code=404, detail="记录不存在")
    cues = [Cue(start=c["start_sec"], end=c["end_sec"], text=c["text"])
            for c in record["cues"]]
    spans = [json.loads(c["source_indices"]) for c in record["cues"]]
    findings = terms_mod.check_cues(cues, spans, _term_entries())
    db.replace_term_findings(sid, findings)
    return {"items": [_finding_dict(r) for r in db.list_term_findings(sid)]}


@app.post("/api/terms/findings/status")
async def api_finding_status(payload: FindingStatusIn):
    if payload.status not in terms_mod.STATUS_LABELS:
        raise HTTPException(status_code=400,
                            detail="status 须为 pending / accepted / dismissed")
    if not payload.ids:
        raise HTTPException(status_code=400, detail="ids 不能为空")
    updated = db.set_finding_status(payload.ids, payload.status)
    return {"updated": updated}


@app.get("/api/submissions/{sid}/term-export")
async def api_term_export(sid: int):
    """把已接受的术语替换批量应用到修订后 cue，导出 WebVTT。"""
    record = db.get_artifact(sid)
    if record is None:
        raise HTTPException(status_code=404, detail="记录不存在")

    cue_rows = record["cues"]
    cues = {c["seq"]: Cue(start=c["start_sec"], end=c["end_sec"],
                          text=c["text"]) for c in cue_rows}
    by_cue: dict[int, list[dict]] = {}
    for f in db.accepted_findings(sid):
        by_cue.setdefault(f["cue_seq"], []).append(f)

    applied = 0
    for seq, items in by_cue.items():
        cue = cues.get(seq)
        if cue is None:
            continue
        text = cue.text
        # 同 cue 多处替换按偏移从后往前应用，保持偏移有效
        for f in sorted(items, key=lambda x: x["start_off"], reverse=True):
            s, e = f["start_off"], f["end_off"]
            if text[s:e] != f["matched"]:
                continue  # 防御：发现快照与当前文本不一致时跳过
            text = text[:s] + f["suggestion"] + text[e:]
            applied += 1
        cues[seq] = Cue(start=cue.start, end=cue.end, text=text)

    vtt = write_vtt([cues[c["seq"]] for c in cue_rows])
    base = record["submission"]["filename"].rsplit(".", 1)[0]
    filename = f"{base}.terms.vtt"
    return Response(
        content=vtt,
        media_type="text/vtt; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"',
                 "X-Term-Replacements": str(applied)},
    )


@app.get("/api/submissions/{sid}/term-report")
async def api_term_report(sid: int, format: str = "csv"):
    """导出术语审校报告（默认 CSV，?format=json 返回 JSON）。"""
    record = db.get_artifact(sid)
    if record is None:
        raise HTTPException(status_code=404, detail="记录不存在")
    rows = db.list_term_findings(sid)
    base = record["submission"]["filename"].rsplit(".", 1)[0]

    if format == "json":
        return {"items": [_finding_dict(r) for r in rows]}

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["规范词", "术语语言", "类别", "发现文本", "建议替换",
                     "修订cue序号", "来源cue序号", "cue开始", "cue结束",
                     "确认状态", "原因", "上下文"])
    for r in rows:
        writer.writerow([
            r["canonical"],
            TERM_LANGUAGES.get(r["language"], r["language"]),
            terms_mod.CATEGORY_LABELS.get(r["category"], r["category"]),
            r["matched"],
            r["suggestion"],
            r["cue_seq"] + 1,
            "+".join(str(s + 1)
                     for s in json.loads(r["source_cues"] or "[]")),
            format_timestamp(r["cue_start"] or 0),
            format_timestamp(r["cue_end"] or 0),
            terms_mod.STATUS_LABELS.get(r["status"], r["status"]),
            r["reason"],
            r["context"].replace("\n", " / "),
        ])
    filename = f"{base}.term-report.csv"
    return Response(
        content="﻿" + buf.getvalue(),  # BOM，便于 Excel 直接打开
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/api/health")
async def health():
    return {"status": "ok"}


app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
