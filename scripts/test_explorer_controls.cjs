'use strict';
// Dependency-free regressions for the transformations used by Cost Explorer's UI.
// DOM interactions are verified separately in the browser.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../static/app.js'), 'utf8');
const context = vm.createContext({});
vm.runInContext(source.slice(0, source.indexOf('const navToggle =')), context);
const evaluate = code => JSON.parse(JSON.stringify(vm.runInContext(code, context)));
const ranges = {
  last_1_day: ['2026-09-21', '2026-09-21'], last_7_days: ['2026-09-16','2026-09-22'],
  this_month: ['2026-09-01','2026-09-22'], last_month: ['2026-08-01','2026-08-31'],
  last_3_months: ['2026-06-01','2026-08-31'], last_6_months: ['2026-03-01','2026-08-31'],
  last_12_months: ['2025-09-01','2026-08-31'], last_36_months: ['2023-09-01','2026-08-31'],
  year_to_date: ['2026-01-01','2026-09-22'], current_month: ['2026-09-01','2026-09-30']
};
for (const [name, [start,end]] of Object.entries(ranges)) {
  assert.deepEqual(evaluate(`relativeDateRange('${name}', new Date('2026-09-22T00:00:00Z'))`), {start,end}, name);
}
assert.deepEqual(evaluate("relativeDateRange('last_month', new Date('2024-03-01T00:00:00Z'))"), {start:'2024-02-01',end:'2024-02-29'});
assert.equal(evaluate("relativeDateRange('custom', new Date('2026-09-22T00:00:00Z'))"), null);
const forecast = evaluate(`(() => {
  const payload={periods:['Aug 2026','Sep 2026'],totals:[100,20],series:[{label:'Actual',values:[100,20]}],forecast_rows:[
    {series_id:'one',customer:'Customer',start:'2026-09-23',end:'2026-10-01',mean:60,lower:30,upper:80},
    {series_id:'two',customer:'Customer',start:'2026-09-23',end:'2026-10-01',mean:10,lower:5,upper:20},
    {series_id:'one',customer:'Customer',start:'2026-10-01',end:'2026-11-01',mean:120,lower:75,upper:150}
  ]};
  const series=prepareForecastSeries(payload);return {payload,series};
})()`);
assert.deepEqual(forecast.payload.periods,['Aug 2026','Sep 2026','Oct 2026']);
assert.deepEqual(forecast.payload.totals,[100,20,null]);
assert.deepEqual(forecast.payload.series[0].values,[100,20,null]);
assert.equal(forecast.series.length,2,'Same customer label must not merge independent forecast intervals');
assert.deepEqual(forecast.series[0].values,[null,60,120]);
assert.deepEqual(forecast.series[1].values,[null,10,null]);
assert.equal(forecast.series[0].intervals[1].lower,30);
assert.equal(forecast.series[1].intervals[1].lower,5);
assert.equal(forecast.series[0].intervals[1].upper,80);
assert.equal(forecast.series[1].intervals[1].upper,20);
assert.deepEqual(evaluate('prepareForecastSeries({comparison:true,forecast_rows:[{}]})'),[]);
const daily=evaluate(`(() => {const p={periods:['2026-09-22'],totals:[9],series:[{values:[9]}],forecast_rows:[{series_id:'d',customer:'Daily',start:'2026-09-23',end:'2026-09-24',mean:3,lower:1,upper:5}]}; const s=prepareForecastSeries(p);return {periods:p.periods,values:s[0].values};})()`);
assert.deepEqual(daily,{periods:['2026-09-22','2026-09-23'],values:[null,3]});
assert.deepEqual(evaluate(`(() => {const p={granularity:'daily',periods:[],totals:[],series:[],forecast_rows:[{series_id:'future',customer:'Future',start:'2026-10-01',end:'2026-10-02',mean:3,lower:1,upper:5}]};prepareForecastSeries(p);return p.periods;})()`),['2026-10-01']);
// Exercise the real delegated checkbox and manual-add handlers with a tiny DOM stub.
// Both new and already-loaded exact values must clear the missing-key selection.
const changeStart = source.indexOf("    list.addEventListener('change', event => {");
const changeEnd = source.indexOf("    box.querySelector('[data-select-matching]')", changeStart);
const addStart = source.indexOf("    box.querySelector('[data-add-value]').addEventListener('click', () => {");
const addEnd = source.indexOf("    box.querySelector('[data-cancel-filter]')", addStart);
const wire = vm.runInContext(`(list, box, makeOption, updateMatching, state, Event) => {
  ${source.slice(changeStart, changeEnd)}
  ${source.slice(addStart, addEnd)}
}`, context);
for (const alreadyLoaded of [false, true]) {
  let onChange, onAdd;
  const options = [];
  const option = (value, checked) => ({value, checked, dispatchEvent(event) {
    assert.equal(event.type, 'change'); assert.equal(event.bubbles, true); onChange({target: this});
  }});
  const missing = option('__billing_absent_key__', true); options.push(missing);
  if (alreadyLoaded) options.push(option('DEV', false));
  const manual = {value: 'DEV'};
  const list = {addEventListener: (_, callback) => { onChange = callback; },
    querySelectorAll: () => options,
    querySelector: () => missing,
    append: label => options.push(label.querySelector('input'))};
  const box = {querySelector: selector => selector === '[data-manual-value]' ? manual : {addEventListener: (_, callback) => { onAdd = callback; }}};
  const makeOption = (value, checked) => { const input = option(value, checked); return {querySelector: () => input}; };
  wire(list, box, makeOption, () => {}, {}, Event);
  onAdd();
  assert.equal(missing.checked, false, `manual Add must clear absence (${alreadyLoaded ? 'existing' : 'new'} value)`);
  assert.equal(options.find(input => input.value === 'DEV').checked, true);
  assert.equal(manual.value, '');
}
// Select-all only changes matching normal values, but absence is globally exclusive.
const selectStart = source.indexOf("    box.querySelector('[data-select-matching]').addEventListener('change', event => {");
const selectEnd = source.indexOf("    box.querySelector('[data-clear-values]')", selectStart);
const wireMatching = vm.runInContext(`(list, box, updateMatching, state, Event) => {
  ${source.slice(changeStart, changeEnd)}
  ${source.slice(selectStart, selectEnd)}
}`, context);
for (const scenario of [
  {name:'normal match clears hidden absence', initial:[true,false,true], matching:[1], check:true, expected:[false,true,true]},
  {name:'absence match clears hidden normal values', initial:[false,true,true], matching:[0], check:true, expected:[true,false,false]},
  {name:'uncheck matching preserves other normal values', initial:[false,true,true], matching:[1], check:false, expected:[false,false,true]},
  {name:'all matching normal values win over missing key', initial:[true,false,false], matching:[0,1,2], check:true, expected:[false,true,true]}
]) {
  let onChange, onSelect;
  const options = ['__billing_absent_key__','DEV','QA'].map((value,index) => ({value,checked:scenario.initial[index],dispatchEvent(event) {
    assert.equal(event.type,'change'); assert.equal(event.bubbles,true); onChange({target:this});
  }}));
  const list = {addEventListener: (_,callback) => { onChange=callback; },
    querySelectorAll: selector => selector === 'input' ? options : scenario.matching.map(index=>options[index]),
    querySelector: () => options[0]};
  const box = {querySelector: () => ({addEventListener: (_,callback) => { onSelect=callback; }})};
  wireMatching(list,box,()=>{}, {},Event);
  onSelect({target:{checked:scenario.check}});
  assert.deepEqual(options.map(input=>input.checked),scenario.expected,scenario.name);
}
console.log('Cost Explorer controls: date boundaries, independent forecasts, and manual/select-all absence regressions passed.');
