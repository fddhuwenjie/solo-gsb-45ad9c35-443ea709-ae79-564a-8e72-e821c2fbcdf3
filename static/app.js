/* 直播节目串联单前端（原生 JavaScript） */

let state = null;       // 最近一次从服务器获取的状态
let editing = false;    // 是否处于“编辑待播清单”模式
let editRows = [];      // 编辑中的待播清单工作副本
let setupRows = [];     // 创建节目时的段落工作副本
let actualDraft = '';   // “实际秒数”输入草稿（定时刷新时保留输入）

const $ = (id) => document.getElementById(id);

async function api(path, options) {
  const res = await fetch(path, options);
  try {
    return await res.json();
  } catch (e) {
    return { ok: false, error: '服务器响应异常' };
  }
}

function postJSON(path, payload) {
  return api(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
}

/* ---------- 格式化 ---------- */

function fmtClock(iso) {
  if (!iso) return '—';
  return new Date(iso).toLocaleTimeString('zh-CN', { hour12: false });
}

function fmtDateTime(iso) {
  if (!iso) return '—';
  const d = new Date(iso);
  return d.toLocaleDateString('zh-CN') + ' ' + d.toLocaleTimeString('zh-CN', { hour12: false });
}

function fmtDur(sec) {
  if (sec === null || sec === undefined) return '—';
  const sign = sec < 0 ? '-' : '';
  sec = Math.abs(Math.round(sec));
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  const s = sec % 60;
  const mm = String(m).padStart(2, '0');
  const ss = String(s).padStart(2, '0');
  return sign + (h > 0 ? `${h}:${mm}:${ss}` : `${m}:${ss}`);
}

function escapeHtml(s) {
  return String(s ?? '').replace(/[&<>"']/g, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));
}

function localISO(d) {
  const p = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}T${p(d.getHours())}:${p(d.getMinutes())}`;
}

/* ---------- 创建节目 ---------- */

function addSetupRow(type) {
  setupRows.push({
    type,
    title: '',
    fixed_seconds: 60,
    min_seconds: 240,
    target_seconds: 300,
    max_seconds: 360,
  });
  renderSetupRows();
}

function renderSetupRows() {
  const box = $('setup-segments');
  box.innerHTML = '';
  setupRows.forEach((row, i) => {
    const div = document.createElement('div');
    div.className = 'seg-row';
    const fields = row.type === 'sponsor'
      ? `<label>固定 <input data-k="fixed_seconds" type="number" min="1" value="${row.fixed_seconds}"> 秒</label>`
      : `<label>最短 <input data-k="min_seconds" type="number" min="1" value="${row.min_seconds}"></label>
         <label>目标 <input data-k="target_seconds" type="number" min="1" value="${row.target_seconds}"></label>
         <label>最长 <input data-k="max_seconds" type="number" min="1" value="${row.max_seconds}"></label>`;
    div.innerHTML = `
      <span class="seg-idx">${i + 1}</span>
      <select data-k="type">
        <option value="sponsor" ${row.type === 'sponsor' ? 'selected' : ''}>赞助段</option>
        <option value="interview" ${row.type === 'interview' ? 'selected' : ''}>访谈段</option>
      </select>
      <input data-k="title" placeholder="标题" value="${escapeHtml(row.title)}">
      ${fields}
      <span class="row-ops">
        <button type="button" data-op="up" ${i === 0 ? 'disabled' : ''}>↑</button>
        <button type="button" data-op="down" ${i === setupRows.length - 1 ? 'disabled' : ''}>↓</button>
        <button type="button" data-op="del">✕</button>
      </span>`;
    div.querySelectorAll('input, select').forEach((el) => {
      el.addEventListener('change', () => {
        row[el.dataset.k] = el.value;
        if (el.dataset.k === 'type') renderSetupRows();
      });
    });
    div.querySelectorAll('button').forEach((btn) => {
      btn.addEventListener('click', () => {
        const op = btn.dataset.op;
        if (op === 'del') setupRows.splice(i, 1);
        else if (op === 'up' && i > 0) [setupRows[i - 1], setupRows[i]] = [setupRows[i], setupRows[i - 1]];
        else if (op === 'down' && i < setupRows.length - 1) [setupRows[i + 1], setupRows[i]] = [setupRows[i], setupRows[i + 1]];
        renderSetupRows();
      });
    });
    box.appendChild(div);
  });
}

async function createShow() {
  const payload = {
    start_at: $('setup-start').value,
    total_seconds: parseInt($('setup-total').value, 10),
    segments: setupRows.map((r) => (r.type === 'sponsor'
      ? { type: 'sponsor', title: r.title, fixed_seconds: parseInt(r.fixed_seconds, 10) }
      : {
          type: 'interview',
          title: r.title,
          min_seconds: parseInt(r.min_seconds, 10),
          target_seconds: parseInt(r.target_seconds, 10),
          max_seconds: parseInt(r.max_seconds, 10),
        })),
  };
  const res = await postJSON('/api/show', payload);
  if (!res.ok) {
    $('setup-error').textContent = res.error || '创建失败';
    return;
  }
  $('setup-error').textContent = '';
  state = res.state;
  render();
}

/* ---------- 实况渲染 ---------- */

function segmentParams(s) {
  if (s.type === 'sponsor') return `固定 ${fmtDur(s.fixed_seconds)}`;
  return `最短 ${fmtDur(s.min_seconds)} ｜ 目标 ${fmtDur(s.target_seconds)} ｜ 最长 ${fmtDur(s.max_seconds)}`;
}

function renderLive() {
  const show = state.show;

  const cards = [
    ['节目起点', fmtDateTime(show.start_at)],
    ['总时长', fmtDur(show.total_seconds)],
    ['已播', fmtDur(show.aired_seconds)],
    ['剩余', fmtDur(show.remaining_seconds)],
    ['预计终点', fmtClock(show.est_end)],
  ];
  $('summary-cards').innerHTML = cards
    .map(([k, v]) => `<div class="stat"><div class="stat-k">${k}</div><div class="stat-v">${v}</div></div>`)
    .join('');

  const banner = $('conflict-banner');
  if (show.conflict_diff !== null && show.conflict_diff !== undefined) {
    banner.classList.remove('hidden');
    banner.textContent = show.conflict_diff > 0
      ? `⚠ 冲突：按当前待播清单无法排满节目，仍差 ${show.conflict_diff} 秒。已播记录已保留，请编辑待播清单解决。`
      : `⚠ 冲突：按当前待播清单节目将超时 ${-show.conflict_diff} 秒。已播记录已保留，请编辑待播清单解决。`;
  } else {
    banner.classList.add('hidden');
  }

  const pending = state.segments.filter((s) => s.status === 'pending');
  const cur = pending[0];
  const cp = $('current-panel');
  if (cur) {
    cp.innerHTML = `
      <h2>当前段 <span class="tag tag-${cur.type}">${cur.type === 'sponsor' ? '赞助段' : '访谈段'}</span></h2>
      <div class="cur-title">${escapeHtml(cur.title)}</div>
      <div class="hint">计划 ${fmtDur(cur.planned_seconds)} ｜ 预计 ${fmtClock(cur.est_start)} – ${fmtClock(cur.est_end)}</div>
      <div class="btn-row">
        <input type="number" id="actual-input" min="1" placeholder="实际秒数" value="${escapeHtml(actualDraft)}">
        <button class="primary" id="complete-btn" type="button">结束当前段并记录</button>
      </div>`;
    $('actual-input').addEventListener('input', (e) => { actualDraft = e.target.value; });
    $('complete-btn').addEventListener('click', completeCurrent);
  } else {
    cp.innerHTML = '<h2>节目已播完</h2><div class="hint">全部段落均已完成。</div>';
  }

  const tbody = $('segments-table').querySelector('tbody');
  tbody.innerHTML = '';
  let seenPending = false;
  state.segments.forEach((s, i) => {
    let statusHtml;
    let rowClass = '';
    if (s.status === 'done') {
      statusHtml = `<span class="done">已播 ${fmtDur(s.actual_seconds)}</span>`;
      rowClass = 'row-done';
    } else if (!seenPending) {
      statusHtml = '<span class="current">▶ 当前段</span>';
      rowClass = 'row-current';
      seenPending = true;
    } else {
      statusHtml = '<span class="muted">待播</span>';
    }
    const tr = document.createElement('tr');
    tr.className = rowClass;
    tr.innerHTML = `
      <td>${i + 1}</td>
      <td><span class="tag tag-${s.type}">${s.type === 'sponsor' ? '赞助段' : '访谈段'}</span></td>
      <td>${escapeHtml(s.title)}</td>
      <td class="muted">${segmentParams(s)}</td>
      <td>${fmtDur(s.planned_seconds)}</td>
      <td>${fmtClock(s.est_start)}</td>
      <td>${fmtClock(s.est_end)}</td>
      <td>${statusHtml}</td>`;
    tbody.appendChild(tr);
  });

  $('edit-btn').disabled = pending.length === 0;
}

/* ---------- 结束当前段 ---------- */

async function completeCurrent() {
  const v = parseInt($('actual-input').value, 10);
  if (!Number.isInteger(v) || v <= 0) {
    alert('请输入有效的实际秒数（正整数）');
    return;
  }
  if (!confirm(`确认结束当前段并记录实际 ${v} 秒？已播记录不可修改。`)) return;
  const res = await postJSON('/api/segments/complete', { actual_seconds: v });
  if (!res.ok) {
    alert(res.error || '操作失败');
    return;
  }
  actualDraft = '';
  state = res.state;
  if (res.conflict) {
    alert(`已记录实际秒数。\n\n${res.message}\n已播记录已保留，请编辑待播清单解决冲突。`);
  }
  render();
}

/* ---------- 编辑待播清单 ---------- */

function enterEdit() {
  editing = true;
  editRows = state.segments
    .filter((s) => s.status === 'pending')
    .map((s) => ({ ...s }));
  $('edit-error').textContent = '';
  $('edit-panel').classList.remove('hidden');
  renderEdit();
  $('edit-panel').scrollIntoView({ behavior: 'smooth' });
}

function cancelEdit() {
  editing = false;
  editRows = [];
  $('edit-panel').classList.add('hidden');
}

function renderEdit() {
  const box = $('edit-rows');
  box.innerHTML = '';
  editRows.forEach((row, i) => {
    const div = document.createElement('div');
    div.className = 'seg-row' + (row.type === 'sponsor' ? ' locked' : '');
    if (row.type === 'sponsor') {
      div.innerHTML = `
        <span class="seg-idx">${i + 1}</span>
        <span class="tag tag-sponsor">赞助段</span>
        <span class="locked-title">${escapeHtml(row.title)}</span>
        <span class="hint">固定 ${row.fixed_seconds} 秒（不可修改）</span>`;
    } else {
      div.innerHTML = `
        <span class="seg-idx">${i + 1}</span>
        <span class="tag tag-interview">访谈段</span>
        <input data-k="title" placeholder="标题" value="${escapeHtml(row.title)}">
        <label>最短 <input data-k="min_seconds" type="number" min="1" value="${row.min_seconds}"></label>
        <label>目标 <input data-k="target_seconds" type="number" min="1" value="${row.target_seconds}"></label>
        <label>最长 <input data-k="max_seconds" type="number" min="1" value="${row.max_seconds}"></label>
        <span class="row-ops">
          <button type="button" data-op="up" ${i === 0 ? 'disabled' : ''}>↑</button>
          <button type="button" data-op="down" ${i === editRows.length - 1 ? 'disabled' : ''}>↓</button>
          <button type="button" data-op="del">✕</button>
        </span>`;
    }
    div.querySelectorAll('input').forEach((el) => {
      el.addEventListener('input', () => { row[el.dataset.k] = el.value; });
    });
    div.querySelectorAll('button').forEach((btn) => {
      btn.addEventListener('click', () => {
        const op = btn.dataset.op;
        if (op === 'del') editRows.splice(i, 1);
        else if (op === 'up' && i > 0) [editRows[i - 1], editRows[i]] = [editRows[i], editRows[i - 1]];
        else if (op === 'down' && i < editRows.length - 1) [editRows[i + 1], editRows[i]] = [editRows[i], editRows[i + 1]];
        renderEdit();
      });
    });
    box.appendChild(div);
  });
}

function addEditInterview() {
  editRows.push({
    id: null,
    type: 'interview',
    title: '',
    min_seconds: 240,
    target_seconds: 300,
    max_seconds: 360,
  });
  renderEdit();
}

async function saveEdit() {
  const payload = {
    segments: editRows.map((r) => (r.type === 'sponsor'
      ? { id: r.id, type: 'sponsor', fixed_seconds: r.fixed_seconds }
      : {
          ...(r.id ? { id: r.id } : {}),
          type: 'interview',
          title: r.title,
          min_seconds: parseInt(r.min_seconds, 10),
          target_seconds: parseInt(r.target_seconds, 10),
          max_seconds: parseInt(r.max_seconds, 10),
        })),
  };
  const res = await postJSON('/api/rundown/edit', payload);
  if (!res.ok) {
    $('edit-error').textContent = res.error || '保存失败';
    return;
  }
  cancelEdit();
  state = res.state;
  render();
}

/* ---------- 重置 ---------- */

async function resetShow() {
  if (!confirm('将删除整档节目及全部已播记录，确定重置？')) return;
  await postJSON('/api/show/reset', {});
  editing = false;
  actualDraft = '';
  $('edit-panel').classList.add('hidden');
  await refresh();
}

/* ---------- 主流程 ---------- */

function render() {
  if (!state || !state.show) {
    $('setup-view').classList.remove('hidden');
    $('live-view').classList.add('hidden');
    renderSetupRows();
  } else {
    $('setup-view').classList.add('hidden');
    $('live-view').classList.remove('hidden');
    if (!editing) renderLive();
  }
}

async function refresh() {
  if (editing) return; // 编辑时不打断
  state = await api('/api/state');
  render();
}

function init() {
  $('setup-start').value = localISO(new Date());
  $('add-sponsor').addEventListener('click', () => addSetupRow('sponsor'));
  $('add-interview').addEventListener('click', () => addSetupRow('interview'));
  $('create-show').addEventListener('click', createShow);
  $('edit-btn').addEventListener('click', enterEdit);
  $('edit-add-interview').addEventListener('click', addEditInterview);
  $('edit-save').addEventListener('click', saveEdit);
  $('edit-cancel').addEventListener('click', cancelEdit);
  $('reset-btn').addEventListener('click', resetShow);

  setInterval(() => {
    $('clock').textContent = new Date().toLocaleTimeString('zh-CN', { hour12: false });
  }, 1000);
  setInterval(refresh, 5000);
  refresh();
}

document.addEventListener('DOMContentLoaded', init);
