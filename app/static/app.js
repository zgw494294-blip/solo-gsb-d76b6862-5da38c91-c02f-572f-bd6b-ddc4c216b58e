"use strict";

/* 最近一次从服务端拿到的状态；所有变更请求都携带 state.version。
 * 服务端版本不一致会返回 409 + 最新状态，页面据此刷新。 */
let state = null;
let editingId = null; // 正在编辑的待发提示 id
let busy = false;     // 有请求在途时禁用确认按钮，防止重复点击
let prevStatus = null; // 上一帧状态，用于识别回到开场前的切换

const $ = (sel, el = document) => el.querySelector(sel);
const $$ = (sel, el = document) => [...el.querySelectorAll(sel)];

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

let toastTimer = null;
function toast(msg) {
  const el = $("#toast");
  el.textContent = msg;
  el.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove("show"), 3200);
}

/* ---------------------------------------------------------------- 请求 */

async function api(path, body) {
  busy = true;
  updateBusy();
  try {
    const res = await fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await res.json();
    if (res.status === 409) {
      // 操作过期：同版本已有其他先生效，用最新状态刷新页面
      applyState(data.state);
      toast("操作未生效：已有人先操作，页面已刷新为最新状态");
      return null;
    }
    if (!res.ok) {
      toast(data.error || "请求失败");
      return null;
    }
    applyState(data);
    return data;
  } catch (err) {
    toast("网络错误：" + err.message);
    return null;
  } finally {
    busy = false;
    updateBusy();
  }
}

function applyState(s) {
  state = s;
  if (editingId !== null && !s.pending.some((c) => c.id === editingId)) {
    editingId = null; // 正在编辑的提示已不在待发列表（如已被确认）
  }
  render();
}

function updateBusy() {
  const btn = $("#confirm-btn");
  if (btn) btn.disabled = busy || !state || !state.current;
  const archiveBtn = $("#archive-btn");
  if (archiveBtn) archiveBtn.disabled = busy || archiveSubmitting || !state || state.status === "setup";
}

/* ---------------------------------------------------------------- 档案库 */

let archiveSubmitting = false; // 归档请求在途，禁用提交防止重复建档案

function statusLabel(status) {
  return status === "done" ? "已完成" : "提前收场";
}

function fmtTime(iso) {
  return iso ? new Date(iso).toLocaleString() : "";
}

function openArchivesModal() {
  $("#archives-modal").classList.remove("hidden");
  loadArchiveList();
}

function closeArchivesModal() {
  $("#archives-modal").classList.add("hidden");
}

async function apiGetJson(path) {
  const res = await fetch(path);
  if (!res.ok) throw new Error("HTTP " + res.status);
  return res.json();
}

async function loadArchiveList() {
  $("#archives-title").textContent = "📚 档案库";
  $("#archives-body").innerHTML = `<p class="empty">加载中…</p>`;
  let data;
  try {
    data = await apiGetJson("/api/archives");
  } catch (err) {
    $("#archives-body").innerHTML = `<p class="empty">档案加载失败：${escapeHtml(err.message)}</p>`;
    return;
  }
  const items = data.archives || [];
  const canArchive = state && state.status !== "setup";
  if (!items.length) {
    $("#archives-body").innerHTML = `
      <p class="empty">还没有归档档案。演出结束或提前收场后，可点击「归档本场」保存回看。</p>
      ${canArchive ? `<div class="actions"><button class="primary" data-action="archive-form">归档当前场次 🗂</button></div>` : ""}`;
    return;
  }
  $("#archives-body").innerHTML = `
    ${canArchive ? `<div class="actions"><button class="primary" data-action="archive-form">归档当前场次 🗂</button></div>` : ""}
    <ul class="archive-list">
      ${items.map((a) => `
        <li class="archive-item" data-action="archive-detail" data-id="${a.id}" tabindex="0">
          <span class="archive-name">${escapeHtml(a.name)}</span>
          <span class="archive-meta">
            <span class="tag ${a.status === "done" ? "done" : "early"}">${statusLabel(a.status)}</span>
            <span class="time">${fmtTime(a.archived_at)}</span>
          </span>
          <span class="archive-count">已发 ${a.issued_count} · 剩余 ${a.pending_count}</span>
        </li>`).join("")}
    </ul>`;
}

async function loadArchiveDetail(id) {
  $("#archives-title").textContent = "档案详情";
  $("#archives-body").innerHTML = `<p class="empty">加载中…</p>`;
  let a;
  try {
    a = await apiGetJson("/api/archives/" + encodeURIComponent(id));
  } catch (err) {
    $("#archives-body").innerHTML = `
      <p class="empty">档案加载失败：${escapeHtml(err.message)}</p>
      <div class="actions"><button data-action="archive-list">← 返回列表</button></div>`;
    return;
  }
  const canStart = !!(state && state.status === "setup");
  $("#archives-body").innerHTML = `
    <div class="archive-detail-head">
      <div class="archive-detail-name">${escapeHtml(a.name)}</div>
      <div class="archive-detail-meta">
        <span class="tag ${a.status === "done" ? "done" : "early"}">${statusLabel(a.status)}</span>
        归档时间：${fmtTime(a.archived_at)}
      </div>
    </div>
    <div class="archive-columns">
      <div>
        <h3>已发记录（${a.issued.length}）<span class="hint-inline">按发出顺序</span></h3>
        <ul class="cue-list">
          ${a.issued.length ? a.issued.map((c) => `
            <li class="issued-item">
              <span class="badge done">✓ ${escapeHtml(c.number)}</span>
              <span class="content">${escapeHtml(c.content)}</span>
              <span class="time">${fmtTime(c.issued_at)}</span>
            </li>`).join("")
            : `<li class="empty">无已发记录（开场后未发出即收场）</li>`}
        </ul>
      </div>
      <div>
        <h3>剩余未发（${a.pending.length}）<span class="hint-inline">按待发顺序</span></h3>
        <ul class="cue-list">
          ${a.pending.length ? a.pending.map((c) => `
            <li class="pending-item">
              <span class="badge">${escapeHtml(c.number)}</span>
              <span class="content">${escapeHtml(c.content)}</span>
            </li>`).join("")
            : `<li class="empty">无（全部提示均已发出）</li>`}
        </ul>
      </div>
    </div>
    <div class="actions">
      <button data-action="archive-list">← 返回列表</button>
      ${canStart
        ? `<button class="primary" data-action="start-from-archive" data-id="${a.id}">以此开新场 ▶</button>`
        : `<button disabled title="回到开场前设置后才能以此开新场">以此开新场 ▶</button>`}
    </div>
    ${canStart
      ? ""
      : `<p class="hint-inline">当前已有进行中的场次；需回到「开场前设置」（重新开始或归档收场后）才能以此档案开新场。</p>`}`;
}

/* ------------------------------------------------------------ 以此开新场 */

let startSubmitting = false; // 以档开新场请求在途，禁用提交防止重复

/* 新场待发 = 档案已发（按发出顺序，取编号+内容快照）+ 剩余未发（按待发顺序）。
 * 不继承发出时间或进度；原档案不修改。 */
function startCues(a) {
  return [
    ...a.issued.map((c) => ({ number: c.number, content: c.content, src: "issued" })),
    ...a.pending.map((c) => ({ number: c.number, content: c.content, src: "pending" })),
  ];
}

function startCueIssues(cues) {
  if (cues.length < state.min_cues || cues.length > state.max_cues) {
    return [`合并后共 ${cues.length} 条，需在 ${state.min_cues}～${state.max_cues} 条之间，不能以此开新场`];
  }
  const numbers = cues.map((c) => c.number.trim());
  if (numbers.some((n) => !n)) return ["档案中存在空编号，不能以此开新场"];
  if (new Set(numbers).size !== numbers.length) return ["合并后编号有重复，不能以此开新场"];
  if (cues.some((c) => !c.content.trim())) return ["档案中存在空内容，不能以此开新场"];
  return null;
}

function openStartConfirm(a) {
  if (!state || state.status !== "setup") {
    toast("仅开场前（设置页）可以此开新场");
    return;
  }
  const cues = startCues(a);
  const issues = startCueIssues(cues);
  $("#archives-title").textContent = "以此开新场";
  $("#archives-body").innerHTML = `
    <p class="hint">
      将以档案「${escapeHtml(a.name)}」开一场新演出：新场待发提示依次为档案
      <b>已发 ${a.issued.length} 条</b>（取编号与内容快照，不含发出时间）再接
      <b>剩余 ${a.pending.length} 条</b>，共 ${cues.length} 条；不继承发出时间或进度，
      原档案保持不变。成功后直接进入演出中，当前指向第一条。
    </p>
    ${issues ? `<p class="form-error">${issues.map(escapeHtml).join("<br>")}</p>` : ""}
    <ul class="cue-list start-preview">
      ${cues.map((c, i) => `
        <li class="${c.src === "issued" ? "issued-item" : "pending-item"}">
          <span class="row-index">${i + 1}</span>
          <span class="badge ${c.src === "issued" ? "done" : ""}">${escapeHtml(c.number)}</span>
          <span class="content">${escapeHtml(c.content)}</span>
          <span class="tag ${c.src === "issued" ? "done" : "next"}">${c.src === "issued" ? "已发快照" : "待发"}</span>
        </li>`).join("")}
    </ul>
    <p class="hint-inline">提交页面当前版本 v${state.version}；非开场前或版本过期会被服务端拒绝（409）。</p>
    <div class="actions">
      <button id="start-submit" class="primary" data-action="start-submit" data-id="${a.id}"
        ${issues ? "disabled" : ""}>确认开新场 ▶</button>
      <button data-action="start-cancel" data-id="${a.id}">取消</button>
    </div>`;
}

async function submitStartFromArchive(id) {
  if (startSubmitting) return;
  startSubmitting = true;
  const btn = $("#start-submit");
  if (btn) btn.disabled = true;
  try {
    const res = await fetch("/api/archives/" + encodeURIComponent(id) + "/start", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ version: state.version }),
    });
    const data = await res.json();
    if (res.status === 409) {
      applyState(data.state);
      closeArchivesModal();
      toast("开新场未生效：非开场前或版本已过期，页面已刷新为最新状态");
      return;
    }
    if (res.status === 404) {
      toast("档案不存在或已被删除");
      loadArchiveList();
      return;
    }
    if (!res.ok) {
      toast(data.error || "开新场失败");
      loadArchiveDetail(id); // 留在档案详情，允许重试
      return;
    }
    // 新场提示与状态推进已在同一事务落库：进入演出中、已发为空、指向第一条
    applyState(data);
    closeArchivesModal();
    toast("已以此档案开新场，共 " + data.total + " 条待发提示");
  } catch (err) {
    toast("网络错误：" + err.message);
    if (btn) btn.disabled = false;
  } finally {
    startSubmitting = false;
    updateBusy();
  }
}

function openArchiveForm() {
  if (!state || state.status === "setup") {
    toast("开场前没有可归档的场次");
    return;
  }
  $("#archives-title").textContent = "归档本场";
  $("#archives-body").innerHTML = `
    <p class="hint">归档后本场将保存为独立档案，并清空当前提示、回到开场前设置；之后录入、重置和重启都不会影响档案。</p>
    <label class="field-label" for="archive-name">场次名称</label>
    <input id="archive-name" maxlength="60" placeholder="例如：10 月 1 日晚场" autocomplete="off">
    <p class="hint-inline">当前页面版本 v${state.version} · ${statusLabel(state.status === "done" ? "done" : "live")}</p>
    <div class="actions">
      <button id="archive-submit" class="primary" data-action="archive-submit">保存档案并收场 🗂</button>
      <button data-action="archive-cancel">取消</button>
    </div>`;
  $("#archives-modal").classList.remove("hidden");
  $("#archive-name").focus();
}

async function submitArchive() {
  if (archiveSubmitting) return;
  const input = $("#archive-name");
  const name = input.value.trim();
  if (!name) { toast("请输入场次名称"); input.focus(); return; }
  archiveSubmitting = true;
  const btn = $("#archive-submit");
  if (btn) btn.disabled = true;
  updateBusy();
  try {
    const res = await fetch("/api/archive", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ version: state.version, name }),
    });
    const data = await res.json();
    if (res.status === 409) {
      applyState(data.state);
      closeArchivesModal();
      toast("归档未生效：操作已过期，页面已刷新为最新状态");
      return;
    }
    if (!res.ok) {
      toast(data.error || "归档失败");
      if (btn) btn.disabled = false;
      return;
    }
    // 档案已落库且当前提示已一起清空（同一事务）
    applyState(data);
    toast(`已归档「${name}」，当前提示已清空`);
    loadArchiveList();
  } catch (err) {
    toast("网络错误：" + err.message);
    if (btn) btn.disabled = false;
  } finally {
    archiveSubmitting = false;
    updateBusy();
  }
}

/* ---------------------------------------------------------------- 渲染 */

function render() {
  if (!state) return;
  $("#version-badge").textContent = "v" + state.version;

  const inSetup = state.status === "setup";
  $("#setup-view").classList.toggle("hidden", !inSetup);
  $("#live-view").classList.toggle("hidden", inSetup);
  if (inSetup) {
    // 从演出中/完成回到开场前（开始前加载、重置或归档成功）时清空旧录入
    if (prevStatus !== "setup") resetSetupRows();
    prevStatus = "setup";
    return;
  }
  prevStatus = state.status;

  $("#done-banner").classList.toggle("hidden", state.status !== "done");
  $("#progress").textContent =
    `已发 ${state.issued.length} / 共 ${state.total} · 待发 ${state.remaining}`;

  const cur = state.current;
  $("#current-card").innerHTML = cur
    ? `<div class="card-label">当前待发</div>
       <div class="cue-number">${escapeHtml(cur.number)}</div>
       <div class="cue-content">${escapeHtml(cur.content)}</div>
       <button id="confirm-btn" class="primary big" data-action="confirm">确认发出 ✓</button>`
    : `<div class="card-label">当前待发</div><div class="empty">— 全部已发出 —</div>`;

  const nxt = state.next;
  $("#next-card").innerHTML = nxt
    ? `<div class="card-label">下一条</div>
       <div class="cue-number small">${escapeHtml(nxt.number)}</div>
       <div class="cue-content small">${escapeHtml(nxt.content)}</div>`
    : `<div class="card-label">下一条</div><div class="empty">— 无 —</div>`;

  renderPending();
  renderIssued();
  updateBusy();
}

function renderPending() {
  const ul = $("#pending-list");
  const items = state.pending;
  if (!items.length) {
    ul.innerHTML = `<li class="empty">全部提示已发出</li>`;
    return;
  }
  ul.innerHTML = items.map((c, i) => {
    if (c.id === editingId) {
      return `<li class="pending-item editing" data-id="${c.id}">
        <input class="num-input" value="${escapeHtml(c.number)}" maxlength="30" aria-label="编号">
        <span class="ops">
          <button class="primary" data-action="save-edit" data-id="${c.id}">保存</button>
          <button data-action="cancel-edit">取消</button>
        </span>
        <textarea class="content-input" rows="2" aria-label="内容">${escapeHtml(c.content)}</textarea>
      </li>`;
    }
    const tag = i === 0 ? `<span class="tag now">当前</span>`
              : i === 1 ? `<span class="tag next">下一条</span>` : "";
    return `<li class="pending-item ${i === 0 ? "is-current" : ""}" data-id="${c.id}">
      <span class="badge">${escapeHtml(c.number)}</span>
      <span class="content">${escapeHtml(c.content)}</span>
      ${tag}
      <span class="ops">
        <button data-action="edit" data-id="${c.id}" title="改写">✎</button>
        <button data-action="up" data-idx="${i}" ${i === 0 ? "disabled" : ""} title="上移">↑</button>
        <button data-action="down" data-idx="${i}" ${i === items.length - 1 ? "disabled" : ""} title="下移">↓</button>
      </span>
    </li>`;
  }).join("");
}

function renderIssued() {
  const ul = $("#issued-list");
  if (!state.issued.length) {
    ul.innerHTML = `<li class="empty">尚无已发记录</li>`;
    return;
  }
  ul.innerHTML = state.issued.map((c) => {
    const t = c.issued_at ? new Date(c.issued_at).toLocaleTimeString() : "";
    return `<li class="issued-item">
      <span class="badge done">✓ ${escapeHtml(c.number)}</span>
      <span class="content">${escapeHtml(c.content)}</span>
      <span class="time">${t}</span>
    </li>`;
  }).join("");
}

/* ---------------------------------------------------------------- 设置页 */

function resetSetupRows() {
  $("#cue-rows").innerHTML = "";
  addRow();
  addRow();
}

function addRow() {
  const div = document.createElement("div");
  div.className = "cue-row";
  div.innerHTML = `
    <span class="row-index"></span>
    <input class="num" placeholder="编号（唯一）" maxlength="30">
    <input class="cnt" placeholder="提示内容（必填）">
    <button class="remove-row" title="删除此行">✕</button>`;
  div.querySelector(".remove-row").addEventListener("click", () => {
    if ($$(".cue-row").length <= state.min_cues) {
      toast(`至少需要 ${state.min_cues} 条提示`);
      return;
    }
    div.remove();
    renumberRows();
  });
  $("#cue-rows").appendChild(div);
  renumberRows();
}

function renumberRows() {
  $$(".cue-row").forEach((r, i) => { $(".row-index", r).textContent = i + 1; });
  $("#add-row").disabled = $$(".cue-row").length >= state.max_cues;
}

/* ---------------------------------------------------------------- 事件 */

document.addEventListener("click", (e) => {
  const btn = e.target.closest("[data-action]");
  if (!btn || btn.disabled) return;
  const action = btn.dataset.action;

  if (action === "confirm") {
    if (!state.current) return;
    api("/api/confirm", { version: state.version });
  } else if (action === "edit") {
    editingId = Number(btn.dataset.id);
    render();
  } else if (action === "cancel-edit") {
    editingId = null;
    render();
  } else if (action === "save-edit") {
    const li = btn.closest("li");
    const number = $(".num-input", li).value.trim();
    const content = $(".content-input", li).value.trim();
    if (!number || !content) { toast("编号和内容都不能为空"); return; }
    const id = Number(btn.dataset.id);
    editingId = null;
    api("/api/edit", { version: state.version, cue_id: id, number, content });
  } else if (action === "up" || action === "down") {
    const ids = state.pending.map((c) => c.id);
    const i = Number(btn.dataset.idx);
    const j = action === "up" ? i - 1 : i + 1;
    if (j < 0 || j >= ids.length) return;
    [ids[i], ids[j]] = [ids[j], ids[i]];
    api("/api/reorder", { version: state.version, order: ids });
  }
});

$("#add-row").addEventListener("click", () => {
  if ($$(".cue-row").length >= state.max_cues) {
    toast(`最多 ${state.max_cues} 条提示`);
    return;
  }
  addRow();
});

$("#start-show").addEventListener("click", () => {
  const cues = $$(".cue-row").map((r) => ({
    number: $(".num", r).value.trim(),
    content: $(".cnt", r).value.trim(),
  }));
  if (cues.length < state.min_cues || cues.length > state.max_cues) {
    toast(`提示数量需在 ${state.min_cues}～${state.max_cues} 条之间`);
    return;
  }
  if (cues.some((c) => !c.number)) { toast("编号不能为空"); return; }
  if (new Set(cues.map((c) => c.number)).size !== cues.length) {
    toast("编号必须唯一");
    return;
  }
  if (cues.some((c) => !c.content)) { toast("内容不能为空"); return; }
  api("/api/setup", { cues });
});

$("#reset-btn").addEventListener("click", () => {
  if (!confirm("确定要清空当前进度，重新设置本场提示吗？")) return;
  api("/api/reset", {});
});

/* ---------------------------------------------------------------- 档案事件 */

$("#archives-btn").addEventListener("click", openArchivesModal);
$("#archive-btn").addEventListener("click", openArchiveForm);
$("#archives-close").addEventListener("click", closeArchivesModal);
$("#archives-modal").addEventListener("click", (e) => {
  if (e.target === $("#archives-modal")) closeArchivesModal();
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !$("#archives-modal").classList.contains("hidden")) {
    closeArchivesModal();
  }
});

$("#archives-body").addEventListener("click", (e) => {
  const btn = e.target.closest("[data-action]");
  if (!btn || btn.disabled) return;
  const action = btn.dataset.action;
  if (action === "archive-form") {
    openArchiveForm();
  } else if (action === "archive-list") {
    loadArchiveList();
  } else if (action === "archive-cancel") {
    closeArchivesModal();
  } else if (action === "archive-submit") {
    submitArchive();
  } else if (action === "archive-detail") {
    loadArchiveDetail(Number(btn.dataset.id));
  } else if (action === "start-from-archive") {
    const id = Number(btn.dataset.id);
    apiGetJson("/api/archives/" + encodeURIComponent(id))
      .then(openStartConfirm)
      .catch((err) => toast("档案加载失败：" + err.message));
  } else if (action === "start-submit") {
    submitStartFromArchive(Number(btn.dataset.id));
  } else if (action === "start-cancel") {
    loadArchiveDetail(Number(btn.dataset.id));
  }
});

$("#archives-body").addEventListener("keydown", (e) => {
  if (e.key !== "Enter") return;
  if (e.target.id === "archive-name") {
    e.preventDefault();
    submitArchive();
    return;
  }
  const item = e.target.closest("[data-action='archive-detail']");
  if (item) loadArchiveDetail(Number(item.dataset.id));
});

/* ---------------------------------------------------------------- 启动 */

(async function init() {
  try {
    const res = await fetch("/api/state");
    applyState(await res.json());
  } catch (err) {
    toast("无法连接服务端：" + err.message);
  }
})();
