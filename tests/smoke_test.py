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
from app.subtitles import format_timestamp  # noqa: E402

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

        # --- 片段合辑 ---
        # cue A：含不可拆分超长拉丁 token（产生超长校验 NOTE），时长充裕；
        # cue B 正常；两条 cue 之间有 1s 静音间隙
        token = "Supercalifragilisticexpialidocious"
        comp_vtt = (
            "WEBVTT\n\n"
            f"00:00:00.000 --> 00:00:04.000\n{token}\n\n"
            "00:00:05.000 --> 00:00:09.000\n正常的一句中文内容。\n"
        )
        cp = client.post(
            "/api/analyze",
            files={"file": ("comp.vtt", comp_vtt, "text/vtt")},
            data={"max_chars": "10", "min_dwell": "0.5", "max_cps": "30"},
        ).json()
        cpid = cp["id"]
        assert len(cp["cues"]) == 2
        assert [i["kind"] for i in cp["issues"]] == ["line_too_long"]

        # 未配置过：GET / 导出均 404；原修订导出不受影响
        assert client.get(
            f"/api/submissions/{cpid}/compilation").status_code == 404
        assert client.get(
            f"/api/submissions/{cpid}/compilation-export").status_code == 404
        plain = client.get(f"/api/submissions/{cpid}/export")
        assert plain.text.count("-->") == 2

        # 参数校验：起点非负、终点大于起点、窗口互不重叠
        for body in ([[{"start": -1, "end": 2}],
                      [{"start": 2, "end": 2}],
                      [{"start": 0, "end": 2}, {"start": 1, "end": 3}]]):
            assert client.put(
                f"/api/submissions/{cpid}/compilation",
                json={"windows": body}).status_code == 400

        # 窗口按用户给定顺序：[6,8) 在前、[1,3) 在后
        # 窗 [6,8) 裁 B → 新轴 [0,2)；窗 [1,3) 裁 A → base 2 + [1,3)
        # = 新轴 [2,4)（基准按窗口长度累加）
        r = client.put(f"/api/submissions/{cpid}/compilation", json={
            "windows": [{"start": 6, "end": 8}, {"start": 1, "end": 3}],
        })
        assert r.status_code == 200
        comp = r.json()
        assert [w["length"] for w in comp["windows"]] == [2.0, 2.0]
        assert comp["timeline_length"] == 4.0
        assert [(c["origin_seq"], c["start"], c["end"], c["text"])
                for c in comp["cues"]] == [
            (1, 0.0, 2.0, "正常的一句中文内容。"),
            (0, 2.0, 4.0, token),
        ]
        assert comp["cues"][0]["notes"] == []
        assert comp["cues"][1]["notes"] and all(
            n.startswith("[校验问题/line_too_long]")
            for n in comp["cues"][1]["notes"])

        # 导出独立 WebVTT：新时间码、保留文本与对应校验 NOTE
        exp = client.get(f"/api/submissions/{cpid}/compilation-export")
        assert exp.status_code == 200
        assert "compilation.vtt" in exp.headers["content-disposition"]
        blocks = _parse_vtt_blocks(exp.text)
        assert len(blocks) == 2
        notes0, s0, e0, t0 = blocks[0]
        notes1, s1, e1, t1 = blocks[1]
        assert notes0 == []
        assert notes1 and all(n.startswith("[校验问题/line_too_long]")
                             for n in notes1)
        assert (s0, e0, t0) == ("00:00:00.000", "00:00:02.000",
                                "正常的一句中文内容。")
        assert (s1, e1, t1) == ("00:00:02.000", "00:00:04.000", token)

        # 跨窗口 cue 拆分 + 窗内静音间隙保留：
        # A[0,4) 跨窗 [1,2.5)/[3,7) 拆成两段；B 被 [3,7) 裁切，
        # A 第二段 [3,4) 与 B[5,7) 间 1s 间隙在新轴上原样保留
        r2 = client.put(f"/api/submissions/{cpid}/compilation", json={
            "windows": [{"start": 1, "end": 2.5}, {"start": 3, "end": 7}],
        }).json()
        assert [(c["origin_seq"], round(c["start"], 3), round(c["end"], 3))
                for c in r2["cues"]] == [
            (0, 0.0, 1.5),       # A 第一段
            (0, 1.5, 2.5),       # A 第二段（基准 += 1.5）
            (1, 3.5, 5.5),       # B[5,7) → 新轴 [3.5,5.5)，与上段间隔 1s
        ]
        # 同一原 cue 拆出的两段都带着同一份校验 NOTE
        assert all(c["notes"] for c in r2["cues"][:2])
        assert r2["cues"][2]["notes"] == []

        # 零时长交集不生成 cue：窗口只落在两 cue 之间的静音间隙上
        gap = client.put(f"/api/submissions/{cpid}/compilation", json={
            "windows": [{"start": 4.2, "end": 4.8}],
        }).json()
        assert gap["cues"] == []
        gap_exp = client.get(
            f"/api/submissions/{cpid}/compilation-export")
        assert gap_exp.status_code == 200
        assert _parse_vtt_blocks(gap_exp.text) == []
        assert gap_exp.text.strip() == "WEBVTT"

        # 空窗口集：保存成功，导出结构有效的空 WebVTT
        empty = client.put(f"/api/submissions/{cpid}/compilation",
                           json={"windows": []}).json()
        assert empty["windows"] == [] and empty["cues"] == []
        assert empty["timeline_length"] == 0
        empty_exp = client.get(
            f"/api/submissions/{cpid}/compilation-export")
        assert empty_exp.status_code == 200
        assert empty_exp.text.strip() == "WEBVTT"

        # 配置按任务持久化，可覆盖：GET 取回最近一次（空集），再改回非空
        got = client.get(f"/api/submissions/{cpid}/compilation").json()
        assert got["windows"] == []
        client.put(f"/api/submissions/{cpid}/compilation", json={
            "windows": [{"start": 1, "end": 3}],
        })
        got = client.get(f"/api/submissions/{cpid}/compilation").json()
        assert len(got["windows"]) == 1 and len(got["cues"]) == 1

        # 覆盖后原修订导出仍保持不变
        plain2 = client.get(f"/api/submissions/{cpid}/export")
        assert plain2.text == plain.text

        # 不存在的任务
        assert client.put("/api/submissions/999999/compilation",
                          json={"windows": []}).status_code == 404

    print("SMOKE_TESTS_OK")


if __name__ == "__main__":
    main()
