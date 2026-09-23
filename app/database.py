"""SQLite 持久化层：启动时初始化，保存校验任务、修订结果与术语审校数据。"""
from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager

DB_PATH = os.environ.get("DB_PATH", "/data/subtitles.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS submissions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    filename      TEXT NOT NULL,
    format        TEXT NOT NULL DEFAULT 'vtt',
    max_chars     INTEGER NOT NULL,
    min_dwell     REAL NOT NULL,
    max_cps       REAL NOT NULL,
    issue_count   INTEGER NOT NULL DEFAULT 0,
    stats_json    TEXT NOT NULL DEFAULT '{}',
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS cues (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    submission_id   INTEGER NOT NULL REFERENCES submissions(id) ON DELETE CASCADE,
    seq             INTEGER NOT NULL,
    start_sec       REAL NOT NULL,
    end_sec         REAL NOT NULL,
    text            TEXT NOT NULL,
    source_indices  TEXT NOT NULL DEFAULT '[]',
    language        TEXT NOT NULL DEFAULT 'unknown'
);

CREATE TABLE IF NOT EXISTS issues (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    submission_id   INTEGER NOT NULL REFERENCES submissions(id) ON DELETE CASCADE,
    cue_seq         INTEGER NOT NULL,
    kind            TEXT NOT NULL,
    severity        TEXT NOT NULL,
    language        TEXT NOT NULL DEFAULT 'unknown',
    reason          TEXT NOT NULL,
    segment         TEXT NOT NULL,
    start_sec       REAL NOT NULL,
    end_sec         REAL NOT NULL,
    redistributable INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS artifacts (
    submission_id INTEGER PRIMARY KEY
        REFERENCES submissions(id) ON DELETE CASCADE,
    revised_vtt TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS compilations (
    submission_id INTEGER PRIMARY KEY
        REFERENCES submissions(id) ON DELETE CASCADE,
    windows_json  TEXT NOT NULL DEFAULT '[]',
    updated_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS terms (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    language    TEXT NOT NULL DEFAULT 'all',
    canonical   TEXT NOT NULL,
    variants    TEXT NOT NULL DEFAULT '[]',
    forbidden   TEXT NOT NULL DEFAULT '[]',
    note        TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS term_findings (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    submission_id INTEGER NOT NULL REFERENCES submissions(id) ON DELETE CASCADE,
    term_id       INTEGER NOT NULL DEFAULT 0,
    language      TEXT NOT NULL DEFAULT 'all',
    canonical     TEXT NOT NULL,
    cue_seq       INTEGER NOT NULL,
    source_cues   TEXT NOT NULL DEFAULT '[]',
    start_off     INTEGER NOT NULL,
    end_off       INTEGER NOT NULL,
    matched       TEXT NOT NULL,
    suggestion    TEXT NOT NULL,
    category      TEXT NOT NULL,
    reason        TEXT NOT NULL,
    context       TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'pending'
);

CREATE INDEX IF NOT EXISTS idx_cues_submission ON cues(submission_id);
CREATE INDEX IF NOT EXISTS idx_issues_submission ON issues(submission_id);
CREATE INDEX IF NOT EXISTS idx_issues_language ON issues(submission_id, language);
CREATE INDEX IF NOT EXISTS idx_term_findings_sub ON term_findings(submission_id);
"""


def init_db(path: str | None = None) -> None:
    target = path or DB_PATH
    os.makedirs(os.path.dirname(os.path.abspath(target)), exist_ok=True)
    with get_conn(target) as conn:
        conn.executescript(_SCHEMA)
        _migrate(conn)


def _migrate(conn: sqlite3.Connection) -> None:
    """为旧版数据库补齐后加的列；既有 cue 的语言按文本即时检测回填。"""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(cues)")}
    if "language" not in cols:
        conn.execute(
            "ALTER TABLE cues ADD COLUMN language TEXT NOT NULL DEFAULT 'unknown'"
        )
        rows = conn.execute("SELECT id, text FROM cues").fetchall()
        if rows:
            from .subtitles import detect_language
            conn.executemany(
                "UPDATE cues SET language = ? WHERE id = ?",
                [(detect_language(r["text"]), r["id"]) for r in rows],
            )


@contextmanager
def get_conn(path: str | None = None):
    conn = sqlite3.connect(path or DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def save_submission(filename: str, fmt: str, max_chars: int, min_dwell: float,
                    max_cps: float, result, revised_vtt: str) -> int:
    """持久化一次分析结果，返回 submission id。"""
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO submissions
               (filename, format, max_chars, min_dwell, max_cps,
                issue_count, stats_json)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (filename, fmt, max_chars, min_dwell, max_cps,
             len(result.issues), json.dumps(result.stats, ensure_ascii=False)),
        )
        sid = cur.lastrowid

        conn.executemany(
            """INSERT INTO cues
               (submission_id, seq, start_sec, end_sec, text, source_indices,
                language)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            [(sid, i, c.start, c.end, c.text,
              json.dumps(spans, ensure_ascii=False), lang)
             for i, (c, spans, lang) in enumerate(
                 zip(result.cues, result.spans, result.cue_languages))],
        )
        conn.executemany(
            """INSERT INTO issues
               (submission_id, cue_seq, kind, severity, language, reason,
                segment, start_sec, end_sec, redistributable)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [(sid, iss.index, iss.kind, iss.severity, iss.language, iss.reason,
              iss.segment, iss.start, iss.end, int(iss.redistributable))
             for iss in result.issues],
        )
        conn.execute(
            "INSERT INTO artifacts (submission_id, revised_vtt) VALUES (?, ?)",
            (sid, revised_vtt),
        )
    return sid


def list_submissions(limit: int = 20) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT id, filename, format, max_chars, min_dwell, max_cps,
                      issue_count, stats_json, created_at
               FROM submissions ORDER BY id DESC LIMIT ?""",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_artifact(submission_id: int) -> dict | None:
    with get_conn() as conn:
        sub = conn.execute(
            "SELECT * FROM submissions WHERE id = ?", (submission_id,)
        ).fetchone()
        if sub is None:
            return None
        art = conn.execute(
            "SELECT revised_vtt FROM artifacts WHERE submission_id = ?",
            (submission_id,),
        ).fetchone()
        cues = conn.execute(
            """SELECT seq, start_sec, end_sec, text, source_indices, language
               FROM cues WHERE submission_id = ? ORDER BY seq""",
            (submission_id,),
        ).fetchall()
        issues = conn.execute(
            """SELECT cue_seq, kind, severity, language, reason, segment,
                      start_sec, end_sec, redistributable
               FROM issues WHERE submission_id = ? ORDER BY cue_seq""",
            (submission_id,),
        ).fetchall()
    return {
        "submission": dict(sub),
        "revised_vtt": art["revised_vtt"] if art else "",
        "cues": [dict(r) for r in cues],
        "issues": [dict(r) for r in issues],
    }


def submission_exists(submission_id: int) -> bool:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM submissions WHERE id = ?", (submission_id,)
        ).fetchone()
    return row is not None


# --- 片段合辑窗口配置（按任务保存，可反复编辑覆盖） ---

def save_compilation(submission_id: int,
                     windows: list[tuple[float, float]]) -> None:
    payload = [{"start": s, "end": e} for s, e in windows]
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO compilations (submission_id, windows_json, updated_at)
               VALUES (?, ?, datetime('now'))
               ON CONFLICT(submission_id) DO UPDATE SET
                   windows_json = excluded.windows_json,
                   updated_at = datetime('now')""",
            (submission_id, json.dumps(payload, ensure_ascii=False)),
        )


def get_compilation(submission_id: int) -> list[tuple[float, float]] | None:
    """返回已保存的窗口列表；任务从未配置过返回 None（空集返回 []）。"""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT windows_json FROM compilations WHERE submission_id = ?",
            (submission_id,),
        ).fetchone()
    if row is None:
        return None
    raw = json.loads(row["windows_json"] or "[]")
    return [(float(w["start"]), float(w["end"])) for w in raw]


# --- 术语表 ---

def _term_row(row) -> dict:
    d = dict(row)
    d["variants"] = json.loads(d["variants"] or "[]")
    d["forbidden"] = json.loads(d["forbidden"] or "[]")
    return d


def list_terms(language: str | None = None) -> list[dict]:
    sql = "SELECT * FROM terms"
    args: tuple = ()
    if language:
        sql += " WHERE language IN (?, 'all')"
        args = (language,)
    sql += " ORDER BY language, id"
    with get_conn() as conn:
        rows = conn.execute(sql, args).fetchall()
    return [_term_row(r) for r in rows]


def create_term(language: str, canonical: str, variants: list[str],
                forbidden: list[str], note: str) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO terms (language, canonical, variants, forbidden, note)
               VALUES (?, ?, ?, ?, ?)""",
            (language, canonical, json.dumps(variants, ensure_ascii=False),
             json.dumps(forbidden, ensure_ascii=False), note),
        )
        return cur.lastrowid


def update_term(term_id: int, language: str, canonical: str,
                variants: list[str], forbidden: list[str], note: str) -> bool:
    with get_conn() as conn:
        cur = conn.execute(
            """UPDATE terms SET language = ?, canonical = ?, variants = ?,
                      forbidden = ?, note = ?
               WHERE id = ?""",
            (language, canonical, json.dumps(variants, ensure_ascii=False),
             json.dumps(forbidden, ensure_ascii=False), note, term_id),
        )
        return cur.rowcount > 0


def delete_term(term_id: int) -> bool:
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM terms WHERE id = ?", (term_id,))
        return cur.rowcount > 0


# --- 术语审校发现 ---

def replace_term_findings(submission_id: int, findings) -> None:
    """整体重建某次任务的术语发现（重新检测会重置确认状态）。"""
    with get_conn() as conn:
        conn.execute(
            "DELETE FROM term_findings WHERE submission_id = ?",
            (submission_id,),
        )
        conn.executemany(
            """INSERT INTO term_findings
               (submission_id, term_id, language, canonical, cue_seq,
                source_cues, start_off, end_off, matched, suggestion,
                category, reason, context)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [(submission_id, f.term_id, f.language, f.canonical, f.cue_seq,
              json.dumps(f.source_cues, ensure_ascii=False), f.start_off,
              f.end_off, f.matched, f.suggestion, f.category, f.reason,
              f.context)
             for f in findings],
        )


def list_term_findings(submission_id: int) -> list[dict]:
    """按任务列出术语发现，并联出所在 cue 的时间码。"""
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT f.*, c.start_sec AS cue_start, c.end_sec AS cue_end
               FROM term_findings f
               LEFT JOIN cues c
                 ON c.submission_id = f.submission_id AND c.seq = f.cue_seq
               WHERE f.submission_id = ? ORDER BY f.id""",
            (submission_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def set_finding_status(ids: list[int], status: str) -> int:
    if not ids:
        return 0
    marks = ",".join("?" * len(ids))
    with get_conn() as conn:
        cur = conn.execute(
            f"UPDATE term_findings SET status = ? WHERE id IN ({marks})",
            [status, *ids],
        )
        return cur.rowcount


def accepted_findings(submission_id: int) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT cue_seq, start_off, end_off, matched, suggestion
               FROM term_findings
               WHERE submission_id = ? AND status = 'accepted'
               ORDER BY cue_seq, start_off""",
            (submission_id,),
        ).fetchall()
    return [dict(r) for r in rows]
