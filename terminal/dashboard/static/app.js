/* 記憶統合ダッシュボード front — vanilla JS, hash router */
"use strict";

const $ = (sel, root = document) => root.querySelector(sel);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const kb = (n) => n < 1024 ? `${n} B` : `${(n / 1024).toFixed(1)} KB`;
const num = (n) => Number(n ?? 0).toLocaleString("ja-JP");

let S = null;   // /api/state
let N = null;   // /api/nas
let factsCache = {};   // project -> rows

const TABS = {
  overview: "概要",
  context: "コンテキスト",
  facts: "記憶 (facts)",
  skills: "スキル",
  hooks: "Hooks",
  collect: "収集設定",
};

async function j(url, opts) {
  const r = await fetch(url, opts);
  const data = await r.json();
  if (data && data.error) throw new Error(data.error);
  return data;
}

function toast(msg, ms = 2600) {
  const t = $("#toast");
  t.textContent = msg;
  t.hidden = false;
  clearTimeout(t._timer);
  t._timer = setTimeout(() => { t.hidden = true; }, ms);
}

/* ---------------- data helpers ---------------- */

function injectedHooks() {
  return S.hooks.filter((h) => h.injected);
}

function budgetSegments() {
  const inj = injectedHooks()
    .reduce((a, h) => a + new TextEncoder().encode(h.injected).length, 0);
  const general = S.memory_indexes.find((m) => m.key === "general");
  return [
    { label: "general index", note: "メモリ本文", bytes: general ? general.bytes : 0, color: "var(--amber)" },
    { label: "hook 注入", note: "作業規律ほか", bytes: inj, color: "var(--blue)" },
    { label: "CLAUDE.md", note: "@include 行", bytes: S.claude_md.bytes, color: "var(--slate)" },
  ];
}

function warnings() {
  const out = [];
  const dup = S.hooks.filter((h) => h.duplicate);
  for (const d of dup) {
    out.push({ kind: "warn", tag: "重複",
      text: `${d.event} に同一コマンドが二重登録されています: ${d.command.slice(0, 80)}…` });
  }
  if (!S.vibe_island_present) {
    const n = S.hooks.filter((h) => h.command.includes("vibe-island")).length;
    if (n) out.push({ kind: "info", tag: "no-op",
      text: `vibe-island ブリッジ未導入のため、${n} 件の hook は実行時に何もしません(存在チェック付き)。` });
  }
  for (const m of S.memory_indexes) {
    if (m.bytes > 32768) out.push({ kind: "warn", tag: "32KiB超",
      text: `${m.key}/index.md が ${kb(m.bytes)}。Codex 既定の project_doc_max_bytes (32KiB) を超えると連結合計で黙って打ち切られます(この MacBook は 64KiB に拡大済)。` });
  }
  return out;
}

/* ---------------- renderers ---------------- */

function renderOverview(el) {
  const segs = budgetSegments();
  const total = segs.reduce((a, s) => a + s.bytes, 0);
  const turnsTotal = N.turns_by_project.reduce((a, r) => a + Number(r.n), 0);
  const factsTotal = N.facts_by_project.reduce((a, r) => a + Number(r.n), 0);
  const lastBatch = N.batch_runs[0];
  const warns = warnings();

  const MAX = 65536;
  let acc = 0;
  const stops = segs.map((s) => {
    const a = acc / MAX * 100; acc += s.bytes; const b = acc / MAX * 100;
    return `${s.color} ${a.toFixed(2)}% ${b.toFixed(2)}%`;
  });
  const usedEnd = (acc / MAX * 100).toFixed(2);
  const grad = `conic-gradient(${stops.join(",")},rgba(255,255,255,.06) ${usedEnd}% 100%)`;
  const usedPct = Math.round(total / MAX * 100);

  el.innerHTML = `
    <div class="numhd"><span class="no">01</span><span class="lb">毎セッション注入されるコンテキスト</span></div>
    <div class="budget-total">
      <span class="kb">${(total / 1024).toFixed(1)}<small>KB</small></span>
      <span class="faint">プロジェクト内ではそのプロジェクトの index が追加</span>
    </div>
    <div class="card budget-panel">
      <div class="donut" style="background:${grad}">
        <div class="hole"><div class="dpct">${usedPct}<i>%</i></div><div class="dsub">of 64KiB</div></div>
      </div>
      <div class="donut-legend">
        <div class="dtotal">Codex の連結上限 <b>64KiB</b>(project_doc_max_bytes)に対する使用率</div>
        ${segs.map((s) => `<div class="drow">
          <span class="dot" style="background:${s.color};width:9px;height:9px;border-radius:3px"></span>
          <span class="dl">${esc(s.label)}<small>${esc(s.note)}</small></span>
          <b>${kb(s.bytes)}</b></div>`).join("")}
      </div>
    </div>

    <div class="numhd"><span class="no">02</span><span class="lb">状態</span></div>
    <div class="stat-strip">
      <div class="ss"><div class="n">${num(turnsTotal)}</div><div class="l">turns(全端末の発話ログ)</div></div>
      <div class="ss"><div class="n">${num(factsTotal)}</div><div class="l">current facts(生きている記憶)</div></div>
      <div class="ss"><div class="n">${S.skills.length}</div><div class="l">スキル</div></div>
      <div class="ss"><div class="n">${S.hooks.length}</div><div class="l">hook 登録</div></div>
      <div class="ss">
        <div class="n ok">${lastBatch ? esc(lastBatch.status) : "—"}</div>
        <div class="l">直近バッチ (run ${lastBatch ? lastBatch.id : "—"})</div>
        <div class="s">${lastBatch ? esc(String(lastBatch.finished_at || "").slice(5, 16).replace("T", " ")) : ""}</div>
      </div>
    </div>

    <div class="numhd"><span class="no">03</span><span class="lb">注意</span></div>
    ${warns.length ? warns.map((w) => `<div class="note ${w.kind}"><span class="tag">${esc(w.tag)}</span><span>${esc(w.text)}</span></div>`).join("")
      : '<div class="note info"><span class="tag">OK</span><span>検出された問題はありません。</span></div>'}

    <div class="numhd"><span class="no">04</span><span class="lb">プロジェクト別の蓄積</span></div>
    <div class="card">
      <table>
        <tr><th>project_key</th><th style="text-align:right">turns</th><th style="text-align:right">facts</th><th>最終収集</th></tr>
        ${N.turns_by_project.map((r) => {
          const f = N.facts_by_project.find((x) => x.project_key === r.project_key ||
            x.project_key === keyToIndexDir(r.project_key));
          return `<tr><td class="mono">${keyLabel(r.project_key)}</td>
            <td class="num">${num(r.n)}</td>
            <td class="num">${f ? num(f.n) : "·"}</td>
            <td class="faint mono">${esc(String(r.last_ts || "").slice(0, 16).replace("T", " "))}</td></tr>`;
        }).join("")}
      </table>
    </div>`;
}

function keyToIndexDir(key) {
  return key.replace(/[^A-Za-z0-9._-]/g, "-");
}

/* ホームディレクトリ系キー(スラッシュ無し=git由来でない)は端末と実質1:1なので、
   turns の device 実績から主要端末名を引いて表示に添える。 */
function deviceOf(key) {
  if (key.includes("/") || key === "general") return null;
  const list = N.devices_by_project || [];
  // 文脈により munged 形(先頭の'-'が落ちた形)でも来るので両方照合する
  const hit = list.find((d) => d.project_key === key) ||
    list.find((d) => d.project_key === `-${key}`);
  return hit ? hit.device : null;
}

function keyLabel(key) {
  const dev = deviceOf(key);
  return `${esc(key)}${dev ? ` <span class="devtag">${esc(dev)}</span>` : ""}`;
}

function renderContext(el) {
  const files = [
    { key: "CLAUDE.md", target: null, bytes: S.claude_md.bytes,
      content: S.claude_md.content, note: "~/.claude/CLAUDE.md — @include の起点(読み取り専用)", mtime: "" },
    ...S.memory_indexes.map((m) => ({
      key: m.key, target: `index:${m.key}`, bytes: m.bytes, content: m.content,
      auto: m.auto_generated, mtime: m.mtime,
      note: `${m.path}(更新 ${m.mtime})`,
    })),
  ];
  el.innerHTML = `
    <div class="note warn"><span class="tag">前提</span><span>index.md は夜間バッチ(03:30)が current_facts から全再生成します。ここでの直接編集は即座に反映されますが翌バッチで上書きされます。恒久的に直したい内容は「記憶 (facts)」タブで facts を修正してください。</span></div>
    <div class="split" style="margin-top:14px">
      <div class="card filelist" id="ctxList"></div>
      <div class="card" id="ctxEditor"></div>
    </div>`;

  const list = $("#ctxList", el);
  const editor = $("#ctxEditor", el);
  let sel = files.find((f) => f.key === "general") || files[0];

  function drawList() {
    list.innerHTML = files.map((f) => `
      <button class="${f === sel ? "sel" : ""}" data-k="${esc(f.key)}">
        <span>${keyLabel(f.key)}${f.auto ? ' <span class="chip amber" title="夜間バッチ生成">auto</span>' : ""}</span>
        <span class="kb">${kb(f.bytes)}</span>
      </button>`).join("");
    list.querySelectorAll("button").forEach((b, i) => {
      b.onclick = () => { sel = files[i]; drawList(); drawEditor(); };
    });
  }

  function drawEditor() {
    const readonly = !sel.target;
    editor.innerHTML = `
      <div class="toolrow">
        <b>${esc(sel.key)}</b>
        <span class="faint">${esc(sel.note)}</span>
        <span style="flex:1"></span>
        <button class="btn mini" id="ctxSave" ${readonly ? "disabled" : ""}>保存</button>
      </div>
      <div class="gauge" id="ctxGauge"></div>
      <div class="gauge-labels"><span>0</span><span>32KiB (Codex既定の打ち切り)</span><span>64KiB (この端末の上限)</span></div>
      <textarea class="editor" id="ctxText" ${readonly ? "readonly" : ""} spellcheck="false"></textarea>`;
    const ta = $("#ctxText", editor);
    ta.value = sel.content;
    const gauge = $("#ctxGauge", editor);
    function drawGauge() {
      const bytes = new TextEncoder().encode(ta.value).length;
      const max = 65536;
      const pct = Math.min(100, bytes / max * 100);
      gauge.innerHTML = `<div class="fill" style="width:${pct}%"></div>
        <div class="mark" style="left:50%"></div>`;
      gauge.title = `${kb(bytes)} / 64 KiB`;
    }
    drawGauge();
    ta.oninput = drawGauge;
    const save = $("#ctxSave", editor);
    if (save && !readonly) save.onclick = async () => {
      try {
        const r = await j("/api/save", { method: "POST",
          body: JSON.stringify({ target: sel.target, content: ta.value }) });
        sel.content = ta.value;
        sel.bytes = r.bytes;
        drawList();
        toast(`保存しました(${kb(r.bytes)}、.bak 退避済み)`);
      } catch (e) { toast(`保存失敗: ${e.message}`, 5000); }
    };
  }

  drawList();
  drawEditor();
}

function renderFacts(el) {
  const projects = N.facts_by_project.map((r) => r.project_key);
  let sel = projects[0] || "general";

  el.innerHTML = `
    <div class="note info"><span class="tag">正道</span><span>ここが恒久的なコンテキスト調整の場所です。facts への追加・修正・撤去は、次回の夜間バッチ(03:30)で各 index.md に反映されます。</span></div>
    <div class="toolrow" id="factProjects" style="margin-top:14px"></div>
    <div class="card" style="margin-bottom:14px">
      <div class="toolrow" style="margin-bottom:0">
        <input type="text" id="factNew" placeholder="新しい事実を1行で(選択中のプロジェクトに追加)">
        <button class="btn mini" id="factAdd">追加</button>
      </div>
    </div>
    <div class="card" id="factList" style="max-height:62vh;overflow-y:auto">読み込み中…</div>
    <h2 class="section">turns 全文検索(PGroonga)</h2>
    <div class="card">
      <div class="toolrow">
        <input type="text" id="turnQ" placeholder="発話ログを検索…">
        <select id="turnProj"><option value="">全プロジェクト</option>
          ${N.turns_by_project.map((r) => {
            const dev = deviceOf(r.project_key);
            return `<option value="${esc(r.project_key)}">${esc(r.project_key)}${dev ? `〈${esc(dev)}〉` : ""}</option>`;
          }).join("")}</select>
        <button class="btn mini ghost" id="turnGo">検索</button>
      </div>
      <div id="turnResults" class="faint">キーワードを入れて検索してください。</div>
    </div>

    <h2 class="section">auto memory スナップショット — 各端末の内蔵メモリ(MEMORY.md 等)の取り込み履歴。夜間バッチが index との食い違い時の参考に使う補助データ</h2>
    <div class="card" id="amList"></div>`;

  const projRow = $("#factProjects", el);
  const listEl = $("#factList", el);

  function drawProjects() {
    projRow.innerHTML = projects.map((p) => {
      const n = N.facts_by_project.find((r) => r.project_key === p);
      const dev = deviceOf(p);
      return `<span class="chip click ${p === sel ? "sel" : ""}" data-p="${esc(p)}">${esc(p)}${dev ? `〈${esc(dev)}〉` : ""} · ${n ? n.n : 0}</span>`;
    }).join("");
    projRow.querySelectorAll(".chip").forEach((c) => {
      c.onclick = () => { sel = c.dataset.p; drawProjects(); loadFacts(); };
    });
  }

  async function loadFacts(force = false) {
    listEl.textContent = "読み込み中…";
    try {
      if (force || !factsCache[sel]) {
        factsCache[sel] = await j(`/api/facts?project=${encodeURIComponent(sel)}`);
      }
      drawFacts();
    } catch (e) { listEl.textContent = `取得失敗: ${e.message}`; }
  }

  function drawFacts() {
    const rows = factsCache[sel] || [];
    if (!rows.length) { listEl.innerHTML = '<span class="faint">facts はありません。</span>'; return; }
    listEl.innerHTML = rows.map((f) => `
      <div class="fact-row" data-id="${f.id}">
        <div class="fact-meta">
          <span class="fact-id">#${f.id}</span>
          <span class="chip ${f.status === "verified" ? "ok" : "warn"}">${esc(f.status)}</span>
          <span class="fact-id">${esc(String(f.created_at || "").slice(0, 10))}<br>${esc(f.created_by || "")}</span>
        </div>
        <div class="fact-body">${esc(f.content)}</div>
        <div class="fact-actions">
          <button class="btn mini ghost act-edit">修正</button>
          <button class="btn mini danger act-retire">撤去</button>
        </div>
      </div>`).join("");

    listEl.querySelectorAll(".fact-row").forEach((row) => {
      const id = Number(row.dataset.id);
      const fact = rows.find((r) => Number(r.id) === id);
      const body = $(".fact-body", row);

      $(".act-edit", row).onclick = () => {
        if (row.classList.contains("editing")) return;
        row.classList.add("editing");
        body.innerHTML = `<div class="fact-edit">
          <textarea spellcheck="false"></textarea>
          <button class="btn mini ok-save">保存(置換 fact を作成)</button>
          <button class="btn mini ghost ok-cancel">取消</button></div>`;
        const ta = $("textarea", body);
        ta.value = fact.content;
        $(".ok-cancel", body).onclick = () => { row.classList.remove("editing"); body.textContent = fact.content; };
        $(".ok-save", body).onclick = async () => {
          try {
            await j("/api/fact", { method: "POST", body: JSON.stringify(
              { op: "replace", id, content: ta.value, project: sel }) });
            toast(`#${id} を置換しました(次回バッチで index に反映)`);
            await loadFacts(true);
          } catch (e) { toast(`失敗: ${e.message}`, 5000); }
        };
      };

      const retire = $(".act-retire", row);
      retire.onclick = async () => {
        if (!retire.dataset.armed) {
          retire.dataset.armed = "1";
          retire.textContent = "本当に撤去?";
          setTimeout(() => { retire.dataset.armed = ""; retire.textContent = "撤去"; }, 3000);
          return;
        }
        try {
          await j("/api/fact", { method: "POST", body: JSON.stringify({ op: "retire", id, project: sel }) });
          toast(`#${id} を撤去しました`);
          await loadFacts(true);
        } catch (e) { toast(`失敗: ${e.message}`, 5000); }
      };
    });
  }

  $("#factAdd", el).onclick = async () => {
    const input = $("#factNew", el);
    if (!input.value.trim()) return;
    try {
      await j("/api/fact", { method: "POST", body: JSON.stringify(
        { op: "add", project: sel, content: input.value.trim() }) });
      toast("追加しました(次回バッチで index に反映)");
      input.value = "";
      await loadFacts(true);
    } catch (e) { toast(`失敗: ${e.message}`, 5000); }
  };

  async function doSearch() {
    const q = $("#turnQ", el).value.trim();
    if (!q) return;
    const res = $("#turnResults", el);
    res.textContent = "検索中…";
    try {
      const proj = $("#turnProj", el).value;
      const rows = await j(`/api/turns?q=${encodeURIComponent(q)}${proj ? `&project=${encodeURIComponent(proj)}` : ""}`);
      res.innerHTML = rows.length ? `<table>
        <tr><th>ts</th><th>project / 発話者</th><th>内容(先頭600字)</th></tr>
        ${rows.map((r) => `<tr>
          <td class="mono faint" style="white-space:nowrap">${esc(String(r.ts || "").slice(0, 16).replace("T", " "))}</td>
          <td><span class="mono">${keyLabel(r.project_key)}</span><br>
            <span class="chip ${r.role === "user" ? "blue" : ""}">${esc(r.role)}</span>
            <span class="faint">${esc(r.device)}/${esc(r.agent)}</span></td>
          <td style="white-space:pre-wrap">${esc(r.snippet)}</td></tr>`).join("")}
      </table>` : '<span class="faint">ヒットなし。</span>';
    } catch (e) { res.textContent = `検索失敗: ${e.message}`; }
  }
  $("#turnGo", el).onclick = doSearch;
  $("#turnQ", el).onkeydown = (e) => { if (e.key === "Enter") doSearch(); };

  function drawAutoMemory() {
    const am = N.auto_memory || [];
    const box = $("#amList", el);
    if (!am.length) { box.innerHTML = '<span class="faint">スナップショットはありません。</span>'; return; }
    box.innerHTML = `<table>
      <tr><th>端末</th><th>project</th><th>ファイル</th><th>更新</th><th style="text-align:right">サイズ</th><th></th></tr>
      ${am.map((a) => `
        <tr data-id="${a.id}">
          <td class="mono">${esc(a.device)}</td>
          <td class="mono faint">${esc(a.project_key)}</td>
          <td class="mono faint">${esc(a.file_path.split("/").slice(-2).join("/"))}</td>
          <td class="mono faint" style="white-space:nowrap">${esc(String(a.file_mtime || "").slice(0, 16).replace("T", " "))}</td>
          <td class="num">${kb(a.bytes)}</td>
          <td><button class="btn mini ghost am-open">開く</button></td>
        </tr>
        <tr class="am-body" data-for="${a.id}" hidden><td colspan="6"><code class="block"></code></td></tr>`).join("")}
    </table>`;
    box.querySelectorAll(".am-open").forEach((btn) => {
      btn.onclick = async () => {
        const row = btn.closest("tr");
        const body = box.querySelector(`.am-body[data-for="${row.dataset.id}"]`);
        if (!body.hidden) { body.hidden = true; btn.textContent = "開く"; return; }
        if (!body.dataset.loaded) {
          btn.textContent = "…";
          try {
            const r = await j(`/api/auto_memory?id=${row.dataset.id}`);
            $("code", body).textContent = r.content || "(空)";
            body.dataset.loaded = "1";
          } catch (e) { $("code", body).textContent = `取得失敗: ${e.message}`; }
        }
        body.hidden = false;
        btn.textContent = "閉じる";
      };
    });
  }

  drawProjects();
  loadFacts();
  drawAutoMemory();
}

function renderSkills(el) {
  const groups = {};
  for (const s of S.skills) (groups[s.source] ??= []).push(s);
  el.innerHTML = Object.entries(groups).map(([src, list]) => `
    <h2 class="section">${esc(src)} — ${list.length} 件</h2>
    <div class="card"><table>
      <tr><th>名前</th><th>説明(SKILL.md frontmatter)</th><th style="text-align:right">サイズ</th></tr>
      ${list.map((s) => `<tr>
        <td class="mono" style="white-space:nowrap">${esc(s.name)}</td>
        <td class="muted">${esc(s.description || "—")}</td>
        <td class="num">${kb(s.bytes)}</td></tr>`).join("")}
    </table></div>`).join("");
}

function renderHooks(el) {
  const order = ["SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse",
    "PermissionRequest", "Notification", "Stop", "StopFailure", "SubagentStart",
    "SubagentStop", "PreCompact", "SessionEnd"];
  const all = [...S.hooks, ...S.codex_hooks];
  const events = [...new Set(all.map((h) => h.event))];
  events.sort((a, b) => (order.indexOf(a) === -1 ? 99 : order.indexOf(a)) - (order.indexOf(b) === -1 ? 99 : order.indexOf(b)));

  const badge = (s) => s === "applied" ? '<div class="sync-on" style="font-size:12px">適用済</div>'
    : s === "pending" ? '<div class="sync-pend" style="font-size:12px">未適用</div>'
    : s === "unsupported" ? '<div><span class="chip err">不可</span></div>' : "";
  const manifestRows = S.manifest.rows.map((r) => {
    const cell = (t) => {
      const chk = `<input type="checkbox" data-mi="${r.index}" data-target="${t}"${r.targets.includes(t) ? " checked" : ""}>`;
      const notes = (r.notes[t] || []).map((n) =>
        `<div class="faint" style="font-size:11px;margin-top:2px">⚠ ${esc(n)}</div>`).join("");
      return `<td style="text-align:center;vertical-align:top">${chk}${badge(r.state[t])}${notes}</td>`;
    };
    return `<tr>
      <td><div>${esc(r.name)}</div>
        <span class="chip mono">${esc(r.event)}</span>
        ${r.matcher ? `<span class="chip">matcher: ${esc(r.matcher)}</span>` : ""}
        ${r.if ? `<span class="chip">if: ${esc(r.if)}</span>` : ""}
        <details><summary>コマンド</summary><code class="block">${esc(r.command)}</code></details></td>
      ${cell("claude")}${cell("codex")}</tr>`;
  }).join("");

  el.innerHTML = `
    <h2 class="section">hooks-manifest(宣言的フック管理)</h2>
    <div class="note info"><span class="tag">仕組み</span><span>正本は <span class="mono">${esc(S.manifest.path)}</span>(git 配布)。チェックで対象 CLI を選び「保存して適用」すると、manifest を書き換えて両設定へ展開します(SessionStart でも自動適用)。手書き・プラグインのフックには触れません。フックの追加・文言変更は manifest を直接編集してください。</span></div>
    ${S.manifest.exists ? `<div class="card"><table>
      <tr><th>フック</th><th style="text-align:center;width:110px">Claude</th><th style="text-align:center;width:110px">Codex</th></tr>
      ${manifestRows}
    </table>
    <div class="toolrow" style="margin-top:10px"><span class="faint">Codex 側を変更した場合は次回 Codex 起動時に /hooks で信頼が必要</span>
      <span style="flex:1"></span><button class="btn mini" id="manifestApply">保存して適用</button></div>
    </div>` : `<div class="note warn"><span class="tag">未作成</span><span>manifest がありません。hooks-manifest.example.json を元に作成してください。</span></div>`}

    <div class="note info"><span class="tag">出所</span><span>settings.json(~/.claude)・各プラグインの hooks.json・Codex(~/.codex/hooks.json)をイベント別にまとめています。琥珀の枠はコンテキストに文字列を注入する hook です。</span></div>
    ${events.map((ev) => {
      const list = all.filter((h) => h.event === ev);
      return `<div class="hook-event">
        <h2 class="section">${esc(ev)} — ${list.length} 件</h2>
        <div class="card">
        ${list.map((h) => `
          <div class="hook-entry">
            <div class="hook-head">
              <span class="chip ${h.source === "settings.json" ? "" : h.source.startsWith("codex") ? "ok" : "blue"}">${esc(h.source)}</span>
              ${h.matcher ? `<span class="chip">matcher: ${esc(h.matcher)}</span>` : ""}
              ${h.condition ? `<span class="chip">if: ${esc(h.condition)}</span>` : ""}
              ${h.timeout ? `<span class="chip">timeout ${h.timeout}s</span>` : ""}
              ${h.duplicate ? '<span class="chip err">重複登録</span>' : ""}
            </div>
            ${h.injected ? `<div class="injected">${esc(h.injected)}</div>` : ""}
            <details><summary>コマンド</summary><code class="block">${esc(h.command)}</code></details>
          </div>`).join("")}
        </div></div>`;
    }).join("")}`;

  const applyBtn = $("#manifestApply", el);
  if (applyBtn) applyBtn.onclick = async () => {
    applyBtn.disabled = true;
    try {
      // チェック状態を行ごとに集約し、変わった行だけ manifest を更新してから適用
      const want = {};
      el.querySelectorAll("[data-mi]").forEach((c) => {
        (want[c.dataset.mi] ??= []).length;
        if (c.checked) (want[c.dataset.mi] ??= []).push(c.dataset.target);
        else want[c.dataset.mi] ??= [];
      });
      for (const r of S.manifest.rows) {
        const t = want[String(r.index)] || [];
        if (JSON.stringify(t) !== JSON.stringify(r.targets)) {
          await j("/api/manifest", { method: "POST",
            body: JSON.stringify({ op: "set_targets", index: r.index, targets: t }) });
        }
      }
      const rep = await j("/api/manifest", { method: "POST",
        body: JSON.stringify({ op: "apply" }) });
      const msg = `適用: 追加${rep.added.length} / 取込${rep.adopted.length} / 削除${rep.removed.length}`
        + (rep.skipped.length ? ` / スキップ${rep.skipped.length}` : "")
        + (rep.notice ? ` — ${rep.notice}` : "");
      toast(msg, 8000);
      S = await j("/api/state");
      route();
    } catch (e) {
      toast(`適用失敗: ${e.message}`, 5000);
      applyBtn.disabled = false;
    }
  };
}

function renderCollect(el) {
  el.innerHTML = `
    <h2 class="section">収集除外 sync-exclude.txt(全端末に配布・手動管理で安全に編集可)</h2>
    <div class="card">
      <div class="toolrow"><span class="faint">${esc(S.sync_exclude.path)}</span>
        <span style="flex:1"></span><button class="btn mini" id="syncSave">保存</button></div>
      <textarea class="editor" id="syncText" style="min-height:260px" spellcheck="false"></textarea>
    </div>

    <h2 class="section">NAS 夜間バッチ(crontab)</h2>
    <div class="card"><code class="block">${esc(S.crontab)}</code></div>

    <h2 class="section">バッチ実行履歴(直近10件)</h2>
    <div class="card"><table>
      <tr><th>run</th><th>開始</th><th>終了</th><th>状態</th><th style="text-align:right">turns処理</th><th style="text-align:right">index行</th></tr>
      ${N.batch_runs.map((b) => `<tr>
        <td class="num">${b.id}</td>
        <td class="mono faint">${esc(String(b.started_at || "").slice(5, 16).replace("T", " "))}</td>
        <td class="mono faint">${esc(String(b.finished_at || "").slice(5, 16).replace("T", " "))}</td>
        <td><span class="chip ${b.status === "success" ? "ok" : "err"}">${esc(b.status)}</span></td>
        <td class="num">${b.turns_processed ?? "·"}</td>
        <td class="num">${b.index_lines ?? "·"}</td></tr>`).join("")}
    </table></div>

    <h2 class="section">端末側 hook スクリプト(claude-config/hooks)</h2>
    <div class="card">${S.hook_scripts.map((h) => `<span class="chip mono">${esc(h)}</span>`).join(" ")}</div>

    <h2 class="section">claude-config リポジトリ</h2>
    <div class="card">
      <div class="faint">最新コミット: <span class="mono">${esc(S.git.last)}</span></div>
      ${S.git.status ? `<div style="margin-top:8px" class="note warn"><span class="tag">未コミット</span><code class="block" style="flex:1">${esc(S.git.status)}</code></div>`
        : '<div class="faint" style="margin-top:6px">作業ツリーはクリーンです。</div>'}
    </div>`;

  $("#syncText", el).value = S.sync_exclude.content;
  $("#syncSave", el).onclick = async () => {
    try {
      const r = await j("/api/save", { method: "POST", body: JSON.stringify(
        { target: "sync_exclude", content: $("#syncText", el).value }) });
      toast(`保存しました(${kb(r.bytes)}、.bak 退避済み)`);
    } catch (e) { toast(`保存失敗: ${e.message}`, 5000); }
  };
}

/* ---------------- router ---------------- */

const RENDER = { overview: renderOverview, context: renderContext, facts: renderFacts,
  skills: renderSkills, hooks: renderHooks, collect: renderCollect };

function route() {
  const tab = (location.hash || "#overview").slice(1);
  const name = RENDER[tab] ? tab : "overview";
  document.querySelectorAll("#nav a").forEach((a) =>
    a.classList.toggle("active", a.dataset.tab === name));
  $("#topbarTitle").textContent = TABS[name];
  const content = $("#content");
  const pane = document.createElement("div");
  pane.className = "pane";
  content.replaceChildren(pane);
  RENDER[name](pane);
}

async function boot() {
  $("#content").innerHTML = '<div class="faint">読み込み中…</div>';
  try {
    [S, N] = await Promise.all([j("/api/state"), j("/api/nas")]);
    if (!N.auto_memory) N = await j("/api/nas?refresh=1");  // 旧キャッシュ対策
  } catch (e) {
    $("#content").innerHTML = `<div class="note warn"><span class="tag">起動失敗</span><span>${esc(e.message)}</span></div>`;
    return;
  }
  $("#nasStamp").textContent = `NAS取得 ${N.fetched_at}`;
  $("#railFoot").textContent =
    `model ${S.settings.model || "—"}\nautoMemory ${S.settings.autoMemoryEnabled ? "on" : "off"}`;
  route();
}

$("#refreshNas").onclick = async () => {
  toast("NAS から再取得中…", 8000);
  try {
    N = await j("/api/nas?refresh=1");
    factsCache = {};
    $("#nasStamp").textContent = `NAS取得 ${N.fetched_at}`;
    toast("NAS データを更新しました");
    route();
  } catch (e) { toast(`更新失敗: ${e.message}`, 5000); }
};

window.addEventListener("hashchange", route);
boot();
