/* MCP Admin Dashboard 前端：零外部 CDN 依赖，Docker 离线环境可用。
   所有服务端数据均经 esc() 转义后插入，避免把工具输出当作 HTML 执行。 */
(function () {
  'use strict';

  const APP = window.APP || {};
  const $ = (sel, base) => (base || document).querySelector(sel);
  const $$ = (sel, base) => Array.from((base || document).querySelectorAll(sel));
  const rootEl = $('#app-root');
  const RANGE_HOURS = { '1h': 1, '24h': 24, '7d': 168, '30d': 720 };
  const RANGES = [['1h', '最近 1 小时'], ['24h', '最近 24 小时'], ['7d', '最近 7 天'],
                  ['30d', '最近 30 天'], ['custom', '自定义']];
  const STATUSES = ['SUCCESS', 'FAILED', 'DENIED', 'TIMEOUT', 'CANCELLED',
                    'INTERRUPTED', 'RUNNING', 'RECEIVED'];
  const THEME_KEY = 'mcp-admin-theme';

  // ------------------------------------------------------------------ 主题
  function applyTheme(theme) {
    document.documentElement.setAttribute('data-theme', theme === 'light' ? 'light' : 'dark');
  }
  window.toggleTheme = function () {
    const next = document.documentElement.getAttribute('data-theme') === 'light' ? 'dark' : 'light';
    try { localStorage.setItem(THEME_KEY, next); } catch (e) { /* 隐私模式 */ }
    applyTheme(next);
  };
  let savedTheme = '';
  try { savedTheme = localStorage.getItem(THEME_KEY) || ''; } catch (e) { /* noop */ }
  if (!savedTheme) {
    savedTheme = window.matchMedia && window.matchMedia('(prefers-color-scheme: light)').matches
      ? 'light' : 'dark';
  }
  applyTheme(savedTheme);

  if (!rootEl) return; // 登录页只使用主题能力

  // ------------------------------------------------------------------ 工具
  const esc = (v) => String(v === null || v === undefined ? '' : v)
    .replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const num = (v) => Number(v || 0);
  const tr = (v, n) => { const t = String(v === null || v === undefined ? '' : v);
    return esc(t.length > (n || 80) ? t.slice(0, n || 80) + '…' : t); };
  const statusBadge = (s) => '<span class="status ' + esc(s) + '">' + esc(s || '—') + '</span>';

  function fmtTime(v) {
    if (!v) return '—';
    const d = new Date(v);
    return isNaN(d.getTime()) ? esc(v) : d.toLocaleString('zh-CN', { hour12: false });
  }
  function fmtDuration(ms) {
    ms = num(ms);
    if (ms < 1000) return ms + ' ms';
    if (ms < 60000) return (ms / 1000).toFixed(2) + ' s';
    return (ms / 60000).toFixed(1) + ' min';
  }
  function fmtUptime(sec) {
    sec = num(sec);
    const d = Math.floor(sec / 86400), h = Math.floor(sec % 86400 / 3600), m = Math.floor(sec % 3600 / 60);
    return (d ? d + ' 天 ' : '') + (h ? h + ' 小时 ' : '') + m + ' 分钟';
  }
  function pretty(text) {
    const raw = String(text === null || text === undefined ? '' : text);
    try { return JSON.stringify(JSON.parse(raw), null, 2); } catch (e) { return raw; }
  }
  function toast(message) {
    const el = $('#toast');
    el.textContent = message;
    el.classList.add('show');
    clearTimeout(toast.timer);
    toast.timer = setTimeout(() => el.classList.remove('show'), 2600);
  }
  function copy(text) {
    const fallback = () => {
      const ta = document.createElement('textarea');
      ta.value = text; ta.style.position = 'fixed'; ta.style.opacity = '0';
      document.body.appendChild(ta); ta.select();
      try { document.execCommand('copy'); toast('已复制'); } catch (e) { toast('复制失败'); }
      ta.remove();
    };
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(() => toast('已复制'), fallback);
    } else fallback();
  }
  function csrfToken() {
    const match = document.cookie.match(/(?:^|;\s*)mcp_admin_csrf=([^;]+)/);
    return match ? decodeURIComponent(match[1]) : '';
  }
  async function api(path, options) {
    const opts = Object.assign({ credentials: 'same-origin' }, options || {});
    const method = String(opts.method || 'GET').toUpperCase();
    if (['POST', 'PUT', 'PATCH', 'DELETE'].indexOf(method) >= 0) {
      const headers = Object.assign({}, opts.headers || {});
      const token = csrfToken();
      if (token && !headers['X-CSRF-Token']) headers['X-CSRF-Token'] = token;
      opts.headers = headers;
    }
    const res = await fetch(path, opts);
    if (res.status === 403) {
      let csrfMsg = '请求被安全策略拒绝';
      try { const j = await res.json(); csrfMsg = j.detail || csrfMsg; } catch (e) { /* noop */ }
      toast(csrfMsg + '，请刷新页面后重试');
      throw new Error(csrfMsg);
    }
    if (res.status === 401) { location.href = '/login'; throw new Error('未登录或会话已过期'); }
    if (!res.ok) {
      let msg = res.statusText;
      try { const j = await res.json(); msg = j.detail || j.message || msg; } catch (e) { /* noop */ }
      throw new Error(msg);
    }
    return res.json();
  }
  const post = (path, body) => api(path, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body || {})
  });

  const state = {
    range: '24h', since: '', until: '',
    sort: 'timestamp_start', order: 'desc',
    offset: 0, limit: 50, filters: {}
  };

  function rangeParams(extra) {
    const p = new URLSearchParams();
    const now = new Date();
    if (state.range === 'custom' && state.since && state.until) {
      p.set('range', 'custom');
      p.set('since', new Date(state.since).toISOString());
      p.set('until', new Date(state.until).toISOString());
    } else {
      const key = RANGE_HOURS[state.range] ? state.range : '24h';
      p.set('range', key);
      p.set('since', new Date(now.getTime() - RANGE_HOURS[key] * 3600000).toISOString());
      p.set('until', now.toISOString());
    }
    Object.keys(extra || {}).forEach((k) => {
      const v = extra[k];
      if (v !== '' && v !== null && v !== undefined) p.set(k, v);
    });
    return p;
  }
  const rangeSelect = (id) => '<select id="' + id + '">' + RANGES
    .map(([v, label]) => '<option value="' + v + '"' + (state.range === v ? ' selected' : '') + '>' + label + '</option>')
    .join('') + '</select>';
  const rangeInputs = () => '<input type="datetime-local" id="range-since" value="' + esc(state.since) + '">' +
    '<input type="datetime-local" id="range-until" value="' + esc(state.until) + '">';

  function bindRange(prefix) {
    const sel = $('#' + prefix), since = $('#range-since'), until = $('#range-until');
    if (sel) sel.value = state.range;
    const sync = () => {
      state.range = sel ? sel.value : state.range;
      if (since) state.since = since.value;
      if (until) state.until = until.value;
      state.offset = 0;
    };
    if (sel) sel.addEventListener('change', sync);
    if (since) since.addEventListener('change', sync);
    if (until) until.addEventListener('change', sync);
  }

  // ------------------------------------------------------------------ 组件
  function kpi(label, value, foot) {
    return '<div class="card kpi"><div class="label">' + esc(label) + '</div>' +
      '<div class="value">' + esc(value) + '</div><div class="foot">' + esc(foot || '') + '</div></div>';
  }
  function card(title, body, sub) {
    return '<section class="card"><h3>' + esc(title) +
      (sub ? '<span class="sub">' + esc(sub) + '</span>' : '') + '</h3>' + body + '</section>';
  }
  function bars(items) {
    if (!items || !items.length) return '<p class="empty muted">暂无数据</p>';
    const max = Math.max(1, ...items.map((i) => num(i.count)));
    return items.map((i) => '<div class="bar-row"><span class="truncate" title="' + esc(i.label) + '">' +
      esc(i.label) + '</span><div class="bar-track"><div class="bar-fill" style="width:' +
      (num(i.count) / max * 100).toFixed(1) + '%"></div></div><span class="num">' +
      esc(i.count) + '</span></div>').join('');
  }
  function lineChart(labels, series) {
    const W = 620, H = 190, P = 26;
    const max = Math.max(1, ...series.flatMap((s) => (s.data || []).map(num)));
    const n = Math.max(1, (labels || []).length - 1);
    const xy = (v, i) => (P + i * (W - 2 * P) / n).toFixed(1) + ',' +
      (H - P - num(v) / max * (H - 2 * P)).toFixed(1);
    const grid = [0, .25, .5, .75, 1].map((f) => '<line class="axis" x1="' + P + '" y1="' +
      (P + f * (H - 2 * P)) + '" x2="' + (W - P) + '" y2="' + (P + f * (H - 2 * P)) + '"/>').join('');
    const lines = series.map((s) => '<polyline class="line ' + esc(s.cls || '') + '" points="' +
      (s.data || []).map(xy).join(' ') + '"' + (s.color ? ' stroke="' + s.color + '"' : '') + '/>').join('');
    const legend = series.map((s) => '<span class="legend" style="color:' + (s.color || 'var(--accent)') +
      '">● ' + esc(s.name) + '</span>').join(' ');
    return '<div class="legend-row">' + legend + '<span class="muted">峰值 ' + esc(max) + '</span></div>' +
      '<svg class="chart" viewBox="0 0 ' + W + ' ' + H + '" preserveAspectRatio="none" role="img" aria-label="调用趋势">' +
      grid + lines + '</svg><div class="chart-x"><span>' + esc((labels || [])[0] || '') +
      '</span><span>时间 →</span><span>' + esc((labels || []).slice(-1)[0] || '') + '</span></div>';
  }
  function table(cols, rows, opts) {
    opts = opts || {};
    if (!rows || !rows.length) return '<p class="empty muted">' + esc(opts.empty || '暂无数据') + '</p>';
    const head = cols.map((c) => '<th' + (c.sort ? ' data-sort="' + esc(c.sort) + '"' : '') + '>' +
      esc(c.label) + (c.sort && opts.sort === c.sort ? (opts.order === 'asc' ? ' ▲' : ' ▼') : '') + '</th>').join('');
    const body = rows.map((r) => {
      const id = opts.rowId ? ' data-id="' + esc(opts.rowId(r)) + '"' : '';
      return '<tr' + id + (opts.rowId ? ' class="clickable"' : '') + '>' + cols.map((c) =>
        '<td class="' + esc(c.cls || '') + '">' + (c.render ? c.render(r) : tr(r[c.key], c.limit)) + '</td>').join('') + '</tr>';
    }).join('');
    return '<div class="table-wrap"><table><thead><tr>' + head + '</tr></thead><tbody>' + body + '</tbody></table></div>';
  }
  function kv(pairs) {
    return '<dl class="kv">' + pairs.map(([k, v]) => '<dt>' + esc(k) + '</dt><dd>' +
      (v === undefined || v === null || v === '' ? '—' : v) + '</dd>').join('') + '</dl>';
  }
  function metricCard(title, m) {
    if (!m) return card(title, '<p class="empty muted">指标不可用（服务可能未启动）</p>');
    if (m.available === false) return card(title, '<p class="muted">' + esc(m.error || m.reason || '不可用') + '</p>');
    const mem = m.memory || {};
    const disks = (m.disks || []).slice(0, 3);
    const gpu = m.gpu || {};
    const docker = m.docker || {};
    let body = kv([
      ['指标来源', esc(m.source_label || m.source || '未知')],
      ['CPU', (m.cpu_percent === null || m.cpu_percent === undefined ? '—' : esc(m.cpu_percent) + ' %') +
        ' · ' + esc(m.cpu_cores || '—') + ' 核'],
      ['内存', mem.percent !== undefined ? esc(mem.percent) + ' %（' + esc(mem.used_mb) + ' / ' + esc(mem.total_mb) + ' MB）' : '—'],
      ['进程数', esc(m.process_count)],
      ['进程运行', fmtUptime(m.process_uptime_seconds)],
      ['磁盘', disks.length ? disks.map((d) => esc(d.mount) + ' ' + esc(d.percent) + '%').join('；') : '—']
    ]);
    if (gpu.available) {
      body += '<div class="hint">GPU：<ul>' + (gpu.devices || []).map((g) => '<li>' + esc(g.name) +
        ' · ' + esc(g.utilization_percent) + '% · ' + esc(g.memory_used_mb) + '/' + esc(g.memory_total_mb) + ' MB</li>').join('') + '</ul></div>';
    } else {
      body += '<p class="hint">GPU：' + esc(gpu.reason || '不可用') + '</p>';
    }
    body += '<p class="hint">Docker 容器状态：' + (docker.available
      ? esc((docker.containers || []).length) + ' 个容器'
      : esc(docker.reason || '不可用')) + '</p>';
    body += '<p class="hint">' + esc(m.scope_note || '') + '</p>';
    return card(title, body);
  }

  // ------------------------------------------------------------------ 首页
  async function renderDashboard(alive) {
    rootEl.innerHTML = '<div class="controls">' + rangeSelect('dash-range') + rangeInputs() +
      '<button class="btn primary" id="dash-refresh">刷新</button>' +
      '<button class="btn ghost" id="dash-all">查看全部记录</button>' +
      '<span class="muted" id="dash-range-label"></span></div><div id="dash-body"><div class="loading">正在加载…</div></div>';
    bindRange('dash-range');
    const load = async () => {
      const q = rangeParams().toString();
      const [d, v3] = await Promise.all([
        api('/api/dashboard?' + q),
        api('/api/v3/overview?' + q).catch(() => ({}))
      ]);
      if (!alive()) return;
      const st = d.stats || {}, by = st.by_status || {}, gw = d.gateway || {}, audit = d.audit || {};
      const okCount = num(by.SUCCESS);
      const failed = num(by.FAILED) + num(by.DENIED) + num(by.INTERRUPTED) + num(by.TIMEOUT);
      const rate = st.total ? (okCount / st.total * 100).toFixed(1) + '%' : '—';
      $('#audit-health').textContent = audit.healthy ? '审计正常 · ' + audit.backend : '审计降级 · ' + (audit.last_error || '');
      $('#audit-health').className = 'badge ' + (audit.healthy ? 'ok' : 'err');
      $('#dash-range-label').textContent = '统计区间：' + d.range.label;
      const ts = d.time_series || {};
      const trend = lineChart(ts.labels || [], [
        { name: '调用', data: ts.calls || [], color: 'var(--accent)' },
        { name: '失败/拒绝', data: ts.failed || [], color: 'var(--err)', cls: 'err' },
        { name: '超时', data: ts.timeouts || [], color: 'var(--warn)', cls: 'warn' }
      ]);
      const mon = d.monitoring || {};
      const gatewayMon = d.monitoring && d.monitoring.gateway_metrics || {};
      const errors = table([
        { key: 'timestamp_start', label: '时间', render: (r) => fmtTime(r.timestamp_start) },
        { key: 'tool_name', label: '工具', render: (r) => esc(r.tool_name) },
        { key: 'status', label: '状态', render: (r) => statusBadge(r.status) },
        { key: 'error_message', label: '错误', render: (r) => tr(r.error_type || r.error_message, 90) }
      ], d.recent_errors || [], { empty: '暂无异常调用' });
      $('#dash-body').innerHTML =
        '<div class="grid kpis">' +
        kpi('区间调用总数', st.total, '今日 ' + d.today + ' · 近 24h ' + d.last_24h) +
        kpi('成功率', rate, '失败/拒绝/中断 ' + failed + ' 次') +
        kpi('平均耗时', fmtDuration(st.avg_duration_ms), '按区间内全部调用') +
        kpi('活跃后台任务', d.active_tasks, '启动与真实运行结果分开记录') +
        kpi('活跃客户端会话', d.active_sessions, '仅显示令牌指纹') +
        kpi('已注册工具', gw.tools_registered === null || gw.tools_registered === undefined ? '—' : gw.tools_registered,
          gw.online ? '网关在线 · 运行 ' + fmtUptime(gw.uptime_seconds) : '网关离线') +
        '</div>' +
        '<div class="grid cols-2" style="margin-top:14px">' +
        card('调用趋势', trend, '按时间桶聚合（调用 / 失败 / 超时）') +
        card('状态分布', bars(Object.keys(by).map((k) => ({ label: k, count: by[k] })))) +
        '</div>' +
        '<div class="grid cols-2" style="margin-top:14px">' +
        card('高频工具 Top 8', bars(st.top_tools)) +
        card('耗时分布', bars(d.duration_distribution)) +
        '</div>' +
        '<div class="grid cols-2" style="margin-top:14px">' +
        card('客户端分布', bars(st.client_distribution)) +
        card('网关与审计状态', kv([
          ['网关状态', gw.online ? '<span class="badge ok">在线</span>' : '<span class="badge err">离线</span>'],
          ['启动时间', fmtTime(gw.started_at)],
          ['注册工具数', gw.tools_registered],
          ['审计后端', audit.backend || '—'],
          ['降级缓冲', audit.buffered],
          ['严格模式', audit.strict ? '已开启' : '关闭']
        ]) + (gw.online ? '' : '<p class="hint">网关 /health 不可达：' + esc((gw.detail || {}).error || '') + '</p>')) +
        '</div>' +
        '<div class="grid cols-2" style="margin-top:14px">' +
        metricCard('服务器状态 · 管理后台容器', mon) +
        metricCard('服务器状态 · MCP 网关容器', gatewayMon) +
        '</div>' +
        '<div class="grid cols-2" style="margin-top:14px">' +
        card('V3 能力占比', bars([
          { label: '搜索事件', count: num(v3.search_events) },
          { label: '论文库条目', count: num(v3.papers) },
          { label: '下载任务', count: Object.values(v3.downloads || {}).reduce((a, b) => a + num(b), 0) },
          { label: '文件传输', count: Object.values(v3.transfers || {}).reduce((a, b) => a + num(b), 0) },
          { label: '工具调用来源数', count: Object.keys(v3.tool_sources || {}).length }
        ]), '统计区间：' + esc((v3.range || {}).label || d.range.label) + ' · 缺失数据标明「暂无数据」') +
        card('最近失败下载', table([
          { key: 'ts', label: '时间', render: (r) => fmtTime(r.ts) },
          { key: 'title', label: '论文', render: (r) => tr(r.title, 46) },
          { key: 'status', label: '状态', render: (r) => dlBadge(r.status) },
          { key: 'error_message', label: '原因', render: (r) => tr(r.error_message, 60) }
        ], v3.recent_failed_downloads || [], { empty: '暂无失败下载或来源不可达提示' }),
        '如实列出失败原因，不以「任务创建成功」冒充文件下载成功') +
        '</div>' +
        '<div style="margin-top:14px">' + card('最近异常调用', errors, '来自 tool_calls 终态记录') + '</div>' +
        scopeBar([
          ['数据来自', sourceChip('container', '容器') + ' ' + sourceChip('external', '外部来源') +
            (v3.tool_sources && Object.keys(v3.tool_sources).length
              ? ' · 调用来源 ' + Object.keys(v3.tool_sources).map((s) => esc(s)).join('/') : '')],
          ['审计状态', audit.healthy ? '<span class="badge ok">正常</span>' : '<span class="badge err">降级</span>'],
          ['缓冲 / spool', esc(audit.buffered) + ' / ' + esc(audit.spool_pending === undefined ? '—' : audit.spool_pending)],
          ['最后刷新', esc(new Date().toLocaleString('zh-CN', { hour12: false }))],
          ['指标口径', 'CPU / 内存 / 磁盘来自容器自身，不是宿主机全机指标；审计数据已脱敏']
        ]);
    };
    $('#dash-refresh').addEventListener('click', () => load().catch(showError));
    $('#dash-all').addEventListener('click', () => { location.hash = '#calls'; });
    await load();
  }

  function showError(err) {
    rootEl.innerHTML = '<div class="card error">加载失败：' + esc(err.message || err) +
      '<p class="hint">请检查服务状态或稍后重试。</p></div>';
  }

  // ------------------------------------------------------------------ 实时监控
  async function renderRealtime(alive) {
    rootEl.innerHTML = '<div class="controls"><span class="badge" id="sse-state"><i class="live-dot off"></i>连接中</span>' +
      '<span class="muted">仅推送已落库的真实工具调用、后台任务生命周期与管理认证事件；SSE 连接本身不算工具执行。</span>' +
      '<button class="btn ghost" id="rt-clear">清空列表</button></div>' +
      '<div class="card"><h3>实时事件流<span class="sub">最新在上，最多保留 200 条</span></h3>' +
      '<div id="events" class="sse-list"></div></div>';
    const list = $('#events');
    function row(e) {
      return '<div class="event-row"><span class="ts">' + fmtTime(e.ts) + '</span>' +
        statusBadge(e.status) + '<b>' + esc(e.tool_name || e.kind) + '</b>' +
        '<span class="muted">' + tr(e.detail || e.request_id, 160) + '</span></div>';
    }
    const add = (e, prepend) => {
      const div = document.createElement('div');
      div.innerHTML = row(e);
      const node = div.firstChild;
      if (prepend) list.insertBefore(node, list.firstChild); else list.appendChild(node);
      while (list.children.length > 200) list.removeChild(list.lastChild);
    };
    const recent = await api('/api/live/recent?after=0');
    if (!alive()) return;
    (recent.events || []).filter((e) => ['tool_call', 'task_lifecycle', 'admin_auth'].indexOf(e.kind) >= 0)
      .slice(-40).forEach((e) => add(e, false));
    if (!list.children.length) list.innerHTML = '<p class="empty muted">暂无事件，等待新的工具调用…</p>';
    const es = new EventSource('/api/live/stream');
    cleanup = () => es.close();
    es.onopen = () => {
      const el = $('#sse-state');
      if (el) el.innerHTML = '<i class="live-dot"></i>实时连接正常';
    };
    es.onmessage = (ev) => {
      if ($('#events') !== list) return;
      try {
        if (list.querySelector('.empty')) list.innerHTML = '';
        add(JSON.parse(ev.data), true);
      } catch (e) { /* 忽略非法帧 */ }
    };
    es.onerror = () => {
      const el = $('#sse-state');
      if (el) el.innerHTML = '<i class="live-dot off"></i>连接断开，浏览器将自动重连';
    };
    $('#rt-clear').addEventListener('click', () => {
      list.innerHTML = '<p class="empty muted">已清空，等待新的事件…</p>';
    });
  }

  // ------------------------------------------------------------------ 调用记录
  const CALL_COLS = [
    { key: 'timestamp_start', label: '时间', sort: 'timestamp_start', render: (r) => fmtTime(r.timestamp_start) },
    { key: 'tool_name', label: '工具', sort: 'tool_name', render: (r) => esc(r.tool_name) },
    { key: 'status', label: '状态', sort: 'status', render: (r) => statusBadge(r.status) },
    { key: 'duration_ms', label: '耗时', sort: 'duration_ms', render: (r) => fmtDuration(r.duration_ms) },
    { key: 'client_id', label: '客户端', sort: 'client_id', render: (r) => '<span class="mono">' + tr(r.declared_client || r.client_id, 28) + '</span>' },
    { key: 'source', label: '来源', sort: 'source', render: (r) => sourceChip(r.source || 'mcp', r.source || 'mcp') },
    { key: 'task_id', label: '任务', render: (r) => tr(r.task_id, 20) },
    { key: 'result', label: '结果摘要', render: (r) =>
      '<span class="mono">' + tr(r.result, 46) + '</span>' +
      (r.result_ref ? '<span class="muted"> · 引用 ' + tr(r.result_ref, 12) + '</span>' : '') },
    { key: 'error_message', label: '异常', render: (r) => tr(r.error_type || r.error_message, 46) },
    { key: 'request_id', label: '请求 ID', render: (r) => '<span class="mono">' + tr(r.request_id, 24) + '</span>' }
  ];

  function collectFilters() {
    const get = (id) => { const el = $('#' + id); return el ? el.value.trim() : ''; };
    state.filters = {
      tool_name: get('f-tool'), status: get('f-status'), client_id: get('f-client'),
      keyword: get('f-keyword'), task_id: get('f-task'), git_repo: get('f-repo'),
      file_path: get('f-file'), actor_id: get('f-actor')
    };
    state.limit = num(get('f-limit')) || 50;
  }

  async function renderCalls(alive) {
    rootEl.innerHTML = '<div class="card"><h3>组合筛选<span class="sub">时间、工具、状态、客户端、关键词、任务、仓库、路径可叠加</span></h3>' +
      '<div class="controls">' + rangeSelect('calls-range') + rangeInputs() +
      '<input id="f-keyword" type="search" placeholder="关键词：参数 / 输出 / 错误">' +
      '<input id="f-tool" placeholder="工具名，如 run_command">' +
      '<select id="f-status"><option value="">全部状态</option>' +
      STATUSES.map((s) => '<option value="' + s + '"' + (state.filters.status === s ? ' selected' : '') + '>' + s + '</option>').join('') + '</select>' +
      '<input id="f-client" placeholder="客户端指纹 tok_…">' +
      '<input id="f-actor" placeholder="操作者 ID">' +
      '<input id="f-task" placeholder="后台任务 ID">' +
      '<input id="f-repo" placeholder="Git 仓库">' +
      '<input id="f-file" placeholder="文件路径">' +
      '<select id="f-source"><option value="">全部来源</option>' +
      ['mcp', 'rest', 'admin', 'http', 'task', 'host-agent', 'oauth', 'system'].map((s) =>
        '<option value="' + s + '"' + (state.filters.source === s ? ' selected' : '') + '>' + s + '</option>').join('') + '</select>' +
      '<select id="f-limit"><option>25</option><option>50</option><option>100</option><option>200</option></select>' +
      '<button class="btn primary" id="f-apply">查询</button><button class="btn ghost" id="f-reset">重置</button></div></div>' +
      '<div class="card" style="margin-top:14px"><div class="controls"><h3 style="margin:0;flex:1">完整调用记录' +
      '<span class="sub" id="calls-total"></span></h3>' +
      '<button class="btn" data-dl="csv">导出 CSV</button><button class="btn" data-dl="jsonl">导出 JSONL</button>' +
      '<button class="btn" data-dl="json">导出 JSON</button>' +
      '<button class="btn" id="btn-bg-export">后台大批量导出</button></div>' +
      '<div id="calls-table"><div class="loading">正在加载…</div></div>' +
      '<div class="pager" id="calls-pager"></div></div>';
    bindRange('calls-range');
    $('#f-limit').value = String(state.limit);
    const FILTER_IDS = {
      'f-tool': 'tool_name', 'f-status': 'status', 'f-client': 'client_id',
      'f-actor': 'actor_id', 'f-task': 'task_id', 'f-repo': 'git_repo',
      'f-file': 'file_path', 'f-keyword': 'keyword', 'f-source': 'source'
    };
    Object.keys(FILTER_IDS).forEach((id) => {
      const el = $('#' + id);
      const key = FILTER_IDS[id];
      if (el && state.filters[key]) el.value = state.filters[key];
      if (el) el.addEventListener('change', collectFilters);
    });

    const load = async () => {
      const p = rangeParams(Object.assign({}, state.filters, {
        limit: state.limit, offset: state.offset, sort: state.sort, order: state.order
      }));
      const d = await api('/api/calls?' + p.toString());
      if (!alive()) return;
      $('#calls-total').textContent = '共 ' + d.total + ' 条';
      $('#calls-table').innerHTML = table(CALL_COLS, d.rows, {
        sort: state.sort, order: state.order, rowId: (r) => r.request_id,
        empty: '当前筛选条件下没有调用记录'
      });
      $$('#calls-table th[data-sort]').forEach((th) => th.addEventListener('click', () => {
        const key = th.dataset.sort;
        if (state.sort === key) state.order = state.order === 'desc' ? 'asc' : 'desc';
        else { state.sort = key; state.order = 'desc'; }
        state.offset = 0;
        load().catch(showError);
      }));
      $$('#calls-table tr[data-id]').forEach((rowEl) => rowEl.addEventListener('click', () => {
        location.hash = '#call/' + encodeURIComponent(rowEl.dataset.id);
      }));
      const pages = Math.max(1, Math.ceil(d.total / state.limit));
      const page = Math.floor(state.offset / state.limit) + 1;
      $('#calls-pager').innerHTML = '<button class="btn" id="pg-prev"' + (page <= 1 ? ' disabled' : '') + '>上一页</button>' +
        '<span>第 ' + page + ' / ' + pages + ' 页 · 每页 ' + state.limit + ' 条</span>' +
        '<button class="btn" id="pg-next"' + (page >= pages ? ' disabled' : '') + '>下一页</button>';
      $('#pg-prev').addEventListener('click', () => { state.offset = Math.max(0, state.offset - state.limit); load().catch(showError); });
      $('#pg-next').addEventListener('click', () => { state.offset += state.limit; load().catch(showError); });
    };
    $('#f-apply').addEventListener('click', () => { collectFilters(); state.offset = 0; load().catch(showError); });
    $('#f-reset').addEventListener('click', () => {
      state.filters = {}; state.offset = 0; state.sort = 'timestamp_start'; state.order = 'desc';
      state.range = '24h'; state.since = ''; state.until = '';
      render();
    });
    $$('[data-dl]').forEach((btn) => btn.addEventListener('click', () => {
      if ($('#f-tool')) collectFilters();
      const p = rangeParams(Object.assign({}, state.filters, { fmt: btn.dataset.dl }));
      window.location.href = '/api/export/stream?' + p.toString();
    }));
    $('#btn-bg-export').addEventListener('click', async () => {
      if ($('#f-tool')) collectFilters();
      const p = rangeParams(state.filters);
      const filters = {};
      p.forEach((v, k) => { filters[k] = v; });
      try {
        const r = await post('/api/exports', { fmt: 'jsonl', filters: filters });
        toast('后台导出任务已创建：' + r.job_id);
      } catch (e) { toast('创建失败：' + e.message); }
    });
    await load();
  }

  // ------------------------------------------------------------------ 调用详情
  async function renderCallDetail(alive, requestId) {
    rootEl.innerHTML = '<div class="controls"><button class="btn" id="back-calls">← 返回调用记录</button></div>' +
      '<div id="detail-body"><div class="loading">正在加载…</div></div>';
    $('#back-calls').addEventListener('click', () => { location.hash = '#calls'; });
    const d = await api('/api/calls/' + encodeURIComponent(requestId));
    if (!alive()) return;
    const c = d.call || {};
    const argsText = pretty(c.arguments);
    const resultText = pretty(c.result);
    const preview = resultText.length > 1200 ? resultText.slice(0, 1200) + '\n…（以下内容已折叠）' : resultText;
    const timeline = (d.timeline || []).map((e) => '<li><span class="ts">' + fmtTime(e.ts) + '</span> ' +
      statusBadge(e.status) + ' ' + esc(e.kind) + ' <span class="muted">' + tr(e.detail, 160) + '</span></li>').join('');
    const related = [];
    if ((d.tasks || []).length) related.push(card('关联后台任务', table([
      { key: 'ts', label: '时间', render: (r) => fmtTime(r.ts) },
      { key: 'status', label: '状态', render: (r) => statusBadge(r.status) },
      { key: 'pid', label: 'PID' }, { key: 'log_path', label: '日志', render: (r) => tr(r.log_path, 60) }
    ], d.tasks)));
    if ((d.git || []).length) related.push(card('关联 Git 操作', table([
      { key: 'ts', label: '时间', render: (r) => fmtTime(r.ts) },
      { key: 'operation', label: '操作' }, { key: 'repo_path', label: '仓库' },
      { key: 'branch', label: '分支' }, { key: 'status', label: '状态', render: (r) => statusBadge(r.status) }
    ], d.git)));
    if ((d.experiments || []).length) related.push(card('关联实验运行', table([
      { key: 'ts', label: '时间', render: (r) => fmtTime(r.ts) },
      { key: 'name', label: '名称' }, { key: 'status', label: '状态', render: (r) => statusBadge(r.status) },
      { key: 'command', label: '命令', render: (r) => tr(r.command, 70) }
    ], d.experiments)));
    $('#detail-body').innerHTML =
      card('调用详情', kv([
        ['请求 ID', '<span class="mono">' + esc(c.request_id) + '</span>'],
        ['链路 ID', '<span class="mono">' + esc(c.trace_id) + '</span>'],
        ['工具 / 状态', esc(c.tool_name) + ' ' + statusBadge(c.status)],
        ['开始 / 结束', fmtTime(c.timestamp_start) + ' → ' + fmtTime(c.timestamp_end)],
        ['耗时 / 退出码', fmtDuration(c.duration_ms) + ' / ' + esc(c.exit_code)],
        ['客户端', '<span class="mono">' + esc(c.client_id) + '</span>'],
        ['自报客户端（未验证）', esc(c.declared_client)],
        ['操作者 / 会话', esc(c.actor_id) + ' / ' + esc(c.session_id)],
        ['来源 / 来源 IP', esc(c.source) + ' / ' + esc(c.remote_ip)],
        ['任务 / 确认 / 仓库', esc(c.task_id) + ' / ' + esc(c.approval_id) + ' / ' + esc(c.git_repo)],
        ['实验 / 文件', esc(c.experiment_id) + ' / ' + esc(c.file_path)],
        ['错误', esc(c.error_type || c.error_message)],
        ['大输出引用', esc(c.result_ref) + (c.result_size ? '（' + esc(c.result_size) + ' 字节, sha256=' + esc(c.result_sha256) + '）' : '')]
      ]), '点击行内按钮可复制字段') +
      '<div class="grid cols-2" style="margin-top:14px">' +
      card('调用参数（已脱敏）', '<div class="controls"><button class="btn" data-copy="#args-json">复制参数</button></div>' +
        '<pre class="json" id="args-json">' + esc(argsText) + '</pre>') +
      card('执行结果（已脱敏）', '<div class="controls"><button class="btn" data-copy="#result-json">复制结果</button></div>' +
        '<pre class="json" id="result-json">' + esc(preview) + '</pre>' +
        (resultText.length > 1200 ? '<details><summary>展开完整结果（' + resultText.length + ' 字符）</summary><pre class="json">' + esc(resultText) + '</pre></details>' : '')) +
      '</div>' +
      '<div style="margin-top:14px">' + card('事件时间线', timeline ? '<ul class="timeline">' + timeline + '</ul>' : '<p class="empty muted">暂无关联事件</p>') + '</div>' +
      (related.length ? '<div class="grid cols-2" style="margin-top:14px">' + related.join('') + '</div>' : '') +
      '<p class="hint" style="margin-top:14px">' + esc(d.source_note || '') + '</p>';
  }

  // ------------------------------------------------------------------ 专项视图
  async function renderGit(alive) {
    rootEl.innerHTML = '<div class="controls"><input id="git-repo" type="search" placeholder="按仓库名 / 路径过滤">' +
      '<button class="btn primary" id="git-apply">查询</button></div><div id="git-body"><div class="loading">正在加载…</div></div>';
    const load = async () => {
      const repo = $('#git-repo').value.trim();
      const d = await api('/api/git?repo=' + encodeURIComponent(repo));
      if (!alive()) return;
      $('#git-body').innerHTML = card('Git 操作审计', table([
        { key: 'ts', label: '时间', render: (r) => fmtTime(r.ts) },
        { key: 'operation', label: '操作' }, { key: 'repo_path', label: '仓库' },
        { key: 'branch', label: '分支' }, { key: 'commit_sha', label: '提交', render: (r) => tr(r.commit_sha, 16) },
        { key: 'commit_message', label: '说明', render: (r) => tr(r.commit_message, 60) },
        { key: 'status', label: '状态', render: (r) => statusBadge(r.status) },
        { key: 'request_id', label: '请求 ID', render: (r) => '<span class="mono">' + tr(r.request_id, 20) + '</span>' }
      ], d.rows, { empty: '暂无 Git 操作（由 run_command 中的 git 命令自动推导）' }),
      '从统一采集层解析，不改动原有 Shell 工具');
    };
    $('#git-apply').addEventListener('click', () => load().catch(showError));
    await load();
  }

  async function renderExperiments(alive) {
    rootEl.innerHTML = '<div class="card"><h3>科研实验 / 后台任务监控<span class="sub">启动工具的调用结果与任务真实运行结果分开记录</span></h3>' +
      '<div id="exp-body"><div class="loading">正在加载…</div></div></div>';
    const [d, backend, audit] = await Promise.all([
      api('/api/experiments'),
      api('/api/host/status').catch(() => ({})),
      api('/api/v3/overview?' + rangeParams().toString()).catch(() => ({}))
    ]);
    if (!alive()) return;
    $('#exp-body').innerHTML = table([
      { key: 'ts', label: '时间', render: (r) => fmtTime(r.ts) },
      { key: 'name', label: '名称 / 任务', render: (r) => esc(r.name || r.task_id) },
      { key: 'status', label: '状态', render: (r) => statusBadge(r.status) },
      { key: 'command', label: '命令', render: (r) => tr(r.command, 60) },
      { key: 'pid', label: 'PID' },
      { key: 'git_commit', label: '代码版本', render: (r) => tr(r.git_commit, 14) },
      { key: 'output_path', label: '输出路径', render: (r) => tr(r.output_path, 26) },
      { key: 'backend', label: '执行后端', render: () => sourceChip(backend.default || 'container',
          backend.default || 'container') },
      { key: 'detail', label: '操作', render: (r) => '<button class="btn" data-log="' + esc(r.task_id) + '">查看日志</button>' }
    ], d.rows, { empty: '暂无后台实验运行记录' }) + '<div id="exp-log"></div>' +
      scopeBar([
        ['来源标识', sourceChip('container', '容器后端') + ' ' +
          ((backend['host-agent'] || {}).available
            ? sourceChip('host-agent', '宿主已授权')
            : '<span class="source-chip offline">宿主未连接 / 未授权</span>')],
        ['复现元数据', 'Git commit、输出路径与命令写入 experiment_runs / task_state'],
        ['审计健康', (audit.audit || {}).healthy === false ? '<span class="badge err">降级</span>' : '<span class="badge ok">正常</span>'],
        ['更新时间', esc(new Date().toLocaleString('zh-CN', { hour12: false }))]
      ]);
  }

  async function renderServer(alive) {
    rootEl.innerHTML = '<div id="server-body"><div class="loading">正在加载…</div></div>';
    const [d, backend] = await Promise.all([
      api('/api/monitoring'), api('/api/host/status').catch(() => ({}))
    ]);
    if (!alive()) return;
    const host = backend['host-agent'] || {};
    $('#server-body').innerHTML = '<div class="controls">' +
      '<select id="server-scope"><option value="container">容器来源</option>' +
      '<option value="host-agent">宿主来源（需授权）</option></select>' +
      '<span class="muted" id="server-scope-note">' + esc(d.scope_note) + '</span></div>' +
      '<div class="grid cols-2" id="server-cards">' +
      metricCard('管理后台容器', d.admin_container) + metricCard('MCP 网关容器', d.gateway_container) +
      '</div>' +
      card('执行后端与宿主来源', kv([
        ['默认后端', sourceChip(backend.default || 'container', backend.default || 'container')],
        ['容器后端', (backend.container || {}).available ? '<span class="badge ok">可用</span>' : '—'],
        ['Host Agent', host.available ? '<span class="badge ok">已连接（Unix Socket）</span>'
          : '<span class="badge err">未连接 / 未授权</span>'],
        ['未授权原因', esc(host.reason || '（无）')],
        ['传输方式', esc(host.transport || 'unix-socket') + ' · 无 TCP 公网端口'],
        ['授权说明', esc(host.note || '真实宿主权限需部署时由用户授予')]
      ]), '不可用项给出具体原因，不虚构 GPU / 宿主数据') +
      scopeBar([
        ['数据来自', sourceChip('container', '容器自身指标') + ' ' +
          (host.available ? sourceChip('host-agent', '宿主机（已授权）') : '<span class="source-chip offline">宿主未授权</span>')],
        ['采集时间', esc(new Date().toLocaleString('zh-CN', { hour12: false }))],
        ['网关状态', d.gateway_online ? '<span class="badge ok">在线</span>' : '<span class="badge err">离线</span>'],
        ['指标口径', 'GPU / Docker 列表仅在显式透传设备或挂载 socket 后可用']
      ]);
    $('#server-scope').addEventListener('change', () => {
      const isHost = $('#server-scope').value === 'host-agent';
      $('#server-scope-note').textContent = isHost
        ? '宿主来源：' + (host.available ? 'Host Agent 已连接，数据由授权宿主目录采集。'
            : 'Host Agent 未连接或未授权，以下不展示任何宿主指标，避免把容器指标误报为宿主指标。')
        : d.scope_note;
      $('#server-cards').innerHTML = isHost
        ? (host.available ? card('宿主机（Host Agent 握手信息）', kv([
              ['角色', esc((host.ping || {}).role || 'host-agent')],
              ['传输', 'Unix Socket（无 TCP 端口）'],
              ['授权目录', esc(((host.ping || {}).roots || []).join('；') || '未返回')],
              ['资源指标', '当前仅完成协议握手，未采集 CPU / 内存 / GPU；不虚构宿主指标']
            ])) : '<div class="card"><p class="empty muted">Host Agent 未连接 / 未授权：' +
            esc(host.reason || '') + '。真实宿主权限只能在部署时由用户显式授予。</p></div>')
        : metricCard('管理后台容器', d.admin_container) +
          metricCard('MCP 网关容器', d.gateway_container);
    });
  }

  async function renderClients(alive) {
    rootEl.innerHTML = '<div id="clients-body"><div class="loading">正在加载…</div></div>';
    const [c, s] = await Promise.all([api('/api/clients'), api('/api/sessions')]);
    if (!alive()) return;
    $('#clients-body').innerHTML = '<div style="margin-bottom:14px">' +
      card('MCP 客户端 / 会话', table([
        { key: 'session_id', label: '会话 ID', render: (r) => '<span class="mono">' + tr(r.session_id, 24) + '</span>' },
        { key: 'client_id', label: '令牌指纹', render: (r) => '<span class="mono">' + esc(r.client_id) + '</span>' },
        { key: 'declared_client', label: '自报名称（未验证）' },
        { key: 'call_count', label: '调用数' },
        { key: 'first_seen', label: '首次', render: (r) => fmtTime(r.first_seen) },
        { key: 'last_seen', label: '最近', render: (r) => fmtTime(r.last_seen) },
        { key: 'status', label: '状态', render: (r) => statusBadge(r.status) }
      ], c.rows, { empty: '暂无客户端会话' }), '仅显示令牌 SHA-256 指纹，绝不显示明文 Token') + '</div>' +
      card('管理员登录会话', table([
        { key: 'created_at', label: '登录时间', render: (r) => fmtTime(r.created_at) },
        { key: 'username', label: '账号' },
        { key: 'remote_ip', label: '来源 IP' },
        { key: 'last_seen', label: '最近活跃', render: (r) => fmtTime(r.last_seen) },
        { key: 'expires_at', label: '过期时间', render: (r) => fmtTime(r.expires_at) },
        { key: 'active', label: '状态', render: (r) => (r.active ? '<span class="badge ok">有效</span>' : '<span class="badge">已失效</span>') },
        { key: 'session_short', label: '操作', render: (r) => r.active ? '<button class="btn" data-revoke="' + esc(r.session_id) + '">强制下线</button>' : '—' }
      ], s.rows), '登录失败会记录并被临时限制；可强制撤销任意会话');
  }

  async function renderConfig(alive) {
    rootEl.innerHTML = '<div id="config-body"><div class="loading">正在加载…</div></div>';
    const d = await api('/api/config');
    if (!alive()) return;
    const runtime = Object.keys(d.runtime).map((k) => ({ key: k, value: d.runtime[k] }));
    const sensitive = Object.keys(d.sensitive).map((k) => ({ key: k, value: d.sensitive[k] }));
    const settings = d.settings || {};
    $('#config-body').innerHTML = '<div class="grid cols-2">' +
      card('运行环境变量（只读）', table([
        { key: 'key', label: '变量', render: (r) => '<span class="mono">' + esc(r.key) + '</span>' },
        { key: 'value', label: '当前值', render: (r) => tr(r.value || '（默认）', 60) }
      ], runtime), '变更需修改环境变量并重启容器') +
      card('敏感配置（不返回明文）', table([
        { key: 'key', label: '变量', render: (r) => '<span class="mono">' + esc(r.key) + '</span>' },
        { key: 'value', label: '状态' }
      ], sensitive) + '<div class="controls" style="margin-top:14px">' +
        '<select id="cfg-key">' + sensitive.map((r) => '<option value="' + esc(r.key) + '">' + esc(r.key) + '</option>').join('') + '</select>' +
        '<input id="cfg-password" type="password" placeholder="重新输入管理员口令">' +
        '<button class="btn" id="cfg-sensitive">提交变更请求</button></div>' +
        '<p class="hint">敏感配置变更需要重新认证，只写入审计事件；实际生效必须通过环境变量并重启。</p>') +
      '</div>' +
      '<div style="margin-top:14px">' + card('界面设置（立即生效）',
        '<form id="cfg-form" class="controls"><label class="inline">默认时间范围 <select id="cfg-range">' +
        RANGES.map(([v, l]) => '<option value="' + v + '"' + (settings.default_range === v ? ' selected' : '') + '>' + l + '</option>').join('') +
        '</select></label><label class="inline">每页条数 <input id="cfg-page" type="number" min="10" max="200" value="' + esc(settings.page_size || 50) + '"></label>' +
        '<label class="inline">主题 <select id="cfg-theme">' +
        [['auto', '跟随系统'], ['dark', '深色'], ['light', '浅色']].map(([v, l]) =>
          '<option value="' + v + '"' + (settings.theme === v ? ' selected' : '') + '>' + l + '</option>').join('') +
        '</select></label><button class="btn primary" type="submit">保存</button></form>' +
        '<p class="hint">' + esc(d.note || '') + '</p>') + '</div>';
    $('#cfg-form').addEventListener('submit', async (ev) => {
      ev.preventDefault();
      try {
        for (const [key, value] of [['default_range', $('#cfg-range').value],
                                    ['page_size', $('#cfg-page').value],
                                    ['theme', $('#cfg-theme').value]]) {
          await post('/api/config', { key: key, value: String(value) });
        }
        const t = $('#cfg-theme').value;
        if (t === 'auto') applyTheme(window.matchMedia && window.matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark');
        else applyTheme(t);
        try { localStorage.setItem(THEME_KEY, document.documentElement.getAttribute('data-theme')); } catch (e) { /* noop */ }
        state.range = $('#cfg-range').value; state.limit = num($('#cfg-page').value) || 50;
        toast('界面设置已保存');
      } catch (e) { toast('保存失败：' + e.message); }
    });
    $('#cfg-sensitive').addEventListener('click', async () => {
      try {
        const r = await post('/api/config', { key: $('#cfg-key').value, password: $('#cfg-password').value });
        toast(r.message || '变更请求已记录');
        $('#cfg-password').value = '';
      } catch (e) { toast('提交失败：' + e.message); }
    });
  }

  async function renderExports(alive) {
    rootEl.innerHTML = '<div class="card"><h3>创建导出任务<span class="sub">按当前组合筛选条件，后台流式写入受保护文件</span></h3>' +
      '<div class="controls"><select id="ex-fmt"><option value="jsonl">JSONL</option><option value="csv">CSV</option><option value="json">JSON</option></select>' +
      '<button class="btn primary" id="ex-create">创建任务</button><span class="hint">导出内容同样经过脱敏；下载链接 10 分钟内有效。</span></div></div>' +
      '<div style="margin-top:14px"><div class="card"><h3>导出任务</h3><div id="ex-body"><div class="loading">正在加载…</div></div></div></div>';
    const load = async () => {
      const d = await api('/api/exports');
      if (!alive()) return;
      $('#ex-body').innerHTML = table([
        { key: 'ts', label: '创建时间', render: (r) => fmtTime(r.ts) },
        { key: 'job_id', label: '任务 ID', render: (r) => '<span class="mono">' + esc(r.job_id) + '</span>' },
        { key: 'fmt', label: '格式' },
        { key: 'status', label: '状态', render: (r) => statusBadge(r.status === 'done' ? 'SUCCESS' : (r.status === 'failed' ? 'FAILED' : 'RUNNING')) },
        { key: 'row_count', label: '行数' },
        { key: 'detail', label: '详情', render: (r) => tr(r.detail, 50) },
        { key: 'job_id2', label: '操作', render: (r) => r.status === 'done'
          ? '<button class="btn" data-export-link="' + esc(r.job_id) + '">下载</button>' : '—' }
      ], d.rows, { empty: '暂无导出任务' });
    };
    $('#ex-create').addEventListener('click', async () => {
      const p = rangeParams(state.filters);
      const filters = {};
      p.forEach((v, k) => { filters[k] = v; });
      try {
        await post('/api/exports', { fmt: $('#ex-fmt').value, filters: filters });
        toast('导出任务已创建，稍后刷新列表');
        setTimeout(() => load().catch(showError), 800);
      } catch (e) { toast('创建失败：' + e.message); }
    });
    await load();
  }

  // ------------------------------------------------------------------ V3 组件
  function fmtBytes(v) {
    const n = num(v);
    if (!n) return '0 B';
    const units = ['B', 'KB', 'MB', 'GB'];
    const i = Math.min(units.length - 1, Math.floor(Math.log(n) / Math.log(1024)));
    return (n / Math.pow(1024, i)).toFixed(i ? 1 : 0) + ' ' + units[i];
  }
  function sourceChip(source, label) {
    const s = String(source || 'unknown').toLowerCase();
    const kind = s.indexOf('host') >= 0 ? 'host-agent'
      : (s.indexOf('container') >= 0 || s.indexOf('gateway') >= 0 || s.indexOf('admin') >= 0)
        ? 'container' : 'external';
    return '<span class="source-chip ' + kind + '">' + esc(label || source || '未知来源') + '</span>';
  }
  function scopeBar(items) {
    return '<div class="scope-bar fade-in">' + items.map(([k, v]) =>
      '<span><strong>' + esc(k) + '：</strong>' + v + '</span>').join('') + '</div>';
  }
  function dlBadge(s) {
    return '<span class="status ' + esc(String(s || '').toLowerCase()) + '">' + esc(s || '—') + '</span>';
  }
  function flowStrip(status) {
    const steps = ['created', 'downloading', 'verifying', 'success'];
    const bad = ['failed', 'error', 'no_oa', 'source_unavailable'];
    const cur = String(status || '').toLowerCase();
    const idx = steps.indexOf(cur);
    return '<span class="flow-strip">' + steps.map((s, i) =>
      '<span class="flow-step ' + (bad.indexOf(cur) >= 0 && i === 0 ? 'bad'
        : idx >= 0 && i <= idx ? (i === steps.length - 1 ? 'good' : 'on') : '') + '">' +
      esc(s) + '</span>').join('<span class="muted">›</span>') +
      (bad.indexOf(cur) >= 0 ? '<span class="flow-step bad">' + esc(cur) + '</span>' : '') + '</span>';
  }
  function progressBar(status, bytes, total) {
    const s = String(status || '').toLowerCase();
    const pct = total > 0 ? Math.min(100, Math.round(num(bytes) / num(total) * 100))
      : (s === 'success' || s === 'duplicate' ? 100 : 0);
    const cls = s === 'success' || s === 'duplicate' ? 'done'
      : (s === 'failed' || s === 'error' ? 'failed' : '');
    return '<div class="progress ' + cls + '"><i style="width:' + pct + '%"></i></div>' +
      '<span class="muted">' + fmtBytes(bytes) + (total ? ' / ' + fmtBytes(total) : '') + ' · ' + pct + '%</span>';
  }

  const SEARCH_SOURCES = ['arxiv', 'crossref', 'openalex', 'pubmed', 'europepmc',
                          'semanticscholar', 'dblp', 'openreview', 'hn', 'stackexchange',
                          'searxng', 'duckduckgo', 'wikipedia'];

  // ------------------------------------------------------------------ 搜索监控
  async function renderSearch(alive) {
    rootEl.innerHTML = '<div class="card"><h3>搜索监控' +
      '<span class="sub">垂直学术源 / 社区源 / 可选通用搜索，覆盖范围如实标注</span></h3>' +
      '<div class="controls">' + rangeSelect('search-range') + rangeInputs() +
      '<select id="search-provider"><option value="">全部数据源</option>' +
      SEARCH_SOURCES.map((s) => '<option value="' + s + '">' + s + '</option>').join('') + '</select>' +
      '<input id="search-keyword" type="search" placeholder="脱敏关键词 / 查询摘要">' +
      '<select id="search-status"><option value="">全部状态</option>' +
      ['SUCCESS', 'FAILED', 'SOURCE_UNAVAILABLE', 'RATE_LIMITED', 'CACHED'].map((s) =>
        '<option value="' + s + '">' + s + '</option>').join('') + '</select>' +
      '<button class="btn primary" id="search-refresh">刷新</button></div></div>' +
      '<div id="search-body" style="margin-top:14px"><div class="loading">正在加载…</div></div>';
    bindRange('search-range');
    const load = async () => {
      const p = rangeParams({ provider: $('#search-provider').value,
                              keyword: $('#search-keyword').value.trim(),
                              status: $('#search-status').value, limit: 200 });
      const d = await api('/api/search/events?' + p.toString());
      if (!alive()) return;
      const matrix = d.by_provider || [];
      const total = matrix.reduce((a, m) => a + num(m.requests), 0);
      const failures = matrix.reduce((a, m) => a + num(m.failures), 0);
      const limited = matrix.reduce((a, m) => a + num(m.rate_limited), 0);
      const cached = matrix.reduce((a, m) => a + num(m.cached), 0);
      const avg = total ? Math.round(matrix.reduce((a, m) => a + num(m.avg_ms) * num(m.requests), 0) / total) : 0;
      const matrixHtml = total ? '<div class="matrix-row head"><span>数据源（外部）</span>' +
        '<span class="num">请求 / 失败</span><span class="num">平均耗时</span>' +
        '<span class="num">返回条数</span><span class="num">限流 / 缓存</span></div>' +
        matrix.map((m) => '<div class="matrix-row ' + (num(m.failures) ? 'fail' : '') + '">' +
          '<span>' + sourceChip(m.provider, m.provider || '未标注') + '</span>' +
          '<span class="num">' + num(m.requests) + ' / ' + num(m.failures) + '</span>' +
          '<span class="num">' + fmtDuration(m.avg_ms) + '</span>' +
          '<span class="num">' + num(m.results) + '</span>' +
          '<span class="num">' + num(m.rate_limited) + ' / ' + num(m.cached) + '</span></div>').join('')
        : '<p class="empty muted">暂无搜索事件（区间内没有真实调用记录）</p>';
      $('#search-body').innerHTML =
        '<div class="grid kpis">' +
        kpi('检索请求', total, '统计区间：' + d.range.label) +
        kpi('失败 / 不可达', failures, '按数据源逐项统计，不掩盖逐源失败') +
        kpi('平均耗时', fmtDuration(avg), '按请求数加权') +
        kpi('API 限流', limited, '429 / Retry-After 触发次数') +
        kpi('缓存命中', cached, '仅统计真实缓存命中事件') +
        '</div>' +
        '<div class="grid cols-2" style="margin-top:14px">' +
        card('来源矩阵', matrixHtml, '数据来源：审计库 search_events（外部来源调用结果）') +
        card('覆盖范围与上游状态', kv([
          ['覆盖范围', esc(d.coverage_note)],
          ['上游连通性', '<span class="source-chip offline">' + esc(d.upstream.status) + '</span>'],
          ['探测说明', esc(d.upstream.note)],
          ['数据来源', esc(d.source_note)],
          ['采样说明', '仅显示区间内最近 ' + d.total + ' 条事件；未落库的请求不在其中']
        ])) +
        '</div>' +
        '<div style="margin-top:14px">' + card('最近检索事件', table([
          { key: 'ts', label: '时间', render: (r) => fmtTime(r.ts) },
          { key: 'tool_name', label: '工具' },
          { key: 'provider', label: '来源', render: (r) => sourceChip(r.provider, r.provider) },
          { key: 'keyword', label: '脱敏关键词', render: (r) => tr(r.keyword || r.query_hash, 40) },
          { key: 'status', label: '状态', render: (r) => dlBadge(r.status) },
          { key: 'duration_ms', label: '耗时', render: (r) => fmtDuration(r.duration_ms) },
          { key: 'result_count', label: '条数' },
          { key: 'rate_limited', label: '限流', render: (r) => num(r.rate_limited) ? '是' : '—' },
          { key: 'error', label: '失败原因', render: (r) => tr(r.error, 60) }
        ], d.rows, { empty: '区间内暂无检索事件' }), '逐源失败原因独立展示，不以「整体成功」掩盖') + '</div>' +
        scopeBar([
          ['数据来自', sourceChip('external', '外部免费数据源') + ' ' + sourceChip('container', '容器内审计库')],
          ['更新时间', esc(new Date().toLocaleString('zh-CN', { hour12: false }))],
          ['限额提示', '免费 API 有额度与限速，SearXNG 软件免费不等于上游可达']
        ]);
    };
    ['search-provider', 'search-keyword', 'search-status', 'search-range'].forEach((id) => {
      const el = $('#' + id);
      if (el) el.addEventListener('change', () => load().catch(showError));
    });
    $('#search-refresh').addEventListener('click', () => load().catch(showError));
    await load();
  }

  // ------------------------------------------------------------------ 论文库与下载
  async function renderPapers(alive) {
    rootEl.innerHTML = '<div class="card"><h3>论文库与下载' +
      '<span class="sub">合法开放获取全文 · 任务创建成功 ≠ 文件最终校验成功</span></h3>' +
      '<div class="controls">' +
      '<input id="p-keyword" type="search" placeholder="标题 / 作者关键词">' +
      '<input id="p-id" placeholder="DOI / arXiv ID / PMID">' +
      '<select id="p-oa"><option value="">全部 OA 状态</option>' +
      ['oa_pdf', 'oa_html', 'metadata_only', 'subscription_required'].map((v) =>
        '<option value="' + v + '">' + v + '</option>').join('') + '</select>' +
      '<input id="p-year" type="number" min="1900" max="2100" placeholder="起始年份" style="width:110px">' +
      '<input id="p-project" placeholder="项目">' +
      '<input id="p-tag" placeholder="标签">' +
      '<button class="btn primary" id="p-apply">查询</button>' +
      '<button class="btn" data-p-export="bibtex">BibTeX</button>' +
      '<button class="btn" data-p-export="ris">RIS</button>' +
      '<button class="btn" data-p-export="csv">CSV</button>' +
      '<button class="btn" data-p-export="json">JSON</button></div></div>' +
      '<div id="papers-body" style="margin-top:14px"><div class="loading">正在加载…</div></div>';
    const params = () => {
      const p = new URLSearchParams();
      [['p-keyword', 'keyword'], ['p-id', 'identifier'], ['p-oa', 'oa_status'],
       ['p-year', 'from_year'], ['p-project', 'project'], ['p-tag', 'tag']].forEach(([id, key]) => {
        const el = $('#' + id); if (el && el.value.trim()) p.set(key, el.value.trim());
      });
      return p;
    };
    const load = async () => {
      const q = params().toString();
      const [lib, dl, tr] = await Promise.all([
        api('/api/papers?' + q), api('/api/papers/downloads?limit=100'),
        api('/api/transfers?limit=50').catch(() => ({ rows: [], counts: {} }))
      ]);
      if (!alive()) return;
      const st = lib.stats || {};
      const cards = (lib.rows || []).map((r) => '<article class="paper-card fade-in">' +
        '<h4>' + esc(r.title || '(无标题)') + '</h4>' +
        '<div class="meta">' + esc((r.authors || []).join(', ') || '作者未标注') + ' · ' +
        esc(r.publication_year || '年份未知') + ' · ' + esc(r.venue || '来源未标注') + '</div>' +
        '<div class="chips">' + dlBadge(r.oa_status) +
        '<span class="badge' + (r.sha256_verified ? ' ok' : '') + '">' +
        (r.sha256_verified ? 'SHA-256 已校验' : '尚未校验') + '</span>' +
        sourceChip('external', (r.sources || [])[0] || 'metadata') + '</div>' +
        '<div class="meta mono">' + esc(r.doi || r.arxiv_id || r.paper_id) + ' · ' +
        fmtBytes(r.file_size) + '</div>' +
        '<div class="actions">' +
        (r.file_path ? '<button class="btn" data-paper-file="' + esc(r.paper_id) + '">PDF 预览 / 下载</button>' : '<span class="muted">无本地 PDF</span>') +
        (r.landing_url ? '<a class="btn ghost" href="' + esc(r.landing_url) + '" target="_blank" rel="noopener">原始页面</a>' : '') +
        '</div></article>').join('');
      const taskRows = (dl.rows || []).slice(0, 40);
      $('#papers-body').innerHTML =
        '<div class="grid kpis">' +
        kpi('文献库条目', st.total, '本地索引持久化于审计库') +
        kpi('已下载并校验', st.verified, '校验通过率 ' + num(st.verified_rate) + '%') +
        kpi('占用空间', fmtBytes(st.bytes), '容器内论文目录') +
        kpi('下载任务', dl.total, Object.keys(dl.counts || {}).map((k) => k + ':' + dl.counts[k]).join(' · ') || '暂无') +
        '</div>' +
        '<div style="margin-top:14px">' + card('论文卡片',
          cards ? '<div class="paper-grid">' + cards + '</div>' : '<p class="empty muted">暂无论文条目（可先用学术检索与 OA 下载工具入库）</p>',
          '共 ' + lib.total + ' 条 · 数据来源：本地文献库') + '</div>' +
        '<div class="grid cols-2" style="margin-top:14px">' +
        card('下载任务面板', taskRows.length ? taskRows.map((r) =>
          '<div style="padding:10px 0;border-bottom:1px solid var(--line)">' +
          '<div style="display:flex;justify-content:space-between;gap:10px;flex-wrap:wrap">' +
          '<b>' + esc(r.title || r.paper_id) + '</b>' + dlBadge(r.status) + '</div>' +
          '<div class="meta">' + esc(r.ts) + ' · ' + sourceChip('external', r.source || 'oa') +
          ' · 请求 ' + esc(r.request_id || '—') + '</div>' +
          '<div style="margin:7px 0">' + progressBar(r.status, r.bytes, r.total_bytes) + '</div>' +
          flowStrip(r.status) +
          (r.error_message ? '<div class="hint">失败原因：' + esc(r.error_message) + '</div>' : '') +
          '</div>').join('') : '<p class="empty muted">暂无下载任务</p>',
          esc(dl.note)) +
        card('任务状态口径', kv([
          ['created', '任务已创建，尚未拿到文件或尚未校验'],
          ['downloading / verifying', '流式下载中 / 校验 %PDF- 头、EOF、SHA-256 中'],
          ['success', '文件最终校验通过并原子改名落盘'],
          ['duplicate', '同 SHA-256 已存在，未重复下载'],
          ['failed / no_oa', '明确失败原因，绝不假报下载成功'],
          ['数据来源', esc(dl.source_note)]
        ])) +
        '</div>' +
        '<div style="margin-top:14px">' + card('通用文件传输', table([
          { key: 'ts', label: '时间', render: (r) => fmtTime(r.ts) },
          { key: 'direction', label: '方向', render: (r) => sourceChip('container', r.direction) },
          { key: 'path', label: '路径', render: (r) => tr(r.path, 50) },
          { key: 'status', label: '状态', render: (r) => dlBadge(r.status) },
          { key: 'bytes', label: '大小', render: (r) => fmtBytes(r.bytes) },
          { key: 'sha256', label: 'SHA-256', render: (r) => tr(r.sha256, 16) },
          { key: 'op', label: '操作', render: (r) => r.status === 'success'
            ? '<button class="btn" data-transfer-link="' + esc(r.path) + '">签名下载</button>' : '—' }
        ], (tr.rows || []).slice(0, 20), { empty: '暂无文件传输任务' }),
        esc(tr.note || 'created 仅表示任务创建，success 表示最终校验通过')) + '</div>' +
        scopeBar([
          ['数据来自', sourceChip('container', '容器内论文目录与审计库') + ' ' + sourceChip('external', '合法 OA 来源')],
          ['索引持久化', 'paper_library / paper_downloads（共享审计数据卷）'],
          ['OCR', esc(lib.ocr_note)],
          ['更新时间', esc(new Date().toLocaleString('zh-CN', { hour12: false }))]
        ]);
    };
    $('#p-apply').addEventListener('click', () => load().catch(showError));
    $$('[data-p-export]').forEach((btn) => btn.addEventListener('click', () => {
      const p = params(); p.set('fmt', btn.dataset.pExport);
      window.location.href = '/api/papers/export?' + p.toString();
    }));
    await load();
  }

  // ------------------------------------------------------------------ 路由
  const VIEWS = {
    dashboard: { title: '首页仪表盘', sub: '调用总量、成功率、趋势与服务器状态', render: renderDashboard },
    realtime: { title: '实时调用监控', sub: 'SSE · 只推送真实调用与任务事件', render: renderRealtime },
    calls: { title: '完整调用记录', sub: '组合筛选、排序、分页、详情与导出', render: renderCalls },
    call: { title: '调用详情', sub: '参数、结果、时间线与关联记录', render: renderCallDetail },
    search: { title: '搜索监控', sub: '垂直学术源与可选通用搜索的逐源成败、耗时与限额', render: renderSearch },
    papers: { title: '论文库与下载', sub: '合法 OA 全文、按状态区分的下载任务与文献导出', render: renderPapers },
    git: { title: 'Git 操作审计', sub: 'commit / push / status 等命令推导', render: renderGit },
    experiments: { title: '科研实验监控', sub: '后台任务生命周期与日志', render: renderExperiments },
    server: { title: '服务器状态', sub: '容器指标来源明确区分', render: renderServer },
    clients: { title: '客户端与会话', sub: '令牌指纹、管理员会话与强制下线', render: renderClients },
    config: { title: '服务配置', sub: '只读运行配置 + 界面设置', render: renderConfig },
    exports: { title: '日志导出', sub: '后台任务与签名下载链接', render: renderExports }
  };

  let cleanup = () => {};
  let renderToken = 0;
  function render() {
    cleanup(); cleanup = () => {};
    const token = ++renderToken;
    const alive = () => token === renderToken;
    const parts = (location.hash || '#dashboard').replace(/^#/, '').split('/');
    const name = parts[0] || 'dashboard';
    const view = VIEWS[name] || VIEWS.dashboard;
    $$('nav a[data-view]').forEach((a) => a.classList.toggle('active', a.dataset.view === name));
    $('#page-title').textContent = view.title;
    $('#page-sub').textContent = view.sub;
    rootEl.innerHTML = '<div class="loading">正在加载…</div>';
    Promise.resolve(view.render(alive, decodeURIComponent(parts.slice(1).join('/'))))
      .catch((err) => { if (alive()) showError(err); });
  }

  // ------------------------------------------------------------------ 全局交互
  rootEl.addEventListener('click', async (ev) => {
    const cp = ev.target.closest('[data-copy]');
    if (cp) {
      const target = $(cp.dataset.copy);
      if (target) copy(target.textContent);
      ev.stopPropagation();
      return;
    }
    const logBtn = ev.target.closest('[data-log]');
    if (logBtn) {
      const taskId = logBtn.dataset.log;
      try {
        const r = await api('/api/experiments/' + encodeURIComponent(taskId) + '/logs?tail=300');
        const holder = $('#exp-log');
        if (holder) holder.innerHTML = '<div style="margin-top:14px">' +
          card('任务日志 · ' + taskId, '<pre class="json">' + esc(r.log || '（空）') + '</pre>') + '</div>';
      } catch (e) { toast('读取日志失败：' + e.message); }
      return;
    }
    const revoke = ev.target.closest('[data-revoke]');
    if (revoke) {
      try {
        await api('/api/sessions/' + encodeURIComponent(revoke.dataset.revoke), { method: 'DELETE' });
        toast('会话已撤销'); render();
      } catch (e) { toast('撤销失败：' + e.message); }
      return;
    }
    const paperFile = ev.target.closest('[data-paper-file]');
    if (paperFile) {
      try {
        const r = await api('/api/papers/link?paper_id=' +
          encodeURIComponent(paperFile.dataset.paperFile));
        if (!r.ok) { toast(r.error || '无法生成下载链接'); return; }
        window.open(r.url, '_blank', 'noopener');
      } catch (e) { toast('获取论文链接失败：' + e.message); }
      return;
    }
    const transferLink = ev.target.closest('[data-transfer-link]');
    if (transferLink) {
      try {
        const r = await api('/api/transfers/link?path=' +
          encodeURIComponent(transferLink.dataset.transferLink));
        if (!r.ok) { toast(r.error || '无法生成签名下载链接'); return; }
        window.open(r.url, '_blank', 'noopener');
      } catch (e) { toast('获取传输链接失败：' + e.message); }
      return;
    }
    const link = ev.target.closest('[data-export-link]');
    if (link) {
      try {
        const r = await api('/api/exports/' + encodeURIComponent(link.dataset.exportLink) + '/link');
        window.location.href = r.url;
      } catch (e) { toast('获取下载链接失败：' + e.message); }
    }
  });

  $('#logout-btn').addEventListener('click', async () => {
    try { await post('/api/logout', {}); } catch (e) { /* 仍跳转登录页 */ }
    location.href = '/login';
  });
  $('#user-label').textContent = '管理员：' + (APP.username || '—') + (APP.mfaEnabled ? ' · MFA 已启用' : '');
  const clock = $('#clock');
  const tick = () => { clock.textContent = new Date().toLocaleString('zh-CN', { hour12: false }); };
  tick(); setInterval(tick, 1000);
  window.addEventListener('hashchange', render);
  render();
})();
