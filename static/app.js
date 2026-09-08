'use strict';
document.querySelectorAll('[data-copy]').forEach(button => button.addEventListener('click', async () => {
  const field = document.getElementById(button.dataset.copy);
  try { await navigator.clipboard.writeText(field.value); button.textContent = 'Link copied'; }
  catch { field.focus(); field.select(); button.textContent = 'Select and copy the link'; }
}));

// All tables remain available when JavaScript is disabled.
const tabs = [...document.querySelectorAll('[data-tab]')];
const panels = [...document.querySelectorAll('[data-table-panel]')];
function searchTables(group) {
  const input = document.querySelector(`[data-search-group="${group}"]`);
  const query = (input?.value || '').toLocaleLowerCase().trim();
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
  input.addEventListener('input', () => searchTables(input.dataset.searchGroup));
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
      el('line', {x1:pad.l, x2:w-pad.r, y1:y(v), y2:y(v), stroke:'#e8edf0', 'stroke-dasharray':'3 4'});
      el('text', {x:pad.l-9, y:y(v)+3, 'text-anchor':'end', fill:'#7d8d98', 'font-size':10}, Intl.NumberFormat('en',{notation:'compact',maximumFractionDigits:1}).format(v));
    }
    el('line', {x1:pad.l, x2:w-pad.r, y1:y(0), y2:y(0), stroke:'#d8e2e6'});
    const step = plotW / Math.max(1,data.length), bw = Math.max(1, Math.min(42,step*.56));
    const labelEvery = Math.max(1,Math.ceil(data.length/Math.max(2,Math.floor(plotW/68))));
    const targets = [];
    data.forEach((d,i) => {
      const x = pad.l+i*step+step/2;
      const bar = el('rect', {x:x-bw/2, y:d.amount === null ? y(0)-2 : Math.min(y(d.amount),y(0)), width:bw,
        height:d.amount === null ? 2 : Math.max(2,Math.abs(y(d.amount)-y(0))), rx:Math.min(3,bw/3),
        fill:d.amount === null ? '#dce4e9' : d.amount < 0 ? '#b78237' : '#168c78', class:'chart-bar',
        tabindex:i===0?0:-1, role:'img', 'aria-label':`${label(d.label)}: ${money(d.amount)}`});
      const title = document.createElementNS(ns,'title');
      title.textContent = `${label(d.label)}: ${money(d.amount)}`; bar.appendChild(title);
      const show = () => { readout.textContent = `${label(d.label)} · ${money(d.amount)}`; bar.setAttribute('fill',d.amount === null ? '#9caeb9' : d.amount < 0 ? '#8c5e23' : '#0c574d'); };
      const hide = () => { bar.setAttribute('fill',d.amount === null ? '#dce4e9' : d.amount < 0 ? '#b78237' : '#168c78'); };
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
      if (i%labelEvery===0) el('text', {x, y:h-11, 'text-anchor':'middle', fill:'#7d8d98', 'font-size':10}, /^\d{4}-/.test(d.label) ? new Date(`${d.label}T00:00:00Z`).toLocaleDateString('en-GB',{day:'numeric',month:'short',timeZone:'UTC'}) : d.label.replace(' 20', ' ’'));
    });
    host.replaceChildren(svg);
  };
  new ResizeObserver(draw).observe(host); draw();
}
