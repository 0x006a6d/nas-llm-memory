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

// パイプライン順: 収集 → 記録 → 編さん(スキル/Hooks) → 配付(例規/routing) → 引継
// タブ名は公文書事務の語に揃える(スキル/Hooks は固有名詞なのでそのまま)
const TABS = {
  overview: "文書事務概況",
  collect: "収受簿",
  facts: "現用文書",
  shelf: "書庫 (決裁済)",
  kanribo: "管理簿",
  skills: "スキル",
  hooks: "Hooks",
  context: "例規 (常用文書)",
  routing: "配付先",
  messages: "事務引継",
  usage: "予算執行",
};

// fact の検証状態の表示ラベル(DB 値は verified/unverified のまま変えない)
const FACT_STATUS = { verified: "検証済", unverified: "未検証" };
const factStatusLabel = (s) => FACT_STATUS[s] || String(s ?? "");

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
    { label: "general 例規", note: "メモリ本文", bytes: general ? general.bytes : 0, color: "var(--amber)" },
    { label: "hook 注入", note: "作業規律ほか", bytes: inj, color: "var(--blue)" },
    { label: "CLAUDE.md", note: "@include 行", bytes: S.claude_md.bytes, color: "var(--slate)" },
  ];
}

/* batch_runs の notes からジョブ種別を判定する。
   running/success/failed のどの段階でも notes 先頭に種別が残る前提
   (nightly は running='P2'・success='inserted=…'・failed='P2 FAILED: …')。
   旧形式の失敗row(エラー文のみ)は nightly に落ちる。 */
function batchKind(notes) {
  const s = String(notes || "");
  if (s.startsWith("backfill-distill")) return "backfill";
  if (s.startsWith("skill-scout-init") || s.startsWith("watermark-init")) return "init";
  if (s.startsWith("skill-scout")) return "skill-scout";
  if (s.startsWith("edges")) return "edges";
  if (s.startsWith("compact")) return "compact";
  return "nightly";
}

/* 夜間バッチの健全性チェック。失敗した・止まっている・そもそも記録が無い、を注意欄に出す。
   ロック敗退や cron 起動失敗は batch_runs に row を残さないため、
   「最新が failed」だけでなく「記録なし」「成功が古い」も欠測のシグナルとして扱う。 */
function batchWarnings() {
  const runs = (N.batch_runs || []).filter((b) => batchKind(b.notes) !== "init");
  const out = [];
  const now = Date.now();
  const jobs = [
    { kind: "nightly", label: "nightly(編さん)", staleH: 30 },
    // backfill は移行期間のみのジョブ。crontab から外した後の欠測は正常なので staleness は見ない
    { kind: "backfill", label: "backfill", staleH: null },
    { kind: "edges", label: "edges(補足関係)", staleH: 30 },
    { kind: "skill-scout", label: "skill-scout(スキル候補)", staleH: 30 },
    { kind: "compact", label: "compact(週次統合)", staleH: 8 * 24 },
  ];
  for (const j of jobs) {
    const rows = runs.filter((b) => batchKind(b.notes) === j.kind);
    if (!rows.length) {
      // backfill(staleH=null)は移行期間のみのジョブ:
      // crontabから外れて記録が窓から消えた後の「記録なし」は正常なので警告しない
      if (j.staleH != null) out.push({ kind: "warn", tag: "バッチ未記録",
        text: `${j.label}: batch_runs に実行記録がありません。cron から起動できていない可能性があります(NAS の /volume2/claude-system/batch/*.log を確認)。` });
      continue;
    }
    const last = rows[0];
    const lastNotes = String(last.notes || "");
    if (last.status === "failed" && !lastNotes.includes("dry-run")) {
      out.push({ kind: "warn", tag: "バッチ失敗",
        text: `${j.label}: 最新 run ${last.id} が失敗 — ${lastNotes.slice(0, 160) || "(メモなし)"}` });
    } else if (last.status === "running" &&
               now - Date.parse(last.started_at) > 12 * 3600 * 1000) {
      out.push({ kind: "warn", tag: "バッチ滞留",
        text: `${j.label}: run ${last.id} が12時間以上 running のままです(途中死の可能性。ログを確認して手動再実行)。` });
    }
    if (j.staleH != null) {
      const okRow = rows.find((b) => b.status === "success");
      if (!okRow) {
        out.push({ kind: "warn", tag: "成功実績なし",
          text: `${j.label}: 直近${runs.length}件の記録に成功がありません。` });
      } else if (now - Date.parse(okRow.finished_at) > j.staleH * 3600 * 1000) {
        out.push({ kind: "warn", tag: "バッチ停止?",
          text: `${j.label}: 最終成功は run ${okRow.id}(${String(okRow.finished_at).slice(0, 16).replace("T", " ")}Z)。それ以降成功していません。` });
      }
    }
  }
  return out;
}

function warnings() {
  const out = [];
  out.push(...batchWarnings());
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
      text: `${m.key} の例規(index.md)が ${kb(m.bytes)}。Codex 既定の project_doc_max_bytes (32KiB) を超えると連結合計で黙って打ち切られます(この MacBook は 64KiB に拡大済)。` });
  }
  const sc = S.skill_candidates || [];
  if (sc.length) out.push({ kind: "info", tag: "スキル候補",
    text: `未採用のスキル候補が ${sc.length} 件あります(スキルタブで確認。採用するときはセッションで「◯◯ を採用して」)。` });
  if (N.shelf_pending) out.push({ kind: "info", tag: "後閲待ち",
    text: `後閲待ちの決裁文書が ${N.shelf_pending} 件あります(書庫タブで後閲印または差し戻し)。` });
  const sy = (S.spool || {}).sync;
  if (sy && sy.failures >= 2) out.push({ kind: "warn", tag: "設定同期",
    text: `この端末の設定同期(git pull)が ${sy.failures} 回続けて失敗しています` +
      (sy.days_since_success !== null && sy.days_since_success !== undefined
        ? `(最後に成功したのは ${sy.days_since_success} 日前)` : "(成功記録なし)") +
      `。配布された hooks・skills・記憶 index が古いままです: ${esc(sy.last_error || "")}` });
  if (N.kanribo && N.kanribo.manryou) out.push({ kind: "warn", tag: "満了",
    text: `保存期間が満了した行政文書ファイルが ${N.kanribo.manryou} 件あります(管理簿タブで確認。夜間バッチが廃棄伺い・移管として起票します)。` });
  if (N.shelf_miketsu) out.push({ kind: "info", tag: "未決",
    text: `決裁が付かず繰越中の文書が ${N.shelf_miketsu} 件あります(翌晩のバッチが再審理します。書庫タブの「未決(繰越中)」で内容を確認できます)。` });
  const B = S.builtin || {};
  if (B.captured_with && B.current_version && B.captured_with !== B.current_version) {
    out.push({ kind: "info", tag: "内蔵一覧が古い",
      text: `内蔵スキル一覧のスナップショットは claude ${B.captured_with} 時点、現在は ${B.current_version} です。セッションで /context を実行して dashboard/builtin-context.json を更新してください。` });
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

  const cands = (S.skill_candidates || []).length;
  const routedN = Object.values((S.routing && S.routing.parsed) || {})
    .reduce((a, e) => a + ((e && e.projects) || []).length, 0);

  el.innerHTML = `
    <div class="numhd"><span class="no">00</span><span class="lb">全体の流れ — このシステムがやっていること</span></div>
    <div class="pipeline">
      <a class="pstage" href="#collect">
        <div class="pt">収受</div>
        <div class="pd">全端末の claude/codex セッションを hook が spool に書き、NAS の turns(生ログ)へ送る</div>
        <div class="pn">${num(turnsTotal)} turns</div>
      </a><span class="parrow">→</span>
      <a class="pstage" href="#facts">
        <div class="pt">編さん</div>
        <div class="pd">夜間バッチ(03:00)が会話から恒久的な事実(facts)を抽出し、繰り返し作業をスキル候補として発掘</div>
        <div class="pn">${num(factsTotal)} facts · 候補 ${cands}</div>
      </a><span class="parrow">→</span>
      <a class="pstage" href="#context">
        <div class="pt">例規 生成</div>
        <div class="pd">facts からプロジェクト別の例規(index.md)を全再生成(配布物。手動編集は翌バッチで上書き)</div>
        <div class="pn">${S.memory_indexes.length} 例規</div>
      </a><span class="parrow">→</span>
      <a class="pstage" href="#routing">
        <div class="pt">配付・注入</div>
        <div class="pd">routing 宣言に従い端末×プロジェクトのセッション冒頭へ注入。general は全端末・毎セッション</div>
        <div class="pn">${routedN} 宣言</div>
      </a>
    </div>
    <div class="note info"><span class="tag">直し方</span><span>恒久的に直す → 「現用文書」タブで facts を修正。繰り返し作業を固定化 → スキル/Hooks タブ。後で見返す会話 → 現用文書タブでフラグ。例規の直接編集は翌バッチで上書きされる一時措置です。</span></div>

    <div class="numhd"><span class="no">01</span><span class="lb">毎セッション注入されるコンテキスト</span></div>
    <div class="budget-total">
      <span class="kb">${(total / 1024).toFixed(1)}<small>KB</small></span>
      <span class="faint">プロジェクト内ではそのプロジェクトの例規が追加</span>
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
        <div class="n ${!lastBatch || lastBatch.status === "running" ? "" : lastBatch.status === "success" ? "ok" : "warn"}">${lastBatch ? esc(lastBatch.status) : "—"}</div>
        <div class="l">直近バッチ (run ${lastBatch ? lastBatch.id : "—"} ${lastBatch ? esc(batchKind(lastBatch.notes)) : ""})</div>
        <div class="s">${lastBatch ? esc(String(lastBatch.finished_at || lastBatch.started_at || "").slice(5, 16).replace("T", " ")) : ""}</div>
      </div>
    </div>

    <div class="numhd"><span class="no">03</span><span class="lb">注意</span></div>
    ${warns.length ? warns.map((w) => `<div class="note ${w.kind}"><span class="tag">${esc(w.tag)}</span><span>${esc(w.text)}</span></div>`).join("")
      : '<div class="note info"><span class="tag">OK</span><span>検出された問題はありません。</span></div>'}

    <div class="numhd"><span class="no">04</span><span class="lb">プロジェクト別の蓄積</span></div>
    <div class="card">
      <table>
        <tr><th>project_key<span class="faint" style="font-weight:400">(タグ=主に使う端末。クリックで配付先タブへ)</span></th><th style="text-align:right">turns</th><th style="text-align:right">facts</th><th>最終収受</th></tr>
        ${N.turns_by_project.map((r) => {
          const f = N.facts_by_project.find((x) => x.project_key === r.project_key ||
            x.project_key === keyToIndexDir(r.project_key));
          return `<tr><td class="mono"><a class="plink" href="#routing" title="配付先タブで、このプロジェクトの例規(index.md)がどの端末に注入されるかを確認・変更">${keyLabel(r.project_key)}</a></td>
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
  // 文脈により munged 形(先頭の'-'が落ちた形)でも来るので両方照合する。
  // index ディレクトリ名は munged+末尾ハッシュ(github.com-…-bloclist-fe6ac891)の
  // 形でも来るため、実キーの munged 形を前方一致で照合するフォールバックを持つ
  // 前方一致は最長のものを採る(短いキーが並び順で先に当たると別端末を引く)
  const hit = list.find((d) => d.project_key === key) ||
    list.find((d) => d.project_key === `-${key}`) ||
    list.filter((d) => d.project_key)
      .map((d) => [keyToIndexDir(d.project_key), d])
      .sort((a, b) => b[0].length - a[0].length)
      .find(([dir]) => dir && key.startsWith(dir))?.[1];
  return hit ? hit.device : null;
}

function keyLabel(key) {
  const dev = deviceOf(key);
  return `${esc(key)}${dev ? ` <span class="devtag" title="このプロジェクトを主に使っている端末(会話履歴 turns からの推定)">${esc(dev)}</span>` : ""}`;
}

/* textarea / contenteditable に行番号ガターを付ける。折返しがあっても論理行の先頭位置に
   番号が揃うよう、同一スタイルの不可視ミラーで各行の描画高さを実測する。 */
function attachLineNumbers(ta, gutter) {
  const getText = () => (ta.tagName === "TEXTAREA" ? ta.value : ta.textContent);
  const mirror = document.createElement("div");
  mirror.className = "ln-mirror";
  ta.parentElement.appendChild(mirror);
  function update() {
    const cs = getComputedStyle(ta);
    for (const p of ["fontFamily", "fontSize", "fontWeight", "lineHeight", "letterSpacing",
                     "paddingTop", "paddingRight", "paddingBottom", "paddingLeft",
                     "borderTopWidth", "borderRightWidth", "borderBottomWidth", "borderLeftWidth",
                     "boxSizing", "whiteSpace", "wordBreak", "overflowWrap", "tabSize"]) {
      mirror.style[p] = cs[p];
    }
    mirror.style.width = `${ta.clientWidth}px`;
    const lines = getText().split("\n");
    mirror.replaceChildren(...lines.map((l) => {
      const d = document.createElement("div");
      d.textContent = l || " ";
      return d;
    }));
    const html = [...mirror.children].map((c, i) =>
      `<div class="ln" style="top:${c.offsetTop}px">${i + 1}</div>`).join("");
    gutter.innerHTML = `<div class="ln-inner">${html}</div>`;
    sync();
  }
  function sync() {
    const inner = gutter.firstElementChild;
    if (inner) inner.style.transform = `translateY(${-ta.scrollTop}px)`;
  }
  ta.addEventListener("input", update);
  ta.addEventListener("scroll", sync, { passive: true });
  new ResizeObserver(update).observe(ta);
  update();
}

/* Codex への例規配布物の状態表(例規タブ)。Codex には @include 構文が
   無いため、agents_sync.py(sender 実行時)が例規をファイルに直接展開する。
   グローバル ~/.codex/AGENTS.md は上のファイル一覧に CLAUDE.md と同格で出すので、
   ここはプロジェクト別の AGENTS.override.md の生成状態だけを扱う。 */
function codexAgentsHtml() {
  const C = S.codex_agents;
  if (!C) return "";
  const cell = (st) => {
    if (!st) return '<span class="chip err">なし</span>';
    return `<span class="mono">${kb(st.bytes)}</span> <span class="mono faint">更新 ${esc(st.mtime)}</span>`;
  };
  return `
    <h2 class="section">Codex 側配布(プロジェクト別 AGENTS.override.md)</h2>
    <div class="note info"><span class="tag">仕組み</span><span>Codex には @include 構文が無いため、hooks/agents_sync.py(sender 実行時 = SessionStart+毎時)が記憶の例規(index.md)をファイルに直接展開して配布します。グローバルは ~/.codex/AGENTS.md のマーカー区切り管理セクション(上のファイル一覧で本文を確認できます)、プロジェクトは「手書き AGENTS.md 全文 + そのプロジェクトの例規」を結合した AGENTS.override.md(git 追跡外)。ここは生成状態の確認のみで、編集は手書き AGENTS.md か「現用文書」へ。プロジェクトの登録は <span class="mono">agents_sync.py register</span>(一覧: ${esc(C.registry_path)})。</span></div>
    <div class="card"><table>
      <tr><th>対象</th><th>手書き AGENTS.md</th><th>配布物</th></tr>
      ${C.projects.length ? C.projects.map((p) => `<tr>
        <td class="mono">${esc(p.dir)}</td>
        <td>${p.agents ? `<span class="mono">${kb(p.agents.bytes)}</span>` : '<span class="chip warn">なし</span>'}</td>
        <td>${p.override ? `<span class="chip ok">AGENTS.override.md</span> ${cell(p.override)}` : '<span class="chip err">未生成</span>'}</td>
      </tr>`).join("") : '<tr><td colspan="3" class="faint">登録プロジェクトはありません。</td></tr>'}
    </table></div>`;
}

function renderContext(el) {
  // グローバルの手引き2本(Claude の CLAUDE.md / Codex の AGENTS.md)は同格。
  // どちらも自動生成物なのでここでは読み取り専用(target 無し=保存ボタン無し)。
  const cg = (S.codex_agents || {}).global;
  const files = [
    { key: "CLAUDE.md", target: null, bytes: S.claude_md.bytes,
      content: S.claude_md.content, note: "~/.claude/CLAUDE.md — @include の起点(読み取り専用)", mtime: "" },
    { key: "AGENTS.md", target: null, bytes: cg ? cg.bytes : 0,
      content: cg ? (cg.content || "") : "",
      note: cg
        ? `~/.codex/AGENTS.md — 管理セクションは例規から自動展開(読み取り専用)${cg.managed ? "" : " ※管理セクション未展開"}`
        : "~/.codex/AGENTS.md — 未生成(読み取り専用)",
      mtime: cg ? cg.mtime : "" },
    ...S.memory_indexes.map((m) => ({
      key: m.key, target: `index:${m.key}`, bytes: m.bytes, content: m.content,
      auto: m.auto_generated, mtime: m.mtime,
      note: `${m.path}(更新 ${m.mtime})`,
    })),
  ];
  el.innerHTML = `
    <div class="note warn"><span class="tag">前提</span><span>例規(index.md)は夜間バッチ(03:00)が current_facts から全再生成します。ここでの直接編集は即座に反映されますが翌バッチで上書きされます。恒久的に直したい内容は「現用文書」タブで facts を修正してください。</span></div>
    <div class="note info"><span class="tag">凡例</span><span>一覧は claude-config/memory/ 配下の全端末・全プロジェクト分の実ファイルで、<b>全部が読み込まれるわけではありません</b>。1セッションに注入されるのは「CLAUDE.md(Codex は AGENTS.md)+ general」と、そのプロジェクトで開いたときのそのプロジェクトの例規 1本だけ(routing 宣言に従う)。<span class="chip amber">auto</span> = 夜間バッチが再生成するファイル。<span class="devtag">端末名</span> = そのプロジェクトを主に使っている端末(会話履歴 turns からの推定)。</span></div>
    <div class="split" style="margin-top:14px">
      <div class="card filelist" id="ctxList"></div>
      <div class="card" id="ctxEditor"></div>
    </div>
    ${codexAgentsHtml()}`;

  const list = $("#ctxList", el);
  const editor = $("#ctxEditor", el);
  let sel = files.find((f) => f.key === "general") || files[0];

  function drawList() {
    // 「このセッションに入るか」で分ける: 毎セッション注入(CLAUDE.md+general) /
    // この端末のプロジェクト(開いたときだけ注入) / 他端末(この端末には注入されない)
    const localDev = (S.routing || {}).local_device || "";
    const groups = [
      { label: "毎セッション注入(この端末の全セッション)", items: [] },
      { label: `この端末${localDev ? `(${localDev})` : ""}のプロジェクト — 開いたときだけ注入`,
        items: [] },
      { label: "他端末のプロジェクト — この端末には注入されない", items: [] },
      { label: "帰属不明(turns 実績なし)", items: [] },
    ];
    for (const f of files) {
      if (f.key === "CLAUDE.md" || f.key === "AGENTS.md" || f.key === "general") groups[0].items.push(f);
      else {
        const dev = deviceOf(f.key);
        groups[dev === localDev ? 1 : dev ? 2 : 3].items.push(f);
      }
    }
    const btn = (f) => `
      <button class="${f === sel ? "sel" : ""}" data-k="${esc(f.key)}">
        <span>${keyLabel(f.key)}${f.auto ? ' <span class="chip amber" title="夜間バッチ生成">auto</span>' : ""}</span>
        <span class="kb">${kb(f.bytes)}</span>
      </button>`;
    list.innerHTML = groups.filter((g) => g.items.length).map((g) =>
      `<div class="grp">${esc(g.label)}</div>${g.items.map(btn).join("")}`).join("");
    list.querySelectorAll("button").forEach((b) => {
      b.onclick = () => { sel = files.find((f) => f.key === b.dataset.k); drawList(); drawEditor(); };
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
      <div class="editor-wrap">
        <div class="editor-gutter" id="ctxGutter" aria-hidden="true"></div>
        <textarea class="editor" id="ctxText" ${readonly ? "readonly" : ""} spellcheck="false"></textarea>
      </div>`;
    const ta = $("#ctxText", editor);
    ta.value = sel.content;
    attachLineNumbers(ta, $("#ctxGutter", editor));
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
    <div class="note info"><span class="tag">用語</span><span><b>turns</b> = 全端末・全エージェント(claude/codex)の生の発話ログ(1発話=1行、NAS に蓄積)。<b>facts</b> = そこから整理・登載された恒久的な事実。この画面に出るのは current_facts(撤去済みを除いた、生きている facts だけ)です。</span></div>
    <div class="note info"><span class="tag">正道</span><span>ここが恒久的なコンテキスト調整の場所です。facts への追加・修正・撤去は、次回の夜間バッチ(03:00)で各プロジェクトの例規(index.md)に反映されます。</span></div>
    <div class="toolrow" id="factProjects" style="margin-top:14px"></div>
    <div class="card" style="margin-bottom:14px">
      <div class="toolrow" style="margin-bottom:0">
        <span class="lnfield"><span class="ln1" aria-hidden="true">1</span><input type="text" id="factNew" placeholder="新しい事実を1行で(選択中のプロジェクトに追加)"></span>
        <button class="btn mini" id="factAdd">追加</button>
      </div>
    </div>
    <div class="card" id="factList" style="max-height:62vh;overflow-y:auto">読み込み中…</div>
    <h2 class="section">重要フラグ付きの会話</h2>
    <div class="note info"><span class="tag">用途</span><span>後で見返したい・skill/hook 化の種になりそうな会話に印を付けて固定表示します。付与・解除は下の全文検索結果の ☆/★ から。フラグは NAS に保存され全端末で共通です(turns 本体は変更しません)。</span></div>
    <div class="card" id="flagList">読み込み中…</div>
    <h2 class="section">turns 全文検索(PGroonga = NAS 上の全文検索。全端末・全エージェントの発話ログが対象)</h2>
    <div class="card">
      <div class="toolrow">
        <span class="lnfield"><span class="ln1" aria-hidden="true">1</span><input type="text" id="turnQ" placeholder="発話ログを検索…"></span>
        <select id="turnProj"><option value="">全プロジェクト</option>
          ${N.turns_by_project.map((r) => {
            const dev = deviceOf(r.project_key);
            return `<option value="${esc(r.project_key)}">${esc(r.project_key)}${dev ? `〈${esc(dev)}〉` : ""}</option>`;
          }).join("")}</select>
        <label class="faint" style="white-space:nowrap"><input type="checkbox" id="turnFlagged"> フラグ付き会話のみ</label>
        <button class="btn mini ghost" id="turnGo">検索</button>
      </div>
      <div id="turnResults" class="faint">キーワードを入れて検索してください。</div>
    </div>

    <h2 class="section">auto memory スナップショット — 各端末の内蔵メモリ(MEMORY.md 等)の取り込み履歴。夜間バッチが例規との食い違い時の参考に使う補助データ</h2>
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
          <span class="chip ${f.status === "verified" ? "ok" : "warn"}" title="fact の検証状態。検証済 = 事実として確定。それ以外はバッチが自動抽出した未確定情報">${esc(factStatusLabel(f.status))}</span>
          <span class="fact-id">${esc(String(f.created_at || "").slice(0, 10))}<br>${esc(f.created_by || "")}</span>
        </div>
        <div class="fact-field">
          <div class="editor-gutter fact-gutter" aria-hidden="true"></div>
          <div class="fact-body" contenteditable="plaintext-only" spellcheck="false"
               title="クリックしてそのまま編集できます。変えると保存/取消が出ます(保存 = 旧 fact を置き換える新 fact を作成。系譜は replaces 列に残る)">${esc(f.content)}</div>
        </div>
        <div class="fact-actions">
          <button class="btn mini ok-save" hidden title="Cmd+Enter でも保存">保存(置換)</button>
          <button class="btn mini ghost ok-cancel" hidden title="Esc でも取消">取消</button>
          <button class="btn mini danger act-retire" title="この fact を撤去する(行は消えるが履歴としては残る)">撤去</button>
        </div>
      </div>`).join("");

    listEl.querySelectorAll(".fact-row").forEach((row) => {
      const id = Number(row.dataset.id);
      const fact = rows.find((r) => Number(r.id) === id);
      const body = $(".fact-body", row);
      const save = $(".ok-save", row);
      const cancel = $(".ok-cancel", row);
      attachLineNumbers(body, $(".fact-gutter", row));

      // 修正ボタン無しの直接編集: 内容が変わったときだけ保存/取消を出す
      // (保存時と同じ trim 済みで比較し、空白だけの変化は変更扱いにしない)
      const dirty = () => body.textContent.trim() !== fact.content.trim();
      const refresh = () => { save.hidden = cancel.hidden = !dirty(); row.classList.toggle("editing", dirty()); };
      body.oninput = refresh;
      body.onkeydown = (e) => {
        if (e.key === "Escape") { cancel.click(); body.blur(); }
        if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) { e.preventDefault(); save.click(); }
      };
      cancel.onclick = () => { body.textContent = fact.content; refresh(); };
      save.onclick = async () => {
        const content = body.textContent.trim();
        if (!content || !dirty()) return;
        try {
          await j("/api/fact", { method: "POST", body: JSON.stringify(
            { op: "replace", id, content, project: sel }) });
          toast(`#${id} を置換しました(次回バッチで例規に反映)`);
          await loadFacts(true);
        } catch (e) { toast(`失敗: ${e.message}`, 5000); }
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
      toast("追加しました(次回バッチで例規に反映)");
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
      const fl = $("#turnFlagged", el).checked;
      const rows = await j(`/api/turns?q=${encodeURIComponent(q)}${proj ? `&project=${encodeURIComponent(proj)}` : ""}${fl ? "&flagged=1" : ""}`);
      res.innerHTML = rows.length ? `<table>
        <tr><th></th><th>ts</th><th>project / 発話者</th><th>内容(先頭600字)</th></tr>
        ${rows.map((r) => `<tr>
          <td><button class="btn mini ghost turn-flag" data-sid="${esc(r.session_id)}"
                title="この発話を含む会話(セッション)全体に重要フラグを付ける/外す。フラグ付き会話は下の一覧に固定表示されます"
                >${r.flagged ? "★" : "☆"}</button></td>
          <td class="mono faint" style="white-space:nowrap">${esc(String(r.ts || "").slice(0, 16).replace("T", " "))}</td>
          <td><span class="mono">${keyLabel(r.project_key)}</span><br>
            <span class="chip ${r.role === "user" ? "blue" : ""}">${esc(r.role)}</span>
            <span class="faint">${esc(r.device)}/${esc(r.agent)}</span></td>
          <td style="white-space:pre-wrap">${esc(r.snippet)}</td></tr>`).join("")}
      </table>` : '<span class="faint">ヒットなし。</span>';
      res.querySelectorAll(".turn-flag").forEach((btn) => {
        btn.onclick = async () => {
          const on = btn.textContent === "★";
          let note = "";
          if (!on) {
            note = prompt("フラグのメモ(任意。何が重要だったか一言)") ?? "";
          }
          try {
            await j("/api/flag", { method: "POST", body: JSON.stringify(
              { op: on ? "remove" : "add", session_id: btn.dataset.sid, note }) });
            res.querySelectorAll(`.turn-flag[data-sid="${CSS.escape(btn.dataset.sid)}"]`)
              .forEach((b) => { b.textContent = on ? "☆" : "★"; });
            loadFlags();
          } catch (e) { toast(`失敗: ${e.message}`, 5000); }
        };
      });
    } catch (e) { res.textContent = `検索失敗: ${e.message}`; }
  }
  $("#turnGo", el).onclick = doSearch;
  $("#turnQ", el).onkeydown = (e) => { if (e.key === "Enter") doSearch(); };

  async function loadFlags() {
    const box = $("#flagList", el);
    try {
      const rows = await j("/api/flags");
      if (!rows.length) {
        box.innerHTML = '<span class="faint">フラグ付きの会話はまだありません。検索結果の ☆ で付けられます。</span>';
        return;
      }
      box.innerHTML = `<table>
        <tr><th></th><th>開始</th><th>project / 端末</th><th>会話の冒頭</th><th>メモ</th><th style="text-align:right">発話数</th></tr>
        ${rows.map((r) => `<tr>
          <td><button class="btn mini ghost flag-del" data-sid="${esc(r.session_id)}" title="フラグを外す">★</button></td>
          <td class="mono faint" style="white-space:nowrap">${esc(String(r.first_ts || "").slice(0, 16).replace("T", " "))}</td>
          <td><span class="mono">${keyLabel(r.project_key || "")}</span><br><span class="faint">${esc(r.device || "?")}/${esc(r.agent || "?")}</span></td>
          <td style="white-space:pre-wrap">${esc(r.head || "(冒頭を取得できません)")}</td>
          <td class="muted">${esc(r.note || "—")}</td>
          <td class="num">${r.n ?? "?"}</td></tr>`).join("")}
      </table>`;
      box.querySelectorAll(".flag-del").forEach((btn) => {
        btn.onclick = async () => {
          try {
            await j("/api/flag", { method: "POST", body: JSON.stringify(
              { op: "remove", session_id: btn.dataset.sid, note: "" }) });
            // 検索結果側の★表示も同期する
            el.querySelectorAll(`.turn-flag[data-sid="${CSS.escape(btn.dataset.sid)}"]`)
              .forEach((b) => { b.textContent = "☆"; });
            loadFlags();
          } catch (e) { toast(`失敗: ${e.message}`, 5000); }
        };
      });
    } catch (e) { box.textContent = `取得失敗: ${e.message}`; }
  }

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
  loadFlags();
  drawAutoMemory();
}

/* ---------------- 管理簿(行政文書ファイル管理簿。公文書管理法7条) ---------------- */

const KANRIBO_STATE = { genyou: ["現用", ""], manryou: ["満了", "warn"],
  haiki_zumi: ["廃棄済", "err"], ikan_zumi: ["移管済", "ok"] };
const KANRIBO_MEASURE = { haiki: "廃棄", ikan: "移管", jouyou: "常用" };

function renderKanribo(el) {
  const filt = renderKanribo._filt || "genyou";
  el.innerHTML = `
    <div class="note info"><span class="tag">仕組み</span><span>収集した記録は<b>分類・名称・保存期間・満了する日・満了時の措置</b>を与えて「行政文書ファイル」(集合物)にまとめ、この管理簿に載ります(整理)。措置は満了より前に決めておきます(レコードスケジュール)。満了したファイルは<b>廃棄伺い</b>または<b>移管</b>として書庫に起票され、決裁を経て実行されます。ここは閲覧のみです。</span></div>
    <h2 class="section">標準文書保存期間基準(規程)</h2>
    <div class="card" id="kanriboRules">読み込み中…</div>
    <h2 class="section">行政文書ファイル管理簿</h2>
    <div class="toolrow" id="kanriboFilters">
      ${[["genyou", "現用"], ["manryou", "満了・満了予定"], ["sumi", "廃棄済・移管済"], ["all", "全件"]].map(([v, l]) =>
        `<button class="btn mini${filt === v ? "" : " ghost"}" data-f="${v}">${l}</button>`).join("")}
    </div>
    <div class="card" id="kanriboList">読み込み中…</div>`;

  el.querySelectorAll("#kanriboFilters .btn").forEach((b) => {
    b.onclick = () => { renderKanribo._filt = b.dataset.f; renderKanribo(el); };
  });

  (async () => {
    const rulesEl = $("#kanriboRules", el);
    const listEl = $("#kanriboList", el);
    try {
      const d = await j(`/api/kanribo?filter=${encodeURIComponent(filt)}`);
      const rules = d.rules || [];
      rulesEl.innerHTML = rules.length ? `<table>
        <tr><th>大分類</th><th>実体</th><th>保存期間</th><th>満了時の措置</th><th>施行</th><th>適用</th><th>備考</th></tr>
        ${rules.map((r) => `<tr>
          <td class="mono">${esc(r.category)}</td>
          <td class="mono faint">${esc(r.source_table)}</td>
          <td>${r.retention_years ? `${r.retention_years}年` : r.retention_days ? `${r.retention_days}日` : "常用"}</td>
          <td>${esc(KANRIBO_MEASURE[r.measure] || r.measure)}</td>
          <td>${r.gate === "sokujiko" ? "決裁で施行" : "後閲印が条件"}</td>
          <td>${r.enabled ? '<span class="chip ok">適用中</span>' : '<span class="chip">未適用</span>'}</td>
          <td class="faint">${esc(r.note || "")}</td>
        </tr>`).join("")}</table>` : '<span class="faint">規程がありません(015 未適用)。</span>';

      const rows = d.list || [];
      if (!rows.length) { listEl.innerHTML = '<span class="faint">該当するファイルはありません。</span>'; return; }
      listEl.innerHTML = `<table>
        <tr><th>名称(小分類)</th><th>大分類</th><th>project</th><th>期間</th><th>保存期間</th>
            <th>満了する日</th><th>措置</th><th style="text-align:right">件数</th><th>保存場所</th><th>状態</th></tr>
        ${rows.map((r) => `<tr>
          <td>${esc(r.name)}</td>
          <td class="mono faint">${esc(r.category)}</td>
          <td class="mono faint">${esc(r.project_key)}</td>
          <td class="mono">${esc(r.period)}</td>
          <td>${esc(r.retention_disp || "")}</td>
          <td class="mono">${r.expires_on ? esc(String(r.expires_on).slice(0, 10)) : "—"}
            ${r.days_left !== null && r.days_left !== undefined
              ? `<span class="faint">${r.days_left <= 0 ? "(満了)" : `(あと${r.days_left}日)`}</span>` : ""}</td>
          <td>${esc(KANRIBO_MEASURE[r.measure] || r.measure)}</td>
          <td style="text-align:right" class="mono">${num(r.n_rows)}</td>
          <td class="mono faint">${esc(r.location)}</td>
          <td>${chipOf(KANRIBO_STATE, r.state)}</td>
        </tr>`).join("")}</table>`;
    } catch (e) { listEl.textContent = `取得失敗: ${e.message}`; }
  })();
}

/* ---------------- 書庫(起案・決裁文書) ---------------- */

const SHELF_STATE = {
  pending_review: ["審査中", "blue"], remanded_to_drafter: ["補正中", "warn"],
  pending_decision: ["決裁待ち", "blue"], remanded_to_reviewer: ["再審査中", "warn"],
  approved: ["決裁済(施行前)", "blue"], executed: ["施行済", "ok"],
  rejected: ["廃案", "err"], reexamine: ["再審理待ち", "warn"],
};
const SHELF_KIND = { fact: "facts登載", index: "index改定", skill: "skill登載", saishinri: "再審理",
  haiki: "廃棄", ikan: "移管", tenken: "管理状況報告" };
const SHELF_SEEN = { pending: ["後閲待ち", "warn"], seen: ["後閲済", "ok"], remanded: ["差し戻し", "err"] };
// 後閲は完結した文書にだけ意味がある(未決・審査中の文書は後閲待ちではない)
const seenChip = (d) => (["executed", "rejected", "approved"].includes(d.state)
  ? chipOf(SHELF_SEEN, d.seen_state) : '<span class="faint">—</span>');
const SHELF_ACTION = { kian: "起案", hosei: "補正", shinsa_ok: "審査済(専決)", joshin: "上申",
  sashimodoshi: "差し戻し", kessai_ok: "決裁", hiketsu: "否決", shiko: "施行",
  kouetsu: "後閲", saishinri: "再審理", kurikoshi: "未決繰越", skill_mv: "git移動" };
// 文書番号の表示形式はサーバが付ける(表記規則の正は ringi.display_doc_no。
// 令和元年度の扱いをここに重複させない)
const docNoDisp = (r) => r.doc_no_disp || r.doc_no;
/* 設定同期(SessionStartのgit pull)の状態。失敗を見えるようにする */
function syncCell(sy) {
  if (!sy) return '<span class="faint">記録なし(次のセッションから記録されます)</span>';
  const last = sy.last_success_at ? String(sy.last_success_at).replace("T", " ") : "—";
  const age = (sy.days_since_success !== null && sy.days_since_success !== undefined)
    ? ` <span class="faint">(${sy.days_since_success}日前)</span>` : "";
  if (!sy.failures) return `<span class="chip ok">成功</span> <span class="mono">${esc(last)}</span>${age}`;
  return `<span class="chip err">${sy.failures}回連続で失敗</span> <span class="mono">最終成功 ${esc(last)}</span>${age}` +
    (sy.last_error ? `<div class="faint mono">${esc(sy.last_error)}</div>` : "");
}
const chipOf = (map, key) => {
  const [label, cls] = map[key] || [key, ""];
  return `<span class="chip ${cls}">${esc(label)}</span>`;
};

/* 停留所(路線図・行程図・便リプレイで共用)。序列は 起案<審査<決裁<施行<後閲 で、
   小さい方へ動く回議(差し戻し・再審理)を逆行として描く。 */
const STATIONS = [["kian", "起案"], ["shinsa", "審査"], ["kessai", "決裁"],
  ["shiko", "施行"], ["kouetsu", "後閲"]];
const stationIdx = (key) => STATIONS.findIndex(([k]) => k === key);
const stationLabel = (key) => (STATIONS.find(([k]) => k === key) || [key, key])[1];

/* 回議録1行がどの停留所での処理かを決める。actor 接頭辞(kian:/shinsa:/kessai:/human)が
   第一キーで、actor が system(モデルを介さない記帳)のときだけ action で振り分ける。 */
function stationOf(e) {
  const actor = String((e && e.actor) || "");
  const pre = actor.split(":")[0];
  if (["kian", "shinsa", "kessai"].includes(pre)) return pre;
  if (actor === "human") return "kouetsu";
  const a = (e && e.action) || "";
  if (["shiko", "skill_mv"].includes(a)) return "shiko";
  if (["shinsa_ok", "joshin"].includes(a)) return "shinsa";
  if (["kessai_ok", "hiketsu", "kurikoshi", "saishinri"].includes(a)) return "kessai";
  return "kian";   // kian/hosei と未知の処理は起案席に置く
}

/* 文書ごとの行程図(決裁欄4マスの補足)。回議録を停留所の並びとして横に描き、
   上流へ戻る回議は逆行コネクタにする。往復の回数と、どこで滞ったかを1行で見る。 */
function routeStrip(log) {
  if (!log.length) return "";
  const cls = (a) => (["sashimodoshi", "hiketsu"].includes(a) ? " err"
    : a === "kurikoshi" ? " warn" : "");
  return `<div class="rt-strip">${log.map((e, i) => {
    const st = stationOf(e);
    const who = e.actor && e.actor.includes(":") ? e.actor.split(":")[1] : (e.actor || "");
    const stamp = `<span class="rt-stamp${cls(e.action)}"${e.memo ? ` title="${esc(e.memo)}"` : ""}>
        <span class="rt-act">${esc(SHELF_ACTION[e.action] || e.action)}</span>
        <span class="rt-stn">${esc(stationLabel(st))}</span>
        <span class="rt-who mono">${esc(who)}</span>
        <span class="rt-when">${esc(String(e.created_at || "").slice(5, 10))}</span>
      </span>`;
    const nx = log[i + 1];
    if (!nx) return stamp;
    const back = stationIdx(stationOf(nx)) < stationIdx(st);
    return `${stamp}<span class="rt-link${back ? " back" : ""}">${back ? "↩" : "→"}</span>`;
  }).join("")}</div>`;
}

function renderShelf(el) {
  // 便リプレイのタイマーは再描画・タブ切替で必ず止める(描画済みDOMは捨てられるため)
  clearTimeout(drawReplay._timer);
  const filt = renderShelf._filt || "pending";
  const kind = renderShelf._kind || "";
  const state = renderShelf._state || "";
  el.innerHTML = `
    <div class="note info"><span class="tag">仕組み</span><span>夜間バッチの判断(facts登載・例規改定・skill登載)は起案文書として起票され、審査(課長専決)・決裁(部長)を経て施行されます。人間はここで<b>後閲</b>します: 妥当なら後閲印、問題があれば<b>メモを付けて差し戻し</b> — 翌晩の便で決裁者が再審理し、是正文書を起票します。skillの施行(全端末配布)だけは後閲印が条件です。決裁が付かなかった案件は<b>未決</b>として繰り越され(承認も廃案もしない)、翌晩に再審理されます。</span></div>
    <div class="card" id="stnMap" style="margin-top:14px">読み込み中…</div>
    <div class="card" id="replayCard" style="margin-top:14px"></div>
    <h2 class="section">専決規程(モデルの役割分担) — NAS batch/config.json</h2>
    <div class="card" id="kiteiCard"></div>
    <div class="toolrow" style="margin-top:14px" id="shelfFilters">
      ${[["pending", "後閲待ち"], ["remanded", "差し戻し・再審理中"], ["miketsu", "未決(繰越中)"], ["all", "全件"]].map(([v, l]) =>
        `<button class="btn mini${filt === v ? "" : " ghost"}" data-f="${v}">${l}</button>`).join("")}
      <select id="shelfKind">
        <option value="">全種別</option>
        ${Object.entries(SHELF_KIND).map(([v, l]) =>
          `<option value="${v}"${kind === v ? " selected" : ""}>${l}</option>`).join("")}
      </select>
    </div>
    <div class="card" id="shelfList">読み込み中…</div>
    <div id="shelfDoc"></div>`;

  drawStationMap(el);
  drawReplay($("#replayCard", el));
  drawKitei($("#kiteiCard", el));
  // フィルタボタンは見方そのものの切替なので、停留所クリックで付いた state 絞り込みは外す
  el.querySelectorAll("#shelfFilters .btn").forEach((b) => {
    b.onclick = () => { renderShelf._filt = b.dataset.f; renderShelf._state = ""; renderShelf(el); };
  });
  $("#shelfKind", el).onchange = (e) => { renderShelf._kind = e.target.value; renderShelf(el); };

  const listEl = $("#shelfList", el);
  const docEl = $("#shelfDoc", el);

  async function loadList() {
    try {
      const rows = await j(`/api/shelf?filter=${filt}${kind ? `&kind=${kind}` : ""}${state ? `&state=${state}` : ""}`);
      if (!rows.length) {
        listEl.innerHTML = '<span class="faint">該当する文書はありません。</span>';
        return;
      }
      listEl.innerHTML = `<table>
        <tr><th>文書番号</th><th>種別</th><th>project</th><th>件名</th><th>決裁区分</th><th>状態</th><th>後閲</th><th>起案日</th></tr>
        ${rows.map((r) => `<tr class="click shelf-row" data-id="${r.id}" style="cursor:pointer">
          <td class="mono" style="white-space:nowrap">${esc(docNoDisp(r))}</td>
          <td>${esc(SHELF_KIND[r.kind] || r.kind)}</td>
          <td class="mono faint">${esc(r.project_key)}</td>
          <td>${esc(r.title)}</td>
          <td>${r.decision_class ? (r.decision_class === "senketsu" ? "課長専決" : "部長決裁") : "—"}</td>
          <td>${chipOf(SHELF_STATE, r.state)}</td>
          <td>${seenChip(r)}</td>
          <td class="mono faint" style="white-space:nowrap">${esc(String(r.created_at || "").slice(0, 10))}</td>
        </tr>`).join("")}
      </table>`;
      listEl.querySelectorAll(".shelf-row").forEach((tr) => {
        tr.onclick = () => loadDoc(Number(tr.dataset.id));
      });
    } catch (e) { listEl.textContent = `取得失敗: ${e.message}`; }
  }

  async function loadDoc(id) {
    docEl.innerHTML = '<div class="faint">読み込み中…</div>';
    let d;
    try { d = await j(`/api/shelf_doc?id=${id}`); }
    catch (e) { docEl.innerHTML = `<div class="note warn"><span class="tag">失敗</span><span>${esc(e.message)}</span></div>`; return; }

    // 決裁欄: 回議録から役職ごとの最終処理を拾う
    const log = d.log || [];
    const stampOf = (actions) => {
      const e2 = [...log].reverse().find((x) => actions.includes(x.action));
      if (!e2) return null;
      return { who: e2.actor.includes(":") ? e2.actor.split(":")[1] : e2.actor,
               what: SHELF_ACTION[e2.action] || e2.action,
               when: String(e2.created_at || "").slice(5, 10) };
    };
    const boxes = [["起 案", stampOf(["kian", "hosei"])],
                   ["審 査", stampOf(["shinsa_ok", "joshin"])],
                   ["決 裁", stampOf(["kessai_ok", "hiketsu"])],
                   ["後 閲", stampOf(["kouetsu"])]];

    const canKouetsu = d.seen_state === "pending" && ["executed", "rejected"].includes(d.state);
    const canApproveSkill = d.seen_state === "pending" && d.state === "approved" && d.kind === "skill";
    const canRemand = d.seen_state === "pending" && ["executed", "approved"].includes(d.state);

    docEl.innerHTML = `
      <div class="card" style="margin-top:14px">
        <div class="toolrow">
          <b class="mono">${esc(docNoDisp(d))}</b>
          <span class="chip">${esc(SHELF_KIND[d.kind] || d.kind)}</span>
          ${chipOf(SHELF_STATE, d.state)}
          ${d.decision_class ? `<span class="chip ${d.decision_class === "bucho" ? "warn" : "ok"}">${d.decision_class === "senketsu" ? "課長専決" : "部長決裁"}</span>` : ""}
          ${seenChip(d)}
          <span style="flex:1"></span>
          <button class="btn mini ghost" id="docClose">閉じる</button>
        </div>
        <div style="font-weight:600;margin:6px 0">${esc(d.title)}</div>
        <div class="kessai-ran">
          ${boxes.map(([role, s]) => `<div class="kbox${s ? " done" : ""}">
            <div class="kr">${role}</div>
            <div class="kv">${s ? `${esc(s.what)}<br><span class="mono faint">${esc(s.who)}</span><br><span class="faint">${esc(s.when)}</span>` : "—"}</div>
          </div>`).join("")}
        </div>
        ${routeStrip(log)}
        <code class="block" style="white-space:pre-wrap">${esc(d.proposal || "")}</code>
        ${(d.facts || []).length ? `<h2 class="section">登載facts</h2><table>
          <tr><th>id</th><th>内容</th><th>状態</th></tr>
          ${d.facts.map((f) => `<tr><td class="num">${f.id}</td>
            <td style="white-space:pre-wrap">${esc(f.content)}</td>
            <td><span class="chip ${f.status === "verified" ? "ok" : "warn"}">${esc(factStatusLabel(f.status))}</span>
              ${f.retired ? '<span class="chip err">撤去済</span>' : ""}
              ${f.superseded ? '<span class="chip warn">置換済</span>' : ""}</td></tr>`).join("")}
        </table>` : ""}
        ${(d.related || []).length ? `<h2 class="section">関連文書</h2>
          ${d.related.map((r) => `<div><a href="#shelf" class="rel-doc mono" data-id="${r.id}">${esc(r.doc_no)}</a> ${esc(SHELF_KIND[r.kind] || r.kind)} — ${esc(r.title)} ${chipOf(SHELF_STATE, r.state)}</div>`).join("")}` : ""}
        <h2 class="section">回議録</h2>
        <table>
          <tr><th>日時</th><th>担当</th><th>処理</th><th>memo</th></tr>
          ${log.map((e2) => `<tr>
            <td class="mono faint" style="white-space:nowrap">${esc(String(e2.created_at || "").slice(5, 16).replace("T", " "))}</td>
            <td class="mono">${esc(e2.actor)}</td>
            <td>${esc(SHELF_ACTION[e2.action] || e2.action)}</td>
            <td style="white-space:pre-wrap">${esc(e2.memo || "—")}</td></tr>`).join("")}
        </table>
        <h2 class="section">後閲</h2>
        ${canKouetsu || canApproveSkill || canRemand ? `
          <div class="toolrow">
            ${canKouetsu ? '<button class="btn mini" id="opKouetsu">後閲印(確認済)</button>' : ""}
            ${canApproveSkill ? '<button class="btn mini" id="opApproveSkill">後閲印(施行許可 — 翌晩skills/へ登載)</button>' : ""}
          </div>
          ${canRemand ? `<div class="toolrow">
            <span class="lnfield" style="flex:1"><span class="ln1" aria-hidden="true">1</span>
              <input type="text" id="remandMemo" placeholder="差し戻しメモ(必須。前の担当者への指示。例: この事実は誤り、撤回してほしい)"></span>
            <button class="btn mini danger" id="opRemand">${d.state === "approved" ? "差し戻し(廃案)" : "差し戻し(翌晩再審理)"}</button>
          </div>` : ""}` : `<span class="faint">この文書に後閲操作はありません(${SHELF_SEEN[d.seen_state] ? SHELF_SEEN[d.seen_state][0] : d.seen_state})。</span>`}
      </div>`;

    $("#docClose", docEl).onclick = () => { docEl.innerHTML = ""; };
    docEl.querySelectorAll(".rel-doc").forEach((a) => {
      a.onclick = (ev) => { ev.preventDefault(); loadDoc(Number(a.dataset.id)); };
    });
    const op = async (body, okMsg) => {
      try {
        await j("/api/shelf_op", { method: "POST", body: JSON.stringify(body) });
        toast(okMsg);
        await loadList();
        await loadDoc(id);
      } catch (e) { toast(`失敗: ${e.message}`, 6000); }
    };
    const bk = $("#opKouetsu", docEl);
    if (bk) bk.onclick = () => op({ op: "kouetsu", id }, "後閲印を押しました");
    const ba = $("#opApproveSkill", docEl);
    if (ba) ba.onclick = () => op({ op: "approve_skill", id }, "施行を許可しました(翌晩のバッチで登載)");
    const br = $("#opRemand", docEl);
    if (br) br.onclick = () => {
      const memo = $("#remandMemo", docEl).value.trim();
      if (!memo) { toast("差し戻しメモを書いてください(前の担当者への指示)", 4000); return; }
      op({ op: "remand", id, memo }, "差し戻しました(翌晩の便で再審理されます)");
    };
  }

  loadList();
}

/* A1 停留所マップ。書庫タブ冒頭の路線図。上段は起案から後閲までの本線、下段は差し戻し・
   再審理の戻り線。各停留所に担当モデル(専決規程の roles)と滞留件数を出し、クリックで
   下の一覧を絞り込む。件数は /api/shelf_counts。0件のときはバッジを出さず枠だけ残す。 */
async function drawStationMap(el) {
  const card = $("#stnMap", el);
  if (!card) return;
  const filt = renderShelf._filt || "pending";
  const state = renderShelf._state || "";
  const roles = (N.batch_config || {}).roles || {};
  let c;
  try { c = await j("/api/shelf_counts"); }
  catch (e) {
    card.innerHTML = `<div class="note warn"><span class="tag">件数未取得</span><span>停留所ごとの件数を取れません: ${esc(e.message)}</span></div>`;
    return;
  }
  if (!c) {   // 012未適用(drafts無し)の環境ではサーバがnullを返す
    card.innerHTML = '<div class="note warn"><span class="tag">未適用</span><span>起案・決裁のスキーマ(012)が未適用のため、停留所マップを出せません。</span></div>';
    return;
  }

  // 起案席に滞留する状態は「補正中」で、これは下段の戻り線に出すため上段は件数を持たない
  const stns = [
    { name: "起案", model: roles.kian || "", n: 0 },
    { name: "審査", model: roles.shinsa || "", n: c.pending_review, filt: "all", state: "pending_review" },
    { name: "決裁", model: roles.kessai || "", n: c.pending_decision, filt: "all", state: "pending_decision",
      extra: N.shelf_miketsu ? { label: `未決 ${num(N.shelf_miketsu)}`, cls: "warn", filt: "miketsu", state: "" } : null },
    { name: "決裁済", model: "", n: c.approved, filt: "all", state: "approved" },
    { name: "施行", model: "system", n: c.executed, filt: "all", state: "executed" },
    { name: "後閲", model: "human", n: c.kouetsu_pending, cls: "warn", filt: "pending", state: "" },
  ];
  const stnHtml = (s) => {
    const on = s.filt && s.filt === filt && (s.state || "") === state;
    return `<div class="stn${s.filt ? " click" : ""}${on ? " sel" : ""}"${s.filt ? ` data-filt="${s.filt}" data-state="${esc(s.state || "")}"` : ""}>
      <div class="stn-name">${esc(s.name)}</div>
      <div class="stn-model mono faint">${esc(s.model || "—")}</div>
      <div class="stn-n">
        ${s.n ? `<span class="chip ${s.cls || "blue"}">${num(s.n)}</span>` : ""}
        ${s.extra ? `<span class="chip ${s.extra.cls} click" data-filt="${s.extra.filt}" data-state="">${esc(s.extra.label)}</span>` : ""}
      </div>
    </div>`;
  };
  // 審査と決裁の間は分岐(課長専決はここで決裁済へ抜け、上申された案件だけ決裁へ進む)
  const branch = `<div class="stn-branch"><span>専決 → 決裁済</span><span>上申 → 決裁</span></div>`;
  const line = stns.map((s, i) => stnHtml(s) +
    (i === stns.length - 1 ? "" : i === 1 ? branch : '<span class="stn-arrow"></span>')).join("");

  const backs = [
    { st: "remanded_to_drafter", to: "起案へ", n: c.remanded_to_drafter, filt: "all", state: "remanded_to_drafter" },
    { st: "remanded_to_reviewer", to: "審査へ", n: c.remanded_to_reviewer, filt: "all", state: "remanded_to_reviewer" },
    { st: "reexamine", to: "決裁へ(翌晩再審理)", n: c.reexamine, filt: "remanded", state: "" },
  ].filter((b) => b.n);
  const backHtml = `<div class="stn-back">
    <span class="stn-back-arrow"></span>
    <span class="faint">戻り線</span>
    ${backs.length ? backs.map((b) => `<span class="chip warn click" data-filt="${b.filt}" data-state="${esc(b.state)}">${esc(SHELF_STATE[b.st][0])} ${b.to} · ${num(b.n)}</span>`).join("")
      : '<span class="faint">戻っている文書はありません。</span>'}
  </div>`;

  card.innerHTML = `<div class="stn-map">
    <div class="stn-line">${line}</div>
    ${backHtml}
  </div>`;
  card.querySelectorAll("[data-filt]").forEach((n2) => {
    n2.onclick = (ev) => {
      ev.stopPropagation();
      renderShelf._filt = n2.dataset.filt;
      renderShelf._state = n2.dataset.state || "";
      renderShelf(el);
    };
  });
}

/* A3 夜間便リプレイ。draft_log を created_by='run-N' で束ねた1便を、文書が席から席へ
   動くアニメーションとして再生する。初期表示は便セレクタと再生ボタンだけで、
   再生(または1ステップ送り)を押すまで舞台には席しか置かない。 */
function drawReplay(card) {
  if (!card) return;
  const SEATS = [["kian", "起案席"], ["shinsa", "審査席"], ["kessai", "決裁席"], ["shiko", "施行・書庫"]];
  // 後閲(human)は便の記帳には出てこないが、来た場合は書庫側の席に置く
  const seatIdx = (key) => { const i = SEATS.findIndex(([k]) => k === key); return i < 0 ? SEATS.length - 1 : i; };
  const seatsHtml = () => SEATS.map(([, label], i) =>
    `<div class="rp-seat" style="left:${i * (100 / SEATS.length)}%;width:${100 / SEATS.length}%">${esc(label)}</div>`).join("")
    + '<div class="rp-trash">廃案置き場</div>';

  card.innerHTML = `
    <div class="toolrow">
      <b>夜間便リプレイ</b>
      <select id="rpRun"></select>
      <button class="btn mini" id="rpPlay">▶ 再生</button>
      <button class="btn mini ghost" id="rpPrev">⏮ 前</button>
      <button class="btn mini ghost" id="rpNext">次 ⏭</button>
      <button class="btn mini ghost" id="rpSpeed">1x</button>
      <span class="faint mono" id="rpStep">step 0/0</span>
    </div>
    <div class="replay-stage" id="rpStage">${seatsHtml()}</div>
    <div class="replay-caption" id="rpCap"><span class="faint">便を選んで ▶ を押すと、その晩の回議をなぞります。</span></div>`;

  const stage = $("#rpStage", card);
  const cap = $("#rpCap", card);
  const stepEl = $("#rpStep", card);
  const runSel = $("#rpRun", card);
  const playBtn = $("#rpPlay", card);
  const speedBtn = $("#rpSpeed", card);
  let evs = [];            // この便の記帳(log順)
  let docs = new Map();    // draft_id -> {el, lane}
  let k = 0;               // 適用済みステップ数(0 = 便の開始前)
  let built = false;
  let playing = false;
  let speed = 1;

  function stop() {
    playing = false;
    clearTimeout(drawReplay._timer);
    playBtn.textContent = "▶ 再生";
  }

  // 便に出てくる文書を1件1レーンに割り当て、舞台に並べる
  function build() {
    docs = new Map();
    stage.innerHTML = seatsHtml();
    for (const e of evs) {
      if (docs.has(e.draft_id)) continue;
      const d = document.createElement("div");
      d.className = "replay-doc";
      d.innerHTML = `<span class="rp-no mono">${esc(e.doc_no_disp || e.doc_no || "")}</span>
        <span class="rp-title">${esc(e.title || "")}</span>
        <span class="rp-stamps"></span>`;
      // 初期位置は起案席。付けてから transform を書くと入場が滑って見えるので生成時に置く
      d.style.transform = `translate(8px, ${34 + docs.size * 52}px)`;
      stage.appendChild(d);
      docs.set(e.draft_id, { el: d, lane: docs.size });
    }
    stage.style.height = `${34 + Math.max(1, docs.size) * 52 + 56}px`;
    built = true;
  }

  function ensureBuilt() { if (!built) { build(); render(k); } }

  /* 舞台の状態は「先頭から i 件を適用した結果」として毎回組み直す(前ステップへも戻せる)。
     位置だけ transform で書き換わるので、動いた文書だけが CSS transition で滑る。 */
  function render(i) {
    const w = stage.clientWidth / SEATS.length;
    const cur = i > 0 ? evs[i - 1] : null;   // 直前に適用した記帳
    let dead = 0;
    for (const [id, d] of docs) {
      const past = evs.slice(0, i).filter((e) => e.draft_id === id);
      const last = past[past.length - 1];
      const done = past.some((e) => e.action === "hiketsu");
      let si = last ? seatIdx(stationOf(last)) : 0;
      // 差し戻しは1つ上流の席へ戻す(次の処理までそこに居る)
      if (last && last.action === "sashimodoshi") si = Math.max(0, si - 1);
      const x = done ? 8 + (dead % SEATS.length) * w : 8 + si * w;
      const y = done ? 34 + docs.size * 52 + 6 : 34 + d.lane * 52;
      if (done) dead += 1;
      d.el.style.width = `${Math.max(90, w - 16)}px`;
      d.el.style.transform = `translate(${x}px, ${y}px)`;
      d.el.classList.toggle("waiting", !last);
      d.el.classList.toggle("dead", done);
      d.el.classList.toggle("miketsu", past.some((e) => e.action === "kurikoshi"));
      // 直前に適用した記帳が差し戻しなら、その文書を warn 色で明滅させる
      d.el.classList.toggle("flash", !!cur && cur.draft_id === id && cur.action === "sashimodoshi");
      $(".rp-stamps", d.el).innerHTML = past.map((e) => {
        const cls = ["sashimodoshi", "hiketsu"].includes(e.action) ? " err"
          : e.action === "kurikoshi" ? " warn" : "";
        const pop = cur && cur.id === e.id ? " pop" : "";
        return `<span class="rt-stamp mini${cls}${pop}"${e.memo ? ` title="${esc(e.memo)}"` : ""}>${esc(SHELF_ACTION[e.action] || e.action)}</span>`;
      }).join("");
    }
    cap.innerHTML = cur
      ? `<span class="mono">${esc(cur.doc_no_disp || cur.doc_no || "")}</span>
         <span class="chip">${esc(stationLabel(stationOf(cur)))}</span>
         <b>${esc(SHELF_ACTION[cur.action] || cur.action)}</b>
         <span class="mono faint">${esc(cur.actor || "")}</span>
         <span>${esc(cur.memo || "")}</span>`
      : '<span class="faint">便の開始前です。</span>';
    stepEl.textContent = `step ${i}/${evs.length}`;
  }

  function setStep(i) { ensureBuilt(); k = Math.max(0, Math.min(evs.length, i)); render(k); }

  function tick() {
    // タブを切り替えて舞台ごと捨てられていたら、そこで打ち切る
    if (!stage.isConnected) { stop(); return; }
    if (k >= evs.length) { stop(); return; }
    setStep(k + 1);
    if (k >= evs.length) { stop(); return; }
    drawReplay._timer = setTimeout(tick, 900 / speed);
  }

  async function load(run) {
    stop();
    try {
      const d = await j(`/api/shelf_replay${run ? `?run=${encodeURIComponent(run)}` : ""}`);
      const runs = d.runs || [];
      runSel.innerHTML = runs.length ? runs.map((r) => `<option value="${esc(r.run_id)}"${String(r.run_id) === String(d.run_id) ? " selected" : ""}>run-${esc(r.run_id)}(${esc(String(r.t0 || "").slice(5, 16).replace("T", " "))}・${num(r.n)}件)</option>`).join("")
        : '<option value="">(便の記録がありません)</option>';
      evs = d.events || [];
      built = false;
      k = 0;
      docs = new Map();
      stage.style.height = "";
      stage.innerHTML = seatsHtml();
      stepEl.textContent = `step 0/${evs.length}`;
      cap.innerHTML = evs.length
        ? '<span class="faint">▶ を押すと、この便の回議を順に再生します。</span>'
        : '<span class="faint">この便には記帳がありません。</span>';
    } catch (e) {
      cap.innerHTML = `<div class="note warn"><span class="tag">失敗</span><span>${esc(e.message)}</span></div>`;
    }
  }

  playBtn.onclick = () => {
    if (playing) { stop(); return; }
    if (!evs.length) return;
    if (k >= evs.length) setStep(0);
    ensureBuilt();
    playing = true;
    playBtn.textContent = "⏸ 一時停止";
    drawReplay._timer = setTimeout(tick, 250);
  };
  $("#rpPrev", card).onclick = () => { stop(); setStep(k - 1); };
  $("#rpNext", card).onclick = () => { stop(); setStep(k + 1); };
  speedBtn.onclick = () => { speed = speed === 1 ? 2 : 1; speedBtn.textContent = `${speed}x`; };
  runSel.onchange = () => load(runSel.value);

  load("");
}

function drawKitei(card) {
  const bc = N.batch_config;
  if (!bc) {
    card.innerHTML = '<div class="note warn"><span class="tag">未取得</span><span>NAS の batch/config.json を読めません(未作成またはssh不通)。</span></div>';
    return;
  }
  const roles = bc.roles || {};
  const rg = bc.ringi || {};
  const ROLE_LABEL = { kian: "起案(係員) — turnsから事実候補を調べ上げる",
    shinsa: "審査(課長) — 既存factsと照合、軽易案件を専決",
    kessai: "決裁(部長) — 置換・撤回・矛盾疑いの上申案件",
    enrich: "例規起草 — 決裁済みfactsから例規(index.md)を起草" };
  card.innerHTML = `
    <div class="note info"><span class="tag">反映</span><span>保存すると NAS の config.json に書き戻され(旧内容は .bak 退避)、<b>翌晩のバッチから</b>反映されます。空欄の役割は共通modelにフォールバックします。</span></div>
    <table>
      <tr><td style="width:340px">共通 model(役割未指定時・従来バッチ)</td>
        <td><input type="text" class="mono" id="kModel" style="width:280px" value="${esc(bc.model || "")}"></td></tr>
      ${Object.entries(ROLE_LABEL).map(([r, l]) => `
        <tr><td>${esc(l)}</td>
          <td><input type="text" class="mono kRole" data-role="${r}" style="width:280px" value="${esc(roles[r] || "")}"></td></tr>`).join("")}
      <tr><td>起案・決裁ワークフロー</td><td>
        <label><input type="checkbox" id="kEnabled"${rg.enabled ? " checked" : ""}> enabled(有効化)</label>
        <label style="margin-left:14px"><input type="checkbox" id="kTrial"${rg.trial ? " checked" : ""}> trial(起案モデル並行試行)</label>
        <label style="margin-left:14px"><input type="checkbox" id="kAutoSkill"${rg.skill_auto_execute ? " checked" : ""}> skill_auto_execute(skill施行に後閲印を待たない)</label>
      </td></tr>
    </table>
    <div class="toolrow" style="margin-top:8px">
      <span class="faint">往復上限等の細かい規程(max_hosei_rounds 等)は config.json を直接編集</span>
      <span style="flex:1"></span>
      <button class="btn mini" id="kSave">保存(翌晩から反映)</button>
    </div>`;
  $("#kSave", card).onclick = async () => {
    const roles2 = {};
    card.querySelectorAll(".kRole").forEach((i) => { roles2[i.dataset.role] = i.value.trim(); });
    try {
      const r = await j("/api/batch_config", { method: "POST", body: JSON.stringify({
        model: $("#kModel", card).value.trim(),
        roles: roles2,
        ringi: { enabled: $("#kEnabled", card).checked,
                 trial: $("#kTrial", card).checked,
                 skill_auto_execute: $("#kAutoSkill", card).checked },
        expected: bc,
      }) });
      N.batch_config = r.config;
      toast("専決規程を保存しました(翌晩のバッチから反映)");
      drawKitei(card);
    } catch (e) { toast(`保存失敗: ${e.message}`, 6000); }
  };
}

function renderSkills(el) {
  if (staleServer(el)) return;
  const cands = S.skill_candidates || [];
  const candHtml = cands.length ? `
    <h2 class="section">スキル候補(自動発掘・未採用) — ${cands.length} 件</h2>
    <div class="note info"><span class="tag">仕組み</span><span>日次バッチが全端末のログから反復手順を発掘した候補です。ここにある間は何も発動しません。採用するときはセッションで「候補の ◯◯ を採用して」と言えば、下書きを検証・仕上げして skills/ に入ります。不要な候補は skills-candidates/ から削除してください。</span></div>
    <div class="card"><table>
      <tr><th>名前</th><th>種別</th><th>要約</th><th style="text-align:right">検出回数</th><th style="text-align:right">根拠turns</th><th>最終検出</th><th></th></tr>
      ${cands.map((c, i) => `<tr>
        <td class="mono" style="white-space:nowrap">${esc(c.name)}</td>
        <td>${c.kind === "improve" ? `<span class="chip warn">改善: ${esc(c.target_skill || "")}</span>` : '<span class="chip blue">新規</span>'}</td>
        <td class="muted">${esc(c.summary)}</td>
        <td class="num">${esc(c.count)}</td>
        <td class="num">${esc(c.evidence_n)}</td>
        <td class="mono faint">${esc(c.updated)}</td>
        <td>${c.draft ? `<button class="btn mini ghost cand-open" data-i="${i}">下書き</button>` : ""}</td>
      </tr>
      <tr class="cand-body" data-for="${i}" hidden><td colspan="7"><code class="block"></code></td></tr>`).join("")}
    </table></div>` : "";

  // 出所 → 見出しと編集可否。プラグインは cache 内の配布物なので編集対象にしない
  function srcHead(src, list) {
    const first = list[0] || {};
    let label = src, chips = "";
    if (src === "user") label = "user — ~/.claude/skills";
    else if (src === "claude-config") label = "claude-config — git で全端末に配布";
    else if (src === "codex") label = "codex — ~/.codex/skills(Codex 専用)";
    else if (src.startsWith("project:")) label = `project — ${src.slice(8)}/.claude`;
    else if (src.startsWith("plugin:")) label = `plugin — ${src.slice(7)}`;
    else if (src === "codex-builtin") label = "Codex 内蔵プラグイン — ~/.codex/plugins/cache/openai-bundled(CLI 同梱)";
    else if (src === "codex-runtime") label = "Codex 実行時ランタイム — ~/.codex/plugins/cache/openai-primary-runtime(実行時に取得・組み立て)";
    else if (src === "codex-remote") label = "Codex リモートプラグイン — ~/.codex/plugins/cache/openai-curated-remote";
    chips += first.editable
      ? ' <span class="chip ok">編集可(ファイル直接編集)</span>'
      : ' <span class="chip warn">編集不可(プラグイン配布物・更新で上書き)</span>';
    if (first.enabled === false) chips += ' <span class="chip err">無効(enabledPlugins)</span>';
    return `${esc(label)} — ${list.length} 件${chips}`;
  }

  // エージェント列はサーバの usage キーから動的に作る(将来 opencode 等が増えても列が自動で増える)
  const AGENT_COL = { claude: "Claude 発動", codex: "Codex 参照" };
  const AGENT_TITLE = {
    claude: "Claude Code の Skill ツール呼び出し回数。構造化された呼び出しがログに残るため、確実に使われた回数",
    codex: "Codex が SKILL.md を読んだ回数。Codex には Skill ツールが無く、シェルでの手順書読み取りしかログに残らないため近似値(検討だけして使わなかった場合も数え、読み直さない再利用は数えない)",
  };
  function usageAgents(items) {
    const set = new Set();
    for (const s of items) for (const a of Object.keys(s.usage || {})) set.add(a);
    return [...Object.keys(AGENT_COL).filter((a) => set.has(a)),
            ...[...set].filter((a) => !(a in AGENT_COL)).sort()];
  }
  const useCount = (s, a) => ((s.usage || {})[a] || {}).count || 0;
  const useTotal = (s, agents) => agents.reduce((n, a) => n + useCount(s, a), 0);
  const useLast = (s, agents) => agents.map((a) => ((s.usage || {})[a] || {}).last || "")
    .reduce((x, y) => (y > x ? y : x), "");

  function grouped(items, nameHead, usage = false) {
    const agents = usage ? usageAgents(items) : [];
    const groups = {};
    for (const s of items) (groups[s.source] ??= []).push(s);
    return Object.entries(groups).map(([src, list]) => {
      const rows = usage
        ? [...list].sort((a, b) => useTotal(b, agents) - useTotal(a, agents)
            || a.name.localeCompare(b.name))
        : list;
      return `
      <h2 class="section">${srcHead(src, list)}</h2>
      <div class="card"><table>
        <tr><th>${nameHead}</th>${agents.map((a) => `<th style="text-align:right" title="${esc(AGENT_TITLE[a] || "")}">${esc(AGENT_COL[a] || a)}</th>`).join("")}${usage ? "<th>最終使用</th>" : ""}<th>説明(frontmatter)</th><th style="text-align:right">サイズ</th></tr>
        ${rows.map((s) => `<tr>
          <td class="mono" style="white-space:nowrap">${esc(s.name)}</td>
          ${agents.map((a) => `<td class="num">${useCount(s, a) > 0 ? `<strong>${useCount(s, a)}</strong>` : '<span class="faint">0</span>'}</td>`).join("")}
          ${usage ? `<td class="mono faint">${esc(useLast(s, agents) || "—")}</td>` : ""}
          <td class="muted">${esc(s.description || "—")}</td>
          <td class="num">${kb(s.bytes)}</td></tr>`).join("")}
      </table></div>`;
    }).join("");
  }

  const B = S.builtin || {};
  const stale = B.captured_with && B.current_version && B.captured_with !== B.current_version;
  const brow = (r, chip) => `<tr>
    <td class="mono" style="white-space:nowrap">${esc(r.name)}</td>
    <td>${chip}</td><td class="muted">${esc(r.description || "—")}</td></tr>`;
  const builtinHtml = `
    <h2 class="section">Claude Code 内蔵 — ${(B.skills || []).length + (B.agents || []).length} 件 <span class="chip warn">変更不可(バイナリ埋め込み)</span></h2>
    <div class="note ${stale ? "warn" : "info"}"><span class="tag">${stale ? "要更新" : "手動採取"}</span><span>内蔵スキル・エージェントはバイナリに埋め込まれ、ファイル走査で列挙できません。この一覧は /context 出力からの手動スナップショットです(採取: claude ${esc(B.captured_with || "?")}、${esc(B.captured_at || "?")} / 現在の claude: ${esc(B.current_version || "不明")})。${stale ? "バージョンが変わっています。セッションで /context を実行し、dashboard/builtin-context.json を更新してください。" : ""}</span></div>
    <div class="card"><table>
      <tr><th>名前</th><th>種別</th><th>説明</th></tr>
      ${(B.skills || []).map((r) => brow(r, '<span class="chip blue">内蔵スキル</span>')).join("")}
      ${(B.agents || []).map((r) => brow(r, '<span class="chip">内蔵エージェント</span>')).join("")}
    </table></div>
    <h2 class="section">実行時に組み立てられるもの <span class="chip warn">変更不可(ファイル実体なし)</span></h2>
    <div class="card"><table>
      <tr><th>名前</th><th>説明</th></tr>
      ${(B.runtime || []).map((r) => `<tr>
        <td class="mono" style="white-space:nowrap">${esc(r.name)}</td>
        <td class="muted">${esc(r.description || "—")}</td></tr>`).join("")}
    </table></div>`;

  el.innerHTML = '<div class="note info"><span class="tag">このタブ</span><span>パイプラインの編さん段: 繰り返し作業を手順書(SKILL.md)として固定化したものがスキルです。夜間バッチが turns から繰り返しを発掘するとスキル候補としてここに並びます。使用実績は claude/codex 同列で数えます。</span></div>'
    + candHtml
    + '<div class="note info"><span class="tag">範囲</span><span>/context に出る構成要素のうちファイル実体があるもの(スキル・コマンド・エージェント)を出所別に、実体が無いもの(内蔵・実行時組み立て)を最後にまとめています。「編集不可」のものを変えたいときは、プラグインなら配布元リポジトリ、内蔵なら Claude Code 本体の更新でしか変わりません。</span></div>'
    + '<div class="note info"><span class="tag">発動と参照</span><span>言葉を分けているのは数字の確度が違うためです。<b>Claude 発動</b> = Claude Code の Skill ツール呼び出し回数。構造化された呼び出しがログ(~/.claude/projects)に残るので、確実に使われた回数です。<b>Codex 参照</b> = Codex が SKILL.md を読んだ回数。Codex には Skill ツールが無く、シェルで手順書を読む形しかログ(~/.codex/sessions)に残らないため近似値です — 検討だけして使わなかった場合も数え、読み直さずに再利用した場合は数えません。共通の限界: 手順を手作業でなぞった使用、他端末、ローテートで消えた古いログは含まず、0 = 記録なし(未使用とは限らない)。</span></div>'
    + grouped(S.skills, "スキル(Skill ツールで発動)", true)
    + grouped(S.commands || [], "コマンド(/ で発動)")
    + grouped(S.agents || [], "エージェント(Agent ツールの subagent_type)")
    + builtinHtml;

  el.querySelectorAll(".cand-open").forEach((btn) => {
    btn.onclick = () => {
      const body = el.querySelector(`.cand-body[data-for="${btn.dataset.i}"]`);
      if (!body.hidden) { body.hidden = true; btn.textContent = "下書き"; return; }
      $("code", body).textContent = (S.skill_candidates[Number(btn.dataset.i)] || {}).draft || "(空)";
      body.hidden = false;
      btn.textContent = "閉じる";
    };
  });
}

function renderHooks(el) {
  const order = ["SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse",
    "PermissionRequest", "Notification", "Stop", "StopFailure", "SubagentStart",
    "SubagentStop", "PreCompact", "SessionEnd"];
  const all = [...S.hooks, ...S.codex_hooks];
  const events = [...new Set(all.map((h) => h.event))];
  events.sort((a, b) => (order.indexOf(a) === -1 ? 99 : order.indexOf(a)) - (order.indexOf(b) === -1 ? 99 : order.indexOf(b)));

  const badge = (s) => s === "applied" ? '<div class="sync-on" style="font-size:12px" title="manifest の宣言どおり、このエージェントの設定に反映済み">適用済</div>'
    : s === "pending" ? '<div class="sync-pend" style="font-size:12px" title="宣言はあるが設定に未反映。「適用」ボタンで反映される">未適用</div>'
    : s === "unsupported" ? '<div><span class="chip err" title="このエージェントはこのイベントに対応していない">不可</span></div>' : "";
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
    <div class="note info"><span class="tag">hookとは</span><span>決まったタイミング(イベント)で必ず実行される仕込みです。判断をモデル任せにせず機械的に強制したいもの(記憶の収集、作業規律の注入、push 前チェック等)をここに置きます。イベント名は発火時点を表します: SessionStart/End = セッション開始/終了、UserPromptSubmit = 毎プロンプト送信時、PreToolUse = ツール実行直前、Stop = 応答完了時。</span></div>
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
    <div class="note info"><span class="tag">このタブ</span><span>パイプラインの収受段の管理と健全性確認です。各端末の hook がセッションを spool(ローカル送信待ちキュー ~/.claude-spool)に書き、NAS の turns へ送ります。ここでは収受から除外するもの(sync-exclude)、NAS 側夜間バッチの実行状況、端末側 hook スクリプト、配布リポジトリの状態を確認します。</span></div>
    <h2 class="section">収集除外 sync-exclude.txt(全端末に配布・手動管理で安全に編集可)</h2>
    <div class="card">
      <div class="toolrow"><span class="faint">${esc(S.sync_exclude.path)}</span>
        <span style="flex:1"></span><button class="btn mini" id="syncSave">保存</button></div>
      <div class="editor-wrap">
        <div class="editor-gutter" id="syncGutter" aria-hidden="true"></div>
        <textarea class="editor" id="syncText" style="min-height:260px" spellcheck="false"></textarea>
      </div>
    </div>

    <h2 class="section">NAS 夜間バッチ(crontab)</h2>
    ${(() => {
      const bc = N.batch_config;
      return bc && bc.model
        ? `<div class="note info"><span class="tag">モデル</span><span>バッチの claude -p は <span class="mono">${esc(bc.model)}</span> で実行(設定: <span class="mono">/volume2/claude-system/batch/config.json</span>。実際に使われたモデルは各 batch/*.log の claude-usage 行に model= で記録)。</span></div>`
        : `<div class="note warn"><span class="tag">モデル未設定</span><span>NAS の batch/config.json が無いか model が空です。CLI デフォルトで動くため、デフォルト変更で夜間バッチのモデルが黙って変わります。</span></div>`;
    })()}
    <div class="card"><code class="block">${esc(S.crontab)}</code></div>

    <h2 class="section">バッチ実行履歴(直近${N.batch_runs.length}件)</h2>
    <div class="note info"><span class="tag">読み方</span><span>時刻は UTC(KST−9時間)。ロック敗退や cron 起動失敗はここに row を残さず「時間が来ても行が増えない」形で現れます — その検知は文書事務概況タブの注意欄(未記録・成功が古い・滞留)が担当します。</span></div>
    <div class="card"><table>
      <tr><th>run</th><th>種別</th><th>開始</th><th>終了</th><th>状態</th><th style="text-align:right">turns処理</th><th style="text-align:right">例規行</th><th>メモ</th></tr>
      ${N.batch_runs.map((b) => `<tr>
        <td class="num">${b.id}</td>
        <td class="mono">${esc(batchKind(b.notes))}</td>
        <td class="mono faint">${esc(String(b.started_at || "").slice(5, 16).replace("T", " "))}</td>
        <td class="mono faint">${esc(String(b.finished_at || "").slice(5, 16).replace("T", " "))}</td>
        <td><span class="chip ${b.status === "success" ? "ok" : b.status === "running" ? "" : "err"}">${esc(b.status)}</span></td>
        <td class="num">${b.turns_processed ?? "·"}</td>
        <td class="num">${b.index_lines ?? "·"}</td>
        <td class="faint" style="max-width:340px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${esc(b.notes || "")}">${esc(String(b.notes || ""))}</td></tr>`).join("")}
    </table></div>

    <h2 class="section">この端末の実接続設定(~/.claude-spool — git 外の秘密層)</h2>
    ${(() => {
      const sp = S.spool || {};
      if (!sp.present) return `<div class="note warn"><span class="tag">未整備</span><span>${esc(sp.config_path || "~/.claude-spool/config.json")} がありません。setup.sh を実行してください。</span></div>`;
      if (sp.error) return `<div class="note warn"><span class="tag">読込失敗</span><span>${esc(sp.error)}</span></div>`;
      return `<div class="note info"><span class="tag">凡例</span><span>実物は git 外(setup.sh が対話生成)。トークンと証明書は生値でなく fp(sha256 先頭12桁)で表示 — fp の一致で確認できるのはトークン・証明書の同一性だけなので、ingest_url は表の値で照合してください。</span></div>
      <div class="card"><table>
        <tr><td>config.json</td><td class="mono faint">${esc(sp.config_path)}</td></tr>
        <tr><td>ingest_url</td><td class="mono">${esc(sp.ingest_url)}</td></tr>
        <tr><td>api_token</td><td>${sp.token_set ? `<span class="chip ok">設定済み</span> <span class="mono faint">fp ${esc(sp.token_fp)}</span>` : '<span class="chip err">未設定</span>'}</td></tr>
        <tr><td>TLS 証明書</td><td><span class="mono faint">${esc(sp.tls_cert || "(未設定)")}</span> ${sp.tls_cert_present ? `<span class="chip ok">あり</span> <span class="mono faint">fp ${esc(sp.tls_cert_fp || "?")}</span>` : '<span class="chip err">なし</span>'}</td></tr>
        <tr><td>送信キュー</td><td>pending ${sp.pending ?? "·"} 件 / sent ${sp.sent ?? "·"} 件 <span class="faint">(pending が溜まり続けるのは送信失敗のシグナル)</span></td></tr>
        <tr><td>最終送信</td><td class="mono">${esc(sp.last_sent_at || "—")} <span class="faint">sender は毎時17分</span></td></tr>
        <tr><td>memory スキャン</td><td class="mono">${esc(sp.last_memory_scan_at || "—")}</td></tr>
        <tr><td>設定同期(git pull)</td><td>${syncCell(sp.sync)}</td></tr>
      </table></div>`;
    })()}

    <h2 class="section">端末側 hook スクリプト(claude-config/hooks)</h2>
    <div class="card">${S.hook_scripts.map((h) => `<span class="chip mono">${esc(h)}</span>`).join(" ")}</div>

    <h2 class="section">claude-config リポジトリ</h2>
    <div class="card">
      <div class="faint">最新コミット: <span class="mono">${esc(S.git.last)}</span></div>
      ${S.git.status ? `<div style="margin-top:8px" class="note warn"><span class="tag">未コミット</span><code class="block" style="flex:1">${esc(S.git.status)}</code></div>`
        : '<div class="faint" style="margin-top:6px">作業ツリーはクリーンです。</div>'}
    </div>`;

  $("#syncText", el).value = S.sync_exclude.content;
  attachLineNumbers($("#syncText", el), $("#syncGutter", el));
  $("#syncSave", el).onclick = async () => {
    try {
      const r = await j("/api/save", { method: "POST", body: JSON.stringify(
        { target: "sync_exclude", content: $("#syncText", el).value }) });
      toast(`保存しました(${kb(r.bytes)}、.bak 退避済み)`);
    } catch (e) { toast(`保存失敗: ${e.message}`, 5000); }
  };
}

function staleServer(el) {
  // server.py 更新後にプロセスが旧コードのままだと、新しい app.js が要求する
  // フィールドが /api に無い。空白で落ちる代わりに再起動を案内する
  if (S.routing && N.device_projects && S.builtin) return false;
  el.innerHTML = '<div class="note warn"><span class="tag">要再起動</span><span>ダッシュボードのサーバプロセスが更新前のコードのまま動いています。server.py を再起動してからリロードしてください。</span></div>';
  return true;
}

function renderRouting(el) {
  if (staleServer(el)) return;
  const R = S.routing;
  const dps = N.device_projects || [];
  const devices = [...new Set(dps.map((d) => d.device))].sort();
  // 行 = turnsで観測されたproject_key(注入対象になり得るもの)。generalは全端末固定なので除外
  const keys = [...new Set(dps.map((d) => d.project_key))]
    .filter((k) => k !== "general").sort();
  const dp = (dev, key) => dps.find((d) => d.device === dev && d.project_key === key);
  const declared = (dev) => (R.parsed[dev] && Array.isArray(R.parsed[dev].projects))
    ? R.parsed[dev].projects : null;

  el.innerHTML = `
    <div class="note info"><span class="tag">仕組み</span><span>パイプラインの配布段です。この表で「どの端末にどのプロジェクトの記憶(例規 index.md)を配るか」を決めます。チェック=配る。保存すると各端末に配られ、次にセッションを開いたときに反映されます。まだ一度も設定していない端末は、チェックを付けて保存した時からこの表に従います。設定ファイル(routing.json)は git で全端末に配布され、各端末が自分の端末名のエントリだけを読みます(この端末のコピー: <span class="mono">${esc(R.path)}</span>)。</span></div>
    ${R.error ? `<div class="note warn"><span class="tag">解析失敗</span><span>routing.json: ${esc(R.error)}</span></div>` : ""}
    <div class="card" style="margin-top:14px">
      <table>
        <tr><th>project_key</th>${devices.map((d) =>
          `<th style="text-align:center"><div class="mono">${esc(d)}</div></th>`
        ).join("")}</tr>
        ${keys.map((k) => `<tr><td>${keyLabel(k)}</td>${devices.map((d) => {
          const o = dp(d, k);
          if (!o) return '<td style="text-align:center" class="faint">·</td>';
          const dec = declared(d);
          const checked = dec !== null && dec.includes(o.cwd);
          return `<td style="text-align:center">
            <input type="checkbox" class="cell" data-dev="${esc(d)}" data-path="${esc(o.cwd)}"
              ${checked ? "checked" : ""} title="${esc(o.cwd)}"></td>`;
        }).join("")}</tr>`).join("")}
      </table>
      <div class="toolrow" style="margin-top:10px">
        <span class="faint" id="routingNote"></span>
        <span style="flex:1"></span>
        <button class="btn mini" id="routingSave">保存して配布(commit &amp; push)</button>
      </div>
    </div>
    <h2 class="section">保存される設定内容のプレビュー(routing.json)</h2>
    <div class="card"><code class="block" id="routingPreview"></code></div>
    <div class="note info"><span class="tag">補足</span><span>各セルのパス(マウスを乗せると表示)は、その端末でそのプロジェクトが実際に開かれた場所の実績から出しています。表に出ない場所を配り先にしたいときは routing.json を直接編集してください(この端末〈${esc(R.local_device)}〉の現在の配布先: ${R.local_registry.length ? R.local_registry.map((p) => `<span class="mono">${esc(p)}</span>`).join(", ") : "なし"})。</span></div>`;

  const preview = $("#routingPreview", el);
  const note = $("#routingNote", el);

  function currentRouting() {
    // エントリを作るのは「チェックのある端末」か「既に設定済みの端末」。
    // どちらでもない端末は書かない(=その端末は今まで通り)。
    // 表に出ないパス(観測外)の既存設定は保持する。
    // turns 未観測でも routing.json に宣言済みの端末は落とさない
    const out = {};
    const allDevices = [...new Set([...devices, ...Object.keys(R.parsed || {})])];
    allDevices.forEach((dev) => {
      const checked = [...el.querySelectorAll(`.cell[data-dev="${CSS.escape(dev)}"]:checked`)]
        .map((c) => c.dataset.path);
      if (!checked.length && declared(dev) === null) return;
      const observed = new Set(dps.filter((d) => d.device === dev).map((d) => d.cwd));
      const kept = (declared(dev) || []).filter((p) => !observed.has(p));
      out[dev] = { projects: [...new Set([...kept, ...checked])].sort() };
    });
    return out;
  }

  const saveBtn = $("#routingSave", el);

  function canon(r) {
    // 比較用の正規形: 端末名・パスとも並び順の揺れを吸収する
    return JSON.stringify(Object.keys(r).sort().map(
      (d) => [d, [...(r[d].projects || [])].sort()]));
  }

  function refresh() {
    const r = currentRouting();
    preview.textContent = JSON.stringify(r, null, 1);
    const newly = Object.keys(r).filter((d) => declared(d) === null);
    note.textContent = newly.length
      ? `注意: ${newly.join(", ")} は今回からこの画面の設定に従います。チェックしていないプロジェクトは配られなくなります。`
      : "";
    const unchanged = canon(r) === canon(R.parsed);
    saveBtn.disabled = unchanged;
    saveBtn.textContent = unchanged ? "変更なし" : "保存して配布(commit & push)";
  }
  el.querySelectorAll(".cell").forEach((c) => { c.onchange = refresh; });
  refresh();

  $("#routingSave", el).onclick = async () => {
    try {
      const r = await j("/api/routing", { method: "POST",
        body: JSON.stringify({ routing: currentRouting(),
          expected: R.raw ?? null }) });
      toast(r.pushed ? "保存して push しました(各端末は次のセッション開始で適用)" : `保存: ${r.note || "変更なし"}`);
      S = await j("/api/state");
      route();
    } catch (e) { toast(`保存失敗: ${e.message}`, 6000); }
  };
}

function renderMessages(el) {
  if (staleServer(el)) return;
  const dps = N.device_projects || [];
  const devices = [...new Set(dps.map((d) => d.device))].sort();
  const keys = [...new Set(dps.map((d) => d.project_key))].sort();
  el.innerHTML = `
    <div class="note info"><span class="tag">仕組み</span><span>宛先に合致する「次のセッション」の開始時に一度だけ表示され、既読になります。恒久的に残したい内容はここではなく「記憶 (facts)」へ。</span></div>
    <div class="ugrid">
    <div class="card upanel"><h2 class="section">送信</h2>
      <div class="toolrow">
        <select id="msgDev"><option value="">端末: 指定なし</option>
          ${devices.map((d) => `<option>${esc(d)}</option>`).join("")}</select>
        <select id="msgProj"><option value="">プロジェクト: 指定なし</option>
          ${keys.map((k) => `<option>${esc(k)}</option>`).join("")}</select>
      </div>
      <div class="toolrow">
        <span class="lnfield" style="flex:1"><span class="ln1" aria-hidden="true">1</span><input type="text" id="msgBody" placeholder="本文(1〜3文)"></span>
        <button class="btn mini" id="msgSend">送信</button>
      </div>
    </div>
    <div class="card upanel"><h2 class="section">履歴(直近30件)</h2><div id="msgList">読み込み中…</div></div>
    </div>`;

  async function loadList() {
    const box = $("#msgList", el);
    try {
      const rows = await j("/api/messages");
      box.innerHTML = rows.length ? `<table>
        <tr><th>id</th><th>日時</th><th>from</th><th>宛先</th><th>本文</th><th>状態</th></tr>
        ${rows.map((m) => `<tr>
          <td class="num">${m.id}</td>
          <td class="mono faint" style="white-space:nowrap">${esc(String(m.created_at || "").slice(5, 16).replace("T", " "))}</td>
          <td class="mono">${esc(m.from_device)}</td>
          <td class="mono faint">${esc(m.to_device || "*")} / ${esc(m.to_project || "*")}</td>
          <td style="white-space:pre-wrap">${esc(m.body)}</td>
          <td>${m.read_at ? '<span class="chip ok">受信済</span>' : '<span class="chip warn">未読</span>'}</td>
        </tr>`).join("")}</table>` : '<span class="faint">メッセージはありません。</span>';
    } catch (e) { box.textContent = `取得失敗: ${e.message}`; }
  }

  $("#msgSend", el).onclick = async () => {
    const body = $("#msgBody", el).value.trim();
    if (!body) return;
    try {
      const r = await j("/api/message_send", { method: "POST", body: JSON.stringify({
        to_device: $("#msgDev", el).value || null,
        to_project: $("#msgProj", el).value || null, body }) });
      toast(`送信しました(id=${r.id})`);
      $("#msgBody", el).value = "";
      await loadList();
    } catch (e) { toast(`送信失敗: ${e.message}`, 6000); }
  };
  $("#msgBody", el).onkeydown = (e) => { if (e.key === "Enter") $("#msgSend", el).click(); };
  loadList();
}

function renderUsage(el) {
  const hours = renderUsage._hours || 168;
  const ranges = [[24, "24時間"], [168, "7日"], [720, "30日"]];
  el.innerHTML = `
    <div class="note info"><span class="tag">出所</span><span>各端末の Claude Code が OTLP で NAS の otel-collector に送るテレメトリ(Prometheus 保持 400日)。詳細は <a id="grafanaLink" href="#" target="_blank">Grafana</a>。</span></div>
    <div class="toolrow rangebtns" style="margin-top:14px">
      ${ranges.map(([h, l]) => `<button class="btn mini${h === hours ? "" : " ghost"}" data-hours="${h}">${l}</button>`).join("")}
      <span class="stamp" id="usageStamp"></span>
    </div>
    <div id="usageBody" style="margin-top:16px"><span class="faint">読み込み中…</span></div>`;

  el.querySelectorAll(".rangebtns .btn").forEach((b) => {
    b.onclick = () => { renderUsage._hours = Number(b.dataset.hours); renderUsage(el); };
  });

  const money = (v) => `$${Number(v ?? 0).toFixed(2)}`;
  const bar = (v, max) =>
    `<div class="ubar"><i style="width:${v > 0 && max > 0 ? Math.max(1, v / max * 100) : 0}%"></i></div>`;
  const panel = (title, inner) =>
    `<div class="card upanel"><h2 class="section">${title}</h2>${inner}</div>`;
  const breakdown = (title, rows, label) => {
    const maxCost = Math.max(...rows.map((r) => r.cost_usd), 0);
    return panel(title, rows.length ? `<table>
      <tr><th></th><th class="num">コスト</th><th></th><th class="num">トークン</th></tr>
      ${rows.map((r) => `<tr>
        <td class="mono">${esc(r[label])}</td>
        <td class="num mono">${money(r.cost_usd)}</td>
        <td>${bar(r.cost_usd, maxCost)}</td>
        <td class="num mono faint">${num(r.tokens)}</td>
      </tr>`).join("")}</table>` : '<span class="faint">期間内のデータがありません。</span>');
  };

  (async () => {
    const box = $("#usageBody", el);
    let U;
    try { U = await j(`/api/usage?hours=${hours}`); }
    catch (e) {
      box.innerHTML = `<div class="note warn"><span class="tag">取得失敗</span><span>${esc(e.message)} — NAS の Prometheus(9090)に届いているか確認してください。</span></div>`;
      return;
    }
    $("#grafanaLink", el).href = U.grafana;
    $("#usageStamp", el).textContent = `取得 ${U.fetched_at}`;
    const t = U.totals;
    const maxDay = Math.max(...U.daily.map((d) => d.cost_usd), 0);
    const maxTok = Math.max(...U.by_type.map((x) => x.tokens), 0);
    box.innerHTML = `
      <div class="stats">
        <div class="stat card"><div class="n">${money(t.cost_usd)}</div><div class="l">コスト</div></div>
        <div class="stat card"><div class="n">${num(t.tokens)}</div><div class="l">トークン</div></div>
        <div class="stat card"><div class="n">${num(t.sessions)}</div><div class="l">セッション</div></div>
        <div class="stat card"><div class="n">${(Number(t.active_seconds ?? 0) / 3600).toFixed(1)}<small>h</small></div><div class="l">アクティブ時間</div></div>
      </div>
      <div class="ugrid">
      ${U.daily.length ? panel("日別コスト",
        U.daily.map((d) => `<div class="urow">
          <span class="mono faint">${esc(d.date)}</span>
          ${bar(d.cost_usd ?? 0, maxDay)}
          <span class="mono">${d.cost_usd == null ? '<span class="faint">欠測</span>' : money(d.cost_usd)}</span>
        </div>`).join("")) : ""}
      ${breakdown("端末別", U.by_host, "host")}
      ${breakdown("モデル別", U.by_model, "model")}
      ${panel("トークン種別", U.by_type.length ? `<table>
        <tr><th></th><th class="num">トークン</th><th></th></tr>
        ${U.by_type.map((r) => `<tr><td class="mono">${esc(r.type)}</td>
            <td class="num mono">${num(r.tokens)}</td>
            <td>${bar(r.tokens, maxTok)}</td></tr>`).join("")}</table>`
        : '<span class="faint">期間内のデータがありません。</span>')}
      </div>`;
  })();
}

/* ---------------- router ---------------- */

const RENDER = { overview: renderOverview, context: renderContext, facts: renderFacts,
  shelf: renderShelf, kanribo: renderKanribo, skills: renderSkills, hooks: renderHooks,
  routing: renderRouting, messages: renderMessages, collect: renderCollect, usage: renderUsage };

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
    if (!N.auto_memory || !N.device_projects) N = await j("/api/nas?refresh=1");  // 旧キャッシュ対策
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
