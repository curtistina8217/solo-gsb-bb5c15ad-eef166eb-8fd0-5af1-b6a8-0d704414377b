/* 字幕断句校验台 — 原生前端逻辑（无框架、无构建） */
(() => {
  const $ = (sel) => document.querySelector(sel);

  const KIND_LABELS = {
    line_too_long: "超长",
    dwell_too_short: "停留过短",
    read_speed: "阅读过快",
  };
  const TERM_CATEGORY_LABELS = {
    forbidden: "禁止替代词",
    case: "大小写",
    width: "全半角",
    punct: "标点",
    spelling: "近似拼写",
  };
  const TERM_STATUS_LABELS = {
    pending: "待确认",
    accepted: "已接受",
    dismissed: "已忽略",
  };

  let current = null;
  let currentFindings = [];
  let termsCache = [];
  let termLanguages = {};
  let compWindows = [];
  let currentCompilation = null;

  const fmtTc = (s) => {
    if (s == null || isNaN(s)) return "--:--:--.---";
    const ms = Math.round(s * 1000);
    const h = String(Math.floor(ms / 3600000)).padStart(2, "0");
    const m = String(Math.floor((ms % 3600000) / 60000)).padStart(2, "0");
    const sec = String(Math.floor((ms % 60000) / 1000)).padStart(2, "0");
    const milli = String(ms % 1000).padStart(3, "0");
    return `${h}:${m}:${sec}.${milli}`;
  };

  $("#file").addEventListener("change", (e) => {
    const label = $("#file-name");
    label.textContent = e.target.files[0]?.name || "选择 .vtt 或 .srt 文件…";
    label.parentElement.classList.toggle("has-file", !!e.target.files[0]);
  });

  $("#analyze-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const errBox = $("#form-error");
    errBox.hidden = true;

    const f = $("#file").files[0];
    if (!f) {
      errBox.textContent = "请先选择字幕文件";
      errBox.hidden = false;
      return;
    }

    const body = new FormData();
    body.append("file", f);
    body.append("max_chars", $("#max-chars").value);
    body.append("min_dwell", $("#min-dwell").value);
    body.append("max_cps", $("#max-cps").value);

    const btn = $("#analyze-btn");
    btn.disabled = true;
    btn.textContent = "断句计算中…";
    try {
      const resp = await fetch("/api/analyze", { method: "POST", body });
      const data = await resp.json();
      if (!resp.ok) throw new Error(data.detail || "分析失败");
      current = data;
      render(data);
      loadFindings(data.id);
      resetCompilation();
    } catch (err) {
      errBox.textContent = err.message;
      errBox.hidden = false;
    } finally {
      btn.disabled = false;
      btn.textContent = "开始校验断句";
      loadHistory();
    }
  });

  $("#lang-filter").addEventListener("change", renderTables);
  $("#only-issues").addEventListener("change", renderTables);

  $("#export-btn").addEventListener("click", () => {
    if (!current) return;
    const lang = $("#lang-filter").value;
    const url = `/api/submissions/${current.id}/export` +
      (lang ? `?language=${encodeURIComponent(lang)}` : "");
    window.location.href = url;
  });

  function render(data) {
    $("#result-card").hidden = false;
    $("#result-file").textContent = `— ${data.filename}`;
    renderStats(data);

    const select = $("#lang-filter");
    const langs = new Set(data.issues.map((i) => i.language));
    const prev = select.value;
    select.innerHTML = '<option value="">全部语言</option>' +
      [...langs].map((l) =>
        `<option value="${l}">${data.language_names[l] || l}</option>`).join("");
    select.value = [...langs].includes(prev) ? prev : "";

    renderTables();
    $("#result-card").scrollIntoView({ behavior: "smooth", block: "start" });
  }

  function renderStats(data) {
    const s = data.stats;
    const cls = s.issue_count ? "stat bad" : "stat good";
    const termBox = data.term_findings != null
      ? `<div class="${data.term_findings ? "stat bad" : "stat good"}">` +
        `<b>${data.term_findings}</b>术语疑似不一致</div>`
      : "";
    $("#stats").innerHTML = `
      <div class="${cls}"><b>${s.original_count}</b>原始 cue</div>
      <div class="stat"><b>${s.revised_count}</b>修订后 cue</div>
      <div class="${cls}"><b>${s.issue_count}</b>问题区段</div>
      <div class="stat"><b>${s.split_cues}</b>被拆分原 cue</div>
      <div class="stat"><b>${s.merged_cues}</b>跨 cue 合并</div>
      <div class="stat"><b>${s.moved_boundaries}</b>边界重分配</div>
      ${termBox}`;
  }

  function activeLang() {
    return $("#lang-filter").value;
  }

  function renderTables() {
    if (!current) return;
    const lang = activeLang();
    const onlyIssues = $("#only-issues").checked;

    const filteredIssues = lang
      ? current.issues.filter((i) => i.language === lang)
      : current.issues;
    const badSeqs = new Map();
    for (const iss of filteredIssues) {
      if (!badSeqs.has(iss.cue_seq)) badSeqs.set(iss.cue_seq, []);
      badSeqs.get(iss.cue_seq).push(iss);
    }

    $("#issue-count-note").textContent =
      `共 ${filteredIssues.length} 个问题` +
      (lang ? `（${current.language_names[lang] || lang}）` : "");

    const tbody = $("#cues-table tbody");
    const rows = current.cues
      .filter((c) => !onlyIssues || badSeqs.has(c.seq));
    tbody.innerHTML = rows.map((c) => {
      const hits = badSeqs.get(c.seq) || [];
      const rowCls = hits.some((h) => h.kind === "line_too_long")
        ? "bad"
        : hits.length ? "warn" : "";
      const tags = hits.map((h) =>
        `<span class="tag ${h.kind}">${KIND_LABELS[h.kind] || h.kind}</span>`
      ).join("");
      const srcCls = c.redistributed ? "src merged" : "src";
      const srcLabel = c.redistributed
        ? `原 cue ${c.sources.join("+")} · 边界已重分配`
        : `原 cue ${c.sources.join(",")}`;
      return `<tr class="${rowCls}">
        <td>${c.seq + 1}</td>
        <td class="tc">${fmtTc(c.start)} →<br/>${fmtTc(c.end)}</td>
        <td class="tc">${c.duration.toFixed(2)}s</td>
        <td>${c.length}</td>
        <td>${c.cps ?? "—"}</td>
        <td class="cue-text">${tags}<span>${escapeHtml(c.text)}</span>
            <span class="${srcCls}">${srcLabel}</span></td>
      </tr>`;
    }).join("");

    const list = $("#issues-list");
    if (!filteredIssues.length) {
      list.innerHTML =
        '<li class="empty">当前筛选条件下没有无法满足约束的区段 ✓</li>';
    } else {
      list.innerHTML = filteredIssues.map((iss) => `
        <li>
          <div class="meta">
            <span class="tag ${iss.kind}">${KIND_LABELS[iss.kind] || iss.kind}</span>
            修订 cue #${iss.cue_seq + 1} ·
            ${iss.language_name} ·
            ${fmtTc(iss.start)} → ${fmtTc(iss.end)}
          </div>
          <span class="seg">${escapeHtml(iss.segment)}</span>
          <span class="reason">${escapeHtml(iss.reason)}</span>
        </li>`).join("");
    }
  }

  /* ---------- 术语表管理 ---------- */

  async function loadTerms() {
    try {
      const resp = await fetch("/api/terms");
      const data = await resp.json();
      termsCache = data.items || [];
      termLanguages = data.languages || {};
      renderTermLangSelect();
      renderTerms();
      renderTermReview();
    } catch (_) {
      /* 术语表加载失败不阻塞主流程 */
    }
  }

  function renderTermLangSelect() {
    const sel = $("#term-lang");
    const prev = sel.value;
    sel.innerHTML = Object.entries(termLanguages)
      .map(([k, v]) => `<option value="${k}">${escapeHtml(v)}</option>`)
      .join("");
    if (termLanguages[prev]) sel.value = prev;
  }

  function renderTerms() {
    const tbody = $("#terms-table tbody");
    tbody.innerHTML = termsCache.map((t) => `<tr>
      <td>${escapeHtml(termLanguages[t.language] || t.language)}</td>
      <td><b>${escapeHtml(t.canonical)}</b></td>
      <td>${t.variants.map(escapeHtml).join("、") || "—"}</td>
      <td>${t.forbidden.map(escapeHtml).join("、") || "—"}</td>
      <td class="muted">${escapeHtml(t.note || "")}</td>
      <td><button class="secondary term-del" data-id="${t.id}">删除</button></td>
    </tr>`).join("") ||
      '<tr><td colspan="6" class="muted">暂无术语，请在上方添加</td></tr>';

    tbody.querySelectorAll(".term-del").forEach((btn) =>
      btn.addEventListener("click", async () => {
        await fetch(`/api/terms/${btn.dataset.id}`, { method: "DELETE" });
        loadTerms();
      }));
  }

  function splitList(value) {
    return value.split(/[,，、;；]/).map((s) => s.trim()).filter(Boolean);
  }

  $("#term-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const errBox = $("#term-error");
    errBox.hidden = true;
    const payload = {
      language: $("#term-lang").value,
      canonical: $("#term-canonical").value.trim(),
      variants: splitList($("#term-variants").value),
      forbidden: splitList($("#term-forbidden").value),
      note: $("#term-note").value.trim(),
    };
    try {
      const resp = await fetch("/api/terms", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const data = await resp.json();
      if (!resp.ok) throw new Error(data.detail || "保存失败");
      $("#term-canonical").value = "";
      $("#term-variants").value = "";
      $("#term-forbidden").value = "";
      $("#term-note").value = "";
      loadTerms();
    } catch (err) {
      errBox.textContent = err.message;
      errBox.hidden = false;
    }
  });

  /* ---------- 术语审校 ---------- */

  async function loadFindings(sid) {
    try {
      const resp = await fetch(`/api/submissions/${sid}/term-findings`);
      currentFindings = resp.ok ? (await resp.json()).items : [];
    } catch (_) {
      currentFindings = [];
    }
    renderTermReview();
  }

  async function setFindingStatus(ids, status) {
    if (!ids.length) return;
    await fetch("/api/terms/findings/status", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ids, status }),
    });
    for (const f of currentFindings) {
      if (ids.includes(f.id)) f.status = status;
    }
    renderTermReview();
  }

  function renderTermReview() {
    const box = $("#term-review");
    if (!current) {
      box.hidden = true;
      return;
    }
    box.hidden = false;
    const list = $("#term-findings");

    if (!termsCache.length) {
      $("#term-summary").textContent =
        "尚未维护术语表，请先在上方「术语表管理」中添加";
      list.innerHTML = "";
      return;
    }
    if (!currentFindings.length) {
      $("#term-summary").textContent = "未发现术语不一致 ✓";
      list.innerHTML = "";
      return;
    }

    const byCat = {};
    const byStatus = { pending: 0, accepted: 0, dismissed: 0 };
    for (const f of currentFindings) {
      byCat[f.category] = (byCat[f.category] || 0) + 1;
      byStatus[f.status] = (byStatus[f.status] || 0) + 1;
    }
    $("#term-summary").textContent =
      `共 ${currentFindings.length} 项疑似不一致（` +
      Object.entries(byCat)
        .map(([k, v]) => `${TERM_CATEGORY_LABELS[k] || k} ${v}`).join(" · ") +
      `）｜已接受 ${byStatus.accepted} · 待确认 ${byStatus.pending} · ` +
      `已忽略 ${byStatus.dismissed}`;

    list.innerHTML = currentFindings.map((f) => {
      const before = escapeHtml(f.context.slice(0, f.start_off));
      const hit = escapeHtml(f.context.slice(f.start_off, f.end_off));
      const after = escapeHtml(f.context.slice(f.end_off));
      const src = f.source_cues.length
        ? ` · 原 cue ${f.source_cues.join("+")}` : "";
      return `<li class="${f.status}">
        <div class="meta">
          <span class="tag ${f.category}">${TERM_CATEGORY_LABELS[f.category] || f.category}</span>
          规范词「${escapeHtml(f.canonical)}」 · ${f.language_name} ·
          修订 cue #${f.cue_seq + 1}${src} ·
          ${fmtTc(f.cue_start)} → ${fmtTc(f.cue_end)}
          <span class="fnd-status">${TERM_STATUS_LABELS[f.status] || f.status}</span>
        </div>
        <span class="ctx">${before}<mark>${hit}</mark>${after}</span>
        <span class="sug">建议替换为：<b>${escapeHtml(f.suggestion)}</b></span>
        <span class="reason">${escapeHtml(f.reason)}</span>
        <div class="fnd-actions">
          <button class="secondary fnd-acc" data-id="${f.id}">接受</button>
          <button class="secondary fnd-dis" data-id="${f.id}">忽略</button>
          <button class="secondary fnd-reset" data-id="${f.id}">恢复待确认</button>
        </div>
      </li>`;
    }).join("");

    list.querySelectorAll(".fnd-actions button").forEach((btn) =>
      btn.addEventListener("click", () => {
        const status = btn.classList.contains("fnd-acc") ? "accepted"
          : btn.classList.contains("fnd-dis") ? "dismissed" : "pending";
        setFindingStatus([Number(btn.dataset.id)], status);
      }));
  }

  $("#term-accept-all").addEventListener("click", () =>
    setFindingStatus(currentFindings.map((f) => f.id), "accepted"));
  $("#term-dismiss-all").addEventListener("click", () =>
    setFindingStatus(currentFindings.map((f) => f.id), "dismissed"));

  $("#term-recheck").addEventListener("click", async () => {
    if (!current) return;
    const resp = await fetch(`/api/submissions/${current.id}/term-check`,
      { method: "POST" });
    const data = await resp.json();
    currentFindings = data.items || [];
    renderTermReview();
  });

  $("#term-export").addEventListener("click", () => {
    if (!current) return;
    window.location.href = `/api/submissions/${current.id}/term-export`;
  });

  $("#term-report").addEventListener("click", () => {
    if (!current) return;
    window.location.href = `/api/submissions/${current.id}/term-report`;
  });

  /* ---------- 片段合辑 ---------- */

  function markCompDirty() {
    currentCompilation = null;
    $("#comp-export").disabled = true;
    $("#comp-summary").textContent = "窗口配置已修改，点击「生成合辑」保存并预览。";
    $("#comp-table tbody").innerHTML = "";
  }

  function renderCompWindows() {
    const box = $("#comp-windows");
    if (!compWindows.length) {
      box.innerHTML = '<p class="muted">尚未添加窗口，点击「添加窗口」开始配置。</p>';
      return;
    }
    box.innerHTML = compWindows.map((w, i) => `
      <div class="comp-row" data-idx="${i}">
        <span class="comp-no">#${i + 1}</span>
        <input type="number" class="comp-start" min="0" step="0.001"
               placeholder="起点（秒）" value="${escapeHtml(String(w.start))}" />
        <span class="comp-arrow">→</span>
        <input type="number" class="comp-end" min="0" step="0.001"
               placeholder="终点（秒）" value="${escapeHtml(String(w.end))}" />
        <button type="button" class="secondary comp-up" title="上移"
                ${i === 0 ? "disabled" : ""}>↑</button>
        <button type="button" class="secondary comp-down" title="下移"
                ${i === compWindows.length - 1 ? "disabled" : ""}>↓</button>
        <button type="button" class="secondary comp-del">删除</button>
      </div>`).join("");

    box.querySelectorAll(".comp-row").forEach((row) => {
      const i = Number(row.dataset.idx);
      row.querySelector(".comp-start").addEventListener("input", (e) => {
        compWindows[i].start = e.target.value;
        markCompDirty();
      });
      row.querySelector(".comp-end").addEventListener("input", (e) => {
        compWindows[i].end = e.target.value;
        markCompDirty();
      });
      row.querySelector(".comp-up").addEventListener("click", () => {
        if (i > 0) {
          [compWindows[i - 1], compWindows[i]] = [compWindows[i], compWindows[i - 1]];
          markCompDirty();
          renderCompWindows();
        }
      });
      row.querySelector(".comp-down").addEventListener("click", () => {
        if (i < compWindows.length - 1) {
          [compWindows[i], compWindows[i + 1]] = [compWindows[i + 1], compWindows[i]];
          markCompDirty();
          renderCompWindows();
        }
      });
      row.querySelector(".comp-del").addEventListener("click", () => {
        compWindows.splice(i, 1);
        markCompDirty();
        renderCompWindows();
      });
    });
  }

  function renderCompilation(data) {
    currentCompilation = data;
    $("#comp-export").disabled = false;
    $("#comp-summary").textContent =
      `共 ${data.windows.length} 个窗口 · 合辑时间轴总长 ` +
      `${data.timeline_length.toFixed(3)}s · 生成 ${data.cues.length} 条 cue` +
      (data.cues.length === 0 ? "（空合辑：窗口内没有任何修订 cue）" : "");

    $("#comp-table tbody").innerHTML = data.cues.map((c) => {
      const notes = (c.notes || []).map((n) =>
        `<span class="comp-note">NOTE ${escapeHtml(n)}</span>`).join("");
      return `<tr>
        <td>${c.seq + 1}</td>
        <td class="tc">${fmtTc(c.start)} →<br/>${fmtTc(c.end)}</td>
        <td class="tc">${c.duration.toFixed(2)}s</td>
        <td>原 cue ${c.origin_seq + 1}</td>
        <td class="cue-text"><span>${escapeHtml(c.text)}</span>${notes}</td>
      </tr>`;
    }).join("") ||
      '<tr><td colspan="5" class="muted">窗口内没有命中任何修订 cue，' +
      '导出将得到结构有效的空 WebVTT。</td></tr>';
  }

  function resetCompilation() {
    compWindows = [];
    currentCompilation = null;
    $("#comp-export").disabled = true;
    $("#comp-error").hidden = true;
    $("#comp-summary").textContent = "";
    $("#comp-table tbody").innerHTML = "";
    renderCompWindows();
  }

  async function buildCompilation() {
    if (!current) return;
    const errBox = $("#comp-error");
    errBox.hidden = true;

    const windows = compWindows.map((w) => ({
      start: w.start === "" ? NaN : Number(w.start),
      end: w.end === "" ? NaN : Number(w.end),
    }));
    const resp = await fetch(`/api/submissions/${current.id}/compilation`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ windows }),
    });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.detail || "保存合辑配置失败");
    renderCompilation(data);
  }

  async function loadCompilation(sid) {
    resetCompilation();
    try {
      const resp = await fetch(`/api/submissions/${sid}/compilation`);
      if (resp.status === 404) return;  // 该任务尚未配置合辑
      const data = await resp.json();
      if (!resp.ok) return;
      compWindows = data.windows.map((w) => ({
        start: String(w.start), end: String(w.end),
      }));
      renderCompWindows();
      renderCompilation(data);
    } catch (_) {
      /* 合辑配置加载失败不阻塞结果页 */
    }
  }

  $("#comp-add").addEventListener("click", () => {
    compWindows.push({ start: "", end: "" });
    markCompDirty();
    renderCompWindows();
  });

  $("#comp-build").addEventListener("click", async () => {
    const errBox = $("#comp-error");
    try {
      await buildCompilation();
    } catch (err) {
      errBox.textContent = err.message;
      errBox.hidden = false;
    }
  });

  $("#comp-export").addEventListener("click", () => {
    if (!current || !currentCompilation) return;
    window.location.href =
      `/api/submissions/${current.id}/compilation-export`;
  });

  /* ---------- 历史任务 ---------- */

  async function loadHistory() {
    try {
      const resp = await fetch("/api/submissions");
      const data = await resp.json();
      $("#history-table tbody").innerHTML = data.items.map((it) => {
        const s = it.stats || {};
        return `<tr>
          <td>${it.id}</td>
          <td>${escapeHtml(it.filename)}</td>
          <td class="tc">${it.max_chars}字 / ${it.min_dwell}s /
              ${it.max_cps}字每秒</td>
          <td>${it.issue_count}</td>
          <td>${it.created_at}</td>
          <td><button class="secondary" data-id="${it.id}">查看</button>
              <a href="/api/submissions/${it.id}/export"
                 download><button class="secondary">VTT</button></a></td>
        </tr>`;
      }).join("") || '<tr><td colspan="6" class="muted">暂无任务</td></tr>';

      $("#history-table tbody").querySelectorAll("button[data-id]")
        .forEach((btn) => btn.addEventListener("click", async () => {
          const resp = await fetch(`/api/submissions/${btn.dataset.id}`);
          current = await resp.json();
          render(current);
          loadFindings(current.id);
          loadCompilation(current.id);
        }));
    } catch (_) {
      /* 历史加载失败不阻塞主流程 */
    }
  }

  function escapeHtml(str) {
    return String(str).replace(/[&<>"']/g, (ch) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;",
      '"': "&quot;", "'": "&#39;",
    }[ch]));
  }

  loadTerms();
  loadHistory();
})();
