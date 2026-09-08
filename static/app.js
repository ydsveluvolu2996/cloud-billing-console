'use strict';
document.querySelectorAll('[data-copy]').forEach(button => button.addEventListener('click', async () => {
  const field = document.getElementById(button.dataset.copy);
  try { await navigator.clipboard.writeText(field.value); button.textContent = 'Link copied'; }
  catch { field.focus(); field.select(); button.textContent = 'Select and copy the link'; }
}));
const host = document.getElementById('cost-chart');
if (host) {
  const data = JSON.parse(document.getElementById('chart-data').textContent);
  const draw = () => {
    const w = Math.max(host.clientWidth, 260), h = host.clientHeight, pad = {t:12,r:12,b:40,l:56};
    const plotW = w-pad.l-pad.r, plotH=h-pad.t-pad.b;
    const values=data.map(d=>d.amount), max=Math.max(0,...values)*1.12||1, min=Math.min(0,...values)*1.12;
    const y=v=>pad.t+(max-v)/(max-min)*plotH;
    const ns='http://www.w3.org/2000/svg';
    const svg=document.createElementNS(ns,'svg'); svg.setAttribute('viewBox',`0 0 ${w} ${h}`);
    function el(tag, attrs, text) {const e=document.createElementNS(ns,tag); for(const [k,v] of Object.entries(attrs))e.setAttribute(k,v); if(text!==undefined)e.textContent=text; svg.appendChild(e); return e;}
    for(let i=0;i<=4;i++) {const v=min+(max-min)*i/4;el('line',{x1:pad.l,x2:w-pad.r,y1:y(v),y2:y(v),stroke:'#e7edf1','stroke-dasharray':'3 4'});el('text',{x:pad.l-10,y:y(v)+4,'text-anchor':'end',fill:'#738795','font-size':11},Intl.NumberFormat('en',{notation:'compact',maximumFractionDigits:1}).format(v));}
    const step=plotW/Math.max(1,data.length), bw=Math.max(1,step*.63), labelEvery=Math.max(1,Math.ceil(data.length/Math.max(2,Math.floor(plotW/85))));
    data.forEach((d,i)=>{const x=pad.l+i*step+step/2;const rect=el('rect',{x:x-bw/2,y:Math.min(y(d.amount),y(0)),width:bw,height:Math.max(1,Math.abs(y(d.amount)-y(0))),rx:Math.min(3,bw/3),fill:d.amount<0?'#aa7131':'#087d6c'});const title=document.createElementNS(ns,'title');title.textContent=`${d.label}: ${d.amount.toLocaleString('en',{minimumFractionDigits:2,maximumFractionDigits:2})}`;rect.appendChild(title);if(i%labelEvery===0){const label=/^\d{4}-/.test(d.label)?d.label.slice(5):d.label;el('text',{x,y:h-12,'text-anchor':'middle',fill:'#738795','font-size':11},label);}});
    host.replaceChildren(svg);
  };
  new ResizeObserver(draw).observe(host); draw();
}
