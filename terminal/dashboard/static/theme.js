/* theme.js — ライト/ダーク切替。app.js の後に読み込む(app.js は変更しない)。
   ・トップバー右端にトグルを差し込み、選択を localStorage に保存
   ・app.js がドーナツの残量アークに rgba(255,255,255,.06) を直書きしているため、
     ライトでは白地に白で消える。描画後に var(--track) へ差し替える。 */
(function () {
  "use strict";
  var KEY = "nasmem.theme";
  var LITERAL = "rgba(255,255,255,.06)";

  function saved() {
    try { return localStorage.getItem(KEY) || "light"; } catch (e) { return "light"; }
  }

  // DOM を待たずに属性だけ先に立てる(ダーク選択時に初回描画でライトが一瞬見えるのを防ぐ)。
  // DOM 依存の処理(トグル生成・apply・fixDonuts)は init 側に残す
  document.documentElement.dataset.theme = saved();

  function label(t) {
    var btn = document.getElementById("themeToggle");
    if (!btn) return;
    btn.innerHTML = t === "dark"
      ? '<span class="ic">\u25D0</span>ダーク'
      : '<span class="ic">\u25D1</span>ライト';
    btn.title = t === "dark" ? "ライトに切替" : "ダークに切替";
  }

  function apply(t) {
    document.documentElement.dataset.theme = t;
    label(t);
  }

  function fixDonuts() {
    var list = document.querySelectorAll(".donut");
    for (var i = 0; i < list.length; i++) {
      var bg = list[i].style.background || "";
      if (bg.indexOf(LITERAL) !== -1) {
        list[i].style.background = bg.split(LITERAL).join("var(--track)");
      }
    }
  }

  function init() {
    var t = saved();

    var right = document.querySelector(".topbar-right");
    if (right) {
      var btn = document.createElement("button");
      btn.id = "themeToggle";
      btn.className = "theme-toggle";
      btn.addEventListener("click", function () {
        var next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
        apply(next);
        try { localStorage.setItem(KEY, next); } catch (e) {}
      });
      right.insertBefore(btn, right.firstChild);
    }
    apply(t);

    var content = document.getElementById("content");
    if (content && window.MutationObserver) {
      new MutationObserver(fixDonuts).observe(content, { childList: true, subtree: true });
    }
    fixDonuts();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
