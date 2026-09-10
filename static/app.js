'use strict';
// Design tokens shared with app.css (blue monochrome). Patterns and dashes carry meaning alongside colour.
const TOKENS = {primary:'#2563EB', hover:'#1D4ED8', deep:'#1E3A8A', soft:'#EFF6FF', selected:'#DBEAFE', border:'#E2E8F0', muted:'#64748B', text2:'#475569', text:'#0F172A', surface:'#FFFFFF'};
const PATTERNS = ['solid', 'diagonal', 'dots', 'horizontal', 'solid', 'cross', 'diagonal-reverse', 'solid', 'dots', 'dense'];
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

// Explorer parameters stay docked on desktop and open as a drawer on smaller screens.
const parameters = document.getElementById('report-parameters');
if (parameters) {
  const toggles = document.querySelectorAll('[data-toggle-parameters]');
  const updateExpanded = () => toggles.forEach(button => button.setAttribute('aria-expanded', String(getComputedStyle(parameters).display !== 'none')));
  toggles.forEach(button => button.addEventListener('click', () => {
    const open = getComputedStyle(parameters).display !== 'none';
    document.body.classList.toggle('parameters-hidden', open);
    document.body.classList.toggle('parameters-open', !open);
    updateExpanded();
    if (!open) parameters.querySelector('input:not([type="hidden"])').focus();
  }));
  window.addEventListener('resize', updateExpanded); updateExpanded();
}
for (const explorerHost of document.querySelectorAll('#explorer-chart, #comparison-chart')) {
  const chartId=explorerHost.id==='comparison-chart'?'comparison':'explorer';
  const chartPanel=explorerHost.closest('.panel');
  const payload = JSON.parse(document.getElementById(chartId+'-data').textContent);
  const tooltip = document.getElementById(chartId+'-tooltip');
  const hidden = new Set();
  const ns = 'http://www.w3.org/2000/svg';
  const amount = value => {
    if (value === null) return '—';
    return new Intl.NumberFormat('en', {style:payload.measure==='usage'?'decimal':'currency', currency:payload.measure==='usage'?undefined:payload.currency, minimumFractionDigits:2,
      maximumFractionDigits:Math.abs(value) > 0 && Math.abs(value) < .01 ? 10 : 2}).format(value);
  };
  const periodLabel = text => /^\d{4}-\d{2}-\d{2} \d{2}:/.test(text) ? text + ' UTC' : /^\d{4}-\d{2}-\d{2}$/.test(text) ? new Date(`${text}T00:00:00Z`).toLocaleDateString('en-GB',{day:'numeric',month:'short',year:'numeric',timeZone:'UTC'}) : text;
  const drawExplorer = () => {
    const w = Math.max(220, explorerHost.clientWidth), h = explorerHost.clientHeight;
    const pad = {l:w < 400 ? 42 : 55,r:14,t:14,b:34}, pw = w-pad.l-pad.r, ph = h-pad.t-pad.b;
    const series = payload.series.filter((_,i) => !hidden.has(i));
    const count = payload.periods.length, step = pw / Math.max(1,count);
    const totals = payload.periods.map((_,i) => ({
      positive:series.reduce((sum,s) => sum+Math.max(0,s.values[i]||0),0),
      negative:series.reduce((sum,s) => sum+Math.min(0,s.values[i]||0),0)
    }));
    const rawMax = payload.style === 'stacked' ? Math.max(0,...totals.map(t=>t.positive)) : Math.max(0,...series.flatMap(s=>s.values.map(v=>v||0)));
    const rawMin = payload.style === 'stacked' ? Math.min(0,...totals.map(t=>t.negative)) : Math.min(0,...series.flatMap(s=>s.values.map(v=>v||0)));
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
    const fills = payload.series.map((s,i) => PATTERNS[i % PATTERNS.length] === 'solid' ? s.color : definePattern(svg, ns, `${chartId}-series-${i}`, s.color, PATTERNS[i % PATTERNS.length]));
    const indexOf = s => payload.series.indexOf(s);
    const positive=Array(count).fill(0),negative=Array(count).fill(0);
    series.forEach((s,j)=>{
      if(payload.style === 'line') {
        let path='',connected=false;
        const k=indexOf(s);
        s.values.forEach((v,i)=>{
          if(v===null){connected=false;return;}
          path+=`${connected?' L':' M'}${x(i)} ${y(v)}`; connected=true;
        });
        const line=node('path',{d:path,stroke:s.color,'stroke-width':2,fill:'none','stroke-linejoin':'round'});
        if(DASHES[k % DASHES.length]) line.setAttribute('stroke-dasharray', DASHES[k % DASHES.length]);
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
            const groupWidth=Math.min(step*.78,115);
            bw=Math.max(.4,groupWidth/Math.max(1,series.length)*.87);
            left=x(i)-groupWidth/2+j*groupWidth/Math.max(1,series.length);
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
      const displayedTotal=payload.comparison?payload.totals[i]:payload.totals[i]===null?null:available.reduce((sum,s)=>sum+s.values[i],0);
      const title=document.createElement('strong'); title.textContent=`${periodLabel(payload.periods[i])} · ${payload.comparison?'Change':hidden.size ? 'Visible total' : 'Total'} ${amount(displayedTotal)}`;tooltip.appendChild(title);
      available.forEach(s=>{const span=document.createElement('span');span.textContent=`${s.label}: ${amount(s.values[i])}`;tooltip.appendChild(span);});
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
        'aria-label':`${periodLabel(period)}. ${payload.comparison?'Change':'Total'} ${amount(payload.totals[i])}. Focus to inspect groups.`});
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
    // Legend swatches show the same pattern/dash as the chart so series are distinguishable without colour.
    const index=Number(button.dataset.series), series=payload.series[index], swatch=button.querySelector('svg');
    if(series && swatch){
      swatch.setAttribute('viewBox','0 0 14 14'); swatch.replaceChildren();
      const kind=PATTERNS[index % PATTERNS.length];
      const fill=kind==='solid'?series.color:definePattern(swatch, ns, `${chartId}-legend-${index}`, series.color, kind);
      const rect=document.createElementNS(ns,'rect'); rect.setAttribute('width','14'); rect.setAttribute('height','14'); rect.setAttribute('fill',fill); swatch.appendChild(rect);
      if(payload.style==='line'){ const line=document.createElementNS(ns,'path'); line.setAttribute('d','M1 7H13'); line.setAttribute('stroke',TOKENS.surface); line.setAttribute('stroke-width','2'); if(DASHES[index % DASHES.length]) line.setAttribute('stroke-dasharray',DASHES[index % DASHES.length]); swatch.appendChild(line); }
    }
  });
  chartPanel.querySelectorAll('[data-series]').forEach(button=>button.addEventListener('click',()=>{
    const index=Number(button.dataset.series);
    if(hidden.has(index))hidden.delete(index);else hidden.add(index);
    button.setAttribute('aria-pressed',String(!hidden.has(index)));drawExplorer();
  }));
  new ResizeObserver(drawExplorer).observe(explorerHost);drawExplorer();
}

// Metadata is fetched on demand and cached by the same background worker as reports.
const reportForm=document.getElementById('report-form');
if(reportForm){
  const field=name=>reportForm.elements.namedItem(name);
  let changed=false;
  const reportToday=new Date(reportForm.dataset.today+'T00:00:00Z');
  const iso=date=>date.toISOString().slice(0,10);
  const monthDate=(offset,day=1)=>new Date(Date.UTC(reportToday.getUTCFullYear(),reportToday.getUTCMonth()+offset,day));
  field('date_range').addEventListener('change',()=>{
    const range=field('date_range').value;let start,end;
    if(range!=='last_month'&&field('compare_range').value==='month_over_month'){field('compare_range').value='previous_period';field('compare_start').value='';field('compare_end').value='';}
    if(range==='this_month'){start=monthDate(0);end=reportToday;}
    else if(range==='last_month'){start=monthDate(-1);end=monthDate(0,0);}
    else if(/^last_(3|6|12)_months$/.test(range)){start=monthDate(-Number(range.split('_')[1]));end=monthDate(0,0);}
    else if(/^last_(7|14)_days$/.test(range)){end=reportToday;start=new Date(reportToday);start.setUTCDate(start.getUTCDate()-Number(range.split('_')[1])+1);}
    if(start){field('start').value=iso(start);field('end').value=iso(end);}
  });
  const monthComparison=()=>{
    field('date_range').value='last_month';field('start').value=iso(monthDate(-1));field('end').value=iso(monthDate(0,0));
    field('compare_start').value=iso(monthDate(-2));field('compare_end').value=iso(monthDate(-1,0));field('granularity').value='monthly';
    field('compare_range').value='month_over_month';field('date_range').dispatchEvent(new Event('change',{bubbles:true}));
  };
  document.querySelector('[data-month-comparison]').addEventListener('click',monthComparison);
  field('compare_range').addEventListener('change',()=>{
    if(field('compare_range').value==='month_over_month')monthComparison();
    else if(field('compare_range').value==='previous_period'){field('compare_start').value='';field('compare_end').value='';}
  });
  reportForm.addEventListener('change',()=>{changed=true;});
  reportForm.addEventListener('input',()=>{changed=true;});
  ['start','end'].forEach(name=>field(name).addEventListener('change',()=>{field('date_range').value='custom';if(field('compare_range').value==='month_over_month')field('compare_range').value='custom';}));
  ['compare_start','compare_end'].forEach(name=>field(name).addEventListener('change',()=>{field('compare_range').value='custom';}));
  field('report_mode').addEventListener('change',()=>{document.querySelector('[data-compare-fields]').hidden=field('report_mode').value!=='compare';if(field('report_mode').value==='compare')monthComparison();});
  field('group_by').addEventListener('change',()=>{document.querySelector('[data-group-key]').hidden=!['tag','cost_category'].includes(field('group_by').value);});
  const makeOption=(key,value,checked=false,display='')=>{
    const label=document.createElement('label');label.className='checkbox-label';
    const input=document.createElement('input');input.type='checkbox';input.name=key;input.value=value===''?'__billing_empty_value__':value;input.checked=checked;
    const span=document.createElement('span');span.textContent=display||value||'(Empty value)';label.append(input,span);return label;
  };
  const metadataRequests=new WeakMap();
  async function metadata(kind,key,status,render){
    const generation=(metadataRequests.get(status)||0)+1;metadataRequests.set(status,generation);
    const query=new URLSearchParams(new FormData(reportForm));query.set('dimension',kind);query.set('key',key);
    let attempts=0;
    const read=async()=>{
      try{
        const response=await fetch(`/explorer/metadata/?${query}`,{credentials:'same-origin'});
        if(response.redirected)throw new Error('Your session expired. Sign in again to load billing values.');
        if(!response.ok){let error={};try{error=await response.json();}catch{}throw new Error(error.error||'Could not load billing values.');}
        const result=await response.json();if(metadataRequests.get(status)!==generation)return;render(result.values,result.labels||{});
        status.textContent=result.errors.length?result.errors.join(' '):result.pending?'Loading from AWS; the worker checks within a minute.':result.values.length?`${result.values.length} available values`:'No values returned for these dates and customers.';
        if(result.pending&&attempts++<24)setTimeout(read,5000);
        else if(result.pending)status.textContent='Still queued. Use Load available values to check again, or review Sync & activity.';
      }catch(error){if(metadataRequests.get(status)===generation)status.textContent=error.message;}
    };await read();
  }
  reportForm.querySelectorAll('[data-filter]').forEach(box=>{
    const key=box.dataset.filter, list=box.querySelector('.dimension-values'), status=box.querySelector('[data-filter-status]');
    const count=()=>{const n=list.querySelectorAll('input:checked').length;box.querySelector('[data-filter-count]').textContent=n?`${n} selected`:'All';};
    const search=()=>{const term=box.querySelector('[data-value-search]').value.toLowerCase();list.querySelectorAll('label').forEach(label=>{label.hidden=!label.textContent.toLowerCase().includes(term);});};
    list.addEventListener('change',count);
    box.querySelector('[data-value-search]').addEventListener('input',search);
    box.querySelector('[data-load-values]').addEventListener('click',()=>{
      const keyValue=field(`${key}_key`)?.value||'';
      if(['tag','cost_category'].includes(key)&&!keyValue){status.textContent='Choose a key first.';return;}
      metadata(key,keyValue,status,(values,labels)=>{list.querySelectorAll('input:not(:checked)').forEach(input=>input.closest('label').remove());const existing=new Map([...list.querySelectorAll('input')].map(i=>[i.value,i]));values.forEach(value=>{const input=existing.get(value===''?'__billing_empty_value__':value);if(!input)list.append(makeOption(key,value,false,labels[value]));else if(labels[value])input.nextElementSibling.textContent=labels[value];});search();});
    });
    box.addEventListener('toggle',()=>{if(box.open&&!box.dataset.loaded){box.dataset.loaded='1';(box.querySelector('[data-load-keys]')||box.querySelector('[data-load-values]')).click();}});
    box.querySelector('[data-add-value]').addEventListener('click',()=>{
      const input=box.querySelector('[data-manual-value]'), value=input.value;
      if(!value)return;
      const existing=[...list.querySelectorAll('input')].find(i=>i.value===value);
      if(existing)existing.checked=true;else list.append(makeOption(key,value,true));input.value='';count();changed=true;list.dispatchEvent(new Event('change',{bubbles:true}));
    });
    box.querySelector('[data-clear-values]').addEventListener('click',()=>{list.querySelectorAll('input').forEach(i=>{i.checked=false;});count();changed=true;list.dispatchEvent(new Event('change',{bubbles:true}));});
    box.querySelector('[data-load-keys]')?.addEventListener('click',()=>metadata(key,'',status,values=>{
      const datalist=document.getElementById(`keys-${key}`);datalist.replaceChildren(...values.map(value=>{const option=document.createElement('option');option.value=value;return option;}));
    }));
    box.querySelector(`[name="${key}_key"]`)?.addEventListener('change',()=>{metadataRequests.set(status,(metadataRequests.get(status)||0)+1);list.replaceChildren();count();status.textContent='Key changed. Load values for this key.';});
  });
  document.querySelector('[data-load-group-keys]').addEventListener('click',()=>metadata(field('group_by').value,'',document.querySelector('[data-key-status]'),values=>{
    document.getElementById('group-key-options').replaceChildren(...values.map(value=>{const option=document.createElement('option');option.value=value;return option;}));
  }));
  reportForm.addEventListener('change',event=>{
    if(!['customer','source','start','end','date_range'].includes(event.target.name)&&!event.target.closest('[data-filter]'))return;
    reportForm.querySelectorAll('[data-filter-status], [data-key-status]').forEach(status=>{metadataRequests.set(status,(metadataRequests.get(status)||0)+1);status.textContent='Filters changed. Reload available values.';});
    reportForm.querySelectorAll('[data-filter]').forEach(box=>{delete box.dataset.loaded;});
  });
  let preferences={};try{preferences=JSON.parse(localStorage.getItem('billing-filter-visibility')||'{}');}catch{}
  reportForm.querySelectorAll('[data-visible-filter]').forEach(toggle=>{
    const box=reportForm.querySelector(`[data-filter="${toggle.dataset.visibleFilter}"]`);
    const apply=()=>{box.hidden=!toggle.checked&&!box.querySelector('input:checked');};
    toggle.checked=preferences[toggle.dataset.visibleFilter]!==false;apply();
    toggle.addEventListener('change',()=>{preferences[toggle.dataset.visibleFilter]=toggle.checked;try{localStorage.setItem('billing-filter-visibility',JSON.stringify(preferences));}catch{}apply();});
  });
  document.getElementById('save-current-report')?.addEventListener('submit',event=>{
    if(!reportForm.reportValidity()){event.preventDefault();return;}
    const saveForm=event.currentTarget;
    saveForm.querySelectorAll('input:not([name="csrfmiddlewaretoken"])').forEach(input=>input.remove());
    for(const [name,value] of new FormData(reportForm)){const input=document.createElement('input');input.type='hidden';input.name=name;input.value=value;saveForm.append(input);}
  });
  const pending=document.getElementById('pending-query-ids');
  if(pending){
    const ids=JSON.parse(pending.textContent);let checks=0;
    const poll=async()=>{
      try{const response=await fetch(`/explorer/status/?ids=${ids.join(',')}`,{credentials:'same-origin'});if(!response.ok)return;
        const state=await response.json();
        if(!state.pending){
          if(!changed)location.reload();else document.querySelector('[data-query-progress]').textContent='Collection completed. Apply parameters or reload to view it.';
        }else if(checks++<36)setTimeout(poll,5000);else document.querySelector('[data-query-progress]').textContent='Still queued. Reload after collection completes; check Activity if it remains pending.';
      }catch{document.querySelector('[data-query-progress]').textContent='Unable to check progress. Reload to retry.';}
    };setTimeout(poll,5000);
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
