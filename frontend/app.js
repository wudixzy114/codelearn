"use strict";

// ------- 小工具 -------
const $ = (sel, root = document) => root.querySelector(sel);
const el = (tag, cls, txt) => {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (txt != null) e.textContent = txt;
  return e;
};
const esc = (s) => s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");

async function api(path, opts) {
  const res = await fetch(path, opts);
  if (!res.ok) {
    let msg = res.status + " " + res.statusText;
    try { const j = await res.json(); if (j.detail) msg = j.detail; } catch (_) {}
    throw new Error(msg);
  }
  return res.json();
}
const postJSON = (path, body) =>
  api(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });

// 语言 → 行注释前缀（把 AI 讲解以“该语言的注释”形态注入代码流）
const COMMENT_TOKEN = {
  cpp: "//", c: "//", rust: "//", go: "//", java: "//", kotlin: "//",
  javascript: "//", typescript: "//", php: "//", protobuf: "//",
  python: "#", bash: "#", ruby: "#", yaml: "#", toml: "#", ini: "#",
  cmake: "#", text: "#", json: "//", markdown: "",
};
const commentToken = (lang) => (lang in COMMENT_TOKEN ? COMMENT_TOKEN[lang] : "//");

// kind → 默认折叠（版权头 / include 区）
const COLLAPSED_KINDS = new Set(["license", "includes"]);

const state = {
  config: null,
  activeFilePath: null,
  roadmap: null,          // 当前路线图对象
  roadmapOrder: [],       // 扁平化的文件阅读顺序（用于“下一文件”预取）
  editMode: false,        // 教案编辑模式
  // 当前打开的文件视图（用于预加载完成后的响应式刷新）
  currentFile: null,      // { path, content, language, explained: bool }
  // 右侧对话
  chat: { history: [], streaming: false, context: null },
};

// ================= 初始化 =================
async function init() {
  bindTabs();
  bindLangSelect();
  $("#btnRoadmap").addEventListener("click", () => loadRoadmap(false));
  $("#btnRoadmapRegen").addEventListener("click", () => loadRoadmap(true));
  $("#btnRoadmapEdit").addEventListener("click", toggleEdit);
  $("#btnRoadmapSave").addEventListener("click", saveRoadmap);
  bindChatUI();

  try {
    state.config = await api("/api/config");
    $("#repoName").textContent = state.config.target_name;
    $("#langSelect").value = state.config.language;
  } catch (e) {
    $("#repoName").textContent = "(配置读取失败)";
  }
  checkHealth();
  loadTree();
  autoLoadRoadmap();

  const params = new URLSearchParams(location.search);
  if (params.get("open")) openFile(params.get("open"));
  else if (params.get("folder")) openFolder(params.get("folder"));
}

async function autoLoadRoadmap() {
  try {
    const res = await fetch("/api/roadmap/cached");
    if (!res.ok) return;
    const data = await res.json();
    if (data && data.steps) setRoadmap(data);
  } catch (_) {}
}

function bindTabs() {
  document.querySelectorAll(".tab").forEach((tab) => {
    tab.addEventListener("click", () => {
      document.querySelectorAll(".tab").forEach((t) => t.classList.remove("active"));
      tab.classList.add("active");
      const name = tab.dataset.tab;
      $("#panel-roadmap").classList.toggle("hidden", name !== "roadmap");
      $("#panel-tree").classList.toggle("hidden", name !== "tree");
    });
  });
}

function bindLangSelect() {
  $("#langSelect").addEventListener("change", async (e) => {
    try {
      state.config = await postJSON("/api/config/language", { language: e.target.value });
    } catch (_) {}
  });
}

async function checkHealth() {
  const dot = $("#healthDot");
  try {
    const h = await api("/api/health");
    if (h.llm && h.llm.ok) {
      dot.className = "health ok";
      dot.title = "LLM 网关正常 · " + (h.llm.model || "");
    } else {
      dot.className = "health bad";
      dot.title = "LLM 不可用：" + ((h.llm && h.llm.error) || "未知");
    }
  } catch (e) {
    dot.className = "health bad";
    dot.title = "健康检查失败：" + e.message;
  }
}

// ================= 预加载队列（改进 1） =================
const preload = {
  queue: [],              // 待预加载的 path
  inflight: new Set(),    // 正在预加载
  done: new Set(),        // 已就绪（本会话内已知）
  concurrency: 2,
  running: 0,
};

function preloadEnqueue(paths) {
  for (const p of paths) {
    if (!p || preload.done.has(p) || preload.inflight.has(p) || preload.queue.includes(p)) continue;
    preload.queue.push(p);
  }
  updatePreloadBadge();
  pumpPreload();
}

function pumpPreload() {
  while (preload.running < preload.concurrency && preload.queue.length) {
    const path = preload.queue.shift();
    preload.inflight.add(path);
    preload.running++;
    updatePreloadBadge();
    postJSON("/api/explain", { path, force: false })
      .then((exp) => { preload.done.add(path); markReady(path); onExplained(path, exp); })
      .catch(() => {})
      .finally(() => {
        preload.inflight.delete(path);
        preload.running--;
        updatePreloadBadge();
        pumpPreload();
      });
  }
}

function updatePreloadBadge() {
  const badge = $("#preloadBadge");
  const pending = preload.queue.length + preload.inflight.size;
  if (pending > 0) {
    badge.classList.remove("hidden");
    badge.textContent = "预加载中 " + pending;
  } else {
    badge.classList.add("hidden");
  }
}

// 标记某文件“讲解已就绪”，点亮所有对应徽标
function markReady(path) {
  preload.done.add(path);
  document.querySelectorAll('.ready-dot[data-path="' + cssEsc(path) + '"]').forEach((d) => {
    d.classList.add("on");
    d.title = "讲解已就绪";
  });
}
const cssEsc = (s) => s.replace(/["\\]/g, "\\$&");

// 讲解就绪回调：若正是当前打开、且仍为纯代码态的文件，则立即刷新为讲解态（修复响应式 bug）
function onExplained(path, exp) {
  const cur = state.currentFile;
  if (!cur || cur.path !== path || cur.explained) return;
  if (!exp || !exp.blocks || !exp.blocks.length) return;
  cur.explained = true;
  const header = $("#readerHeader");
  const bodyEl = $("#readerBody");
  renderExplanation(exp, cur.content, header, bodyEl);
  const btn = header.querySelector(".btn.primary");
  if (btn) btn.textContent = "重新生成讲解";
}

// 批量查询就绪状态，点亮徽标
async function refreshReady(paths) {
  const unknown = paths.filter((p) => p && !preload.done.has(p));
  if (!unknown.length) return;
  try {
    const status = await postJSON("/api/explain/status", { paths: unknown });
    Object.entries(status).forEach(([p, ok]) => { if (ok) markReady(p); });
  } catch (_) {}
}

// ================= 学习路线图 =================
async function loadRoadmap(force) {
  const body = $("#roadmapBody");
  body.innerHTML = "";
  body.appendChild(loadingEl(force ? "正在重新生成路线图…" : "正在加载/生成路线图…"));
  try {
    const data = force
      ? await postJSON("/api/roadmap/regenerate", {})
      : await api("/api/roadmap");
    setRoadmap(data);
  } catch (e) {
    body.innerHTML = "";
    body.appendChild(errorEl("路线图生成失败：" + e.message));
  }
}

// 设置路线图 + 扁平化阅读顺序 + 渲染
function setRoadmap(data) {
  state.roadmap = data;
  state.roadmapOrder = [];
  (data.steps || []).forEach((s) => {
    (s.files || []).forEach((f) => {
      const path = typeof f === "string" ? f : f.path;
      const isDir = typeof f === "object" && f.is_dir;
      if (path && !isDir) state.roadmapOrder.push(path);
    });
  });
  renderRoadmap();
  // 点亮已就绪徽标
  refreshReady(state.roadmapOrder);
}

function renderRoadmap() {
  const data = state.roadmap;
  const body = $("#roadmapBody");
  body.innerHTML = "";
  $("#btnRoadmapEdit").classList.toggle("hidden", !data);
  $("#btnRoadmapSave").classList.toggle("hidden", !state.editMode);
  if (!data) return;

  if (data.summary || data.title) {
    const sum = el("div", "roadmap-summary");
    if (data.title) {
      if (state.editMode) {
        const inp = el("input", "edit-input title");
        inp.value = data.title;
        inp.addEventListener("change", () => (data.title = inp.value));
        sum.appendChild(inp);
      } else {
        sum.appendChild(el("b", null, data.title));
      }
    }
    if (data.summary) {
      sum.appendChild(document.createElement("br"));
      sum.appendChild(document.createTextNode(data.summary));
    }
    if (data.edited) sum.appendChild(el("span", "edited-tag", "已微调"));
    body.appendChild(sum);
  }

  (data.steps || []).forEach((step, i) => body.appendChild(renderStep(step, i)));
}

function renderStep(step, i) {
  const wrap = el("div", "step" + (state.editMode ? " editing" : ""));
  const head = el("div", "step-head");
  head.appendChild(el("span", "step-num", String(i + 1)));

  const titleWrap = el("div", "step-title");
  if (state.editMode) {
    const inp = el("input", "edit-input");
    inp.value = step.title || "";
    inp.addEventListener("change", () => (step.title = inp.value));
    inp.addEventListener("click", (e) => e.stopPropagation());
    titleWrap.appendChild(inp);
  } else {
    titleWrap.appendChild(document.createTextNode(step.title || "步骤 " + (i + 1)));
    if (step.goal) titleWrap.appendChild(el("div", "step-goal", step.goal));
  }
  head.appendChild(titleWrap);

  if (state.editMode) {
    head.appendChild(stepEditControls(i));
  } else {
    // 预加载本步按钮
    const files = (step.files || [])
      .map((f) => (typeof f === "string" ? f : f.path))
      .filter(Boolean);
    const pl = el("button", "mini-btn", "预加载");
    pl.title = "后台预生成本步全部文件的讲解";
    pl.addEventListener("click", (e) => { e.stopPropagation(); preloadEnqueue(files); });
    head.appendChild(pl);
  }

  head.addEventListener("click", () => wrap.classList.toggle("open"));
  wrap.appendChild(head);

  const bodyEl = el("div", "step-body");
  if (!state.editMode && step.description) bodyEl.appendChild(el("div", "step-desc", step.description));
  (step.files || []).forEach((f, fi) => bodyEl.appendChild(renderFileLink(step, i, fi, f)));
  wrap.appendChild(bodyEl);
  if (i === 0 || state.editMode) wrap.classList.add("open");
  return wrap;
}

function renderFileLink(step, si, fi, f) {
  const path = typeof f === "string" ? f : f.path;
  const isDir = typeof f === "object" && f.is_dir;
  const link = el("div", "file-link");

  const dot = el("span", "ready-dot");
  dot.dataset.path = path;
  if (preload.done.has(path)) dot.classList.add("on");
  link.appendChild(dot);
  link.appendChild(el("span", "fic", isDir ? "▸" : "≡"));
  link.appendChild(document.createTextNode(path));

  if (state.editMode) {
    link.appendChild(fileEditControls(step, fi));
  } else {
    link.addEventListener("click", () => isDir ? openFolder(path) : openFile(path));
  }
  return link;
}

// ---- 教案编辑控件（改进 3） ----
function stepEditControls(i) {
  const box = el("div", "edit-ctrls");
  const steps = state.roadmap.steps;
  box.appendChild(ctrlBtn("↑", "上移步骤", i > 0, (e) => { e.stopPropagation(); swap(steps, i, i - 1); renderRoadmap(); }));
  box.appendChild(ctrlBtn("↓", "下移步骤", i < steps.length - 1, (e) => { e.stopPropagation(); swap(steps, i, i + 1); renderRoadmap(); }));
  box.appendChild(ctrlBtn("✕", "删除步骤", true, (e) => {
    e.stopPropagation(); steps.splice(i, 1); renderRoadmap();
  }, "danger"));
  return box;
}

function fileEditControls(step, fi) {
  const box = el("div", "edit-ctrls");
  const files = step.files;
  box.appendChild(ctrlBtn("↑", "上移", fi > 0, (e) => { e.stopPropagation(); swap(files, fi, fi - 1); renderRoadmap(); }));
  box.appendChild(ctrlBtn("↓", "下移", fi < files.length - 1, (e) => { e.stopPropagation(); swap(files, fi, fi + 1); renderRoadmap(); }));
  box.appendChild(ctrlBtn("✕", "移除", true, (e) => { e.stopPropagation(); files.splice(fi, 1); renderRoadmap(); }, "danger"));
  return box;
}

function ctrlBtn(txt, title, enabled, onClick, extra) {
  const b = el("button", "ctrl-btn" + (extra ? " " + extra : ""), txt);
  b.title = title;
  if (!enabled) b.disabled = true;
  else b.addEventListener("click", onClick);
  return b;
}
function swap(arr, a, b) { const t = arr[a]; arr[a] = arr[b]; arr[b] = t; }

function toggleEdit() {
  if (!state.roadmap) return;
  state.editMode = !state.editMode;
  $("#btnRoadmapEdit").textContent = state.editMode ? "退出编辑" : "编辑";
  renderRoadmap();
}

async function saveRoadmap() {
  if (!state.roadmap) return;
  const btn = $("#btnRoadmapSave");
  btn.disabled = true;
  btn.textContent = "保存中…";
  try {
    const saved = await postJSON("/api/roadmap/save", { roadmap: state.roadmap });
    state.editMode = false;
    $("#btnRoadmapEdit").textContent = "编辑";
    setRoadmap(saved);
  } catch (e) {
    alert("保存失败：" + e.message);
  } finally {
    btn.disabled = false;
    btn.textContent = "保存微调";
  }
}

// ================= 文件树 =================
async function loadTree() {
  const body = $("#treeBody");
  body.innerHTML = "";
  try {
    const root = await api("/api/tree?path=");
    body.appendChild(renderTreeLevel(root));
  } catch (e) {
    body.appendChild(errorEl("加载文件树失败：" + e.message));
  }
}

function renderTreeLevel(listing) {
  const frag = document.createDocumentFragment();
  listing.dirs.forEach((d) => frag.appendChild(makeDirNode(d)));
  listing.files.forEach((f) => frag.appendChild(makeFileNode(f)));
  return frag;
}

function makeDirNode(d) {
  const node = el("div", "tree-node");
  const row = el("div", "tree-row");
  const caret = el("span", "tree-caret", "▸");
  row.appendChild(caret);
  row.appendChild(el("span", "tree-icon dir", "▤"));
  row.appendChild(document.createTextNode(d.name));
  const children = el("div", "tree-children");
  children.style.display = "none";
  let loaded = false;
  row.addEventListener("click", async (ev) => {
    ev.stopPropagation();
    if (ev.target === row || ev.target.classList.contains("tree-icon")) openFolder(d.path);
    const open = children.style.display === "none";
    children.style.display = open ? "block" : "none";
    caret.classList.toggle("open", open);
    if (open && !loaded) {
      loaded = true;
      children.appendChild(loadingEl("…"));
      try {
        const sub = await api("/api/tree?path=" + encodeURIComponent(d.path));
        children.innerHTML = "";
        children.appendChild(renderTreeLevel(sub));
        // 点亮该层已就绪文件
        refreshReady(sub.files.filter((f) => f.code).map((f) => f.path));
      } catch (e) {
        children.innerHTML = "";
        children.appendChild(errorEl(e.message));
      }
    }
  });
  node.appendChild(row);
  node.appendChild(children);
  return node;
}

function makeFileNode(f) {
  const node = el("div", "tree-node");
  const row = el("div", "tree-row");
  row.appendChild(el("span", "tree-caret", ""));
  const dot = el("span", "ready-dot");
  dot.dataset.path = f.path;
  if (preload.done.has(f.path)) dot.classList.add("on");
  row.appendChild(dot);
  row.appendChild(el("span", "tree-icon file", "≡"));
  row.appendChild(document.createTextNode(f.name));
  row.dataset.path = f.path;
  row.addEventListener("click", (ev) => {
    ev.stopPropagation();
    document.querySelectorAll(".tree-row.active").forEach((r) => r.classList.remove("active"));
    row.classList.add("active");
    if (f.binary) showError("二进制文件不支持讲解：" + f.path);
    else openFile(f.path);
  });
  node.appendChild(row);
  return node;
}

// ================= 打开文件 =================
async function openFile(path) {
  state.activeFilePath = path;
  const header = $("#readerHeader");
  const bodyEl = $("#readerBody");
  header.innerHTML = "";
  bodyEl.innerHTML = "";

  const pathRow = el("div", "file-path-row");
  pathRow.appendChild(el("div", "file-path", path));
  const actions = el("div", "reader-actions");
  const explainBtn = el("button", "btn primary", "生成逐块讲解");
  actions.appendChild(explainBtn);
  pathRow.appendChild(actions);
  header.appendChild(pathRow);
  const metaEl = el("div", "file-meta", "加载中…");
  header.appendChild(metaEl);

  let fileData;
  try {
    fileData = await api("/api/file?path=" + encodeURIComponent(path));
  } catch (e) {
    showError("读取文件失败：" + e.message);
    return;
  }
  metaEl.textContent = `${fileData.language} · ${fileData.total_lines} 行`;

  state.currentFile = {
    path, content: fileData.content, language: fileData.language, explained: false,
  };
  // 打开新文件时，清掉右栏的“引用选中代码”上下文（保留对话历史）
  clearChatContext();

  if (fileData.explanation && fileData.explanation.blocks && fileData.explanation.blocks.length) {
    markReady(path);
    state.currentFile.explained = true;
    renderExplanation(fileData.explanation, fileData.content, header, bodyEl);
    explainBtn.textContent = "重新生成讲解";
  } else {
    renderCodeOnly(fileData.content, fileData.language, bodyEl);
    // 未就绪 → 自动后台预加载当前文件，无需手动点（完成后 onExplained 会刷新）
    preloadEnqueue([path]);
  }
  explainBtn.addEventListener("click", () => runExplain(path, fileData.content, header, bodyEl, explainBtn));

  // 自动预取路线图里的“下一个文件”（改进 1）
  prefetchNext(path);
}

function prefetchNext(path) {
  const idx = state.roadmapOrder.indexOf(path);
  if (idx === -1) return;
  const nexts = state.roadmapOrder.slice(idx + 1, idx + 3); // 预取接下来 2 个
  if (nexts.length) preloadEnqueue(nexts);
}

function splitLines(content) {
  const lines = content.split("\n");
  if (lines.length && lines[lines.length - 1] === "") lines.pop();
  return lines;
}

function renderCodeOnly(content, language, bodyEl) {
  bodyEl.innerHTML = "";
  const lines = splitLines(content);
  const wrap = el("div", "code-doc");
  wrap.appendChild(buildCodeBlock(lines, 1, lines.length, language, false));
  bodyEl.appendChild(wrap);
}

async function runExplain(path, content, header, bodyEl, btn) {
  btn.disabled = true;
  const oldText = btn.textContent;
  btn.textContent = "AI 讲解生成中…";
  bodyEl.innerHTML = "";
  bodyEl.appendChild(loadingEl("AI 正在逐块分析该文件，请稍候（大文件需要更久）…"));
  try {
    const exp = await postJSON("/api/explain", { path, force: true });
    markReady(path);
    if (state.currentFile && state.currentFile.path === path) state.currentFile.explained = true;
    renderExplanation(exp, content, header, bodyEl);
    btn.textContent = "重新生成讲解";
  } catch (e) {
    bodyEl.innerHTML = "";
    bodyEl.appendChild(errorEl("讲解生成失败：" + e.message));
    btn.textContent = oldText;
  } finally {
    btn.disabled = false;
  }
}

// ---- 讲解渲染：注释为主（代码内嵌），detail 为选读（右侧点击展开）----
function renderExplanation(exp, content, header, bodyEl) {
  const old = header.querySelector(".overview");
  if (old) old.remove();
  if (exp.overview || exp.role) {
    const ov = el("div", "overview");
    if (exp.role) ov.appendChild(el("span", "role-tag", exp.role));
    if (exp.overview) {
      const p = el("div", "note-text");
      p.textContent = exp.overview;
      ov.appendChild(p);
    }
    header.appendChild(ov);
  }
  if (exp.truncated) {
    header.appendChild(el("div", "file-meta", "（文件过大，讲解已截断到前 " + exp.total_lines + " 行）"));
  }

  const lines = splitLines(content);
  const lang = exp.language;
  const token = commentToken(lang);
  bodyEl.innerHTML = "";
  const doc = el("div", "code-doc annotated");

  (exp.blocks || []).forEach((b, bi) => {
    const seg = el("div", "seg");
    const hasComment = b.comment && b.kind !== "raw";

    // 行内注释（主讲解）：以该语言注释形态嵌在代码上方
    if (hasComment) {
      const cmt = el("div", "inline-comment");
      const firstLine = lines[b.start_line - 1] || "";
      const indent = (firstLine.match(/^\s*/) || [""])[0];
      const label = el("span", "cmt-token");
      label.textContent = (indent ? indent : "") + (token ? token + " " : "") + "🔎 ";
      cmt.appendChild(label);
      cmt.appendChild(document.createTextNode(b.comment));
      // “详解”小按钮：不懂时点它，在右侧对话栏展开数百字深入讲解
      const chip = el("button", "detail-chip", "详解 ↦");
      chip.title = "在右侧展开对这段代码的深入讲解";
      chip.addEventListener("click", () => explainDetail(b, exp));
      cmt.appendChild(chip);
      seg.appendChild(cmt);
    }

    // 代码块
    const collapsible = COLLAPSED_KINDS.has(b.kind);
    seg.appendChild(buildCodeBlock(lines, b.start_line, b.end_line, lang, collapsible));
    doc.appendChild(seg);
  });

  bodyEl.appendChild(doc);
}

// 构造代码块（含绝对行号 + 高亮）；collapsible 时默认折叠
function buildCodeBlock(lines, start, end, language, collapsible) {
  const cell = el("div", "code-block" + (collapsible ? " collapsible" : ""));
  const inner = el("div", "code-inner");
  const slice = lines.slice(start - 1, end).join("\n");
  let highlighted;
  try {
    highlighted = language && hljs.getLanguage(language)
      ? hljs.highlight(slice, { language }).value
      : hljs.highlightAuto(slice).value;
  } catch (_) {
    highlighted = esc(slice);
  }
  highlighted.split("\n").forEach((html, idx) => {
    const line = el("div", "code-line");
    line.appendChild(el("span", "ln", String(start + idx)));
    const lc = el("span", "lc");
    lc.innerHTML = html || " ";
    line.appendChild(lc);
    inner.appendChild(line);
  });

  if (collapsible) {
    inner.classList.add("collapsed");
    const bar = el("div", "collapse-bar", `▸ 展开 ${start}–${end} 行（${end - start + 1} 行，已折叠）`);
    bar.addEventListener("click", () => {
      const collapsed = inner.classList.toggle("collapsed");
      bar.textContent = collapsed
        ? `▸ 展开 ${start}–${end} 行（${end - start + 1} 行，已折叠）`
        : `▾ 折叠 ${start}–${end} 行`;
    });
    cell.appendChild(bar);
  }
  cell.appendChild(inner);
  return cell;
}

// ================= 打开文件夹 =================
async function openFolder(path) {
  const header = $("#readerHeader");
  const bodyEl = $("#readerBody");
  header.innerHTML = "";
  bodyEl.innerHTML = "";
  const pathRow = el("div", "file-path-row");
  pathRow.appendChild(el("div", "file-path", "📁 " + (path || state.config.target_name)));
  const actions = el("div", "reader-actions");
  const preloadBtn = el("button", "btn", "预加载本目录");
  const btn = el("button", "btn primary", "生成模块导读");
  actions.appendChild(preloadBtn);
  actions.appendChild(btn);
  pathRow.appendChild(actions);
  header.appendChild(pathRow);

  bodyEl.appendChild(loadingEl("AI 正在分析该目录…"));
  let lastData = null;
  const run = async (force) => {
    bodyEl.innerHTML = "";
    bodyEl.appendChild(loadingEl("AI 正在分析该目录…"));
    try {
      const data = await postJSON("/api/folder", { path, force });
      lastData = data;
      renderFolder(data, header, bodyEl);
      refreshReady((data.files || []).map((f) => f.path));
    } catch (e) {
      bodyEl.innerHTML = "";
      bodyEl.appendChild(errorEl("文件夹学习失败：" + e.message));
    }
  };
  btn.addEventListener("click", () => run(true));
  preloadBtn.addEventListener("click", () => {
    const files = lastData ? (lastData.files || []).map((f) => f.path) : [];
    if (files.length) preloadEnqueue(files);
    else alert("请先生成模块导读，或展开目录后再预加载。");
  });
  run(false);
}

function renderFolder(data, header, bodyEl) {
  const old = header.querySelector(".overview");
  if (old) old.remove();
  if (data.overview) {
    const ov = el("div", "overview");
    const p = el("div", "note-text");
    p.textContent = data.overview;
    ov.appendChild(p);
    if (data.notes) {
      const n = el("div", "note-text");
      n.style.marginTop = "6px";
      n.style.color = "var(--text-dim)";
      n.textContent = "阅读建议：" + data.notes;
      ov.appendChild(n);
    }
    header.appendChild(ov);
  }

  bodyEl.innerHTML = "";
  const view = el("div", "folder-view");
  const orderMap = {};
  (data.suggested_order || []).forEach((p, i) => (orderMap[p] = i + 1));

  if (data.subdirs && data.subdirs.length) {
    view.appendChild(el("div", "section-label", "子目录"));
    data.subdirs.forEach((d) => {
      const row = el("div", "folder-file");
      row.appendChild(el("span", "order-badge", "▤"));
      row.appendChild(el("span", "ff-name", d.name + "/"));
      row.addEventListener("click", () => openFolder(d.path));
      view.appendChild(row);
    });
  }

  view.appendChild(el("div", "section-label", "文件" + (data.suggested_order && data.suggested_order.length ? "（按建议顺序）" : "")));
  const files = (data.files || []).slice().sort((a, b) => {
    const oa = orderMap[a.path] || 999, ob = orderMap[b.path] || 999;
    return oa - ob;
  });
  files.forEach((f) => {
    const row = el("div", "folder-file");
    const ord = orderMap[f.path];
    row.appendChild(el("span", "order-badge", ord ? String(ord) : "·"));
    const dot = el("span", "ready-dot");
    dot.dataset.path = f.path;
    if (preload.done.has(f.path)) dot.classList.add("on");
    row.appendChild(dot);
    row.appendChild(el("span", "ff-name", f.name));
    if (f.summary) row.appendChild(el("span", "ff-sum", f.summary));
    row.addEventListener("click", () => openFile(f.path));
    view.appendChild(row);
  });

  bodyEl.appendChild(view);
}

// ================= 通用 UI 片段 =================
function loadingEl(text) {
  const box = el("div", "loading");
  box.appendChild(el("div", "spinner"));
  box.appendChild(document.createTextNode(text || "加载中…"));
  return box;
}
function errorEl(text) { return el("div", "error-box", text); }
function showError(text) {
  const bodyEl = $("#readerBody");
  bodyEl.innerHTML = "";
  bodyEl.appendChild(errorEl(text));
}

// ================= 右侧对话/详解分栏 =================
function bindChatUI() {
  $("#chatCloseBtn").addEventListener("click", closeChat);
  $("#chatFab").addEventListener("click", openChat);
  $("#chatClearBtn").addEventListener("click", clearChat);
  $("#chatSendBtn").addEventListener("click", sendChat);
  const input = $("#chatInput");
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); sendChat(); }
  });
  input.addEventListener("input", () => {
    input.style.height = "auto";
    input.style.height = Math.min(140, input.scrollHeight) + "px";
  });
  // 划选代码 → 浮现“引用提问”按钮
  const askBtn = $("#askSelectionBtn");
  document.addEventListener("selectionchange", onSelectionChange);
  askBtn.addEventListener("mousedown", (e) => e.preventDefault()); // 别让按钮抢走选区
  askBtn.addEventListener("click", quoteSelection);
  renderChatEmpty();
}

function openChat() {
  $("#chatPanel").classList.remove("collapsed");
  $("#chatFab").classList.add("hidden");
  setTimeout(() => $("#chatInput").focus(), 50);
}
function closeChat() {
  $("#chatPanel").classList.add("collapsed");
  $("#chatFab").classList.remove("hidden");
}
function clearChat() {
  state.chat.history = [];
  clearChatContext();
  renderChatEmpty();
}

function renderChatEmpty() {
  const box = $("#chatMessages");
  if (state.chat.history.length) return;
  box.innerHTML = "";
  const e = el("div", "chat-empty");
  e.innerHTML =
    "<b>我可以帮你：</b><br>" +
    "· 直接提问通识知识（无需上下文）<br>" +
    "· 在左侧划选一段代码后「引用提问」，我会结合选中代码回答<br>" +
    "· 点代码注释旁的「详解 ↦」，我会展开对那段代码的深入讲解";
  box.appendChild(e);
}

// ---- 上下文芯片（引用 / 详解目标）----
function setChatContext(ctx) {
  // ctx: { title, code, path, start, end } 或 null
  state.chat.context = ctx;
  const box = $("#chatContext");
  if (!ctx) { box.classList.add("hidden"); box.innerHTML = ""; return; }
  box.classList.remove("hidden");
  box.innerHTML = "";
  const title = el("div", "ctx-title");
  title.appendChild(el("span", null, ctx.title || "引用上下文"));
  const clr = el("span", "ctx-clear", "移除 ✕");
  clr.addEventListener("click", clearChatContext);
  title.appendChild(clr);
  box.appendChild(title);
  if (ctx.code) {
    const pre = el("pre");
    pre.textContent = ctx.code.length > 800 ? ctx.code.slice(0, 800) + "\n…" : ctx.code;
    box.appendChild(pre);
  }
}
function clearChatContext() {
  state.chat.context = null;
  const box = $("#chatContext");
  box.classList.add("hidden");
  box.innerHTML = "";
}

// ---- 划选代码 ----
let selTimer = null;
function onSelectionChange() {
  clearTimeout(selTimer);
  selTimer = setTimeout(() => {
    const sel = window.getSelection();
    const askBtn = $("#askSelectionBtn");
    const text = sel ? sel.toString() : "";
    if (!text.trim() || !sel.rangeCount) { askBtn.classList.add("hidden"); return; }
    // 仅当选区落在阅读区内才显示
    const reader = $("#readerBody");
    const anchor = sel.anchorNode;
    if (!anchor || !reader.contains(anchor.nodeType === 1 ? anchor : anchor.parentNode)) {
      askBtn.classList.add("hidden"); return;
    }
    const rect = sel.getRangeAt(0).getBoundingClientRect();
    const readerRect = $(".reader").getBoundingClientRect();
    askBtn.style.left = Math.max(8, rect.left - readerRect.left) + "px";
    askBtn.style.top = (rect.bottom - readerRect.top + 6) + "px";
    askBtn.classList.remove("hidden");
    askBtn._selText = text;
  }, 60);
}

function quoteSelection() {
  const askBtn = $("#askSelectionBtn");
  const text = askBtn._selText || window.getSelection().toString();
  if (!text.trim()) return;
  const path = state.currentFile ? state.currentFile.path : null;
  setChatContext({
    title: "引用自 " + (path || "选中代码"),
    code: text, path, start: 0, end: 0,
  });
  askBtn.classList.add("hidden");
  openChat();
  $("#chatInput").focus();
}

// ---- “详解”按钮 → 右栏流式深入讲解 ----
async function explainDetail(block, exp) {
  openChat();
  const path = state.currentFile ? state.currentFile.path : exp.path;
  setChatContext({
    title: `详解 · 第 ${block.start_line}–${block.end_line} 行`,
    code: null, path, start: block.start_line, end: block.end_line,
  });
  // 作为一条用户消息呈现（不写入通用 history，避免污染后续问答）
  addMsg("user", `请详解第 ${block.start_line}–${block.end_line} 行：${block.comment || ""}`);
  const bubble = addMsg("assistant", "", { detail: true });
  await streamInto("/api/detail", {
    path, start_line: block.start_line, end_line: block.end_line,
    comment: block.comment || "",
  }, bubble);
}

// ---- 发送对话（三种任务合一）----
async function sendChat() {
  if (state.chat.streaming) return;
  const input = $("#chatInput");
  const text = input.value.trim();
  if (!text) return;
  input.value = "";
  input.style.height = "auto";

  addMsg("user", text);
  state.chat.history.push({ role: "user", content: text });

  const bubble = addMsg("assistant", "");
  const ctx = state.chat.context;
  const body = {
    messages: state.chat.history,
    file_path: (ctx && ctx.path) || (state.currentFile && state.currentFile.path) || null,
  };
  if (ctx && ctx.code) {
    body.selection = ctx.code;
    body.sel_start = ctx.start || 0;
    body.sel_end = ctx.end || 0;
  }
  const full = await streamInto("/api/chat", body, bubble);
  if (full) state.chat.history.push({ role: "assistant", content: full });
}

// ---- SSE 流式读取，边收边渲染 ----
async function streamInto(url, body, bubble) {
  state.chat.streaming = true;
  $("#chatSendBtn").disabled = true;
  const cursor = el("span", "cursor", " ");
  bubble.appendChild(cursor);
  let acc = "";
  try {
    const res = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!res.ok || !res.body) {
      let msg = res.status + "";
      try { const j = await res.json(); if (j.detail) msg = j.detail; } catch (_) {}
      throw new Error(msg);
    }
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buf = "";
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      // 解析 SSE：以空行分隔的事件块
      const events = buf.split("\n\n");
      buf = events.pop(); // 最后一段可能不完整，留到下轮
      for (const ev of events) {
        const dataLines = ev.split("\n").filter((l) => l.startsWith("data: "));
        if (!dataLines.length) continue;
        const payload = dataLines.map((l) => l.slice(6)).join("\n");
        if (payload === "[DONE]") continue;
        if (payload.startsWith("[ERROR]")) { acc += "\n\n⚠️ " + payload.slice(7).trim(); }
        else acc += payload;
        renderMarkdownInto(bubble, acc);
        bubble.appendChild(cursor);
        scrollChatBottom();
      }
    }
  } catch (e) {
    acc += (acc ? "\n\n" : "") + "⚠️ 请求失败：" + e.message;
    renderMarkdownInto(bubble, acc);
  } finally {
    cursor.remove();
    state.chat.streaming = false;
    $("#chatSendBtn").disabled = false;
    scrollChatBottom();
  }
  return acc;
}

function addMsg(role, text, opts) {
  const box = $("#chatMessages");
  const empty = box.querySelector(".chat-empty");
  if (empty) empty.remove();
  const msg = el("div", "msg " + role);
  msg.appendChild(el("div", "who", role === "user" ? "你" : "AI 助手"));
  const bubble = el("div", "bubble" + (opts && opts.detail ? " detail" : ""));
  if (text) renderMarkdownInto(bubble, text);
  msg.appendChild(bubble);
  box.appendChild(msg);
  scrollChatBottom();
  return bubble;
}

function scrollChatBottom() {
  const box = $("#chatMessages");
  box.scrollTop = box.scrollHeight;
}

// ---- 极简 Markdown 渲染（代码块/行内码/粗体/标题/列表），安全转义 ----
function renderMarkdownInto(node, md) {
  node.innerHTML = mdToHtml(md);
  // 高亮代码块
  node.querySelectorAll("pre code").forEach((c) => {
    try { hljs.highlightElement(c); } catch (_) {}
  });
}

function mdToHtml(md) {
  // 先抽取围栏代码块，避免其中内容被其他规则破坏
  const blocks = [];
  md = md.replace(/```(\w*)\n?([\s\S]*?)```/g, (_, lang, code) => {
    const i = blocks.length;
    blocks.push(`<pre><code class="language-${esc(lang || "")}">${esc(code.replace(/\n$/, ""))}</code></pre>`);
    return ` BLOCK${i} `;
  });
  let html = esc(md);
  // 行内代码
  html = html.replace(/`([^`]+)`/g, (_, c) => `<code>${c}</code>`);
  // 粗体
  html = html.replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>");
  // 标题
  html = html.replace(/^######\s*(.+)$/gm, "<b>$1</b>")
             .replace(/^#{1,5}\s*(.+)$/gm, "<b>$1</b>");
  // 无序列表项
  html = html.replace(/^\s*[-*]\s+(.+)$/gm, "• $1");
  // 换行
  html = html.replace(/\n/g, "<br>");
  // 还原代码块（先把可能被 <br> 化的占位符复原）
  html = html.replace(/ BLOCK(\d+) /g, (_, i) => blocks[+i]);
  return html;
}

init();
