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
    runtime:{active_run_id:null, active_runs:[], variables:[]},
    cells:[cell(runningId,"order"),cell(codeId,"code"),cell(markdownId,"markdown"),cell(orderId,"order")]};
}

function finalDocument(doc, revision=3) {
  const latest = structuredClone(doc);
  latest.revision = revision;
  latest.runtime.active_run_id = null;
  latest.runtime.active_runs = [];
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
  const modal = {open:false, innerHTML:"", className:"", closeListeners:[],
    addEventListener(name, callback){if(name === "close")this.closeListeners.push(callback);},
    showModal(){this.open = true;},
    close(){if(!this.open)return;this.open = false;this.closeListeners.splice(0).forEach(callback=>callback());}};
  elements.set("#modal", modal);
  let nextTimer = 0;
  const sandbox = {
    document, renderHooks, structuredClone,
    window:{addEventListener(){}}, localStorage:{getItem(){},setItem(){}},
    setTimeout:callback => {timers.set(++nextTimer,callback);return nextTimer;},
    clearTimeout:id => timers.delete(id),
    requestAnimationFrame:callback => {callback();return 0;},
    setInterval:() => 0,
    MutationObserver:class {observe(){}},
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
    refreshOrderOutputs=async()=>{};refreshContext=()=>{};toast=()=>{};
    globalThis.app={state,window,cellLocked,canRun,anyBusy,codeBusy,liveFor,cellHTML,inputFileStateHTML,save,flushSave,watchRun,renderAll,
      outputsHTML,orderInputMode,updateOrderInput,markdown,inline,mathSpan,splitMath,
      mathState,mathScript,typeset,startMath,runCell,confirmOrderRun,settleOrderRunConfirm,
      orderRunConfirmationEnabled,orderRunConfirmBody};
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
  return {app,doc,edit,watch,finish,take,finishList,requests,streams,document,elements,renderHooks,sandbox};
}

test("only the running cell is locked, including during Run All", () => {
  const {app,doc} = makeApp();
  app.state.lives.set("run",{id:"run",cell_id:runningId,cell_type:"order",outputs:[]});
  doc.runtime.active_run_id = "run";
  doc.runtime.active_runs = [{id:"run",cell_id:runningId,cell_type:"order",operation:false}];
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
      // Other Orders stay runnable while an Order runs; Python/Markdown remain exclusive.
      const runButton = html.match(/<button\b[^>]*data-action="run"[^>]*>/)[0];
      if (!sequence && other.type==="order") assert.doesNotMatch(runButton,/\bdisabled\b/);
      else assert.match(runButton,/\bdisabled\b/);
    }
    assert.doesNotMatch(app.inputFileStateHTML(doc.cells[3]),/\bdisabled\b/);
  }
  app.state.sequence = false;
  app.state.lives.get("run").operation = {kind:"merge"};
  assert.equal(app.cellLocked(codeId),true);
  app.state.lives.clear(); // Reconnecting to an active run must not expose its input.
  assert.equal(app.cellLocked(runningId),true);
});

test("two Order runs stream in parallel and finish independently", async () => {
  const env = makeApp(), {app,doc,take} = env;
  const first = env.watch();
  const second = app.watchRun({id:"run2",notebook_id:nid,cell_id:orderId,
    cell_type:"order",status:"running",outputs:[],execution_count:2,context:{}});
  assert.equal(app.state.lives.size,2);
  assert.equal(app.cellLocked(runningId),true);
  assert.equal(app.cellLocked(orderId),true);
  assert.equal(app.cellLocked(codeId),false);
  assert.equal(app.canRun(codeId),false);
  assert.equal(app.codeBusy(),false);
  assert.equal(app.anyBusy(),true);
  env.edit(codeId,"source","Draft during parallel orders");
  env.streams[0].send("finished",{status:"succeeded"});
  const mid = finalDocument(structuredClone(doc));
  mid.runtime.active_run_id = "run2";
  mid.runtime.active_runs = [{id:"run2",cell_id:orderId,cell_type:"order",operation:false}];
  (await take("GET",`/api/notebooks/${nid}`)).respond(mid);
  const save = await take("PUT",`/api/notebooks/${nid}`);
  assert.equal(save.body.cells[1].source,"Draft during parallel orders");
  save.respond(savedDocument(mid,save.body));
  await first;
  assert.equal(app.state.lives.size,1);
  assert.equal(app.cellLocked(runningId),false);
  assert.equal(app.canRun(runningId),true);
  assert.equal(app.canRun(codeId),false);
  assert.equal(app.state.doc.cells[0].outputs[0].text,"Completed output");
  env.streams[1].send("finished",{status:"succeeded"});
  (await take("GET",`/api/notebooks/${nid}`)).respond(finalDocument(structuredClone(doc),4));
  await env.finishList();
  await second;
  assert.equal(app.state.lives.size,0);
  assert.equal(app.state.doc.runtime.active_run_id,null);
  assert.equal(app.canRun(codeId),true);
});

test("an Order run waits for the confirmation dialog before starting", async () => {
  const env = makeApp(), {app} = env;
  app.state.boot.settings = {confirm_order_run:true};
  const pending = app.runCell(orderId);
  await tick(); await tick();
  assert.equal(env.elements.get("#modal").open,true);
  assert.equal(env.requests.filter(request => request.method === "POST").length,0);
  app.settleOrderRunConfirm(false);
  const result = await pending;
  assert.equal(result.status,"cancelled");
  assert.equal(result.cancelled,true);
  assert.equal(result.cell_id,orderId);
  assert.equal(env.requests.filter(request => request.method === "POST").length,0);
});

test("the confirmation names the software, configured modes and target Order", async () => {
  const env = makeApp(), {app,doc} = env;
  Object.assign(app.state.boot.capabilities,{claude:true,claude_launch_mode:"print",
    claude_permission_mode:"default"});
  doc.cells[3].title = "审阅假设与下一步";
  const body = app.orderRunConfirmBody(doc.cells[3]);
  assert.match(body,/Claude Code/);
  assert.match(body,/print 非交互/);
  assert.match(body,/default（逐项询问）/);
  assert.match(body,/审阅假设与下一步/);
  assert.match(body,/output\.md/);
});

test("confirming starts the run, and settings can disable the prompt", async () => {
  const env = makeApp(), {app} = env;
  app.state.boot.settings = {confirm_order_run:true};
  let pending = app.runCell(orderId);
  await tick(); await tick();
  app.settleOrderRunConfirm(true);
  let request = await env.take("POST",`/api/notebooks/${nid}/cells/${orderId}/run`);
  request.respond({error:"test stop"},500);
  await assert.rejects(pending,/test stop/);

  app.state.boot.settings = {confirm_order_run:false};
  pending = app.runCell(orderId);
  request = await env.take("POST",`/api/notebooks/${nid}/cells/${orderId}/run`);
  assert.equal(env.elements.get("#modal").open,false);
  request.respond({error:"test stop"},500);
  await assert.rejects(pending,/test stop/);
});

test("Run All keeps starting cells while the sequence flag is set", async () => {
  const env = makeApp(), {app} = env;
  app.state.boot.settings = {confirm_order_run:false};
  app.state.sequence = true;
  const pending = app.runCell(orderId);
  const request = await env.take("POST",`/api/notebooks/${nid}/cells/${orderId}/run`);
  request.respond({id:"run",notebook_id:nid,cell_id:orderId,cell_type:"order",
    status:"running",outputs:[],execution_count:1,context:{}});
  await tick();
  env.streams[0].send("finished",{status:"succeeded"});
  (await env.take("GET",`/api/notebooks/${nid}`)).respond(finalDocument(env.doc));
  await env.finishList();
  const result = await pending;
  assert.equal(result.status,"succeeded");
  app.state.sequence = false;
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
  assert.equal(app.state.lives.size,0);
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

test("order input renders markdown whenever it is not being edited", () => {
  const env = makeApp(), {app} = env;
  const order = env.doc.cells[3];
  order.run_ids = ["r1"];
  order.source = "# 需求\n\n- 条目一\n- 条目二";
  assert.equal(app.orderInputMode(order),"preview");
  const html = app.cellHTML(order,3);
  assert.match(html, /<div class="order-input-preview markdown"/);
  assert.match(html, /<h1>需求<\/h1>/);
  assert.match(html, /<ul><li>条目一<\/li><li>条目二<\/li><\/ul>/);
  assert.doesNotMatch(html, /<textarea[^>]*id="source-/);
  app.state.orderEditing.add(order.id);
  assert.equal(app.orderInputMode(order),"edit");
  assert.match(app.cellHTML(order,3), /<textarea[^>]*id="source-/);
  app.state.orderEditing.delete(order.id);
  assert.match(app.cellHTML(order,3), /<div class="order-input-preview markdown"/);
});

test("an unrun order still previews its requirement and offers an edit toggle", () => {
  const env = makeApp(), {app} = env;
  const order = env.doc.cells[3];
  order.source = "## 需求\n\n公式 $E = mc^2$ 也应渲染。";
  assert.equal(order.run_ids.length, 0);
  assert.equal(app.orderInputMode(order),"preview");
  const html = app.cellHTML(order,3);
  assert.match(html, /<div class="order-input-preview markdown"/);
  assert.match(html, /<h2>需求<\/h2>/);
  assert.match(html, /<span class="math-tex"/);
  assert.match(html, /data-action="edit-order"/);
  assert.doesNotMatch(html, /<textarea[^>]*id="source-/);
  app.state.orderEditing.add(order.id);
  assert.equal(app.orderInputMode(order),"edit");
  const editing = app.cellHTML(order,3);
  assert.match(editing, /<textarea[^>]*id="source-/);
  assert.match(editing, /data-action="done-order"/);
  app.state.orderEditing.delete(order.id);
  assert.equal(app.orderInputMode(order),"preview");
});

test("order outputs render markdown evidence carried as json payloads", () => {
  const {app} = makeApp();
  const md = app.outputsHTML([{kind:"json",text:"# 板块 01\n\n- **结论**：AUROC 0.610"}]);
  assert.match(md, /<div class="markdown rendered-output">/);
  assert.match(md, /<h1>板块 01<\/h1>/);
  assert.match(md, /<strong>结论<\/strong>/);
  const fenced = app.outputsHTML([{kind:"json",text:'```json\n{"a":1}\n```'}]);
  assert.match(fenced, /<pre class="output-text /);
  assert.doesNotMatch(fenced, /rendered-output/);
});

test("typeset hands KaTeX the TeX body, never the delimiters", () => {
  const env = makeApp(), {app} = env;
  const calls = [];
  env.sandbox.katex = {render(tex, node, options) { calls.push({tex, display:options.displayMode}); }};
  app.window.katex = env.sandbox.katex;
  app.mathState.ready = true;
  const spans = [
    {dataset:{tex:"E = mc^2", display:"false"}, removeAttribute(name){delete this.dataset.tex;}},
    {dataset:{tex:"\\int_0^1 f", display:"true"}, removeAttribute(name){delete this.dataset.tex;}}
  ];
  app.typeset({querySelectorAll: () => spans});
  assert.deepEqual(calls, [{tex:"E = mc^2", display:false}, {tex:"\\int_0^1 f", display:true}]);
  assert.equal(spans[0].dataset.typeset, "1");
  assert.equal(spans[0].dataset.tex, undefined);
  app.mathState.ready = false;
});

test("display formulas may put the delimiters on their own lines", () => {
  const {app} = makeApp();
  const dollars = app.markdown("前文\n\n$$\n\\sum_i x_i\n$$\n\n后文");
  assert.match(dollars, /data-display="true"/);
  assert.ok(dollars.includes('data-tex="\n\\sum_i x_i\n"'), dollars);
  assert.match(dollars, /后文/);
  const spaced = app.markdown("$$\n  \\frac{1}{2}\n  $$");
  assert.match(spaced, /data-tex="\n  \\frac\{1\}\{2\}\n  "/);
  const brackets = app.markdown("\\[\n\\alpha \\ge \\beta\n\\]");
  assert.match(brackets, /data-display="true"/);
  assert.ok(brackets.includes("alpha"), brackets);
  const unclosed = app.markdown("$$\nsum\n\n# 标题");
  assert.match(unclosed, /<h1>标题<\/h1>/);
  assert.doesNotMatch(unclosed, /data-tex="[^"]*sum[^"]*标题/);
});

const formulaCell = (source) => ({...cell(markdownId,"markdown"), source});
const B = String.fromCharCode(92); // one literal backslash

// Rendered markup for a formula: the KaTeX-ready body stays in data-tex until
// the engine runs; the delimiters only remain in the visible fallback text.
const D = "$"; // one dollar sign

test("markdown keeps inline and display formulas as typeset placeholders", () => {
  const {app} = makeApp();
  const inline = app.markdown(`风险指标满足 ${D}sigma^2 = ${B}sum x_i${D} 的关系。`);
  assert.ok(inline.includes(`data-tex="sigma^2 = ${B}sum x_i"`), inline);
  assert.match(inline, /data-display="false"/);
  assert.ok(inline.includes(`${D}sigma^2 = ${B}sum x_i${D}`), "fallback keeps the source");
  assert.doesNotMatch(inline, /<em>/);
  const display = app.markdown(`${D}${D}${B}int_0^1 f(x)${B},dx = 1${D}${D}`);
  assert.match(display, /data-display="true"/);
  assert.ok(display.includes(`data-tex="${B}int_0^1 f(x)${B},dx = 1"`), display);
  assert.ok(display.includes(`${D}${D}${B}int_0^1 f(x)${B},dx = 1${D}${D}`), display);
  const bracket = app.markdown(`${B}${B}[ ${B}alpha ${B}ge ${B}beta ${B}${B}]`);
  assert.match(bracket, /data-display="true"/);
  assert.ok(bracket.includes(`data-tex=" ${B}alpha ${B}ge ${B}beta ${B}"`), bracket);
  const paren = app.markdown(`${B}${B}(${B}lambda ${B}${B})`);
  assert.match(paren, /data-display="false"/);
});

test("dollar amounts and code spans never become formulas", () => {
  const {app} = makeApp();
  assert.doesNotMatch(app.markdown("价格 $100 与 $200 不应渲染。"), /math-tex/);
  const code = app.markdown("行内 `cost = $5` 保持字面量。");
  assert.match(code, /<code>cost = \$5<\/code>/);
  const fenced = app.markdown("```python\nprice = 100 # $100\n```");
  assert.match(fenced, /<pre><code>price = 100 # \$100<\/code><\/pre>/);
  assert.doesNotMatch(fenced, /math-tex/);
});

test("markdown renders images without letting raw HTML through", () => {
  const {app} = makeApp();
  const remote = app.markdown("![图](https://example.com/a.png?x=1&y=2)");
  assert.ok(remote.includes('<img class="markdown-image"'), remote);
  assert.ok(remote.includes('src="https://example.com/a.png?x=1&amp;y=2"'), remote);
  assert.ok(remote.includes('alt="图"'), remote);
  const local = app.markdown("![](docs/ui preview.png)");
  assert.ok(local.includes(`src="/api/notebooks/${nid}/asset?path=docs%2Fui%20preview.png"`), local);
  const data = app.markdown("![x](data:image/png;base64,iVBORw0KGgo=)");
  assert.ok(data.includes('src="data:image/png;base64,iVBORw0KGgo="'), data);
  const attachments = {"image-abc.png": {"image/png": "iVBORw0KGgo="}};
  const embedded = app.markdown("![x](attachment:image-abc.png)", attachments);
  assert.ok(embedded.includes('src="data:image/png;base64,iVBORw0KGgo="'), embedded);
  assert.doesNotMatch(app.markdown("![x](attachment:missing.png)", attachments), /<img/);
  assert.doesNotMatch(app.markdown("![x](attachment:image-abc.png)",
    {"image-abc.png": {"image/svg+xml": "PHN2Zz4="}}), /<img/);
  const linked = app.markdown("[![x](https://example.com/a.png)](https://example.com)");
  assert.match(linked, /<a href="https:\/\/example\.com"[^>]*><img class="markdown-image"/);
  const escaped = app.markdown('![<img src=x onerror=alert(1)>](https://example.com/a.png)');
  assert.doesNotMatch(escaped, /<img src=x/);
  assert.match(escaped, /alt="&lt;img src=x onerror=alert\(1\)&gt;"/);
  for (const source of ["![x](javascript:alert(1))", "![x](../secret.png)", "![x](C:/secret.png)", "![x](file:///etc/passwd)"]) {
    assert.doesNotMatch(app.markdown(source), /<img/, source);
  }
  assert.match(app.markdown("`![x](https://example.com/a.png)`"),
    /<code>!\[x\]\(https:\/\/example\.com\/a\.png\)<\/code>/);
  assert.doesNotMatch(app.markdown("```\n![x](https://example.com/a.png)\n```"), /<img/);
});

test("markdown cell previews render images stored as notebook attachments", () => {
  const {app, doc} = makeApp();
  const cell = doc.cells.find(c => c.id === markdownId);
  cell.source = "![x](attachment:image-a.png)";
  cell.attachments = {"image-a.png": {"image/png": "iVBORw0KGgo="}};
  const html = app.cellHTML(cell, doc.cells.indexOf(cell));
  assert.match(html, /<img class="markdown-image" src="data:image\/png;base64,iVBORw0KGgo="/);
});

test("mathScript waits for the local status before loading KaTeX", async () => {
  const env = makeApp(), {app} = env;
  const loading = app.mathScript();
  await tick();
  const status = env.requests.find(r => r.path === "/api/math/status");
  assert.ok(status, "mathScript must ask this process for readiness");
  assert.equal(env.requests.some(r => r.path.endsWith("katex.min.js")), false,
    "no engine fetch before the status says ready");
  status.respond({ready:false, downloading:true});
  await loading;
  assert.equal(app.mathState.ready, false);
});

test("a ready status loads KaTeX from this process only", async () => {
  const env = makeApp(), {app} = env;
  const created = [];
  const make = tag => {
    const node = {tag, dataset:{}, setAttribute(){}, removeAttribute(){}};
    node.onload = node.onerror = null;
    created.push(node);
    return node;
  };
  env.document.createElement = make;
  env.document.getElementById = () => null;
  env.document.head = {appendChild(node) { if (node.onload) node.onload(); }};
  app.window.katex = {render(){}}; // the engine the served script would define
  const loading = app.mathScript();
  await tick();
  env.requests.find(r => r.path === "/api/math/status").respond({ready:true, downloading:false, missing:[]});
  await loading;
  await tick();
  assert.ok(created.some(node => node.tag === "script" && node.src === "/api/math/katex.min.js"));
  assert.ok(created.some(node => node.tag === "link" && node.href === "/api/math/katex.min.css"));
  assert.equal(app.mathState.ready, true);
});
