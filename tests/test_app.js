"use strict";

// Exercise the shipped frontend with deterministic HTTP/SSE ordering. No browser,
// model calls or third-party packages are needed: node --test tests/test_app.js.
const assert = require("node:assert/strict");
const {readFileSync} = require("node:fs");
const {test} = require("node:test");
const vm = require("node:vm");

const source = readFileSync(require.resolve("../castle_notebook/static/app.js"), "utf8");
const nid = "111111111111", runningId = "aaaaaaaaaaaa", codeId = "bbbbbbbbbbbb";
const markdownId = "cccccccccccc", orderId = "dddddddddddd";
const tick = () => new Promise(resolve => setImmediate(resolve));

function cell(id, type) {
  return {id, type, title:type, source:`original ${type}`, expected_output:"Markdown",
    output_format:"markdown", mode:"llm", context_note:"", revision:1,
    status:"idle", stale:false, outputs:[], run_ids:[]};
}

function documentFixture() {
  return {id:nid, title:"Concurrent editing", revision:2, project_path:"/project",
    global:{revision:1, goal:"Original goal", constraints:""},
    runtime:{active_run_id:null, variables:[]},
    cells:[cell(runningId,"order"),cell(codeId,"code"),cell(markdownId,"markdown"),cell(orderId,"order")]};
}

function finalDocument(doc, revision=3) {
  const latest = structuredClone(doc);
  latest.revision = revision;
  latest.runtime.active_run_id = null;
  Object.assign(latest.cells[0], {status:"succeeded", source:"Model-updated requirements",
    outputs:[{kind:"markdown", text:"Completed output"}], last_run_id:"run", run_ids:["run"]});
  return latest;
}

function savedDocument(doc, payload) {
  const saved = structuredClone(doc);
  saved.revision++;
  saved.title = payload.title;
  Object.assign(saved.global, payload.global);
  for (const item of payload.cells) {
    const target = saved.cells.find(c => c.id === item.id);
    for (const key of ["type","title","source","expected_output","output_format","mode","context_note"])
      target[key] = item[key];
  }
  return saved;
}

function makeApp() {
  const requests = [], streams = [], listeners = new Map(), elements = new Map(), timers = new Map();
  const renderHooks = {};
  const document = {activeElement:null, querySelector:selector => elements.get(selector) || null,
    querySelectorAll:() => [], addEventListener:(name, callback) => listeners.set(name, callback)};
  elements.set("#modal", {open:false, addEventListener(){}});
  let nextTimer = 0;
  const sandbox = {
    document, renderHooks, structuredClone,
    window:{addEventListener(){}}, localStorage:{getItem(){},setItem(){}},
    setTimeout:callback => {timers.set(++nextTimer,callback);return nextTimer;},
    clearTimeout:id => timers.delete(id),
    fetch:(path, options={}) => {
      // Leave startup pending so each test controls the document explicitly.
      if (path === "/api/bootstrap") return new Promise(() => {});
      return new Promise(resolve => requests.push({path, method:options.method || "GET",
        body:options.body && JSON.parse(options.body),
        respond:(data,status=200) => resolve({ok:status<400,status,json:async()=>structuredClone(data)})}));
    },
    EventSource:class {
      constructor(){streams.push(this);}
      close(){this.closed=true;}
      send(type,data){this.onmessage({data:JSON.stringify({type,data})});}
    }
  };
  vm.createContext(sandbox);
  vm.runInContext(source + `
    renderSidebar=()=>{};renderNotebook=()=>{};
    inspectorFrame=()=>renderHooks.inspector?.();
    refreshOrderOutputs=async()=>{};refreshContext=()=>{};updateOrderInput=()=>{};toast=()=>{};
    globalThis.app={state,cellLocked,cellHTML,inputFileStateHTML,save,flushSave,watchRun,renderAll};
  `, sandbox);
  const app = sandbox.app, doc = documentFixture();
  app.state.doc = doc;
  app.state.boot = {token:"fixture",notebooks:[{id:nid,title:doc.title}],capabilities:{claude:true}};
  const edit = (id,field,value) => listeners.get("input")({target:{dataset:{cell:id,field},value,
    tagName:"TEXTAREA",style:{},scrollHeight:32,classList:{contains:()=>false}}});
  const watch = () => app.watchRun({id:"run",notebook_id:nid,cell_id:runningId,
    cell_type:"order",status:"running",outputs:[],execution_count:1,context:{}});
  const finish = () => streams.at(-1).send("finished",{status:"succeeded"});
  const take = async (method,path) => {
    for(let i=0;i<20;i++) {
      const request = requests.find(r=>!r.taken && r.method===method && r.path===path);
      if(request){request.taken=true;return request;}
      await tick();
    }
    assert.fail(`Missing ${method} ${path}; saw ${requests.map(r=>r.method+" "+r.path).join(", ")}`);
  };
  const finishList = async () => (await take("GET","/api/notebooks")).respond([{id:nid,title:doc.title}]);
  return {app,doc,edit,watch,finish,take,finishList,requests,document,elements,renderHooks};
}

test("only the running cell is locked, including during Run All", () => {
  const {app,doc} = makeApp();
  app.state.live = {id:"run",cell_id:runningId};
  doc.runtime.active_run_id = "run";
  doc.cells[3].run_ids = ["earlier"];
  app.state.markdownEditing.add(markdownId);
  app.state.orderEditing.add(orderId);
  for (const sequence of [false,true]) {
    app.state.sequence = sequence;
    assert.equal(app.cellLocked(runningId),true);
    for (const other of doc.cells.slice(1)) {
      assert.equal(app.cellLocked(other.id),false);
      const html = app.cellHTML(other,doc.cells.indexOf(other));
      const inputs = html.match(/<(?:input|textarea|select)\b[^>]*data-field="[^"]+"[^>]*>/g);
      assert.ok(inputs.length);
      for (const input of inputs) assert.doesNotMatch(input,/\bdisabled\b/);
      assert.match(html,/<button\b[^>]*data-action="run"[^>]*\bdisabled\b/);
    }
    assert.doesNotMatch(app.inputFileStateHTML(doc.cells[3]),/\bdisabled\b/);
  }
  app.state.live.operation = {kind:"merge"};
  assert.equal(app.cellLocked(codeId),true);
  app.state.live = null; // Reconnecting to an active run must not expose its input.
  assert.equal(app.cellLocked(runningId),true);
});

test("completion rebases unsaved edits without replacing model-written requirements", async () => {
  const env = makeApp(), {app,edit,take} = env, finished = env.watch();
  const server = structuredClone(env.doc);
  app.state.selected = codeId;
  edit(codeId,"source","Draft before completion");
  env.finish();
  const reload = await take("GET",`/api/notebooks/${nid}`);
  edit(codeId,"source","Typed while completion reloads");
  const manualSave = app.flushSave();
  await tick();
  assert.equal(env.requests.filter(r=>r.method==="PUT").length,0);
  const latest = finalDocument(server);
  reload.respond(latest);
  const save = await take("PUT",`/api/notebooks/${nid}`);
  assert.equal(save.body.revision,latest.revision);
  assert.equal(save.body.cells[0].source,"Model-updated requirements");
  assert.equal(save.body.cells[1].source,"Typed while completion reloads");
  save.respond(savedDocument(latest,save.body));
  await env.finishList();
  await Promise.all([finished,manualSave]);
  assert.equal(app.state.doc.cells[1].source,"Typed while completion reloads");
  assert.equal(app.state.doc.cells[0].outputs[0].text,"Completed output");
  assert.equal(app.state.selected,codeId);
  assert.equal(app.state.dirty,false);
});

test("an in-flight save and newer keystrokes wait for the final revision", async () => {
  const env = makeApp(), {app,edit,take} = env, finished = env.watch();
  const server = structuredClone(env.doc);
  edit(codeId,"source","First draft");
  const saving = app.save();
  const first = await take("PUT",`/api/notebooks/${nid}`);
  edit(codeId,"source","Newer draft");
  env.finish();
  first.respond(savedDocument(server,first.body));
  const reload = await take("GET",`/api/notebooks/${nid}`);
  assert.equal(env.requests.filter(r=>r.method==="PUT").length,1,
    "The second save must not use a revision predating completion");
  const latest = finalDocument(server,4);
  reload.respond(latest);
  const second = await take("PUT",`/api/notebooks/${nid}`);
  assert.equal(second.body.revision,4);
  assert.equal(second.body.cells[1].source,"Newer draft");
  second.respond(savedDocument(latest,second.body));
  await env.finishList();
  await Promise.all([finished,saving]);
  assert.equal(app.state.doc.cells[1].source,"Newer draft");
  assert.equal(app.state.dirty,false);
});

test("a save rejected by completion keeps the Order draft and retries on the latest revision", async () => {
  const env = makeApp(), {app,edit,take} = env, finished = env.watch();
  Object.assign(env.doc.cells[3], {run_ids:["earlier"],file_input:{sha256:"old-hash"}});
  const server = structuredClone(env.doc);
  edit(orderId,"source","Order requirements draft");
  edit(orderId,"expected_output","JSON report");
  const rejected = assert.rejects(app.save(),error=>error.status===409);
  const first = await take("PUT",`/api/notebooks/${nid}`);
  env.finish();
  first.respond({error:"Document revision changed"},409);
  const latest = finalDocument(server);
  (await take("GET",`/api/notebooks/${nid}`)).respond(latest);
  const retry = await take("PUT",`/api/notebooks/${nid}`);
  assert.equal(retry.body.revision,latest.revision);
  assert.equal(retry.body.cells[3].source,"Order requirements draft");
  assert.equal(retry.body.cells[3].expected_output,"JSON report");
  assert.equal(retry.body.cells[3].input_edited,true);
  assert.equal(retry.body.cells[3].input_file_sha256,"old-hash");
  retry.respond(savedDocument(latest,retry.body));
  await env.finishList();
  await Promise.all([finished,rejected]);
  assert.equal(app.state.dirty,false);
  assert.equal(app.state.inputEditBases.size,0);
});

test("a real input-file conflict during completion retains the draft and latest output", async () => {
  const env = makeApp(), {app,edit,take} = env;
  Object.assign(env.doc.cells[3], {run_ids:["earlier"],file_input:{sha256:"old-hash"}});
  const server = structuredClone(env.doc);
  const rejected = assert.rejects(env.watch(),error=>error.status===409);
  edit(orderId,"source","My unsaved requirements");
  env.finish();
  const latest = finalDocument(server);
  Object.assign(latest.cells[3], {source:"External requirements",file_input:{sha256:"new-hash"}});
  (await take("GET",`/api/notebooks/${nid}`)).respond(latest);
  const save = await take("PUT",`/api/notebooks/${nid}`);
  assert.equal(save.body.cells[3].input_file_sha256,"old-hash");
  save.respond({error:"Requirements file changed"},409);
  await rejected;
  assert.equal(app.state.doc.cells[3].source,"My unsaved requirements");
  assert.equal(app.state.doc.cells[0].outputs[0].text,"Completed output");
  assert.equal(app.state.dirty,true);
  assert.equal(app.state.live,null);
  assert.equal(app.state.runRefresh,null);
});

test("completion rendering restores the editor and cursor after the inspector rebuilds", () => {
  const env = makeApp(), {app,document,elements,renderHooks} = env;
  document.activeElement = {dataset:{cell:codeId,field:"context_note"},
    selectionStart:4,selectionEnd:9,scrollTop:37,scrollLeft:0};
  const details = {open:false};
  const target = {disabled:false,closest:()=>details,
    focus:options=>{document.activeElement=target;assert.equal(options.preventScroll,true);},
    setSelectionRange:(start,end)=>{target.selectionStart=start;target.selectionEnd=end;}};
  renderHooks.inspector = () => elements.set(`[data-cell="${codeId}"][data-field="context_note"]`,target);
  app.renderAll();
  assert.equal(document.activeElement,target);
  assert.equal(target.selectionStart,4);
  assert.equal(target.selectionEnd,9);
  assert.equal(target.scrollTop,37);
  assert.equal(details.open,true);
});
