/* The NVMe + PCIe Engineering Journey — shared behaviour.
   Plain ES2017, no dependencies; works from file:// and GitHub Pages.
   Everything here is progressive enhancement: the pages read fine without it. */
(function () {
  "use strict";

  var body = document.body;
  var root = document.documentElement;
  var PAGE = body.getAttribute("data-page") || "";
  var BOOK = body.getAttribute("data-mode") === "book";
  var TOTAL = parseInt(body.getAttribute("data-total") || "0", 10);
  var PREFIX = "nvme-journey:";

  function store(key, val) {
    try {
      if (val === undefined) return localStorage.getItem(PREFIX + key);
      if (val === null) localStorage.removeItem(PREFIX + key);
      else localStorage.setItem(PREFIX + key, val);
    } catch (e) { return null; }
    return null;
  }
  function $(sel, ctx) { return (ctx || document).querySelector(sel); }
  function $$(sel, ctx) { return Array.prototype.slice.call((ctx || document).querySelectorAll(sel)); }
  function esc(s) { return String(s).replace(/[&<>"]/g, function (c) { return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]; }); }

  /* ---------- Theme ---------- */
  var themeBtn = $("#theme-toggle");
  function applyTheme(t) {
    root.setAttribute("data-theme", t);
    if (themeBtn) themeBtn.textContent = t === "dark" ? "☀" : "☾";
  }
  applyTheme(root.getAttribute("data-theme") || "light");
  if (themeBtn) themeBtn.addEventListener("click", function () {
    var t = root.getAttribute("data-theme") === "dark" ? "light" : "dark";
    applyTheme(t); store("theme", t);
  });

  /* ---------- Mobile sidebar ---------- */
  var menuBtn = $("#menu-toggle");
  if (menuBtn) menuBtn.addEventListener("click", function () { body.classList.toggle("nav-open"); });
  document.addEventListener("click", function (e) {
    if (!body.classList.contains("nav-open")) return;
    if (e.target.closest(".sidebar") || e.target.closest("#menu-toggle")) return;
    body.classList.remove("nav-open");
  });
  var current = $(".sidebar a.current");
  if (current && current.scrollIntoView) current.scrollIntoView({ block: "center" });
  var sbExpand = $("#sb-expand"), sbCollapse = $("#sb-collapse");
  if (sbExpand) sbExpand.addEventListener("click", function () { $$(".sidebar details").forEach(function (d) { d.open = true; }); });
  if (sbCollapse) sbCollapse.addEventListener("click", function () {
    $$(".sidebar details").forEach(function (d) { d.open = !!d.querySelector("a.current"); });
  });

  /* ---------- Progress tracking ---------- */
  function doneSet() {
    try { return JSON.parse(store("done") || "{}") || {}; } catch (e) { return {}; }
  }
  function saveDone(s) { store("done", JSON.stringify(s)); }
  function refreshProgress() {
    var s = doneSet();
    var n = 0;
    $$(".sidebar a[data-id]").forEach(function (a) {
      var d = !!s[a.getAttribute("data-id")];
      a.classList.toggle("done", d);
      if (d) n++;
    });
    var cp = $("#course-progress");
    if (cp && TOTAL) cp.textContent = n + " / " + TOTAL + " complete (" + Math.round((100 * n) / TOTAL) + "%)";
    var btn = $("#mark-complete");
    if (btn) {
      var d = !!s[PAGE];
      btn.classList.toggle("done", d);
      btn.textContent = d ? "✓ Completed" : "Mark this page complete";
    }
  }
  var markBtn = $("#mark-complete");
  if (markBtn) markBtn.addEventListener("click", function () {
    var s = doneSet();
    if (s[PAGE]) delete s[PAGE]; else s[PAGE] = Date.now();
    saveDone(s); refreshProgress();
  });
  refreshProgress();

  /* ---------- Reading progress + back to top ---------- */
  var bar = $(".read-progress"), toTop = $(".to-top");
  function onScroll() {
    var h = document.documentElement.scrollHeight - window.innerHeight;
    var p = h > 0 ? window.scrollY / h : 0;
    if (bar) bar.style.width = (p * 100).toFixed(2) + "%";
    if (toTop) toTop.classList.toggle("show", window.scrollY > 700);
  }
  window.addEventListener("scroll", onScroll, { passive: true });
  onScroll();
  if (toTop) toTop.addEventListener("click", function () { window.scrollTo({ top: 0, behavior: "smooth" }); });

  /* ---------- Collapsible sections ---------- */
  var sections = $$(".content section").filter(function (s) { return s.firstElementChild && s.firstElementChild.tagName === "H2"; });
  var collapsedKey = "collapsed:" + PAGE;
  var collapsed = {};
  try { collapsed = JSON.parse(store(collapsedKey) || "{}") || {}; } catch (e) { collapsed = {}; }
  sections.forEach(function (sec) {
    var h = sec.firstElementChild;
    var b = document.createElement("button");
    b.type = "button"; b.className = "sec-toggle";
    function sync() {
      var c = sec.classList.contains("collapsed");
      b.textContent = c ? "Expand" : "Collapse";
      b.setAttribute("aria-expanded", c ? "false" : "true");
    }
    b.addEventListener("click", function () {
      sec.classList.toggle("collapsed");
      if (sec.id) {
        if (sec.classList.contains("collapsed")) collapsed[sec.id] = 1; else delete collapsed[sec.id];
        if (!BOOK) store(collapsedKey, JSON.stringify(collapsed));
      }
      sync();
    });
    if (!BOOK && sec.id && collapsed[sec.id] && location.hash.slice(1) !== sec.id) sec.classList.add("collapsed");
    h.appendChild(b); sync();
    sec._sync = sync;
  });
  function setAll(c) {
    sections.forEach(function (s) { s.classList.toggle("collapsed", c); if (s._sync) s._sync(); });
    collapsed = {};
    if (c) sections.forEach(function (s) { if (s.id) collapsed[s.id] = 1; });
    if (!BOOK) store(collapsedKey, JSON.stringify(collapsed));
  }
  var ea = $("#expand-all"), ca = $("#collapse-all");
  if (ea) ea.addEventListener("click", function () { setAll(false); });
  if (ca) ca.addEventListener("click", function () { setAll(true); });
  function revealHash() {
    var id = decodeURIComponent(location.hash.slice(1));
    if (!id) return;
    var el = document.getElementById(id);
    if (!el) return;
    var sec = el.closest ? el.closest("section.collapsed") : null;
    if (sec) { sec.classList.remove("collapsed"); if (sec._sync) sec._sync(); }
    var det = el.closest ? el.closest("details") : null;
    while (det) { det.open = true; det = det.parentElement && det.parentElement.closest ? det.parentElement.closest("details") : null; }
  }
  window.addEventListener("hashchange", revealHash);
  revealHash();

  /* ---------- Mastery checklists ---------- */
  $$("ul.checklist").forEach(function (ul, ui) {
    $$(":scope > li", ul).forEach(function (li, i) {
      var key = "check:" + PAGE + ":" + ui + ":" + i;
      var cb = document.createElement("input");
      cb.type = "checkbox";
      cb.checked = store(key) === "1";
      li.classList.toggle("checked", cb.checked);
      cb.addEventListener("change", function () { store(key, cb.checked ? "1" : null); li.classList.toggle("checked", cb.checked); });
      li.insertBefore(cb, li.firstChild);
    });
  });

  /* ---------- Interactive multiple choice ---------- */
  $$("ul.options[data-answer]").forEach(function (ul) {
    var ans = parseInt(ul.getAttribute("data-answer"), 10);
    var items = $$(":scope > li", ul);
    if (!ans || ans > items.length) return;
    ul.classList.add("interactive");
    items.forEach(function (li, i) {
      li.setAttribute("tabindex", "0");
      li.setAttribute("role", "button");
      function pick() {
        items.forEach(function (x) { x.classList.remove("right", "wrong"); });
        li.classList.add(i + 1 === ans ? "right" : "wrong");
        if (i + 1 !== ans) items[ans - 1].classList.add("right");
      }
      li.addEventListener("click", pick);
      li.addEventListener("keydown", function (e) { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); pick(); } });
    });
  });

  /* ---------- Copy buttons on code ---------- */
  $$(".codeblock").forEach(function (cb) {
    var head = $(".codeblock-head", cb), pre = $("pre", cb);
    if (!head || !pre || !navigator.clipboard) return;
    var b = document.createElement("button");
    b.type = "button"; b.className = "icon-btn copy"; b.textContent = "Copy";
    b.addEventListener("click", function () {
      navigator.clipboard.writeText(pre.innerText).then(function () {
        b.textContent = "Copied"; setTimeout(function () { b.textContent = "Copy"; }, 1200);
      }, function () {});
    });
    head.appendChild(b);
  });

  /* ---------- Glossary popups ---------- */
  var GL = window.NVME_GLOSSARY || {};
  var pop = null;
  function hidePop() { if (pop) { pop.remove(); pop = null; } }
  function showPop(a) {
    var key = a.getAttribute("data-term");
    var g = GL[key];
    if (!g) return;
    hidePop();
    pop = document.createElement("div");
    pop.className = "gpop"; pop.setAttribute("role", "tooltip");
    pop.innerHTML = '<div class="gt">' + esc(g.t) + "</div>" +
      (g.x ? '<div class="gx">' + esc(g.x) + "</div>" : "") +
      "<div>" + g.d + "</div>" +
      '<div class="gl"><a href="' + esc(a.getAttribute("href")) + '">Open in glossary →</a></div>';
    document.body.appendChild(pop);
    var r = a.getBoundingClientRect();
    var left = Math.min(window.scrollX + r.left, window.scrollX + document.documentElement.clientWidth - pop.offsetWidth - 12);
    pop.style.left = Math.max(window.scrollX + 8, left) + "px";
    var top = window.scrollY + r.bottom + 6;
    if (r.bottom + pop.offsetHeight + 12 > window.innerHeight && r.top > pop.offsetHeight + 60) top = window.scrollY + r.top - pop.offsetHeight - 6;
    pop.style.top = top + "px";
  }
  var hoverTimer = null;
  document.addEventListener("mouseover", function (e) {
    var a = e.target.closest && e.target.closest("a.g");
    if (!a) return;
    clearTimeout(hoverTimer);
    hoverTimer = setTimeout(function () { showPop(a); }, 220);
  });
  document.addEventListener("mouseout", function (e) {
    var a = e.target.closest && e.target.closest("a.g");
    if (a) clearTimeout(hoverTimer);
  });
  document.addEventListener("click", function (e) {
    var a = e.target.closest && e.target.closest("a.g");
    if (a && GL[a.getAttribute("data-term")] && (!pop || pop._for !== a)) {
      if (window.matchMedia && window.matchMedia("(hover: none)").matches) {
        e.preventDefault(); showPop(a); if (pop) pop._for = a; return;
      }
    }
    if (pop && !(e.target.closest && e.target.closest(".gpop"))) hidePop();
  });
  document.addEventListener("keydown", function (e) { if (e.key === "Escape") { hidePop(); closeSearch(); } });
  window.addEventListener("scroll", function () { if (pop && !pop.matches(":hover")) hidePop(); }, { passive: true });

  /* Glossary page filter */
  var gf = $("#glossary-filter");
  if (gf) gf.addEventListener("input", function () {
    var q = gf.value.trim().toLowerCase();
    $$(".glossary-list dt").forEach(function (dt) {
      var dd = dt.nextElementSibling;
      var hit = !q || (dt.textContent + " " + (dd ? dd.textContent : "")).toLowerCase().indexOf(q) >= 0;
      dt.style.display = hit ? "" : "none";
      if (dd) dd.style.display = hit ? "" : "none";
    });
  });

  /* ---------- Search ---------- */
  var sInput = $("#search-input"), sOut = $("#search-results");
  var loading = false, activeIdx = -1;
  function closeSearch() { if (sOut) { sOut.classList.remove("open"); activeIdx = -1; } }
  function ensureIndex(cb) {
    if (window.NVME_SEARCH) return cb();
    if (loading) return;
    loading = true;
    var s = document.createElement("script");
    s.src = body.getAttribute("data-search-src") || "assets/search-index.js";
    s.onload = function () { loading = false; cb(); };
    s.onerror = function () { loading = false; if (sOut) { sOut.innerHTML = '<div class="sr-empty">Search index could not be loaded.</div>'; sOut.classList.add("open"); } };
    document.head.appendChild(s);
  }
  function hrefFor(r) {
    if (BOOK) return "#" + r.p + (r.a ? "--" + r.a : "");
    return r.p + ".html" + (r.a ? "#" + r.a : "");
  }
  function runSearch() {
    var q = sInput.value.trim().toLowerCase();
    if (q.length < 2) { closeSearch(); return; }
    var terms = q.split(/\s+/).filter(Boolean);
    var idx = window.NVME_SEARCH || [];
    var hits = [];
    for (var i = 0; i < idx.length; i++) {
      var r = idx[i];
      var hay = r.l;
      var score = 0, ok = true;
      for (var j = 0; j < terms.length; j++) {
        var t = terms[j];
        var inH = r.hl.indexOf(t) >= 0, inT = r.tl.indexOf(t) >= 0, inB = hay.indexOf(t) >= 0;
        if (!inH && !inT && !inB) { ok = false; break; }
        score += (inH ? 6 : 0) + (inT ? 4 : 0) + (inB ? 1 : 0);
      }
      if (ok) hits.push({ r: r, s: score });
    }
    hits.sort(function (a, b) { return b.s - a.s; });
    hits = hits.slice(0, 40);
    var re = new RegExp("(" + terms.map(function (t) { return t.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"); }).join("|") + ")", "gi");
    if (!hits.length) {
      sOut.innerHTML = '<div class="sr-empty">No matches for “' + esc(q) + '”.</div>';
    } else {
      sOut.innerHTML = hits.map(function (h) {
        var r = h.r;
        var snip = r.s;
        var pos = snip.toLowerCase().indexOf(terms[0]);
        if (pos > 60) snip = "…" + snip.slice(pos - 50);
        if (snip.length > 170) snip = snip.slice(0, 170) + "…";
        return '<a href="' + hrefFor(r) + '"><div class="sr-page">' + esc(r.t) + '</div><div class="sr-head">' +
          esc(r.h).replace(re, "<mark>$1</mark>") + '</div><div class="sr-snip">' + esc(snip).replace(re, "<mark>$1</mark>") + "</div></a>";
      }).join("");
    }
    activeIdx = -1;
    sOut.classList.add("open");
  }
  if (sInput && sOut) {
    var deb = null;
    sInput.addEventListener("focus", function () { ensureIndex(function () { if (sInput.value.trim().length > 1) runSearch(); }); });
    sInput.addEventListener("input", function () { clearTimeout(deb); deb = setTimeout(function () { ensureIndex(runSearch); }, 120); });
    sInput.addEventListener("keydown", function (e) {
      var items = $$("a", sOut);
      if (e.key === "ArrowDown" || e.key === "ArrowUp") {
        if (!items.length) return;
        e.preventDefault();
        activeIdx = (activeIdx + (e.key === "ArrowDown" ? 1 : -1) + items.length) % items.length;
        items.forEach(function (a, i) { a.classList.toggle("active", i === activeIdx); });
        items[activeIdx].scrollIntoView({ block: "nearest" });
      } else if (e.key === "Enter" && items.length) {
        e.preventDefault();
        (items[activeIdx >= 0 ? activeIdx : 0]).click();
      }
    });
    sOut.addEventListener("click", function (e) { if (e.target.closest("a")) closeSearch(); });
    document.addEventListener("click", function (e) { if (!e.target.closest(".search")) closeSearch(); });
    document.addEventListener("keydown", function (e) {
      if (e.key === "/" && !/input|textarea|select/i.test((document.activeElement || {}).tagName || "")) { e.preventDefault(); sInput.focus(); }
    });
  }

  /* ---------- Print: open every collapsible ---------- */
  var reopen = [];
  window.addEventListener("beforeprint", function () {
    reopen = $$("details:not([open])");
    reopen.forEach(function (d) { d.open = true; });
  });
  window.addEventListener("afterprint", function () { reopen.forEach(function (d) { d.open = false; }); reopen = []; });
})();
