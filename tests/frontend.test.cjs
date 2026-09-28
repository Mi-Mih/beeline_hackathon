// Dependency-free regressions for dispatcher state and timeline rendering.
// Run: node --test tests/frontend.test.cjs
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function app() {
  const elements = new Map();
  const element = (id) => {
    if (!elements.has(id)) elements.set(id, {
      textContent: '', innerHTML: '', disabled: false,
      style: { setProperty() {} }, scrollLeft: 0, scrollWidth: 2000, clientWidth: 600,
      parentElement: { classList: { toggle() {} } },
      querySelectorAll: () => [],
    });
    return elements.get(id);
  };
  const context = vm.createContext({
    Intl, Date, Map, console, AbortController, setTimeout, clearTimeout,
    document: { getElementById: element, querySelector: element, querySelectorAll: () => [] },
  });
  const source = fs.readFileSync(path.join(__dirname, '../frontend/app.js'), 'utf8');
  vm.runInContext(source.replace(/\binit\(\);\s*$/, ''), context);
  vm.runInContext('render = () => {}; showToast = () => {};', context);
  return { context, element, run: (code) => vm.runInContext(code, context) };
}

test('failed plan leaves current input, baseline and selection intact', async () => {
  const { context, run, element } = app();
  run('state.input = {id:"current"}; state.baseline = {id:"baseline"}; state.selectedRequestId = "REQ-1";');
  context.fetch = async () => ({ ok: false, json: async () => ({ error: 'Invalid input' }) });
  await assert.rejects(run('requestPlan({})'), /Invalid input/);
  assert.equal(run('state.input.id'), 'current');
  assert.equal(run('state.baseline.id'), 'baseline');
  assert.equal(run('state.selectedRequestId'), 'REQ-1');
  assert.equal(run('state.busy'), false);
  assert.equal(element('plan-status').textContent, 'Ошибка расчёта');
});

test('concurrent calculation is refused before a second network request', async () => {
  const { context, run } = app();
  let finish;
  let calls = 0;
  context.fetch = () => { calls++; return new Promise(resolve => { finish = resolve; }); };
  const first = run('requestPlan({requests:[],technicians:[]})');
  await assert.rejects(run('requestPlan({})'), /Дождитесь/);
  assert.equal(calls, 1);
  finish({ ok: true, json: async () => ({ routes: [], unassigned: [] }) });
  await first;
  assert.equal(run('state.busy'), false);
});

test('unsuccessful demo reload does not replace reset snapshot', async () => {
  const { context, run } = app();
  run('state.originalInput = {id:"original"};');
  context.fetch = async url => url === '/api/demo'
    ? { ok: true, json: async () => ({ requests: [], technicians: [] }) }
    : { ok: false, json: async () => ({ error: 'Offline' }) };
  await assert.rejects(run('loadDemo()'), /Offline/);
  assert.equal(run('state.originalInput.id'), 'original');
});

test('timeline follows actual shift bounds and does not inflate short stops', () => {
  const { run, element } = app();
  run(`state.input = {technicians:[{id:'t',name:'Engineer',available_from:'2026-09-27T06:00:00',shift_end:'2026-09-27T22:00:00'}]};
    state.plan = {routes:[{technician_id:'t',metrics:{request_count:1,distance_km:2},stops:[{request_id:'r',service_start_at:'2026-09-27T21:45:00',service_end_at:'2026-09-27T22:00:00'}]}]};
    renderTimeline();`);
  assert.match(element('.timeline-scale').innerHTML, /06:00/);
  assert.match(element('.timeline-scale').innerHTML, /22:00/);
  assert.match(element('timeline').innerHTML, /left:98\.4375%;width:1\.5625%/);
});

test('hidden UI and narrow assignment grid have explicit layout guards', () => {
  const css = fs.readFileSync(path.join(__dirname, '../frontend/styles.css'), 'utf8');
  assert.match(css, /\[hidden\]\s*\{\s*display:\s*none\s*!important/);
  assert.match(css, /\.manual-assignment\s*\{[^}]*grid-template-columns:\s*minmax\(0,\s*1fr\)/);
  assert.match(css, /\.manual-assignment > div\s*\{[^}]*grid-template-columns:\s*minmax\(0,\s*1fr\) auto/);
});

test('map frontend uses pinned MapLibre assets with integrity metadata', () => {
  const html = fs.readFileSync(path.join(__dirname, '../frontend/index.html'), 'utf8');
  assert.match(html, /maplibre-gl@5\.24\.0\/dist\/maplibre-gl\.(?:css|js)/);
  assert.match(html, /integrity="sha256-[^"]+"/);
});

test('pagination caps large lists and clamps a page after filtering', () => {
  const { run, element } = app();
  assert.equal(run('paginate(Array.from({length:300}, (_, i) => i), "requestPage", 50, "requests").length'), 50);
  assert.equal(element('requests-page').textContent, '1 / 6');
  run('state.requestPage = 5;');
  assert.equal(run('paginate([1,2], "requestPage", 50, "requests").length'), 2);
  assert.equal(run('state.requestPage'), 0);
  assert.equal(element('requests-next').disabled, true);
});

test('imports disable public routing only after a successful plan', async () => {
  const { context, run } = app();
  run('state.demoSource = "synthetic";');
  context.fetch = async () => ({ ok: false, json: async () => ({ error: 'Invalid' }) });
  await assert.rejects(run('requestPlan({}, {demoSource:null})'), /Invalid/);
  assert.equal(run('state.demoSource'), 'synthetic');
  context.fetch = async () => ({ ok: true, json: async () => ({ routes: [], unassigned: [] }) });
  await run('requestPlan({requests:[],technicians:[]}, {demoSource:null})');
  assert.equal(run('state.demoSource'), null);
});

test('road geometry requests carry the correct privacy scope', async () => {
  const { context, run } = app();
  const urls = [];
  context.fetch = async url => { urls.push(url); return { ok: true, json: async () => ({geometry:{coordinates:[[37.7,55.6],[37.8,55.7]]}}) }; };
  await run('roadGeometry([[55.6,37.7],[55.7,37.8]], "car")');
  assert.doesNotMatch(urls[0], /demo=/);
  run('state.demoSource = "synthetic";');
  await run('roadGeometry([[55.6,37.7],[55.7,37.8]], "car")');
  assert.match(urls[1], /demo=synthetic/);
});

test('Gantt zoom expands time proportionally and fit resets horizontal scroll', () => {
  const { run, element } = app();
  run(`state.input = {technicians:[{id:'t',name:'Crew',available_from:'2026-09-27T09:00:00',shift_end:'2026-09-27T18:00:00'}]};
    state.plan = {routes:[]}; renderTimeline();`);
  assert.equal(element('timeline-canvas').style.minWidth, '2305px');
  run('changeTimelineZoom(1)');
  assert.equal(element('timeline-canvas').style.minWidth, '4465px');
  assert.equal(element('timeline-zoom-label').textContent, '200%');
  run('changeTimelineZoom(1); changeTimelineZoom(1);');
  assert.equal(run('state.timelineZoom'), 4);
  assert.equal(element('timeline-zoom-in').disabled, true);
  run('changeTimelineZoom(0)');
  assert.equal(element('timeline-canvas').style.minWidth, '100%');
  assert.equal(element('timeline-zoom-out').disabled, true);
});

function mockMap(context) {
  const lines = [];
  context.createRouteLine = (points, style) => {
    const line = { points, style,
      setLatLngs(points) { this.points = points; return this; },
      setStyle(style) { Object.assign(this.style, style); return this; }, setTooltipContent() {} };
    lines.push(line);
    return line;
  };
  return lines;
}

const mapSetup = `state.plan = {routes:[
  {technician_id:'a',technician_name:'A',start_location_id:'office',stops:[{location_id:'x'}]},
  {technician_id:'b',technician_name:'B',start_location_id:'office',stops:[{location_id:'y'}]}
]}; state.input = {technicians:[{id:'a',vehicle:'car'},{id:'b',vehicle:'car'}]};
var locations = new Map([['office',{latitude:55,longitude:37}],['x',{latitude:55.1,longitude:37.1}],['y',{latitude:55.2,longitude:37.2}]]);
var controller = new AbortController();`;

test('all crews draw all paths, replace geometry sequentially and retain failed schematics', async () => {
  const { context, run, element } = app();
  const lines = mockMap(context);
  let active = 0, peak = 0, calls = 0;
  context.fetch = async () => {
    active++; peak = Math.max(peak, active); calls++;
    await Promise.resolve(); active--;
    return calls === 1 ? {ok:true,json:async()=>({geometry:{coordinates:[[37,55],[37.05,55.06],[37.1,55.1]]}})}
      : {ok:false,json:async()=>({error:'Offline'})};
  };
  run(mapSetup);
  await run('drawRoadRoutes(state.plan.routes, locations, controller, mapRenderToken, false)');
  assert.equal(lines.length, 2);
  assert.equal(peak, 1);
  assert.equal(lines[0].style.dashArray, null);
  assert.equal(lines[1].style.dashArray, '7 9');
  assert.match(element('map-caption').textContent, /По дорогам: 1\/2/);
  assert.equal(element('map-retry').hidden, false);
});

test('changing map selection prevents stale geometry and stops the remaining queue', async () => {
  const { context, run } = app();
  const lines = mockMap(context);
  let complete, calls = 0;
  context.fetch = () => { calls++; return new Promise(resolve => { complete = resolve; }); };
  run(mapSetup);
  const pending = run('drawRoadRoutes(state.plan.routes, locations, controller, mapRenderToken, false)');
  run('mapRenderToken++; controller.abort();');
  complete({ok:true,json:async()=>({geometry:{coordinates:[[37,55],[37.1,55.1]]}})});
  await pending;
  assert.equal(calls, 1);
  assert.equal(lines[0].style.dashArray, '7 9');
});

function liveFixture(run, element) {
  const input = JSON.parse(fs.readFileSync(path.join(__dirname, '../examples/sample-input.json'), 'utf8'));
  run(`state.input = ${JSON.stringify(input)}; state.originalInput = deepClone(state.input);`);
  element('event-at').value = '2026-08-17T09:00';
  return {id:'LIVE-1',locationId:'client-d',type:'emergency',priority:'urgent',duration:'30',start:'2026-08-17T09:00',end:'2026-08-17T12:00',skill:'emergency',vehicle:'',equipment:'repair-kit, repair-kit',address:'',latitude:'',longitude:''};
}

test('manual draft validates and leaves existing input and plan unchanged', () => {
  const {run,element} = app();
  const values = liveFixture(run, element);
  run('state.plan = {id:"old-plan"};');
  run(`state.pendingRequests.push(makePendingRequest(${JSON.stringify(values)}));`);
  assert.equal(run('state.input.requests.length'), 4);
  assert.equal(run('workingInput().requests.length'), 5);
  assert.equal(run('state.plan.id'), 'old-plan');
  assert.equal(run('state.pendingRequests[0].request.required_equipment.length'), 1);
  assert.equal(run('state.pendingRequests[0].request.window.start'), '2026-08-17T06:00:00.000Z');
  assert.throws(() => run(`makePendingRequest(${JSON.stringify(values)})`), /уже существует/);
});

test('manual draft rejects invalid windows, duration and unknown points', () => {
  const {run,element} = app();
  const values = liveFixture(run,element);
  for (const patch of [{duration:0},{end:values.start},{locationId:'missing'},{priority:'bad'},{start:'2026-02-30T09:00'}]) {
    assert.throws(() => run(`makePendingRequest(${JSON.stringify({...values,...patch})})`));
  }
});

test('new coordinates can be saved without gateway; recalculation requires it', async () => {
  const {run,element} = app();
  const values = {...liveFixture(run,element),locationId:'__new__',address:'New site',latitude:'55.7',longitude:'37.7'};
  const draft = run(`makePendingRequest(${JSON.stringify(values)})`);
  assert.equal(draft.location.latitude, 55.7);
  assert.equal(run('state.demoSource'), null);
  run(`state.plan = {routes:[]}; state.pendingRequests.push(makePendingRequest(${JSON.stringify(values)})); apiJson = async () => ({matrix_mode:'input'});`);
  await assert.rejects(run('recalculatePending()'), /локальный OSRM/);
  assert.equal(run('state.pendingRequests.length'), 1);
});

test('Gantt selection shows inclusive prefix, never mutates the route or its color', async () => {
  const {run, element} = app();
  run(`state.plan={routes:[{technician_id:'a',technician_name:'Анна',start_location_id:'office',stops:[
    {request_id:'r1',location_id:'x',service_start_at:'2026-08-17T10:00:00+03:00'},
    {request_id:'r2',location_id:'y',service_start_at:'2026-08-17T11:00:00+03:00'},
    {request_id:'r3',location_id:'z',service_start_at:'2026-08-17T12:00:00+03:00'}]}]};
    state.input={technicians:[{id:'a',vehicle:'car'}]};
    renderRequests=renderTimeline=renderTechnicians=renderDecision=renderMap=()=>{};
    selectRequest('r2',true);`);
  assert.equal(run('selectedMapRoute().stops.length'), 2);
  assert.equal(run('state.plan.routes[0].stops.length'), 3);
  run(`var capturedStyle; createRouteLine=(points,style)=>{capturedStyle=style;return null;};
    var prefixLocations=new Map(['office','x','y','z'].map((id,i)=>[id,{latitude:55+i*.01,longitude:37}]));`);
  await run('drawRoadRoutes([selectedMapRoute()],prefixLocations,new AbortController(),mapRenderToken,true)');
  assert.equal(run('capturedStyle.color'), '#536b45');
  assert.match(element('map-caption').textContent, /до r2 включительно/);
  run("selectRequest('r1',true)");
  assert.equal(run('selectedMapRoute().stops.length'), 1);
  run("selectTechnician('a')");
  assert.equal(run('selectedMapRoute().stops.length'), 3);
});

test('drafts explain why demo events cannot apply and do not trap the button', () => {
  const {run,element} = app();
  element('replan-dialog').showModal = () => { element('replan-dialog').open = true; };
  run('state.plan={routes:[]};state.pendingRequests=[{}];openDemoEvents();');
  assert.equal(element('replan-dialog').open, true);
  assert.equal(element('demo-scenarios').disabled, true);
  assert.equal(element('demo-go-recalculate').hidden, false);
  assert.match(element('demo-pending-count').textContent, /Ожидают расчёта: 1/);
  run('state.pendingRequests=[];openDemoEvents()');
  assert.equal(element('apply-scenario').disabled, false);
});

test('editing an address invalidates coordinates and reuses only an exact known match', () => {
  const {run,element} = app();
  liveFixture(run,element);
  element('new-address').value = run('state.input.locations[1].address');
  run('updateAddressInput()');
  assert.equal(element('new-location').value, run('state.input.locations[1].id'));
  element('new-address').value = 'Совсем другой адрес';
  run('updateAddressInput()');
  assert.equal(element('new-location').value, '__new__');
  assert.equal(element('new-latitude').value, '');
  assert.equal(element('new-longitude').value, '');
});

test('stale geocoder response cannot replace the edited address', async () => {
  const {run,element,context} = app();
  liveFixture(run,element);
  run('state.planningConfig={geocoding_mode:"local"}');
  element('new-address').value = 'Новый адрес';
  let finish;
  context.fetch = () => new Promise(resolve => {finish=resolve;});
  const pending = run('findAddress()');
  element('new-address').value = 'Другой адрес';
  run('updateAddressInput()');
  finish({ok:true,json:async()=>({results:[{address:'Новый адрес',latitude:55,longitude:37}]})});
  await pending;
  assert.equal(element('address-results').hidden, true);
  assert.equal(element('new-latitude').value, '');
  assert.equal(element('find-address').disabled, false);
});

test('comparison distinguishes finished and active jobs from cancellations and time shifts', () => {
  const {run} = app();
  run(`var previous = {routes:[{technician_name:'Anna',stops:[
    {request_id:'done',sequence:1,service_start_at:'2026-08-17T09:00:00+03:00',service_end_at:'2026-08-17T10:00:00+03:00'},
    {request_id:'active',sequence:2,service_start_at:'2026-08-17T10:00:00+03:00',service_end_at:'2026-08-17T11:00:00+03:00'},
    {request_id:'later',sequence:3,service_start_at:'2026-08-17T12:00:00+03:00',service_end_at:'2026-08-17T13:00:00+03:00'},
    {request_id:'cancel',sequence:4,service_start_at:'2026-08-17T14:00:00+03:00',service_end_at:'2026-08-17T15:00:00+03:00'}]}],unassigned:[]};
    var next = {routes:[{technician_name:'Anna',stops:[{request_id:'later',sequence:1,service_start_at:'2026-08-17T12:30:00+03:00'}]}],unassigned:[]};`);
  const changes = run('comparePlans(previous,next,"2026-08-17T10:30:00+03:00")');
  assert.match(changes.find(x=>x.title.startsWith('done')).title, /завершена/);
  assert.match(changes.find(x=>x.title.startsWith('active')).title, /в работе/);
  assert.match(changes.find(x=>x.title.startsWith('cancel')).title, /отменена/);
  assert.match(changes.find(x=>x.title.startsWith('later')).title, /время/);
});

test('failed live recalculation retains drafts, selected request and current plan', async () => {
  const {context,run,element} = app();
  const values = liveFixture(run,element);
  run(`state.plan = {routes:[],unassigned:[],metrics:{assigned_count:0}}; state.selectedRequestId='LIVE-1'; state.pendingRequests.push(makePendingRequest(${JSON.stringify(values)}));`);
  context.fetch = async () => ({ok:false,json:async()=>({error:'Gateway offline'})});
  await assert.rejects(run('recalculatePending()'), /Gateway offline/);
  assert.equal(run('state.pendingRequests.length'), 1);
  assert.equal(run('state.input.requests.length'), 4);
  assert.equal(run('state.selectedRequestId'), 'LIVE-1');
  assert.equal(run('state.busy'), false);
});

test('snapshot keeps active work through consecutive replans and accepts multiple new IDs', () => {
  const {run,element} = app(); liveFixture(run,element);
  run(`state.input.replan_context = {active_work:[{technician_id:'TECH-1',request_id:'RUNNING',location_id:'client-a',finish_at:'2026-08-17T11:00:00+03:00'}]};
    prepareEventSnapshot(state.input,{routes:[]},['NEW-1','NEW-2'],'2026-08-17T10:30:00+03:00');`);
  assert.equal(run('state.input.replan_context.active_work.length'), 1);
  assert.equal(run('state.input.replan_context.new_request_ids.length'), 2);
});

test('snapshot freezes an arrived crew waiting for a window, including exact arrival', () => {
  const {run,element} = app(); liveFixture(run,element);
  run(`var beforeArrival = {routes:[{technician_id:'TECH-1',stops:[{request_id:'REQ-1',location_id:'client-a',arrival_at:'2026-08-17T09:20:00+03:00',service_start_at:'2026-08-17T10:00:00+03:00',service_end_at:'2026-08-17T11:10:00+03:00'}]}]};
    prepareEventSnapshot(state.input,beforeArrival,[],'2026-08-17T09:20:00+03:00');`);
  assert.equal(run('state.input.requests.some(r=>r.id==="REQ-1")'), false);
  assert.equal(run('state.input.replan_context.active_work[0].request_id'), 'REQ-1');
  assert.equal(run('state.input.technicians[0].start_location_id'), 'client-a');
  assert.equal(run('state.input.technicians[0].available_from'), '2026-08-17T08:10:00.000Z');
});

test('manual reassignment and cancellation cannot touch a crew already on site', () => {
  const {run} = app();
  run(`state.plan = {routes:[{stops:[{request_id:'A',arrival_at:'2026-08-17T09:20:00+03:00',service_start_at:'2026-08-17T10:00:00+03:00',service_end_at:'2026-08-17T11:10:00+03:00'}]}]};`);
  assert.throws(() => run('assertRequestNotStarted("A","2026-08-17T09:20:00+03:00")'), /уже на месте/);
  assert.doesNotThrow(() => run('assertRequestNotStarted("A","2026-08-17T09:19:00+03:00")'));
});

test('comparison baseline excludes frozen work, not cancelled future work', () => {
  const {run} = app();
  run(`var baseline = {metrics:{},unassigned:[],routes:[{stops:[
    {request_id:'A',arrival_at:'2026-08-17T09:20:00+03:00',distance_km:4,travel_minutes:10},
    {request_id:'B',arrival_at:'2026-08-17T12:00:00+03:00',distance_km:5,travel_minutes:20}]}]};
    var input = {requests:[],replan_context:{event_at:'2026-08-17T10:00:00+03:00'}};`);
  const result = run('comparisonBaseline(baseline,input)');
  assert.equal(result.metrics.assigned_count, 1);
  assert.equal(result.metrics.distance_km, 5);
});

test('recalculation time can be inside a busy crew interval', () => {
  const {run,element} = app(); liveFixture(run,element);
  run(`state.input.technicians.forEach(tech=>tech.available_from='2026-08-17T11:00:00+03:00'); state.lastEventAt='2026-08-17T10:00:00+03:00';`);
  element('event-at').value = '2026-08-17T10:30';
  assert.doesNotThrow(() => run('validatedEventAt()'));
  element('event-at').value = '2026-08-17T09:00';
  assert.throws(() => run('validatedEventAt()'), /назад/);
});

test('map readiness remains resolved while updated sources temporarily report not loaded', async () => {
  const {run} = app();
  run('var loadHandler; var loaded=false; mapInstance={loaded:()=>loaded,once:(name,handler)=>{loadHandler=handler;}};');
  const first = run('mapReady()');
  run('loaded=true; loadHandler();');
  await first;
  run('loaded=false;');
  assert.equal(run('mapReady()'), first);
});
