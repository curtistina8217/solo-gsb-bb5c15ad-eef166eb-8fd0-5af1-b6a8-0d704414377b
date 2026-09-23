"""端到端冒烟测试：

    DB_PATH=/tmp/test.db python3 tests/smoke_test.py

需要 fastapi / httpx（见 requirements.txt）。
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("DB_PATH", os.path.join(tempfile.mkdtemp(), "test.db"))

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app.subtitles import Cue, format_timestamp  # noqa: E402

VTT = (
    "WEBVTT\n\n"
    "00:00:01.000 --> 00:00:01.300\n这是一个非常非常长的句子根本来不及读完\n\n"
    "00:00:01.300 --> 00:00:01.500\n后面紧接着另一句内容同样也是超长的呢\n"
)
SRT = (
    "1\n00:00:01,000 --> 00:00:01,200\n"
    "This is the longest sentence and you will not have time\n\n"
    "2\n00:00:01,200 --> 00:00:01,400\n另一句中文也同样非常拥挤根本读不完呢\n"
)


def _has_cjk(text: str) -> bool:
    return any("一" <= ch <= "鿿" for ch in text)


def _parse_vtt_blocks(vtt: str):
    """把导出的 WebVTT 拆成 (NOTE 行列表, 时间码, 文本) 形式的 cue 块。"""
    assert vtt.startswith("WEBVTT")
    blocks, pending_notes = [], []
    for block in vtt.rstrip("\n").split("\n\n")[1:]:
        lines = block.split("\n")
        if lines[0].startswith("NOTE"):
            pending_notes.append(lines[0][len("NOTE "):])
            continue
        timing = next((ln for ln in lines if "-->" in ln), None)
        assert timing is not None, f"非 NOTE 块缺少时间码：{block!r}"
        start, end = [t.strip() for t in timing.split("-->")]
        text = "\n".join(ln for ln in lines if ln != timing)
        blocks.append((pending_notes, start, end, text))
        pending_notes = []
    assert not pending_notes, "存在未附着到 cue 的尾部 NOTE"
    return blocks


def main() -> None:
    with TestClient(app) as client:
        assert client.get("/").status_code == 200
        assert client.get("/api/health").json() == {"status": "ok"}

        data = client.post(
            "/api/analyze",
            files={"file": ("demo.vtt", VTT, "text/vtt")},
            data={"max_chars": "10", "min_dwell": "0.8", "max_cps": "12"},
        ).json()
        assert all(len(c["text"]) <= 10 for c in data["cues"])
        assert data["stats"]["moved_boundaries"] >= 0
        assert data["stats"]["split_cues"] == 2
        assert {i["language"] for i in data["issues"]} == {"zh"}
        assert data["term_findings"] == 0  # 术语表为空时不产生术语发现
        sid = data["id"]

        mixed = client.post(
            "/api/analyze",
            files={"file": ("demo.srt", SRT, "text/plain")},
            data={"max_chars": "12", "min_dwell": "1", "max_cps": "8"},
        ).json()
        assert {"zh", "en"} <= {i["language"] for i in mixed["issues"]}

        # 重叠 cue：切点应落在重叠区间内，统计为一次边界重分配
        overlap_vtt = (
            "WEBVTT\n\n"
            "00:00:01.000 --> 00:00:03.000\n第一句内容比较长需要拆开显示才行的啊\n\n"
            "00:00:02.000 --> 00:00:04.000\n第二句内容也是比较长需要拆开显示啊\n"
        )
        ov = client.post(
            "/api/analyze",
            files={"file": ("ov.vtt", overlap_vtt, "text/vtt")},
            data={"max_chars": "10", "min_dwell": "0.8", "max_cps": "12"},
        ).json()
        assert ov["stats"]["moved_boundaries"] == 1
        for a, b in zip(ov["cues"], ov["cues"][1:]):
            if a["sources"] != b["sources"]:
                assert 2.0 <= a["end"] <= 3.0

        bad = client.post(
            "/api/analyze",
            files={"file": ("x.vtt", b"not a subtitle", "text/vtt")},
            data={"max_chars": "10", "min_dwell": "0.8", "max_cps": "12"},
        )
        assert bad.status_code == 400

        assert len(client.get("/api/submissions").json()["items"]) == 3
        detail = client.get(f"/api/submissions/{sid}").json()
        assert len(detail["cues"]) == len(data["cues"])
        zh = client.get(f"/api/submissions/{sid}?language=zh").json()
        assert all(i["language"] == "zh" for i in zh["issues"])

        export = client.get(f"/api/submissions/{sid}/export")
        assert export.status_code == 200
        assert export.text.startswith("WEBVTT\n")
        assert export.text.count("-->") == len(data["cues"])

        # --- 导出接口 language 参数回归 ---
        # 中文 cue 时间过紧会产生问题；英文 cue 时长充足且不会被切分
        mixed_vtt = (
            "WEBVTT\n\n"
            "00:00:01.000 --> 00:00:01.200\n这是一个非常非常长的句子根本来不及读完\n\n"
            "00:00:01.200 --> 00:00:01.400\n后面紧接着另一句内容也是超长的呢\n\n"
            "00:00:03.000 --> 00:00:07.000\nThis is a calm English cue with enough time.\n"
        )
        mx = client.post(
            "/api/analyze",
            files={"file": ("mixed.vtt", mixed_vtt, "text/vtt")},
            data={"max_chars": "50", "min_dwell": "0.8", "max_cps": "12"},
        ).json()
        mxid = mx["id"]

        # 未选择语言：完整导出，与分析时持久化的修订 VTT 一致
        full = client.get(f"/api/submissions/{mxid}/export")
        assert full.text.count("-->") == len(mx["cues"])
        zh_cues = [c for c in mx["cues"] if _has_cjk(c["text"])]
        en_cues = [c for c in mx["cues"] if not _has_cjk(c["text"])]
        zh_issues = [i for i in mx["issues"] if i["language"] == "zh"]
        en_issues = [i for i in mx["issues"] if i["language"] == "en"]
        assert zh_cues and en_cues and zh_issues and not en_issues

        # 选择中文：只输出中文修订 cue，保持顺序、时间码与文本，
        # 且仅重建这些 cue 对应的 NOTE 标注
        zh_resp = client.get(f"/api/submissions/{mxid}/export?language=zh")
        assert zh_resp.status_code == 200
        assert "attachment" in zh_resp.headers["content-disposition"]
        zh_blocks = _parse_vtt_blocks(zh_resp.text)
        assert len(zh_blocks) == len(zh_cues)
        for block, cue in zip(zh_blocks, zh_cues):
            notes, start, end, text = block
            assert _has_cjk(text)
            assert text == cue["text"]
            assert start == format_timestamp(cue["start"])
            assert end == format_timestamp(cue["end"])
        note_count = sum(len(b[0]) for b in zh_blocks)
        assert note_count == len(zh_issues)
        for notes, _start, _end, text in zh_blocks:
            for note in notes:
                assert note.startswith("[校验问题/")
                assert text in {i["segment"] for i in zh_issues}
        # 顺序与时间码单调不减，文本中不混入英文 cue
        starts = [b[1] for b in zh_blocks]
        assert starts == sorted(starts)
        assert all("calm english" not in b[3].lower() for b in zh_blocks)

        # 选择英语：无问题 cue 也应原样导出，且没有任何 NOTE 标注
        en_resp = client.get(f"/api/submissions/{mxid}/export?language=en")
        en_blocks = _parse_vtt_blocks(en_resp.text)
        assert len(en_blocks) == len(en_cues)
        assert sum(len(b[0]) for b in en_blocks) == 0
        assert en_blocks[0][3] == "This is a calm English cue with enough time."
        assert en_blocks[0][1] == format_timestamp(en_cues[0]["start"])
        assert en_blocks[0][2] == format_timestamp(en_cues[0]["end"])

        # 选择无匹配 cue 的语言：仍返回结构有效的空 WebVTT
        fr_resp = client.get(f"/api/submissions/{mxid}/export?language=fr")
        assert fr_resp.status_code == 200
        fr_blocks = _parse_vtt_blocks(fr_resp.text)
        assert fr_blocks == []
        assert fr_resp.text.strip() == "WEBVTT"

        # --- 片段合辑 ---
        # 说明：修订会把相邻 cue 间静音间隙的中点并入两侧修订 cue 的时间码，
        # 因此合辑期望值基于修订 cue 的实际起止时间推算。
        comp_vtt = (
            "WEBVTT\n\n"
            "00:00:00.000 --> 00:00:02.000\n第一条字幕内容\n\n"
            "00:00:03.000 --> 00:00:05.000\n第二条字幕内容宽松阈值下不会被切分\n\n"
            "00:00:08.000 --> 00:00:12.000\n第三条跨越后续窗口边界的字幕\n"
        )
        cp = client.post(
            "/api/analyze",
            files={"file": ("comp.vtt", comp_vtt, "text/vtt")},
            data={"max_chars": "80", "min_dwell": "0.5", "max_cps": "30"},
        ).json()
        cpid = cp["id"]
        # 宽松阈值下不切分，修订 cue 与输入一一对应
        assert len(cp["cues"]) == 3
        src = {c["seq"]: c for c in cp["cues"]}

        # 未配置过：GET 返回空窗口集
        assert client.get(
            f"/api/submissions/{cpid}/compilation").json() == {"windows": []}

        # 窗口按用户顺序（先 8s 后 0s）：基准按窗口长度累加，
        # 窗内静音保留，窗间间隙丢弃
        resp = client.put(f"/api/submissions/{cpid}/compilation", json={
            "windows": [
                {"start": 8, "end": 13},   # 窗1，长 5s
                {"start": 0, "end": 6},    # 窗2，长 6s（与窗1相切）
            ],
        })
        assert resp.status_code == 200, resp.text
        prev = resp.json()
        assert prev["window_offsets"] == [0.0, 5.0]
        assert prev["total_duration"] == 11.0

        expected = []
        for wi, (ws, we) in enumerate([(8, 13), (0, 6)]):
            base_off = 0.0 if wi == 0 else 5.0
            last_end = None
            for seq, cue in sorted(src.items()):
                s, e = max(cue["start"], ws), min(cue["end"], we)
                if last_end is not None and s < last_end:
                    s = last_end
                if e > s:
                    expected.append((
                        round(base_off + s - ws, 3),
                        round(base_off + e - ws, 3),
                        seq, wi + 1))
                    last_end = e
        cc = prev["cues"]
        assert [(c["start"], c["end"], c["source_seq"], c["window_index"])
                for c in cc] == expected
        assert all(c["text"] == src[c["source_seq"]]["text"] for c in cc)
        assert all(c["source_cue"] == c["source_seq"] + 1 for c in cc)
        # 顺序与合辑时间码单调不减
        starts = [c["start"] for c in cc]
        assert starts == sorted(starts)
        for a, b in zip(cc, cc[1:]):
            assert b["start"] >= a["end"]

        # 合辑核心逻辑（合成 cue 直接验证）：窗内静音保留、窗间丢弃、跨窗拆分
        from app.compilation import compile_cues as _cc, normalize_windows
        syn = [Cue(0, 2, "甲"), Cue(5, 8, "乙"), Cue(9, 9, "零时长")]
        one = _cc(syn, normalize_windows([{"start": 0, "end": 10}]))
        # 甲 0→2、乙 5→8：中间 3s 静音间隙原样保留；零时长 cue 不生成
        assert [(c.start, c.end, c.text) for c in one] == [
            (0.0, 2.0, "甲"), (5.0, 8.0, "乙")]
        two = _cc(syn, normalize_windows(
            [{"start": 0, "end": 2}, {"start": 5, "end": 8}]))
        # 两段拼接：窗间间隙丢弃，乙紧随甲之后
        assert [(c.start, c.end, c.text) for c in two] == [
            (0.0, 2.0, "甲"), (2.0, 5.0, "乙")]
        cross = _cc([Cue(4, 6, "跨窗")], normalize_windows(
            [{"start": 0, "end": 5}, {"start": 5, "end": 10}]))
        assert [(c.start, c.end, c.text) for c in cross] == [
            (4.0, 5.0, "跨窗"), (5.0, 6.0, "跨窗")]
        # cue 仅在闭端与窗口相接：半开交集为空，不生成零时长 cue
        edge = _cc([Cue(8, 10, "边")], normalize_windows(
            [{"start": 0, "end": 8}]))
        assert edge == []
        # 裁后不足半毫秒、落到同一毫秒刻度的片段不生成 cue
        tinycue = _cc([Cue(0, 0.0003, "短")], normalize_windows(
            [{"start": 0, "end": 1}]))
        assert tinycue == []
        for bad in ([{"start": -1, "end": 1}],
                    [{"start": 2, "end": 2}],
                    [{"start": 0, "end": 3}, {"start": 2, "end": 5}]):
            try:
                normalize_windows(bad)
                raise AssertionError(f"非法窗口未被拒绝：{bad}")
            except ValueError:
                pass
        # 相切窗口合法
        normalize_windows([{"start": 0, "end": 3}, {"start": 3, "end": 5}])

        # 跨窗口 cue 拆成多条：cue2 同时落在 [6,10) 与 [10,12)，
        # 文本相同、合辑时间连续；在切点 10 不产生零时长 cue。
        # cue1 修订末端的毫秒余量也进入窗1，因此先筛出 cue2 的两段。
        split = client.put(f"/api/submissions/{cpid}/compilation", json={
            "windows": [{"start": 6, "end": 10}, {"start": 10, "end": 12}],
        }).json()
        all_split = split["cues"]
        # cue1 的窗内尾部排在 cue2 第一段之前，时间码与 cue2 起点不重叠
        tail = [c for c in all_split if c["source_cue"] == 2]
        assert len(tail) == 1
        split_cues = [c for c in all_split if c["source_cue"] == 3]
        assert len(split_cues) == 2
        assert split_cues[0]["text"] == split_cues[1]["text"] == src[2]["text"]
        assert (split_cues[0]["start"], split_cues[0]["end"]) == (
            tail[0]["end"], 4.0)
        assert split_cues[1]["start"] == split_cues[0]["end"]
        assert (split_cues[1]["start"], split_cues[1]["end"]) == (
            4.0, 6.0)
        assert tail[0]["end"] <= split_cues[0]["start"]
        # 整个合辑时间码单调不减
        for a, b in zip(all_split, all_split[1:]):
            assert a["end"] <= b["start"]

        # 半开窗：落在最后一条 cue 之后的窗口与任何 cue 无交集 → 空合辑
        after = client.put(f"/api/submissions/{cpid}/compilation", json={
            "windows": [{"start": src[2]["end"] + 1,
                         "end": src[2]["end"] + 2}],
        }).json()
        assert after["cue_count"] == 0

        # 校验：负起点 / 终点不大于起点 / 重叠窗口 → 400
        bad_payloads = [
            {"windows": [{"start": -1, "end": 2}]},
            {"windows": [{"start": 3, "end": 3}]},
            {"windows": [{"start": 0, "end": 5}, {"start": 4, "end": 9}]},
        ]
        for body in bad_payloads:
            r = client.put(f"/api/submissions/{cpid}/compilation", json=body)
            assert r.status_code == 400, body

        # 空窗口集：合法保存，导出结构有效的空 WebVTT
        r = client.put(f"/api/submissions/{cpid}/compilation",
                       json={"windows": []})
        assert r.status_code == 200
        assert r.json()["cue_count"] == 0
        empty_exp = client.get(f"/api/submissions/{cpid}/compilation/export")
        assert empty_exp.status_code == 200
        assert empty_exp.text.strip() == "WEBVTT"
        assert empty_exp.text.count("-->") == 0
        assert "attachment" in empty_exp.headers["content-disposition"]
        assert "compilation.vtt" in empty_exp.headers["content-disposition"]

        # 保存有效配置后：GET 可回读（重开历史任务可再编辑）
        client.put(f"/api/submissions/{cpid}/compilation", json={
            "windows": [{"start": 8, "end": 13}, {"start": 0, "end": 6}],
        })
        saved = client.get(f"/api/submissions/{cpid}/compilation").json()
        assert saved["windows"] == [
            {"start": 8.0, "end": 13.0}, {"start": 0.0, "end": 6.0}]

        # 导出独立合辑 WebVTT：合辑时间码、原 cue 文本
        cv = client.get(f"/api/submissions/{cpid}/compilation/export")
        assert cv.status_code == 200
        cblocks = _parse_vtt_blocks(cv.text)
        assert len(cblocks) == len(prev["cues"])
        for (_notes, s, e, text), cue in zip(cblocks, prev["cues"]):
            assert s == format_timestamp(cue["start"])
            assert e == format_timestamp(cue["end"])
            assert text == cue["text"]

        # NOTE 保留：用有校验问题的任务，窗口完整覆盖全部修订 cue
        assert mx["issues"], "该任务应存在校验问题"
        ip = client.put(f"/api/submissions/{mxid}/compilation", json={
            "windows": [{"start": 0, "end": 100}],
        }).json()
        assert ip["cue_count"] == len(mx["cues"])
        noted = [c for c in ip["cues"] if c["notes"]]
        assert len({c["source_cue"] for c in noted}) == len(
            {i["cue_seq"] for i in mx["issues"]})
        iexp = client.get(f"/api/submissions/{mxid}/compilation/export")
        iblocks = _parse_vtt_blocks(iexp.text)
        assert sum(len(b[0]) for b in iblocks) == len(mx["issues"])
        assert any(n.startswith("[校验问题/")
                   for b in iblocks for n in b[0])
        # 完整覆盖单窗口时合辑时间码等于原修订时间码
        for (_notes, s, e, text), cue in zip(iblocks, ip["cues"]):
            assert s == format_timestamp(cue["start"])
            assert e == format_timestamp(cue["end"])
            assert text == cue["text"]

        # 问题 cue 被跨窗拆分时，每个拆分段都带同样的校验 NOTE
        iss_seq = mx["issues"][0]["cue_seq"]
        icue = next(c for c in mx["cues"] if c["seq"] == iss_seq)
        mid = icue["start"] + (icue["end"] - icue["start"]) / 2
        split2 = client.put(f"/api/submissions/{mxid}/compilation", json={
            "windows": [{"start": 0, "end": mid},
                        {"start": mid, "end": 100}],
        }).json()
        split_parts = [c for c in split2["cues"]
                       if c["source_seq"] == iss_seq]
        assert len(split_parts) == 2
        assert all(c["notes"] for c in split_parts)

        # 原修订导出不受合辑窗口配置影响
        full_after = client.get(f"/api/submissions/{mxid}/export").text
        assert full_after.count("-->") == len(mx["cues"])

        # 不存在的任务 → 404
        assert client.get(
            "/api/submissions/9999/compilation").status_code == 404
        assert client.put("/api/submissions/9999/compilation",
                          json={"windows": []}).status_code == 404


        # --- 术语审校 ---
        # 1. 维护按语言区分的术语表
        r = client.post("/api/terms", json={
            "language": "all", "canonical": "WebVTT",
            "forbidden": ["webvtt"], "note": "格式名称",
        })
        assert r.status_code == 201
        assert client.post("/api/terms", json={
            "language": "zh", "canonical": "账号",
            "forbidden": ["帐号"],
        }).status_code == 201
        assert client.post("/api/terms", json={
            "language": "zh", "canonical": "人工智能",
        }).status_code == 201
        # 校验：禁止替代词不能与规范词相同
        assert client.post("/api/terms", json={
            "language": "zh", "canonical": "账号",
            "forbidden": ["账号"],
        }).status_code == 400

        terms = client.get("/api/terms").json()
        assert len(terms["items"]) == 3
        assert terms["languages"]["all"] == "通用"
        # 更新术语
        first_id = terms["items"][0]["id"]
        assert client.put(f"/api/terms/{first_id}", json={
            "language": "all", "canonical": "WebVTT",
            "forbidden": ["webvtt"],
        }).status_code == 200

        # 2. 上传含各类不一致写法的字幕（每条 cue 均短于阈值，不会被重新切分）
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
        checked = client.post(
            "/api/analyze",
            files={"file": ("terms-demo.vtt", term_vtt, "text/vtt")},
            data={"max_chars": "30", "min_dwell": "0.5", "max_cps": "20"},
        ).json()
        assert len(checked["cues"]) == 9
        assert checked["term_findings"] == 6
        tsid = checked["id"]

        findings = client.get(
            f"/api/submissions/{tsid}/term-findings").json()["items"]
        by_cat = {}
        matched = set()
        for f in findings:
            by_cat[f["category"]] = by_cat.get(f["category"], 0) + 1
            matched.add(f["matched"])
            # 上下文与偏移一致，来源 cue 与修订 cue 一一对应
            assert f["context"][f["start_off"]:f["end_off"]] == f["matched"]
            assert f["source_cues"] == [f["cue_seq"] + 1]
            assert f["status"] == "pending"
        assert by_cat == {"forbidden": 2, "width": 1, "punct": 1,
                          "spelling": 2}
        assert {"webvtt", "帐号", "ＷｅｂＶＴＴ", "Web VTT",
                "WebVTs", "人工知能"} <= matched

        # 3. 逐项/批量确认：接受全部
        ids = [f["id"] for f in findings]
        st = client.post("/api/terms/findings/status", json={
            "ids": ids, "status": "accepted",
        })
        assert st.status_code == 200
        assert st.json()["updated"] == 6
        assert client.post("/api/terms/findings/status", json={
            "ids": [1], "status": "bogus",
        }).status_code == 400

        # 4. 导出术语修订 WebVTT：所有问题写法被规范词替换
        revised = client.get(f"/api/submissions/{tsid}/term-export")
        assert revised.status_code == 200
        assert revised.headers["X-Term-Replacements"] == "6"
        assert revised.text.count("-->") == 9
        assert "webvtt" not in revised.text
        assert "帐号" not in revised.text
        assert "人工知能" not in revised.text
        assert revised.text.count("WebVTT") == 5
        assert revised.text.count("账号") == 2
        assert revised.text.count("人工智能") == 2

        # 忽略的发现不应被替换
        client.post("/api/terms/findings/status", json={
            "ids": ids[:1], "status": "dismissed",
        })
        partly = client.get(f"/api/submissions/{tsid}/term-export")
        assert partly.headers["X-Term-Replacements"] == "5"

        # 5. 重新检测：发现重建，确认状态重置为待确认
        recheck = client.post(
            f"/api/submissions/{tsid}/term-check").json()
        assert len(recheck["items"]) == 6
        assert all(f["status"] == "pending" for f in recheck["items"])

        # 6. 审校报告：CSV（含 BOM 与中文表头）与 JSON
        report = client.get(f"/api/submissions/{tsid}/term-report")
        assert report.status_code == 200
        assert report.text.startswith("﻿")
        assert "规范词,术语语言,类别" in report.text
        assert "WebVTT" in report.text and "待确认" in report.text
        rj = client.get(
            f"/api/submissions/{tsid}/term-report?format=json").json()
        assert len(rj["items"]) == 6

        # 删除全部术语后重新检测，发现清空
        client.delete(f"/api/terms/{first_id}")
        for t in client.get("/api/terms").json()["items"]:
            assert client.delete(f"/api/terms/{t['id']}").status_code == 200
        assert client.post(
            f"/api/submissions/{tsid}/term-check").json()["items"] == []
        assert client.delete("/api/terms/9999").status_code == 404

    print("SMOKE_TESTS_OK")


if __name__ == "__main__":
    main()
