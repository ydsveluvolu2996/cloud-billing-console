'use strict';
// Design tokens shared with app.css (blue monochrome). Patterns and dashes carry meaning alongside colour.
const TOKENS = {primary:'#2563EB', hover:'#1D4ED8', deep:'#1E3A8A', soft:'#EFF6FF', selected:'#DBEAFE', border:'#E2E8F0', muted:'#64748B', text2:'#475569', text:'#0F172A', surface:'#FFFFFF'};
const DASHES = ['', '6 4', '2 4', '10 4 2 4', '', '4 4', '12 4', '', '2 4', '8 3'];
const MARKERS = ['circle', 'square', 'diamond', 'triangle', 'circle', 'square', 'diamond', 'triangle', 'circle', 'square'];
function definePattern(svg, ns, id, color, kind) {
  const defs = svg.querySelector('defs') || svg.insertBefore(document.createElementNS(ns, 'defs'), svg.firstChild);
  const pattern = document.createElementNS(ns, 'pattern');
  pattern.setAttribute('id', id); pattern.setAttribute('patternUnits', 'userSpaceOnUse'); pattern.setAttribute('width', '6'); pattern.setAttribute('height', '6');
  const base = document.createElementNS(ns, 'rect'); base.setAttribute('width', '6'); base.setAttribute('height', '6'); base.setAttribute('fill', color); pattern.appendChild(base);
  const mark = document.createElementNS(ns, kind === 'dots' || kind === 'dense' ? 'circle' : 'path');
  mark.setAttribute('stroke', 'rgba(255,255,255,.75)'); mark.setAttribute('stroke-width', '1.2'); mark.setAttribute('fill', 'rgba(255,255,255,.8)');
  if (kind === 'diagonal') mark.setAttribute('d', 'M0 6L6 0');
  else if (kind === 'diagonal-reverse') mark.setAttribute('d', 'M0 0L6 6');
  else if (kind === 'horizontal') mark.setAttribute('d', 'M0 3H6');
  else if (kind === 'cross') mark.setAttribute('d', 'M0 6L6 0M0 0L6 6');
  else if (kind === 'dots') { mark.setAttribute('cx', '3'); mark.setAttribute('cy', '3'); mark.setAttribute('r', '1.1'); mark.removeAttribute('stroke'); }
  else if (kind === 'dense') { mark.setAttribute('cx', '1.5'); mark.setAttribute('cy', '1.5'); mark.setAttribute('r', '1'); mark.removeAttribute('stroke'); }
  if (kind !== 'solid') { if (kind === 'dots' || kind === 'dense') pattern.appendChild(mark); else { mark.setAttribute('fill', 'none'); pattern.appendChild(mark); } }
  defs.appendChild(pattern);
  return `url(#${id})`;
}
function marker(ns, kind, cx, cy, r, color) {
  const node = document.createElementNS(ns, kind === 'circle' ? 'circle' : 'path');
  if (kind === 'circle') { node.setAttribute('cx', cx); node.setAttribute('cy', cy); node.setAttribute('r', r); }
  else if (kind === 'square') node.setAttribute('d', `M${cx-r} ${cy-r}h${2*r}v${2*r}h${-2*r}z`);
  else if (kind === 'diamond') node.setAttribute('d', `M${cx} ${cy-r*1.3}L${cx+r*1.3} ${cy}L${cx} ${cy+r*1.3}L${cx-r*1.3} ${cy}z`);
  else node.setAttribute('d', `M${cx} ${cy-r*1.3}L${cx+r*1.25} ${cy+r}H${cx-r*1.25}z`);
  node.setAttribute('fill', color); node.setAttribute('stroke', TOKENS.surface); node.setAttribute('stroke-width', '1');
  return node;
}
// Pure date and forecast transformations are shared by the controls and regression checks.
function relativeDateRange(range, today) {
  const month = (offset, day=1) => new Date(Date.UTC(today.getUTCFullYear(), today.getUTCMonth()+offset, day));
  let start, end;
  if (range === 'this_month') { start=month(0); end=today; }
  else if (range === 'current_month') { start=month(0); end=month(1,0); }
  else if (range === 'last_month') { start=month(-1); end=month(0,0); }
  else if (range === 'year_to_date') { start=new Date(Date.UTC(today.getUTCFullYear(),0,1)); end=today; }
  else if (/^last_(3|6|12|36)_months$/.test(range)) { start=month(-Number(range.split('_')[1])); end=month(0,0); }
  else if (/^last_(1|7|14)_day(s)?$/.test(range)) { const days=Number(range.split('_')[1]); end=new Date(today); if(days===1)end.setUTCDate(end.getUTCDate()-1); start=new Date(end); start.setUTCDate(start.getUTCDate()-days+1); }
  return start ? {start: start.toISOString().slice(0,10), end: end.toISOString().slice(0,10)} : null;
}
function prepareForecastSeries(payload) {
  if (payload.comparison || !payload.forecast_rows?.length) return [];
    const monthly = payload.granularity ? payload.granularity === 'monthly' : !payload.periods.length || !/^\d{4}-/.test(payload.periods[0]);
    const labelFor = row => monthly ? new Date(`${row.start}T00:00:00Z`).toLocaleDateString('en', {month:'short', year:'numeric', timeZone:'UTC'}) : row.start;
    const originalPeriods = [...payload.periods];
    const periods = [...new Set([...originalPeriods, ...payload.forecast_rows.map(labelFor)])];
    const periodDate = value => /^\d{4}-/.test(value) ? value : new Date(`${value} 1 UTC`).toISOString().slice(0,10);
    periods.sort((a,b) => periodDate(a).localeCompare(periodDate(b)));
    const remap = values => periods.map(period => { const index = originalPeriods.indexOf(period); return index < 0 ? null : values[index]; });
    payload.series.forEach(series => { series.values = remap(series.values); }); payload.totals = remap(payload.totals); payload.periods = periods;
    const forecasts = new Map();
    payload.forecast_rows.forEach(row => {
      const key = row.series_id || row.customer;
      if (!forecasts.has(key)) forecasts.set(key, {label: `${row.customer} — forecast`, color: TOKENS.deep, forecast: true, values: periods.map(() => null), intervals: {}});
      const series = forecasts.get(key), index = periods.indexOf(labelFor(row));
      series.values[index] = row.mean; series.intervals[index] = row;
    });
  return [...forecasts.values()];
}

const navToggle = document.querySelector('[data-nav-toggle]');
if (navToggle) {
  const sidebar = document.getElementById('sidebar');
  const mobile = window.matchMedia('(max-width: 1024px)');
  let collapsed = false;
  try { collapsed = localStorage.getItem('billing.sidebarCollapsed') === 'true'; } catch { /* Storage may be disabled. */ }
  function syncSidebar() {
    const visible = mobile.matches ? document.body.classList.contains('nav-open') : !collapsed;
    document.body.classList.toggle('sidebar-collapsed', !mobile.matches && collapsed);
    sidebar.inert = !visible;
    navToggle.setAttribute('aria-expanded', String(visible));
    navToggle.setAttribute('aria-label', visible ? 'Hide sidebar' : 'Show sidebar');
    navToggle.querySelector('[data-nav-label]').textContent = visible ? 'Hide sidebar' : 'Show sidebar';
    window.dispatchEvent(new Event('resize'));
  }
  navToggle.addEventListener('click', () => {
    if (mobile.matches) document.body.classList.toggle('nav-open');
    else { collapsed = !collapsed; try { localStorage.setItem('billing.sidebarCollapsed', String(collapsed)); } catch { /* Optional preference. */ } }
    syncSidebar();
  });
  document.addEventListener('keydown', event => {
    if (event.key === 'Escape' && document.body.classList.contains('nav-open')) {
      document.body.classList.remove('nav-open'); syncSidebar(); navToggle.focus();
    }
  });
  document.addEventListener('click', event => {
    if (mobile.matches && document.body.classList.contains('nav-open') && !event.target.closest('#sidebar, [data-nav-toggle]')) {
      document.body.classList.remove('nav-open'); syncSidebar();
    }
  });
  mobile.addEventListener('change', () => { document.body.classList.remove('nav-open'); syncSidebar(); });
  syncSidebar();
}
document.querySelectorAll('[data-copy]').forEach(button => button.addEventListener('click', async () => {
  const field = document.getElementById(button.dataset.copy);
  try { await navigator.clipboard.writeText(field.value); button.textContent = 'Link copied'; }
  catch { field.focus(); field.select(); button.textContent = 'Select and copy the link'; }
}));

// Preferences are scoped to the signed-in report workspace.
const reportScope = document.getElementById('report-form')?.dataset.scope || 'default';
const preferenceKey = kind => `billing.explorer.${reportScope}.${kind}`;
const readPreference = (kind, fallback) => { try { return JSON.parse(localStorage.getItem(preferenceKey(kind))) ?? fallback; } catch { return fallback; } };
const writePreference = (kind, value) => { try { localStorage.setItem(preferenceKey(kind), JSON.stringify(value)); } catch { /* Optional preference. */ } };
const dialogOpeners = new WeakMap();
function openReportDialog(dialog, opener) {
  if (!dialog) return;
  dialogOpeners.set(dialog, opener || document.activeElement);
  dialog.dispatchEvent(new Event('dialogopen'));
  dialog.showModal();
}
function closeReportDialog(dialog) { dialog.close(); dialogOpeners.get(dialog)?.focus(); }
document.querySelectorAll('[data-open-dialog]').forEach(button => button.addEventListener('click', () => openReportDialog(document.getElementById(button.dataset.openDialog), button)));
document.querySelectorAll('[data-close-dialog]').forEach(button => button.addEventListener('click', () => closeReportDialog(button.closest('dialog'))));
document.querySelectorAll('dialog.report-dialog').forEach(dialog => dialog.addEventListener('cancel', () => dialogOpeners.get(dialog)?.focus()));
document.querySelectorAll('[data-scroll-prompts]').forEach(button => button.addEventListener('click', () => {
  const list = document.querySelector('.quick-report-list');
  list?.scrollBy({left: Number(button.dataset.scrollPrompts) * list.clientWidth * .8, behavior: matchMedia('(prefers-reduced-motion: reduce)').matches ? 'instant' : 'smooth'});
}));
const paginatedTables = new Map();
function renderPaginatedTable(table, query) {
  const group = table.dataset.paginated;
  let state = paginatedTables.get(group);
  if (!state) {
    const saved = readPreference(`table.${group}`, {});
    state = {page: 1, size: [10,20,50,100,200].includes(saved.size) ? saved.size : 20, wrap: saved.wrap === true};
    paginatedTables.set(group, state);
  }
  const rows = [...table.querySelectorAll('tbody [data-data-row]')];
  const matches = rows.filter(row => row.textContent.toLocaleLowerCase().includes(query));
  const pages = Math.max(1, Math.ceil(matches.length / state.size));
  state.page = Math.min(pages, Math.max(1, state.page));
  const from = (state.page - 1) * state.size;
  const displayed = new Set(matches.slice(from, from + state.size));
  rows.forEach(row => { row.hidden = !displayed.has(row); });
  table.classList.toggle('wrap-lines', state.wrap);
  const section = table.closest('[data-search-container]');
  section.querySelector('.search-empty').hidden = matches.length > 0 || rows.length === 0;
  section.querySelector('[data-search-count]').textContent = matches.length ? `${from + 1}–${Math.min(from + state.size, matches.length)} of ${matches.length} rows${query ? ` (${rows.length} total)` : ''}` : `0 of ${rows.length} rows`;
  const pagination = section.querySelector('[data-pagination]');
  pagination.replaceChildren();
  const button = (label, page, disabled, current = false) => {
    const node = document.createElement('button'); node.type = 'button'; node.className = 'pagination-button';
    node.textContent = label; node.disabled = disabled;
    node.setAttribute('aria-label', /^\d+$/.test(label) ? `Page ${label}` : `${label} page`);
    if (current) node.setAttribute('aria-current', 'page');
    node.addEventListener('click', () => { state.page = page; renderPaginatedTable(table, query); pagination.querySelector('[aria-current]')?.focus(); });
    pagination.append(node);
  };
  button('Previous', state.page - 1, state.page === 1);
  const start = Math.max(1, Math.min(state.page - 2, pages - 4));
  const pageNumbers = [...new Set([1, ...Array.from({length: Math.min(5, pages)}, (_, i) => start + i), pages])];
  let previous = 0;
  pageNumbers.forEach(page => {
    if (previous && page > previous + 1) { const gap = document.createElement('span'); gap.textContent = '…'; pagination.append(gap); }
    button(String(page), page, false, page === state.page); previous = page;
  });
  button('Next', state.page + 1, state.page === pages);
}
let preferenceTable;
document.querySelectorAll('[data-table-preferences]').forEach(button => button.addEventListener('click', () => {
  preferenceTable = button.dataset.tablePreferences;
  const state = paginatedTables.get(preferenceTable);
  const dialog = document.getElementById('table-preferences-dialog');
  dialog.querySelector(`[name="table-page-size"][value="${state.size}"]`).checked = true;
  dialog.querySelector('[data-wrap-lines]').checked = state.wrap;
  openReportDialog(dialog, button);
}));
document.querySelector('[data-confirm-table-preferences]')?.addEventListener('click', () => {
  const dialog = document.getElementById('table-preferences-dialog'), state = paginatedTables.get(preferenceTable);
  state.size = Number(dialog.querySelector('[name="table-page-size"]:checked').value);
  state.wrap = dialog.querySelector('[data-wrap-lines]').checked; state.page = 1;
  writePreference(`table.${preferenceTable}`, {size: state.size, wrap: state.wrap});
  searchTables(preferenceTable); closeReportDialog(dialog);
});

// All tables remain available when JavaScript is disabled.
const tabs = [...document.querySelectorAll('[data-tab]')];
const panels = [...document.querySelectorAll('[data-table-panel]')];
function searchTables(group) {
  const input = document.querySelector(`[data-search-group="${group}"]`);
  const query = (input?.value || '').toLocaleLowerCase().trim();
  const paginated = document.querySelector(`table[data-paginated="${group}"]`);
  if (paginated) { renderPaginatedTable(paginated, query); return; }
  let total = 0, visible = 0;
  document.querySelectorAll(`table[data-searchable="${group}"]`).forEach(table => {
    if (table.closest('[data-table-panel]')?.hidden) return;
    table.querySelectorAll('tbody [data-data-row]').forEach(row => {
      row.hidden = !row.textContent.toLocaleLowerCase().includes(query);
      total++; if (!row.hidden) visible++;
    });
  });
  const section = document.querySelector(`[data-search-container="${group}"]`) || document.getElementById(group);
  if (section) {
    section.querySelector('.search-empty').hidden = !query || visible > 0 || total === 0;
    section.querySelector('[data-search-count]').textContent = total ? `${visible} of ${total} rows${query ? ' match your search' : ''}` : '';
  }
}
function selectTab(id, focus = false) {
  if (!tabs.some(tab => tab.dataset.tab === id)) return;
  tabs.forEach(tab => {
    const active = tab.dataset.tab === id;
    tab.classList.toggle('selected', active);
    tab.setAttribute('aria-selected', String(active));
    tab.tabIndex = active ? 0 : -1;
    if (focus && active) tab.focus();
  });
  panels.forEach(panel => { panel.hidden = panel.id !== id; });
  searchTables('breakdown');
}
if (tabs.length) {
  document.querySelector('.tabs').setAttribute('role', 'tablist');
  tabs.forEach((tab, index) => {
    tab.setAttribute('role', 'tab');
    tab.id = `tab-${tab.dataset.tab}`;
    tab.setAttribute('aria-controls', tab.dataset.tab);
    const panel = document.getElementById(tab.dataset.tab);
    panel.setAttribute('role', 'tabpanel');
    panel.setAttribute('aria-labelledby', tab.id);
    tab.addEventListener('click', event => { event.preventDefault(); selectTab(tab.dataset.tab); history.replaceState(null, '', `#${tab.dataset.tab}`); });
    tab.addEventListener('keydown', event => {
      let next;
      if (event.key === 'ArrowRight') next = (index + 1) % tabs.length;
      if (event.key === 'ArrowLeft') next = (index + tabs.length - 1) % tabs.length;
      if (event.key === 'Home') next = 0;
      if (event.key === 'End') next = tabs.length - 1;
      if (next !== undefined) { event.preventDefault(); selectTab(tabs[next].dataset.tab, true); }
    });
  });
  selectTab(tabs.some(t => t.dataset.tab === location.hash.slice(1)) ? location.hash.slice(1) : tabs[0].dataset.tab);
  document.querySelectorAll('[data-open-tab]').forEach(link => link.addEventListener('click', () => selectTab(link.dataset.openTab)));
  window.addEventListener('hashchange', () => selectTab(location.hash.slice(1)));
}
document.querySelectorAll('[data-search-group]').forEach(input => {
  input.addEventListener('input', () => { const state = paginatedTables.get(input.dataset.searchGroup); if (state) state.page = 1; searchTables(input.dataset.searchGroup); });
  searchTables(input.dataset.searchGroup);
});
document.querySelectorAll('[data-sort]').forEach(button => button.addEventListener('click', () => {
  const th = button.closest('th'), table = th.closest('table'), body = table.tBodies[0], index = th.cellIndex;
  const ascending = th.getAttribute('aria-sort') !== 'ascending';
  const numeric = button.dataset.sort === 'number';
  const rows = [...body.querySelectorAll('[data-data-row]')];
  const value = row => {
    const cell = row.cells[index], raw = cell.dataset.sortValue ?? cell.textContent.trim();
    return numeric ? (raw === '' || raw === '—' ? null : Number(raw.replaceAll(',', ''))) : raw;
  };
  rows.sort((a, b) => {
    const x = value(a), y = value(b);
    if (x === null) return y === null ? 0 : 1;
    if (y === null) return -1;
    return (numeric ? x - y : x.localeCompare(y, undefined, {numeric: true, sensitivity: 'base'})) * (ascending ? 1 : -1);
  });
  table.querySelectorAll('th[aria-sort]').forEach(h => h.removeAttribute('aria-sort'));
  table.querySelectorAll('[data-sort] span').forEach(s => { s.textContent = '↕'; });
  th.setAttribute('aria-sort', ascending ? 'ascending' : 'descending');
  button.querySelector('span').textContent = ascending ? '↑' : '↓';
  rows.forEach(row => body.appendChild(row));
  if (table.dataset.paginated) { paginatedTables.get(table.dataset.paginated).page = 1; searchTables(table.dataset.paginated); }
}));

const host = document.getElementById('cost-chart');
if (host) {
  const data = JSON.parse(document.getElementById('chart-data').textContent);
  const readout = document.getElementById('chart-readout');
  const currency = host.dataset.currency;
  const money = value => value === null ? 'No imported rows' : `${Intl.NumberFormat('en', {minimumFractionDigits: 2, maximumFractionDigits: Math.abs(value) > 0 && Math.abs(value) < 0.01 ? 5 : 2}).format(value)} ${currency}`;
  const label = text => /^\d{4}-/.test(text) ? new Date(`${text}T00:00:00Z`).toLocaleDateString('en-GB', {day:'numeric', month:'short', year:'numeric', timeZone:'UTC'}) : text;
  const draw = () => {
    const w = Math.max(host.clientWidth, 220), h = host.clientHeight, pad = {t:12, r:12, b:38, l:46};
    const plotW = w-pad.l-pad.r, plotH = h-pad.t-pad.b;
    const values = data.map(d => d.amount).filter(v => v !== null);
    const rawMax = Math.max(0, ...values), rawMin = Math.min(0, ...values);
    const rawStep = (rawMax - rawMin || 1) / 4;
    const magnitude = 10 ** Math.floor(Math.log10(rawStep));
    const tick = [1, 2, 2.5, 5, 10].find(v => v * magnitude >= rawStep) * magnitude;
    const max = Math.ceil(rawMax / tick) * tick || (rawMin < 0 ? 0 : tick * 4);
    const min = Math.floor(rawMin / tick) * tick;
    const y = v => pad.t+(max-v)/(max-min)*plotH;
    const ns = 'http://www.w3.org/2000/svg';
    const svg = document.createElementNS(ns, 'svg');
    svg.setAttribute('viewBox', `0 0 ${w} ${h}`);
    svg.setAttribute('role', 'group');
    svg.setAttribute('aria-label', 'Spending by period. Use left and right arrow keys to explore.');
    function el(tag, attrs, text) {
      const node = document.createElementNS(ns, tag);
      for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
      if (text !== undefined) node.textContent = text;
      svg.appendChild(node); return node;
    }
    for (let i=0; i<=Math.round((max-min)/tick); i++) {
      const v = min+tick*i;
      el('line', {x1:pad.l, x2:w-pad.r, y1:y(v), y2:y(v), stroke:TOKENS.border, 'stroke-dasharray':'3 4'});
      el('text', {x:pad.l-9, y:y(v)+3, 'text-anchor':'end', fill:TOKENS.muted, 'font-size':11}, Intl.NumberFormat('en',{notation:'compact',maximumFractionDigits:1}).format(v));
    }
    el('line', {x1:pad.l, x2:w-pad.r, y1:y(0), y2:y(0), stroke:TOKENS.muted});
    const creditFill = definePattern(svg, ns, 'credit-hatch', TOKENS.deep, 'diagonal');
    const fillFor = (d, active) => d.amount === null ? (active ? TOKENS.muted : TOKENS.border) : d.amount < 0 ? creditFill : active ? TOKENS.hover : TOKENS.primary;
    const step = plotW / Math.max(1,data.length), bw = Math.max(1, Math.min(42,step*.56));
    const labelEvery = Math.max(1,Math.ceil(data.length/Math.max(2,Math.floor(plotW/68))));
    const targets = [];
    data.forEach((d,i) => {
      const x = pad.l+i*step+step/2;
      const bar = el('rect', {x:x-bw/2, y:d.amount === null ? y(0)-2 : Math.min(y(d.amount),y(0)), width:bw,
        height:d.amount === null ? 2 : Math.max(2,Math.abs(y(d.amount)-y(0))), rx:Math.min(3,bw/3),
        fill:fillFor(d, false), class:'chart-bar',
        tabindex:i===0?0:-1, role:'img', 'aria-label':`${label(d.label)}: ${money(d.amount)}${d.amount !== null && d.amount < 0 ? ' (credit)' : ''}`});
      const title = document.createElementNS(ns,'title');
      title.textContent = `${label(d.label)}: ${money(d.amount)}`; bar.appendChild(title);
      const show = () => { readout.textContent = `${label(d.label)} · ${money(d.amount)}${d.amount !== null && d.amount < 0 ? ' (credit)' : ''}`; bar.setAttribute('fill', fillFor(d, true)); bar.setAttribute('stroke', TOKENS.deep); };
      const hide = () => { bar.setAttribute('fill', fillFor(d, false)); bar.removeAttribute('stroke'); };
      bar.addEventListener('pointerenter',show); bar.addEventListener('pointerleave',hide);
      bar.addEventListener('focus',show); bar.addEventListener('blur',hide);
      bar.addEventListener('keydown',event => {
        let next;
        if(event.key==='ArrowRight') next=Math.min(data.length-1,i+1);
        if(event.key==='ArrowLeft') next=Math.max(0,i-1);
        if(event.key==='Home') next=0;
        if(event.key==='End') next=data.length-1;
        if(next!==undefined){event.preventDefault();bar.tabIndex=-1;targets[next].tabIndex=0;targets[next].focus();}
      });
      targets.push(bar);
      if (i%labelEvery===0) el('text', {x, y:h-11, 'text-anchor':'middle', fill:TOKENS.muted, 'font-size':11}, /^\d{4}-/.test(d.label) ? new Date(`${d.label}T00:00:00Z`).toLocaleDateString('en-GB',{day:'numeric',month:'short',timeZone:'UTC'}) : d.label.replace(' 20', ' ’'));
    });
    host.replaceChildren(svg);
  };
  new ResizeObserver(draw).observe(host); draw();
}

// The dock can resize with a pointer or keyboard and becomes a dismissible mobile drawer.
const parameters = document.getElementById('report-parameters');
if (parameters) {
  const toggles = [...document.querySelectorAll('[data-toggle-parameters]')];
  const opener = toggles.find(button => !parameters.contains(button));
  const mobile = matchMedia('(max-width: 1280px)');
  const resizer = parameters.querySelector('.parameter-resizer');
  const setWidth = width => {
    const value = Math.max(280, Math.min(560, innerWidth * .55, width));
    document.documentElement.style.setProperty('--params-w', `${value}px`);
    resizer.setAttribute('aria-valuenow', String(Math.round(value))); return value;
  };
  setWidth(Number(readPreference('panel-width', 350)) || 350);
  const sync = () => {
    const visible = getComputedStyle(parameters).display !== 'none';
    toggles.forEach(button => button.setAttribute('aria-expanded', String(visible)));
    parameters.inert = !visible;
    if (mobile.matches && visible) { parameters.setAttribute('role', 'dialog'); parameters.setAttribute('aria-modal', 'true'); }
    else { parameters.removeAttribute('role'); parameters.removeAttribute('aria-modal'); }
    window.dispatchEvent(new Event('resize'));
  };
  const close = () => { document.body.classList.add('parameters-hidden'); document.body.classList.remove('parameters-open'); sync(); opener?.focus(); };
  toggles.forEach(button => button.addEventListener('click', () => {
    if (getComputedStyle(parameters).display !== 'none') close();
    else { document.body.classList.remove('parameters-hidden'); document.body.classList.add('parameters-open'); sync(); parameters.querySelector('[data-toggle-parameters]').focus(); }
  }));
  document.addEventListener('keydown', event => {
    if (!mobile.matches || getComputedStyle(parameters).display === 'none' || document.querySelector('dialog[open]')) return;
    if (event.key === 'Escape') { event.preventDefault(); close(); }
    if (event.key === 'Tab') {
      const focusable = [...parameters.querySelectorAll('button, input, select, a, summary, [tabindex="0"]')].filter(node => !node.disabled && node.offsetParent !== null && node.type !== 'hidden');
      const first = focusable[0], last = focusable.at(-1);
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
    }
  });
  document.addEventListener('click', event => {
    if (mobile.matches && document.body.classList.contains('parameters-open') && !event.target.closest('#report-parameters, [data-toggle-parameters], dialog')) close();
  });
  mobile.addEventListener('change', () => { document.body.classList.remove('parameters-open'); sync(); });
  let resizing = false;
  resizer.addEventListener('pointerdown', event => { if (mobile.matches) return; resizing = true; resizer.setPointerCapture(event.pointerId); document.body.classList.add('resizing-parameters'); event.preventDefault(); });
  resizer.addEventListener('pointermove', event => { if (resizing) setWidth(innerWidth - event.clientX); });
  const finishResize = () => { if (resizing) { resizing = false; document.body.classList.remove('resizing-parameters'); writePreference('panel-width', Number(resizer.getAttribute('aria-valuenow'))); } };
  resizer.addEventListener('pointerup', finishResize); resizer.addEventListener('pointercancel', finishResize);
  resizer.addEventListener('keydown', event => {
    const change = {ArrowLeft: 20, ArrowRight: -20}[event.key]; if (!change) return;
    event.preventDefault(); writePreference('panel-width', setWidth(Number(resizer.getAttribute('aria-valuenow')) + change));
  });
  sync();
}

for (const explorerHost of document.querySelectorAll('#explorer-chart, #comparison-chart, #usage-chart, #usage-comparison-chart')) {
  const chartId=explorerHost.dataset.chartId || (explorerHost.id==='comparison-chart'?'comparison':'explorer');
  const chartPanel=explorerHost.closest('.panel');
  const payload = JSON.parse(document.getElementById(chartId+'-data').textContent);
  const tooltip = document.getElementById(chartId+'-tooltip');
  // Each AWS customer/query keeps its own expected value and prediction interval.
  // Forecast series are overlays, never members of the actual-cost stack or total.
  if (explorerHost.dataset.granularity) payload.granularity = explorerHost.dataset.granularity;
  prepareForecastSeries(payload).forEach(series => {
    const index = payload.series.length; payload.series.push(series);
    const button = document.createElement('button'); button.type = 'button'; button.dataset.series = String(index); button.setAttribute('aria-pressed', 'true'); button.className = 'forecast-legend';
    const swatch = document.createElementNS('http://www.w3.org/2000/svg', 'svg'); swatch.setAttribute('width','14'); swatch.setAttribute('height','14'); swatch.setAttribute('aria-hidden','true');
    button.append(swatch, document.createTextNode(series.label)); chartPanel.querySelector('.chart-legend').append(button);
  });
  const hidden = new Set();
  const ns = 'http://www.w3.org/2000/svg';
  const amount = value => {
    if (value === null || value === undefined) return '—';
    return new Intl.NumberFormat('en', {style:payload.measure==='usage'?'decimal':'currency', currency:payload.measure==='usage'?undefined:payload.currency, minimumFractionDigits:2,
      maximumFractionDigits:Math.abs(value) > 0 && Math.abs(value) < .01 ? 10 : 2}).format(value) + (payload.measure === 'usage' ? ` ${payload.currency}` : '');
  };
  const periodLabel = text => /^\d{4}-\d{2}-\d{2} \d{2}:/.test(text) ? text + ' UTC' : /^\d{4}-\d{2}-\d{2}$/.test(text) ? new Date(`${text}T00:00:00Z`).toLocaleDateString('en-GB',{day:'numeric',month:'short',year:'numeric',timeZone:'UTC'}) : text;
  const drawExplorer = () => {
    const w = Math.max(220, explorerHost.clientWidth), h = explorerHost.clientHeight;
    const pad = {l:w < 400 ? 42 : 55,r:14,t:14,b:34}, pw = w-pad.l-pad.r, ph = h-pad.t-pad.b;
    const series = payload.series.filter((_,i) => !hidden.has(i));
    const count = payload.periods.length, step = pw / Math.max(1,count);
    const totals = payload.periods.map((_,i) => ({
      positive:series.filter(s=>!s.forecast).reduce((sum,s) => sum+Math.max(0,s.values[i]||0),0),
      negative:series.filter(s=>!s.forecast).reduce((sum,s) => sum+Math.min(0,s.values[i]||0),0)
    }));
    const bounds = series.filter(s=>s.forecast).flatMap(s=>Object.values(s.intervals).flatMap(row=>[row.lower,row.upper,row.mean]));
    const rawMax = Math.max(0,...bounds,...(payload.style === 'stacked' ? totals.map(t=>t.positive) : series.flatMap(s=>s.values.map(v=>v||0))));
    const rawMin = Math.min(0,...bounds,...(payload.style === 'stacked' ? totals.map(t=>t.negative) : series.flatMap(s=>s.values.map(v=>v||0))));
    const roughStep = (rawMax-rawMin || 1)/4, magnitude = 10 ** Math.floor(Math.log10(roughStep));
    const tick = [1,2,2.5,5,10].find(n=>n*magnitude>=roughStep)*magnitude;
    const max = Math.ceil(rawMax/tick)*tick || (rawMin < 0 ? 0 : tick*4);
    // Preserve tiny credits without reserving an entire large negative tick.
    const min = rawMax > 0 && Math.abs(rawMin) < tick*.05 ? rawMin*1.12 : Math.floor(rawMin/tick)*tick;
    const y = value => pad.t+(max-value)/(max-min)*ph;
    const x = i => pad.l+step*(i+.5);
    const svg = document.createElementNS(ns,'svg'); svg.setAttribute('viewBox',`0 0 ${w} ${h}`);
    svg.setAttribute('role','group'); svg.setAttribute('aria-label',`${payload.style} chart. Use left and right arrow keys to inspect periods.`);
    const node = (tag, attrs, text, parent=svg) => {
      const element=document.createElementNS(ns,tag);
      for(const [key,value] of Object.entries(attrs)) element.setAttribute(key,value);
      if(text !== undefined) element.textContent=text;
      parent.appendChild(element); return element;
    };
    for(let value=(Math.ceil(min/tick)*tick || 0);value<=max+tick*.001;value+=tick) {
      node('line',{x1:pad.l,x2:w-pad.r,y1:y(value),y2:y(value),stroke:value===0?TOKENS.muted:TOKENS.border,'stroke-width':value===0?1.5:1});
      node('text',{x:pad.l-9,y:y(value)+3,fill:TOKENS.muted,'text-anchor':'end','font-size':11},Intl.NumberFormat('en',{notation:'compact',maximumFractionDigits:2}).format(value));
    }
    const fills = payload.series.map(s => s.color);
    const indexOf = s => payload.series.indexOf(s);
    const positive=Array(count).fill(0),negative=Array(count).fill(0);
    series.forEach((s,j)=>{
      if(payload.style === 'line' || s.forecast) {
        let path='',connected=false;
        const k=indexOf(s);
        s.values.forEach((v,i)=>{
          if(v===null){connected=false;return;}
          path+=`${connected?' L':' M'}${x(i)} ${y(v)}`; connected=true;
        });
        const line=node('path',{d:path,stroke:s.color,'stroke-width':2,fill:'none','stroke-linejoin':'round'});
        if(s.forecast || DASHES[k % DASHES.length]) line.setAttribute('stroke-dasharray', s.forecast ? '7 4' : DASHES[k % DASHES.length]);
        if(s.forecast) Object.entries(s.intervals).forEach(([index,row])=>{ const cx=x(Number(index)); node('line',{x1:cx,x2:cx,y1:y(row.lower),y2:y(row.upper),stroke:s.color,'stroke-width':2,opacity:.4}); [row.lower,row.upper].forEach(value=>node('line',{x1:cx-4,x2:cx+4,y1:y(value),y2:y(value),stroke:s.color,'stroke-width':2,opacity:.4})); });
        if(count<=80) s.values.forEach((v,i)=>{ if(v!==null) svg.appendChild(marker(ns, MARKERS[k % MARKERS.length], x(i), y(v), 3, s.color)); });
      } else {
        s.values.forEach((v,i)=>{
          if(v===null || v===0) return;
          let from=0,to=v,bw,left;
          if(payload.style === 'stacked') {
            from=v>=0?positive[i]:negative[i];to=from+v;
            if(v>=0)positive[i]=to;else negative[i]=to;
            bw=Math.max(.5,Math.min(75,step*.57));left=x(i)-bw/2;
          } else {
            const groupWidth=Math.min(step*.78,115), actualCount=series.filter(item=>!item.forecast).length;
            bw=Math.max(.4,groupWidth/Math.max(1,actualCount)*.87);
            left=x(i)-groupWidth/2+j*groupWidth/Math.max(1,actualCount);
          }
          const bar=node('rect',{x:left,y:Math.min(y(from),y(to)),width:bw,height:Math.max(.2,Math.abs(y(from)-y(to))),fill:fills[indexOf(s)]});
          if(v<0) bar.setAttribute('stroke', TOKENS.deep), bar.setAttribute('stroke-dasharray', '2 2'), bar.setAttribute('stroke-width', '1');
        });
      }
    });
    const every=Math.max(1,Math.ceil(count/Math.max(2,Math.floor(pw/78))));
    payload.periods.forEach((period,i)=>{
      if(i%every===0)node('text',{x:x(i),y:h-10,'text-anchor':'middle',fill:TOKENS.muted,'font-size':11},payload.comparison&&period.length>13?period.slice(0,12)+'…':/^\d{4}-/.test(period)?periodLabel(period).replace(/ 20\d\d$/,''):period);
    });
    const guide=node('line',{x1:0,x2:0,y1:pad.t,y2:h-pad.b,stroke:TOKENS.primary,'stroke-dasharray':'3 3',visibility:'hidden','pointer-events':'none'});
    const targets=[];
    let overlay;
    const showPeriod=i=>{
      guide.setAttribute('x1',x(i)); guide.setAttribute('x2',x(i)); guide.setAttribute('visibility','visible');
      tooltip.replaceChildren();
      const available=series.filter(s=>s.values[i]!==null);
      const displayedTotal=payload.comparison?payload.totals[i]:payload.totals[i]===null?null:available.filter(s=>!s.forecast).reduce((sum,s)=>sum+s.values[i],0);
      const title=document.createElement('strong'); title.textContent=`${periodLabel(payload.periods[i])} · ${payload.comparison?'Change':hidden.size ? 'Visible actual total' : 'Actual total'} ${amount(displayedTotal)}`;tooltip.appendChild(title);
      available.forEach(s=>{const span=document.createElement('span');span.textContent=`${s.label}: ${amount(s.values[i])}${s.forecast ? ` · ${payload.forecast_interval || 80}% customer interval ${amount(s.intervals[i].lower)}–${amount(s.intervals[i].upper)} · ${s.intervals[i].start} to ${s.intervals[i].end} (exclusive)` : ''}`;tooltip.appendChild(span);});
      if(overlay)overlay.remove();
      const ow=Math.min(310,pw),oh=Math.min(h-pad.t-pad.b,32+available.length*20);
      const ox=Math.max(pad.l,Math.min(w-pad.r-ow,x(i)+(i<count/2?14:-ow-14)));
      overlay=node('g',{transform:`translate(${ox},${pad.t})`,'pointer-events':'none','aria-hidden':'true'});
      node('rect',{x:0,y:0,width:ow,height:oh,rx:8,fill:TOKENS.surface,stroke:TOKENS.border,'stroke-width':1},undefined,overlay);
      node('text',{x:12,y:20,fill:TOKENS.text,'font-size':11,'font-weight':600},periodLabel(payload.periods[i]),overlay);
      node('text',{x:ow-12,y:20,fill:TOKENS.text,'font-size':11,'font-weight':600,'text-anchor':'end'},amount(displayedTotal),overlay);
      available.slice(0,Math.floor((oh-32)/20)).forEach((s,j)=>{
        const text=s.label.length>27?s.label.slice(0,25)+'…':s.label;
        node('rect',{x:12,y:34+j*20,width:9,height:9,rx:2,fill:fills[indexOf(s)],stroke:TOKENS.border,'stroke-width':.5},undefined,overlay);
        node('text',{x:27,y:41+j*20,fill:TOKENS.text2,'font-size':10},text,overlay);
        node('text',{x:ow-12,y:41+j*20,fill:TOKENS.text,'font-size':10,'text-anchor':'end'},amount(s.values[i]),overlay);
      });
    };
    payload.periods.forEach((period,i)=>{
      const hit=node('rect',{x:pad.l+i*step,y:pad.t,width:step,height:ph,fill:'transparent',tabindex:i===0?0:-1,role:'img',
        'aria-label':`${periodLabel(period)}. ${payload.comparison?'Change':'Actual total'} ${amount(payload.totals[i])}. ${series.filter(s=>s.forecast&&s.values[i]!==null).map(s=>`${s.label}: ${amount(s.values[i])}, ${payload.forecast_interval || 80}% customer interval ${amount(s.intervals[i].lower)} to ${amount(s.intervals[i].upper)}.`).join(' ')} Focus to inspect groups.`});
      hit.addEventListener('pointerenter',()=>showPeriod(i));hit.addEventListener('focus',()=>showPeriod(i));
      hit.addEventListener('click',()=>showPeriod(i));
      hit.addEventListener('keydown',event=>{
        let next;
        if(event.key==='ArrowRight')next=Math.min(count-1,i+1);
        if(event.key==='ArrowLeft')next=Math.max(0,i-1);
        if(event.key==='Home')next=0;
        if(event.key==='End')next=count-1;
        if(event.key==='Escape'){overlay?.remove();guide.setAttribute('visibility','hidden');return;}
        if(next!==undefined){event.preventDefault();hit.tabIndex=-1;targets[next].tabIndex=0;targets[next].focus();}
      });
      targets.push(hit);
    });
    svg.addEventListener('pointerleave',()=>{overlay?.remove();guide.setAttribute('visibility','hidden');});
    explorerHost.replaceChildren(svg);
  };
  chartPanel.querySelectorAll('[data-series]').forEach(button=>{
    // Solid swatches match bar fills; line and forecast swatches retain their dash cues.
    const index=Number(button.dataset.series), series=payload.series[index], swatch=button.querySelector('svg');
    if(series && swatch){
      swatch.setAttribute('viewBox','0 0 14 14'); swatch.replaceChildren();
      const rect=document.createElementNS(ns,'rect'); rect.setAttribute('width','14'); rect.setAttribute('height','14'); rect.setAttribute('fill',series.color); swatch.appendChild(rect);
      if(payload.style==='line'||series.forecast){ const line=document.createElementNS(ns,'path'); line.setAttribute('d','M1 7H13'); line.setAttribute('stroke',TOKENS.surface); line.setAttribute('stroke-width','2'); if(series.forecast||DASHES[index % DASHES.length]) line.setAttribute('stroke-dasharray',series.forecast?'4 2':DASHES[index % DASHES.length]); swatch.appendChild(line); }
    }
  });
  chartPanel.querySelectorAll('[data-series]').forEach(button=>button.addEventListener('click',()=>{
    const index=Number(button.dataset.series);
    if(hidden.has(index))hidden.delete(index);else hidden.add(index);
    button.setAttribute('aria-pressed',String(!hidden.has(index)));drawExplorer();
  }));
  new ResizeObserver(drawExplorer).observe(explorerHost);drawExplorer();
}

// Draft selectors commit only on Apply; the outer form always contains the applied values.
const reportForm = document.getElementById('report-form');
if (reportForm) {
  const initial = JSON.parse(document.getElementById('report-parameters-data').textContent);
  const field = name => reportForm.elements.namedItem(name);
  const checkbox = name => reportForm.querySelector(`input[type="checkbox"][name="${name}"]`);
  const dateNames = ['start', 'end', 'historical_end', 'date_range', 'future_range'];
  const iso = date => date.toISOString().slice(0, 10);
  const today = new Date(`${reportForm.dataset.today}T00:00:00Z`);
  const monthDate = (offset, day = 1) => new Date(Date.UTC(today.getUTCFullYear(), today.getUTCMonth() + offset, day));
  const markChanged = () => { changed = true; };
  let changed = false, moreFilters = false, extraId = 0;
  let visibility = readPreference('filters', {});
  const states = new Map(), requests = new WeakMap();
  const additional = document.querySelector('[data-additional-filters]');
  const originalTemplates = new Map(['tag', 'cost_category'].map(key => [key, reportForm.querySelector(`[data-filter="${key}"]`).cloneNode(true)]));
  function formData() {
    const data = new FormData(reportForm);
    for (const key of [...data.keys()]) if (key.startsWith('draft-')) data.delete(key);
    if (field('granularity').disabled) data.set('granularity', field('granularity').value);
    if (field('metric').disabled) data.set('metric', field('metric').value);
    return data;
  }
  reportForm.addEventListener('formdata', event => {
    for (const key of [...event.formData.keys()]) if (key.startsWith('draft-')) event.formData.delete(key);
    if (field('granularity').disabled) event.formData.set('granularity', field('granularity').value);
    if (field('metric').disabled) event.formData.set('metric', field('metric').value);
  });
  const absentFlag = type => type === 'tag' ? 'untagged' : type === 'cost_category' ? 'uncategorized' : '';
  const hasAbsence = state => state.extra ? state.committed.absent : Boolean(absentFlag(state.type) && checkbox(absentFlag(state.type)).checked);
  const applied = state => state.committed.values.length > 0 || hasAbsence(state);
  function syncAdditional() {
    field('keyed_filters').value = JSON.stringify([...states.values()].filter(state => state.extra && applied(state)).map(state => ({type: state.type, ...state.committed})));
  }
  function updateVisibility() {
    let count = 0;
    states.forEach(state => {
      if (applied(state)) count++;
      state.box.hidden = !state.extra && !applied(state) && (visibility[state.type] === false || (state.box.dataset.more === 'true' && !moreFilters));
    });
    document.querySelector('[data-applied-count]').textContent = String(count);
  }
  function invalidateMetadata() {
    reportForm.querySelectorAll('[data-filter-status], [data-key-status]').forEach(status => {
      requests.set(status, (requests.get(status) || 0) + 1);
      status.textContent = 'Parameters changed. Reload available values.';
    });
    states.forEach(state => { state.loaded = false; });
  }
  const setHidden = (parent, name, value) => { const input = document.createElement('input'); input.type = 'hidden'; input.name = name; input.value = value; parent.append(input); };
  function renderCommitted(state) {
    const {box, committed, type} = state;
    if (!state.extra) {
      const container = box.querySelector('.filter-committed'); container.replaceChildren();
      setHidden(container, `${type}_mode`, committed.mode);
      if (box.querySelector('[data-draft-key]')) setHidden(container, `${type}_key`, committed.key);
      committed.values.forEach(value => setHidden(container, type, value));
    }
    const absent = hasAbsence(state);
    box.querySelector('[data-filter-count]').textContent = absent ? `${committed.mode === 'exclude' ? 'Excludes ' : ''}missing key` : committed.values.length ? `${committed.mode === 'exclude' ? 'Excludes ' : ''}${committed.values.length} selected` : 'All';
    const chips = box.querySelector('[data-selected-chips]'); chips.replaceChildren();
    const chip = (label, remove) => {
      const button = document.createElement('button'); button.type = 'button'; button.className = 'filter-chip';
      button.textContent = `${label} ×`; button.setAttribute('aria-label', `Remove ${label}`); button.addEventListener('click', event => { event.preventDefault(); event.stopPropagation(); remove(); }); chips.append(button);
    };
    committed.values.forEach(value => chip(state.labels[value] || (value === '__billing_empty_value__' ? `(Empty value) ${committed.key}` : value), () => {
      committed.values = committed.values.filter(v => v !== value); commit(state); resetDraft(state);
    }));
    if (absent) chip(`No ${type === 'tag' ? 'tag' : 'cost category'} key: ${committed.key}`, () => {
      if (state.extra) committed.absent = false; else checkbox(absentFlag(type)).checked = false;
      commit(state); resetDraft(state);
    });
    syncAdditional(); updateVisibility();
  }
  function commit(state) { renderCommitted(state); markChanged(); invalidateMetadata(); syncAdvanced(); }
  function makeOption(value, selected, label) {
    const node = document.createElement('label'); node.className = 'checkbox-label';
    const input = document.createElement('input'); input.type = 'checkbox'; input.value = value === '' ? '__billing_empty_value__' : value; input.checked = selected;
    const span = document.createElement('span'); span.textContent = label || value || '(Empty value)'; node.append(input, span); return node;
  }
  function updateMatching(state) {
    const term = state.box.querySelector('[data-value-search]').value.toLocaleLowerCase().trim();
    const options = [...state.list.querySelectorAll('label')];
    options.forEach(label => { label.hidden = !label.textContent.toLocaleLowerCase().includes(term); });
    const matches = options.filter(label => !label.hidden), selected = matches.filter(label => label.querySelector('input').checked).length;
    const select = state.box.querySelector('[data-select-matching]');
    select.checked = matches.length > 0 && selected === matches.length;
    select.indeterminate = selected > 0 && selected < matches.length; select.disabled = matches.length === 0;
    state.box.querySelector('[data-matching-count]').textContent = `(${matches.length})`;
  }
  function ensureMissingOption(state, key) {
    if (!['tag','cost_category'].includes(state.type) || !key) return;
    const label = `No ${state.type === 'tag' ? 'tag' : 'cost category'} key: ${key}`;
    const input = state.list.querySelector('input[value="__billing_absent_key__"]');
    if (input) input.nextElementSibling.textContent = label;
    else state.list.prepend(makeOption('__billing_absent_key__', hasAbsence(state), label));
  }
  function resetDraft(state) {
    const {box, committed, list} = state;
    const key = box.querySelector('[data-draft-key]'); if (key) key.value = committed.key;
    box.querySelectorAll('[data-draft-mode]').forEach(input => { input.checked = input.dataset.draftMode === committed.mode; });
    const known = new Set([...list.querySelectorAll('input')].map(input => input.value));
    committed.values.forEach(value => { if (!known.has(value)) list.append(makeOption(value, true, state.labels[value])); });
    ensureMissingOption(state, committed.key);
    list.querySelectorAll('input').forEach(input => { input.checked = input.value === '__billing_absent_key__' ? hasAbsence(state) : committed.values.includes(input.value); });
    box.querySelector('[data-value-search]').value = ''; updateMatching(state);
  }
  async function metadata(type, key, status, render, editedState = null) {
    const generation = (requests.get(status) || 0) + 1; requests.set(status, generation);
    const query = new URLSearchParams(formData()); query.set('dimension', type); query.set('key', key);
    if (editedState) {
      if (editedState.extra) query.set('keyed_filters', JSON.stringify([...states.values()].filter(state=>state.extra && state !== editedState && applied(state)).map(state=>({type:state.type,...state.committed}))));
      else { query.delete(type); if (absentFlag(type)) query.set(absentFlag(type),'0'); }
    }
    let attempts = 0;
    const read = async () => {
      if (requests.get(status) !== generation) return;
      try {
        const response = await fetch(`/explorer/metadata/?${query}`, {credentials: 'same-origin'});
        if (response.redirected) throw new Error('Your session expired. Sign in again to load billing values.');
        let result; try { result = await response.json(); } catch { throw new Error('Could not read billing values. Reload to retry.'); }
        if (!response.ok) throw new Error(result.error || 'Could not load billing values.');
        if (requests.get(status) !== generation) return;
        render(result.values || [], result.labels || {});
        status.textContent = result.errors?.length ? result.errors.join(' ') : result.pending ? 'Loading from AWS; the worker checks within a minute.' : result.values.length ? `${result.values.length} available values. Select all applies to the matching values.` : 'No values returned for these dates and customers.';
        if (result.pending && attempts++ < 24) setTimeout(read, 5000);
        else if (result.pending) status.textContent = 'Still queued. Load all values to check again, or review Sync & activity.';
      } catch (error) { if (requests.get(status) === generation) status.textContent = error.message; }
    };
    status.textContent = 'Loading available values…'; await read();
  }
  function setupFilter(box, extra = false, value) {
    const type = box.dataset.filter, list = box.querySelector('.dimension-values'), status = box.querySelector('[data-filter-status]');
    const container = box.querySelector('.filter-committed');
    const state = {box, type, list, status, extra, loaded: false, labels: {}, committed: value || {
      key: container.querySelector(`[name="${type}_key"]`)?.value || '', mode: container.querySelector(`[name="${type}_mode"]`).value,
      values: [...container.querySelectorAll(`[name="${type}"]`)].map(input => input.value), absent: false
    }};
    if (extra) container.replaceChildren();
    states.set(box, state); resetDraft(state); renderCommitted(state);
    const loadValues = () => {
      const key = box.querySelector('[data-draft-key]')?.value.trim() || '';
      if (['tag', 'cost_category'].includes(type) && !key) { status.textContent = 'Choose a key first.'; return; }
      metadata(type, key, status, (values, labels) => {
        const selected = new Set([...list.querySelectorAll('input:checked')].map(input => input.value));
        list.replaceChildren();
        const all = [...new Set([...selected, ...values.map(value => value === '' ? '__billing_empty_value__' : value)])];
        all.forEach(value => { const display = labels[value] || labels[value === '__billing_empty_value__' ? '' : value]; if (display) state.labels[value] = display; list.append(makeOption(value, selected.has(value), display)); });
        state.loaded = true; ensureMissingOption(state, key); updateMatching(state);
      }, state);
    };
    box.querySelector('[data-load-values]').addEventListener('click', loadValues);
    box.querySelector('[data-value-search]').addEventListener('input', () => updateMatching(state));
    list.addEventListener('change', event => {
      if (event.target.checked) {
        if (event.target.value === '__billing_absent_key__') list.querySelectorAll('input').forEach(input=>{if(input!==event.target)input.checked=false;});
        else { const missing=list.querySelector('input[value="__billing_absent_key__"]'); if(missing)missing.checked=false; }
      }
      updateMatching(state);
    });
    box.querySelector('[data-select-matching]').addEventListener('change', event => {
      const matching = [...list.querySelectorAll('label:not([hidden]) input')], checked = event.target.checked;
      matching.forEach(input => {
        input.checked = checked && (matching.length === 1 || input.value !== '__billing_absent_key__');
        if (input.checked) input.dispatchEvent(new Event('change', {bubbles: true}));
      });
      updateMatching(state);
    });
    box.querySelector('[data-clear-values]').addEventListener('click', () => { list.querySelectorAll('input').forEach(input => { input.checked = false; }); updateMatching(state); });
    box.querySelector('[data-add-value]').addEventListener('click', () => {
      const input = box.querySelector('[data-manual-value]'), value = input.value;
      if (!value) return;
      let selected = [...list.querySelectorAll('input')].find(item => item.value === value);
      if (selected) selected.checked = true;
      else { const option = makeOption(value, true); list.append(option); selected = option.querySelector('input'); }
      selected.dispatchEvent(new Event('change', {bubbles: true}));
      input.value = '';
    });
    box.querySelector('[data-cancel-filter]').addEventListener('click', () => { resetDraft(state); box.open = false; box.querySelector('summary').focus(); });
    box.querySelector('[data-apply-filter]').addEventListener('click', () => {
      const chosen = [...list.querySelectorAll('input:checked')].map(input => input.value);
      const missing = chosen.includes('__billing_absent_key__');
      const values = chosen.filter(value=>value!=='__billing_absent_key__');
      const key = box.querySelector('[data-draft-key]')?.value.trim() || '';
      if (values.length > 100) { status.textContent = 'Choose at most 100 values. Narrow your search or selection.'; return; }
      if (box.querySelector('[data-draft-key]') && !key && (values.length || missing || hasAbsence(state))) { status.textContent = 'Choose a key before applying this filter.'; return; }
      state.committed = {key, values, mode: box.querySelector('[data-draft-mode]:checked').dataset.draftMode, absent: extra && missing};
      if (!extra && absentFlag(type)) checkbox(absentFlag(type)).checked = missing;
      if (values.length && !extra && absentFlag(type)) checkbox(absentFlag(type)).checked = false;
      commit(state); box.open = false; box.querySelector('summary').focus();
    });
    box.querySelector('[data-clear-filter]').addEventListener('click', () => {
      if (extra) { states.delete(box); box.remove(); syncAdditional(); updateVisibility(); markChanged(); invalidateMetadata(); return; }
      state.committed.values = []; state.committed.mode = 'include';
      if (absentFlag(type)) checkbox(absentFlag(type)).checked = false;
      commit(state); resetDraft(state); box.open = false;
    });
    box.querySelector('[data-clear-filter-summary]').addEventListener('click', event => {
      event.preventDefault(); event.stopPropagation();
      box.querySelector('[data-clear-filter]').click();
    });
    box.querySelector('[data-load-keys]')?.addEventListener('click', () => metadata(type, '', status, values => {
      const datalist = box.querySelector('datalist');
      datalist.replaceChildren(...values.map(value => { const option = document.createElement('option'); option.value = value; return option; }));
    }));
    box.querySelector('[data-draft-key]')?.addEventListener('change', () => {
      requests.set(status, (requests.get(status) || 0) + 1); list.replaceChildren(); updateMatching(state); state.loaded = false;
      status.textContent = 'Key changed. Loading values for this key.'; loadValues();
    });
    box.addEventListener('toggle', () => {
      if (box.open) {
        resetDraft(state);
        if (!state.loaded) (box.querySelector('[data-draft-key]') && !state.committed.key ? box.querySelector('[data-load-keys]') : box.querySelector('[data-load-values]')).click();
      } else resetDraft(state);
    });
    box.addEventListener('keydown', event => { if (event.key === 'Escape' && box.open) { event.stopPropagation(); resetDraft(state); box.open = false; box.querySelector('summary').focus(); } });
    return state;
  }
  reportForm.querySelectorAll('[data-filter]').forEach(box => setupFilter(box));
  function addKeyed(type, value, open = true) {
    const box = originalTemplates.get(type).cloneNode(true); box.hidden = false; box.dataset.extra = 'true'; box.open = false;
    const id = `extra-filter-${++extraId}`;
    box.querySelector('.dimension-name').textContent = `Additional ${type === 'tag' ? 'tag' : 'cost category'}`;
    box.querySelectorAll('[data-draft-mode]').forEach(input => { input.name = `draft-${id}-mode`; });
    box.querySelector('datalist').id = `keys-${id}`; box.querySelector('[data-draft-key]').setAttribute('list', `keys-${id}`);
    box.querySelector('[data-clear-filter]').textContent = 'Remove filter';
    const clearSummary = box.querySelector('[data-clear-filter-summary]');
    clearSummary.textContent = 'Remove'; clearSummary.setAttribute('aria-label', `Remove additional ${type === 'tag' ? 'tag' : 'cost category'} filter`);
    box.querySelector('.dimension-values').replaceChildren(); additional.append(box);
    setupFilter(box, true, value || {key: '', values: [], mode: 'include', absent: false});
    if (open) { box.open = true; box.scrollIntoView({block: 'nearest'}); box.querySelector('[data-draft-key]').focus(); }
  }
  let extraFilters = []; try { extraFilters = typeof initial.keyed_filters === 'string' ? JSON.parse(initial.keyed_filters) : initial.keyed_filters || []; } catch { /* Invalid server data is rejected by the contract. */ }
  extraFilters.forEach(value => addKeyed(value.type, {key: value.key, values: value.values, mode: value.mode, absent: value.absent}, false));
  document.querySelectorAll('[data-add-keyed]').forEach(button => button.addEventListener('click', () => {
    if ([...states.values()].filter(state => state.extra).length >= 20) return;
    addKeyed(button.dataset.addKeyed);
  }));
  document.querySelector('[data-more-filters]').addEventListener('click', event => {
    moreFilters = !moreFilters; event.currentTarget.textContent = moreFilters ? 'Show less' : 'More filters'; event.currentTarget.setAttribute('aria-expanded', String(moreFilters)); updateVisibility();
  });
  document.querySelector('[data-clear-filters]').addEventListener('click', () => {
    states.forEach(state => {
      if (state.extra) { state.box.remove(); states.delete(state.box); return; }
      state.committed.values = []; state.committed.mode = 'include'; state.committed.key = '';
      if (absentFlag(state.type)) checkbox(absentFlag(state.type)).checked = false;
      state.list.replaceChildren(); renderCommitted(state); resetDraft(state);
    });
    syncAdditional(); updateVisibility(); invalidateMetadata(); markChanged(); syncAdvanced();
  });
  const preferenceDialog = document.getElementById('filter-preferences-dialog');
  preferenceDialog.addEventListener('dialogopen', () => preferenceDialog.querySelectorAll('[data-visible-filter]').forEach(input => { input.checked = visibility[input.dataset.visibleFilter] !== false; }));
  document.querySelector('[data-save-filter-preferences]').addEventListener('click', () => {
    preferenceDialog.querySelectorAll('[data-visible-filter]').forEach(input => { visibility[input.dataset.visibleFilter] = input.checked; });
    writePreference('filters', visibility); updateVisibility(); closeReportDialog(preferenceDialog);
  });
  // Date selection has its own draft, including historical and forecast presets.
  const dateDialog = document.getElementById('date-range-dialog');
  const draftField = name => dateDialog.querySelector(`[data-date-draft="${name}"]`);
  let dateDraft = {}, calendarMonth = monthDate(0), choosingEnd = false;
  const futureEnd = () => dateDraft.future_range === 'none' ? dateDraft.historical_end : [dateDraft.historical_end, iso(monthDate(Number(dateDraft.future_range.split('_')[1]) + 1, 0))].sort().at(-1);
  function renderCalendar() {
    const host = dateDialog.querySelector('[data-date-calendars]'); host.replaceChildren();
    for (let offset = 0; offset < 2; offset++) {
      const month = new Date(Date.UTC(calendarMonth.getUTCFullYear(), calendarMonth.getUTCMonth() + offset, 1));
      const panel = document.createElement('section'), title = document.createElement('h3');
      title.textContent = month.toLocaleDateString('en', {month: 'long', year: 'numeric', timeZone: 'UTC'}); panel.append(title);
      const grid = document.createElement('div'); grid.className = 'calendar-grid';
      ['Su','Mo','Tu','We','Th','Fr','Sa'].forEach(day => { const name = document.createElement('span'); name.textContent = day; name.className = 'calendar-day-name'; grid.append(name); });
      for (let i = 0; i < month.getUTCDay(); i++) { const blank = document.createElement('span'); blank.setAttribute('aria-hidden', 'true'); grid.append(blank); }
      const last = new Date(Date.UTC(month.getUTCFullYear(), month.getUTCMonth() + 1, 0)).getUTCDate();
      for (let day = 1; day <= last; day++) {
        const value = iso(new Date(Date.UTC(month.getUTCFullYear(), month.getUTCMonth(), day))), button = document.createElement('button');
        button.type = 'button'; button.textContent = String(day); button.dataset.calendarDate = value;
        button.setAttribute('aria-label', `${value}${value === dateDraft.start ? ', start date' : value === dateDraft.historical_end ? ', end date' : ''}`);
        button.setAttribute('aria-pressed', String(value === dateDraft.start || value === dateDraft.historical_end));
        if (value > dateDraft.start && value < dateDraft.historical_end) button.classList.add('in-range');
        button.addEventListener('click', () => {
          if (!choosingEnd || value < dateDraft.start) { dateDraft.start = value; dateDraft.historical_end = value; choosingEnd = true; }
          else { dateDraft.historical_end = value; choosingEnd = false; }
          dateDraft.date_range = 'custom'; renderDates(); dateDialog.querySelector(`[data-calendar-date="${value}"]`)?.focus();
        });
        button.addEventListener('keydown', event => {
          const shift = {ArrowLeft: -1, ArrowRight: 1, ArrowUp: -7, ArrowDown: 7}[event.key];
          if (!shift) return; event.preventDefault();
          const next = new Date(`${value}T00:00:00Z`); next.setUTCDate(next.getUTCDate() + shift); const target = iso(next);
          if (!host.querySelector(`[data-calendar-date="${target}"]`)) { calendarMonth = new Date(Date.UTC(next.getUTCFullYear(), next.getUTCMonth(), 1)); renderCalendar(); }
          host.querySelector(`[data-calendar-date="${target}"]`)?.focus();
        });
        grid.append(button);
      }
      panel.append(grid); host.append(panel);
    }
    dateDialog.querySelector('[data-calendar-instruction]').textContent = choosingEnd ? 'Select end date' : 'Select start date';
  }
  function renderDates() {
    draftField('start').value = dateDraft.start; draftField('end').value = dateDraft.historical_end;
    dateDialog.querySelectorAll('[data-date-preset]').forEach(button => button.setAttribute('aria-pressed', String(button.dataset.datePreset === dateDraft.date_range)));
    dateDialog.querySelectorAll('[data-future-preset]').forEach(button => button.setAttribute('aria-pressed', String(button.dataset.futurePreset === dateDraft.future_range)));
    dateDialog.querySelector('[data-date-preview]').textContent = `Report range: ${dateDraft.start} – ${futureEnd()}${dateDraft.future_range === 'none' ? '' : ' (includes forecast dates)'}`;
    dateDialog.querySelector('[data-date-error]').textContent = ''; renderCalendar();
  }
  dateDialog.addEventListener('dialogopen', () => {
    dateDraft = Object.fromEntries(dateNames.map(name => [name, field(name).value]));
    calendarMonth = new Date(`${dateDraft.start.slice(0,7)}-01T00:00:00Z`); choosingEnd = false; renderDates();
  });
  dateDialog.querySelectorAll('[data-date-preset]').forEach(button => button.addEventListener('click', () => {
    const range = button.dataset.datePreset; dateDraft.date_range = range;
    const resolved = relativeDateRange(range, today);
    if (resolved) { dateDraft.start=resolved.start; dateDraft.historical_end=resolved.end; calendarMonth=new Date(`${resolved.start.slice(0,7)}-01T00:00:00Z`); }
    choosingEnd = false; renderDates();
  }));
  dateDialog.querySelectorAll('[data-future-preset]').forEach(button => button.addEventListener('click', () => { dateDraft.future_range = button.dataset.futurePreset; renderDates(); }));
  dateDialog.querySelector('[data-more-presets]').addEventListener('click', event => {
    const expand = event.currentTarget.getAttribute('aria-expanded') !== 'true';
    dateDialog.querySelectorAll('[data-extra-preset]').forEach(button => { button.hidden = !expand; });
    event.currentTarget.setAttribute('aria-expanded', String(expand)); event.currentTarget.textContent = expand ? 'Hide' : 'More';
  });
  dateDialog.querySelectorAll('[data-calendar-move]').forEach(button => button.addEventListener('click', () => { calendarMonth.setUTCMonth(calendarMonth.getUTCMonth()+Number(button.dataset.calendarMove)); renderCalendar(); }));
  ['start','end'].forEach(name => draftField(name).addEventListener('change', () => {
    dateDraft[name === 'end' ? 'historical_end' : 'start'] = draftField(name).value; dateDraft.date_range = 'custom'; renderDates();
  }));
  const updateDateSummary = () => { document.querySelector('[data-date-summary]').textContent = `${field('start').value} – ${field('end').value}`; };
  dateDialog.querySelector('[data-apply-dates]').addEventListener('click', () => {
    if (!draftField('start').reportValidity() || !draftField('end').reportValidity()) return;
    if (dateDraft.start > dateDraft.historical_end) { dateDialog.querySelector('[data-date-error]').textContent = 'Start date must be on or before end date.'; return; }
    dateDraft.end = futureEnd(); dateNames.forEach(name => { field(name).value = dateDraft[name]; });
    if (dateDraft.future_range !== 'none') checkbox('forecast').checked = true;
    markChanged(); invalidateMetadata(); updateDateSummary(); closeReportDialog(dateDialog);
  });
  const syncComparisonDates = () => {
    document.querySelector('[data-selected-start]').value = field('start').value; document.querySelector('[data-selected-end]').value = field('end').value;
    document.querySelector('[data-compare-month="selected"]').value = field('start').value.slice(0,7);
    document.querySelector('[data-compare-month="baseline"]').value = field('compare_start').value.slice(0,7); updateDateSummary();
  };
  function monthComparison() {
    field('date_range').value = 'last_month'; field('future_range').value = 'none';
    field('start').value = iso(monthDate(-1)); field('end').value = iso(monthDate(0,0)); field('historical_end').value = field('end').value;
    field('compare_start').value = iso(monthDate(-2)); field('compare_end').value = iso(monthDate(-1,0)); field('granularity').value = 'monthly';
    field('compare_range').value = 'month_over_month'; syncComparisonDates();
  }
  function syncMode() {
    const compare = field('report_mode').value === 'compare';
    document.querySelector('[data-compare-fields]').hidden = !compare; document.querySelector('[data-standard-fields]').hidden = compare;
    field('granularity').disabled = compare; checkbox('forecast').disabled = compare;
    const resource = reportForm.querySelector('[data-filter="resource"]');
    resource.classList.toggle('filter-unavailable', compare);
    resource.querySelectorAll('.dimension-body input, .dimension-body button, [data-clear-filter-summary]').forEach(input => { input.disabled = compare; });
    resource.querySelector('[data-resource-note]').textContent = compare ? 'Resource selection is unavailable in Compare. Switch to Standard to edit this filter.' : 'Choose EC2-Instances in Service. AWS resource data must already be enabled.';
    document.querySelector('[data-granularity-note]').textContent = compare ? 'Comparison uses monthly granularity. Forecasts and resource selection are unavailable.' : 'Hourly and resource reports require enabled AWS granular data and dates within the last 14 days.';
    document.querySelector('[data-open-dialog="save-report-dialog"]')?.toggleAttribute('disabled', compare);
  }
  field('report_mode').forEach(input => input.addEventListener('change', () => { if (field('report_mode').value === 'compare') monthComparison(); syncMode(); invalidateMetadata(); }));
  field('compare_range').addEventListener('change', () => {
    if (field('compare_range').value === 'month_over_month') monthComparison();
    else if (field('compare_range').value === 'previous_period') { field('compare_start').value = ''; field('compare_end').value = ''; }
    document.querySelector('[data-compare-months]').hidden = field('compare_range').value === 'previous_period';
  });
  document.querySelectorAll('[data-compare-month]').forEach(input => input.addEventListener('change', () => {
    if (!input.value) return;
    const date = new Date(`${input.value}-01T00:00:00Z`), end = iso(new Date(Date.UTC(date.getUTCFullYear(),date.getUTCMonth()+1,0)));
    if (input.dataset.compareMonth === 'selected') { field('start').value = iso(date); field('end').value = end; field('historical_end').value = end; }
    else { field('compare_start').value = iso(date); field('compare_end').value = end; }
    field('date_range').value = 'custom'; field('compare_range').value = 'custom'; field('future_range').value = 'none'; syncComparisonDates(); invalidateMetadata();
  }));
  ['start','end'].forEach(name => document.querySelector(`[data-selected-${name}]`).addEventListener('change', event => {
    field(name).value = event.target.value; field('historical_end').value = field('end').value; field('date_range').value = 'custom'; field('compare_range').value = 'custom'; syncComparisonDates(); invalidateMetadata();
  }));
  ['compare_start','compare_end'].forEach(name => field(name).addEventListener('change', () => { field('compare_range').value = 'custom'; syncComparisonDates(); }));
  function syncGroup(clearKey = false) {
    const keyed = ['tag','cost_category'].includes(field('group_by').value);
    document.querySelector('[data-group-key]').hidden = !keyed; document.querySelector('[data-group-resource]').hidden = field('group_by').value !== 'resource';
    if (clearKey) { field('group_key').value = ''; document.getElementById('group-key-options').replaceChildren(); requests.set(document.querySelector('[data-key-status]'), (requests.get(document.querySelector('[data-key-status]')) || 0) + 1); }
    if (keyed && clearKey) queueMicrotask(() => document.querySelector('[data-load-group-keys]').click());
  }
  field('group_by').addEventListener('change', () => syncGroup(true));
  document.querySelector('[data-clear-group]').addEventListener('click', () => { field('group_by').value = 'none'; syncGroup(true); markChanged(); });
  document.querySelector('[data-group-search]').addEventListener('input', event => { const search = event.target.value.toLocaleLowerCase(); [...field('group_by').options].forEach(option => { option.hidden = !option.textContent.toLocaleLowerCase().includes(search); }); });
  document.querySelector('[data-load-group-keys]').addEventListener('click', () => metadata(field('group_by').value, '', document.querySelector('[data-key-status]'), values => {
    document.getElementById('group-key-options').replaceChildren(...values.map(value => { const option = document.createElement('option'); option.value = value; return option; }));
  }));
  function syncAdvanced() {
    const usage = ['usage','cost_usage'].includes(field('measure').value);
    checkbox('normalized').disabled = !usage; field('metric').disabled = field('measure').value === 'usage';
    if (!usage) checkbox('normalized').checked = false;
  }
  ['untagged','uncategorized'].forEach(name => checkbox(name).addEventListener('change', () => {
    const state = [...states.values()].find(state => !state.extra && absentFlag(state.type) === name);
    if (checkbox(name).checked) {
      state.committed.values = []; state.committed.mode = 'include'; state.committed.key = state.box.querySelector('[data-draft-key]').value.trim() || state.committed.key;
      state.box.open = true;
      if (!state.committed.key) { state.status.textContent = 'Choose a key and Apply this filter to use this absence option.'; state.box.querySelector('[data-draft-key]').focus(); }
    }
    renderCommitted(state); resetDraft(state); invalidateMetadata();
  }));
  field('measure').addEventListener('change', syncAdvanced);
  reportForm.addEventListener('change', event => {
    markChanged();
    if (['customer','source','granularity','measure','group_key','group_by','forecast','normalized'].includes(event.target.name)) invalidateMetadata();
  });
  reportForm.addEventListener('input', event => { markChanged(); });
  document.querySelectorAll('[data-chart-style]').forEach(link => link.addEventListener('click', event => {
    event.preventDefault(); field('chart_style').value = link.dataset.chartStyle; reportForm.requestSubmit();
  }));
  const saveForm = document.getElementById('save-current-report');
  if (saveForm) {
    const name = saveForm.elements.namedItem('saved_report_name'), save = saveForm.querySelector('[data-save-report]');
    const validateName = () => { save.disabled = !name.value.trim(); };
    name.addEventListener('input', validateName); validateName();
    saveForm.addEventListener('submit', event => {
      if (!reportForm.reportValidity() || !name.value.trim()) { event.preventDefault(); return; }
      saveForm.querySelectorAll('[data-copied-parameter]').forEach(input => input.remove());
      const data = formData(); data.set('report_name', name.value.trim());
      for (const [key,value] of data) { const input = document.createElement('input'); input.type = 'hidden'; input.name = key; input.value = value; input.dataset.copiedParameter = ''; saveForm.append(input); }
    });
  }
  syncAdditional(); updateVisibility(); syncMode(); syncGroup(); syncAdvanced();
  const pending = document.getElementById('pending-query-ids');
  if (pending) {
    const ids = JSON.parse(pending.textContent); let checks = 0;
    const poll = async () => {
      try {
        const response = await fetch(`/explorer/status/?ids=${ids.join(',')}`, {credentials: 'same-origin'}); if (!response.ok || response.redirected) return;
        const state = await response.json();
        if (!state.pending) { if (!changed && !document.querySelector('dialog[open]')) location.reload(); else document.querySelector('[data-query-progress]').textContent = 'Collection completed. Apply parameters or reload to view it.'; }
        else if (checks++ < 36) setTimeout(poll,5000); else document.querySelector('[data-query-progress]').textContent = 'Still queued. Reload after collection completes; check Activity if it remains pending.';
      } catch { document.querySelector('[data-query-progress]').textContent = 'Unable to check progress. Reload to retry.'; }
    };
    setTimeout(poll,5000);
  }
}

document.querySelectorAll('[data-customer-tree]').forEach(details => {
  details.addEventListener('toggle', async () => {
    if (!details.open || details.dataset.loaded) return;
    details.dataset.loaded = 'loading';
    const content = details.querySelector('[data-tree-content]');
    content.textContent = 'Loading assigned accounts…';
    try {
      const response = await fetch(details.dataset.customerTree, {credentials: 'same-origin'});
      if (!response.ok || response.redirected) throw new Error('Scope unavailable');
      content.innerHTML = await response.text();
      details.dataset.loaded = 'true';
    } catch (error) {
      content.textContent = 'Account details are unavailable. Close and reopen to retry, or sign in again.';
      delete details.dataset.loaded;
    }
  });
});

const accountSettings = document.querySelector('[data-account-settings]');
if (accountSettings) {
  accountSettings.addEventListener('toggle', () => {
    if (accountSettings.open) accountSettings.scrollIntoView({block: 'nearest'});
  });
  accountSettings.addEventListener('keydown', event => {
    if (event.key === 'Escape' && accountSettings.open) {
      event.stopPropagation(); accountSettings.open = false;
      accountSettings.querySelector('summary').focus();
    }
  });
  document.addEventListener('click', event => {
    if (!accountSettings.contains(event.target)) accountSettings.open = false;
  });
}
