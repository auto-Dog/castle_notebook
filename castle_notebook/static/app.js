"use strict";

const $ = (selector, root = document) => root.querySelector(selector);
const h = (value) => String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const state = {boot:null, doc:null, selected:null, tab:"context", context:null, contextRequest:0,
  projectRequest:0,
  outputNotebook:null, outputFingerprint:"", outputInFlight:false, outputDisconnected:false,
  dirty:false, globalDirty:false, documentDirty:false, dirtyFields:new Set(), editSeq:0, savePromise:null, timer:null, live:null, source:null, runRefresh:null,
  sequence:false, sequenceStop:false, markdownEditing:new Set(), orderEditing:new Set(),
  inputEditBases:new Map(), inputConflicts:new Map(), savingInputs:new Map(), expandedOrderOutputs:new Set(), inspectorOpen:false};

const paths = {
  merge:'m4 4 6 6m10-6-6 6m-2 0v11m-4-4 4 4 4-4',
  split:'M12 21V10m0 0L5 3m7 7 7-7M5 8V3h5m4 0h5v5',
  plus:'M12 5v14M5 12h14', play:'m8 5 11 7-11 7V5Z', stop:'M6 6h12v12H6z',
  chevron:'m9 5 7 7-7 7', down:'m5 9 7 7 7-7', up:'m5 15 7-7 7 7',
  notebook:'M5 3h14v18H5V3Zm4 0v18M12 8h4m-4 4h4',
  code:'m8 7-5 5 5 5m8-10 5 5-5 5m-3-12-2 14',
  order:'m12 3 2.5 6.5L21 12l-6.5 2.5L12 21l-2.5-6.5L3 12l6.5-2.5L12 3Z',
  markdown:'M4 6v12M4 6l5 7 5-7v12m3-4 3 4 3-4m-3-8v12',
  target:'M21 12a9 9 0 1 1-9-9m0 4a5 5 0 1 0 5 5m-5 0 9-9m-4 0h4v4',
  folder:'M3 7V5h6l2 2h10v13H3V7Z', clock:'M12 8v5l3 2m6-3a9 9 0 1 1-18 0 9 9 0 0 1 18 0',
  download:'M12 3v12m-5-5 5 5 5-5M4 16v5h16v-5', refresh:'M20 8a8 8 0 1 0 0 8m0-8V3m0 5h-5',
  check:'m5 12 4 4L19 6', close:'m6 6 12 12M6 18 18 6', trash:'M3 6h18M9 6V3h6v3M5 6l1 15h12l1-15M10 10v7m4-7v7',
  copy:'M9 8h12v13H9V8ZM5 16H3V3h12v2', eye:'M2 12s4-7 10-7 10 7 10 7-4 7-10 7S2 12 2 12Zm13 0a3 3 0 1 1-6 0 3 3 0 0 1 6 0',
  info:'M12 11v6m0-10v.1m9 4.9a9 9 0 1 1-18 0 9 9 0 0 1 18 0',
  panel:'M3 4h18v16H3V4Zm12 0v16', arrow:'M4 12h16m-5-5 5 5-5 5',
  terminal:'m5 6 6 6-6 6m8 0h6', spark:'m12 3 2 7 7 2-7 2-2 7-2-7-7-2 7-2 2-7',
  link:'m10 13 4-4m-6 7-2 2a4 4 0 0 1-6-6l5-5a4 4 0 0 1 6 0m2-2 2-2a4 4 0 0 1 6 6l-5 5a4 4 0 0 1-6 0',
  warning:'m12 3 10 18H2L12 3Zm0 6v5m0 3v.2', menu:'M4 6h16M4 12h16M4 18h16',
  save:'M4 3h13l4 4v14H3V3h1Zm3 0v6h10V3M7 21v-8h10v8',
};
const icon = (name, cls="") => `<svg class="icon ${cls}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="${paths[name] || paths.info}"/></svg>`;
const timeText = (value, full=false) => value ? new Date(value).toLocaleString("zh-CN", full ? {month:"2-digit",day:"2-digit",hour:"2-digit",minute:"2-digit",second:"2-digit",hour12:false} : {hour:"2-digit",minute:"2-digit",second:"2-digit",hour12:false}) : "—";
const typeLabel = {code:"PYTHON", markdown:"MARKDOWN", order:"ORDER"};
const statusLabel = {idle:"待执行", running:"执行中", succeeded:"已完成", failed:"执行失败", cancelled:"已停止", interrupted:"已中断"};
const launchModeLabel = {background:"原生后台", print:"print 非交互"};
const permissionModeLabel = {bypassPermissions:"bypassPermissions（跳过审批）", auto:"auto（自动审批）",
  acceptEdits:"acceptEdits（自动接受编辑）", default:"default（逐项询问）",
  plan:"plan（只读规划）", dontAsk:"dontAsk（不主动询问）"};
const codexSandboxLabel = {bypassPermissions:"跳过沙箱", auto:"workspace-write（自动审批）",
  acceptEdits:"workspace-write（自动审批）", dontAsk:"workspace-write（自动审批）",
  default:"只读沙箱", plan:"只读沙箱"};
// Old documents and cells may lack provider; they always follow the server default.
const providerLabel = name => name==="codex"?"Codex":name==="claude"?"Claude":String(name||"AI");
const providerName = cell => {const raw=cell?.provider||"default";return raw==="default"?(state.boot?.capabilities?.default_provider||"claude"):raw;};
const providerAvailable = cell => Boolean(state.boot?.capabilities?.[providerName(cell)]);
const busy = () => Boolean(state.live || state.doc?.runtime?.active_run_id || state.runRefresh);
const cellLocked = id => Boolean(state.live?.operation || state.live?.cell_id===id ||
  (state.doc?.runtime?.active_run_id && !state.live));
const currentCell = () => state.doc?.cells.find(c => c.id === state.selected);
const newId = () => crypto.randomUUID().replaceAll("-", "").slice(0,12);

async function api(path, method="GET", data) {
  const response = await fetch(path, {method, headers:{...(method !== "GET" ? {"Content-Type":"application/json", "X-Castle-Token":state.boot.token} : {})}, body:data === undefined ? undefined : JSON.stringify(data)});
  let result;
  try { result = await response.json(); } catch { throw new Error("服务返回了无法读取的响应，请检查本地服务。"); }
  if (!response.ok) {const error=new Error(result.error || `请求失败 (${response.status})`);error.status=response.status;throw error;}
  return result;
}

let toastTimer;
function toast(message, error=false) {
  const el = $("#toast");
  el.textContent = message;
  el.className = `visible ${error ? "error" : ""}`;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.className = "", error ? 7000 : 3500);
}

// A deliberately small, escaped Markdown renderer. Raw HTML is never inserted.
// Formulas are lifted out before escaping, then KaTeX replaces the placeholder
// span once the bundled engine has loaded (see typeset()).
// Delimiters are recognised literally; lookbehind would reject `\[` itself.
const MATH_RE = /\$\$([\s\S]+?)\$\$|\\\[([\s\S]+?)\\\]|\\\(([\s\S]+?)\\\)|(?<![\\\w$])\$([^$\n]+?)\$(?![\w$])/g;
function splitMath(text) {
  const parts = [], found = [];
  let offset = 0;
  text = String(text); MATH_RE.lastIndex = 0;
  for (const match of text.matchAll(MATH_RE)) {
    if (match.index > offset) parts.push({text:text.slice(offset, match.index)});
    const display = match[1] !== undefined || match[2] !== undefined;
    const body = match[1] ?? match[2] ?? match[3] ?? match[4];
    // KaTeX expects the TeX body, never the delimiters that framed it.
    found.push({tex:body, display});
    parts.push({tex:found.length - 1}); offset = match.index + match[0].length;
  }
  if (offset < text.length) parts.push({text:text.slice(offset)});
  return {parts, found};
}
// Images stay passive: Jupyter-style attachments embedded in the notebook,
// remote http(s), inline data:image, or a project-relative file served by
// /api/notebooks/<id>/asset. Other schemes are left as text.
const DATA_IMAGE_RE = /^data:image\/(?:png|jpe?g|gif|webp|avif|bmp);base64,[A-Za-z0-9+/=]+$/i;
const ATTACHMENT_MIME_RE = /^image\/(?:png|jpe?g|gif|webp|avif|bmp)$/i;
function attachmentURL(name, attachments) {
  const entry = attachments?.[name];
  if (!entry || typeof entry !== "object") return null;
  for (const [mime, data] of Object.entries(entry)) {
    if (ATTACHMENT_MIME_RE.test(mime) && typeof data === "string" && /^[A-Za-z0-9+/]+={0,2}$/.test(data))
      return `data:${mime.toLowerCase()};base64,${data}`;
  }
  return null;
}
function imageURL(source, attachments = {}) {
  const src = String(source || "").trim().replaceAll("\\", "/");
  if (!src) return null;
  if (/^attachment:/i.test(src)) return attachmentURL(src.slice(11), attachments);
  if (/^https?:\/\/[^\s"<>]+$/i.test(src) || DATA_IMAGE_RE.test(src)) return src;
  // Absolute, scheme-bearing or escaping paths never reach the project.
  if (/^[a-z][a-z0-9+.-]*:/i.test(src) || src.startsWith("/") || src.split("/").includes("..")) return null;
  const id = state.doc?.id;
  return id ? `/api/notebooks/${encodeURIComponent(id)}/asset?path=${encodeURIComponent(src)}` : null;
}
function inline(text, attachments = {}) {
  const {parts, found} = splitMath(text), tokens = [];
  const save = html => `\u0000${tokens.push(html) - 1}\u0000`;
  const esc = value => {
    // Code spans are lifted first so `![alt](url)` stays literal inside them.
    let out = String(value).replace(/`([^`]+)`/g, (_,v) => save(`<code>${h(v)}</code>`))
      .replace(/!\[([^\]]*)\]\(([^)]+)\)/g, (match, alt, src) => {
        const url = imageURL(src, attachments);
        if (!url) return match;
        const label = alt.replace(/\u0000\d+\u0000/g, "");
        return save(`<img class="markdown-image" src="${h(url)}" alt="${h(label)}" loading="lazy" referrerpolicy="no-referrer">`);
      });
    out = h(out).replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>')
      .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
      .replace(/(?<!\*)\*([^*]+)\*(?!\*)/g, "<em>$1</em>");
    return out.replace(/\u0000(\d+)\u0000/g, (_,i) => tokens[Number(i)]);
  };
  return parts.map(part => part.tex === undefined ? esc(part.text) : mathSpan(found[part.tex])).join("");
}
function mathSpan(source) {
  const display = source.display ? "true" : "false";
  const literal = source.display ? `$$${source.tex}$$` : `$${source.tex}$`;
  return `<span class="math-tex" data-tex="${h(source.tex)}" data-display="${display}">${h(literal)}</span>`;
}
function markdown(source, attachments = {}) {
  const lines = String(source).replaceAll("\r", "").split("\n");
  let result = "", i=0;
  while (i < lines.length) {
    const line = lines[i], trimmed = line.trim();
    if (trimmed.startsWith("```")) {
      const language = trimmed.slice(3); const code=[]; i++;
      while (i < lines.length && !lines[i].trim().startsWith("```")) code.push(lines[i++]);
      result += `<div class="md-code"><span>${h(language || "code")}</span><pre><code>${h(code.join("\n"))}</code></pre></div>`; i++; continue;
    }
    // Display math delimiters may sit alone on their lines ($$ / $$ or \[ / \]).
    if (trimmed.startsWith("$$") && trimmed.indexOf("$$", 2) === -1) {
      const end = mathBlockEnd(lines, i, "$$");
      if (end !== -1) { result += `<p>${inline(lines.slice(i, end + 1).join("\n"), attachments)}</p>`; i = end + 1; continue; }
    }
    if (trimmed.startsWith("\\[") && !trimmed.includes("\\]")) {
      const end = mathBlockEnd(lines, i, "\\]");
      if (end !== -1) { result += `<p>${inline(lines.slice(i, end + 1).join("\n"), attachments)}</p>`; i = end + 1; continue; }
    }
    if (/^#{1,6}\s/.test(line)) { const level = line.match(/^#+/)[0].length; result += `<h${level}>${inline(line.replace(/^#+\s/, ""), attachments)}</h${level}>`; i++; continue; }
    if (!trimmed) { i++; continue; }
    if (/^\s*>/.test(line)) { result += `<blockquote>${inline(line.replace(/^\s*>\s?/, ""), attachments)}</blockquote>`; i++; continue; }
    if (/^\s*([-*]|\d+\.)\s/.test(line)) {
      const ordered = /^\s*\d+\./.test(line); const tag=ordered ? "ol" : "ul"; result+=`<${tag}>`;
      while (i < lines.length && /^\s*([-*]|\d+\.)\s/.test(lines[i])) {
        const item=lines[i++].replace(/^\s*([-*]|\d+\.)\s/, ""), task=item.match(/^\[([ xX])\]\s+(.*)$/);
        result += task ? `<li class="task-list-item"><input type="checkbox" disabled ${task[1].toLowerCase()==="x"?"checked":""} aria-label="${task[1]===" "?"未完成":"已完成"}">${inline(task[2], attachments)}</li>` : `<li>${inline(item, attachments)}</li>`;
      }
      result += `</${tag}>`; continue;
    }
    if (line.includes("|") && i+1 < lines.length && /^\s*\|?\s*:?-{3,}/.test(lines[i+1])) {
      const split = row => row.trim().replace(/^\|/,"").replace(/\|$/,"").split("|");
      result += `<div class="table-wrap"><table><thead><tr>${split(line).map(v=>`<th>${inline(v.trim(), attachments)}</th>`).join("")}</tr></thead><tbody>`; i+=2;
      while(i<lines.length && lines[i].includes("|")) result += `<tr>${split(lines[i++]).map(v=>`<td>${inline(v.trim(), attachments)}</td>`).join("")}</tr>`;
      result += "</tbody></table></div>"; continue;
    }
    const para = [line]; i++;
    while (i<lines.length && lines[i].trim() && !/^(#{1,6}\s|```|\s*[-*>]\s|\s*\d+\.\s|\s*\$\$|\s*\\\[)/.test(lines[i])) para.push(lines[i++]);
    result += `<p>${para.map(part => inline(part, attachments)).join("<br>")}</p>`;
  }
  return result;
}
function mathBlockEnd(lines, start, close) {
  for (let i = start + 1; i < lines.length; i++) if (lines[i].includes(close)) return i;
  return -1;
}

// KaTeX assets are served from this process only after /api/math/status reports
// ready. Until then the spans above show their source, exactly as before.
const mathState = {loading:null, ready:false, retryAt:0};
const MATH_STYLE_ID = "castle-math-style";
function mathScript() {
  if (mathState.ready) return Promise.resolve(true);
  if (mathState.loading) return mathState.loading;
  mathState.loading = (async () => {
    const status = await api("/api/math/status").catch(() => null);
    if (!status?.ready) {
      // Poll fast while the background download runs; back off when offline.
      mathState.retryAt = Date.now() + (status?.downloading ? 1500 : 15000);
      return false;
    }
    await loadCss("/api/math/katex.min.css");
    await loadScript("/api/math/katex.min.js");
    if (!window.katex) {
      // A failed asset fetch must not turn the poll into a reload storm.
      mathState.retryAt = Date.now() + 15000;
      return false;
    }
    mathState.ready = true;
    return true;
  })().catch(() => false).finally(() => {mathState.loading = null;});
  return mathState.loading;
}
function loadCss(href) {
  if (document.getElementById(MATH_STYLE_ID)) return Promise.resolve();
  return new Promise(resolve => {
    const link = document.createElement("link");
    link.id = MATH_STYLE_ID; link.rel = "stylesheet"; link.href = href;
    link.onload = link.onerror = () => resolve();
    document.head.appendChild(link);
  });
}
function loadScript(src) {
  return new Promise(resolve => {
    const script = document.createElement("script");
    script.src = src; script.async = true;
    script.onload = script.onerror = () => resolve();
    document.head.appendChild(script);
  });
}
function typeset(root=document) {
  if (!mathState.ready || !window.katex) return;
  for (const span of root.querySelectorAll(".math-tex:not([data-typeset])")) {
    try {
      katex.render(span.dataset.tex || "", span, {displayMode:span.dataset.display==="true", throwOnError:false, output:"html"});
      span.removeAttribute("data-tex");
      span.dataset.typeset = "1";
    } catch { /* keep the literal source visible */ }
  }
}
function scheduleTypeset() {
  if (mathState.ready) requestAnimationFrame(() => typeset());
}
function startMath() {
  new MutationObserver(scheduleTypeset).observe(document.body, {childList:true, subtree:true});
  // No request ever blocks first paint: one in-flight check, then a slow poll.
  const tick = () => {
    if (mathState.ready || mathState.loading || Date.now() < mathState.retryAt) return;
    mathScript().then(ok => { if (ok) scheduleTypeset(); });
  };
  tick();
  setInterval(tick, 1500);
}

function outputsHTML(outputs, attachments = {}) {
  return outputs.map(output => {
    if (output.kind === "markdown") return `<div class="markdown rendered-output">${markdown(output.text, attachments)}</div>`;
    if (output.kind === "json") {
      const text = String(output.text ?? "");
      const fenced = /^\s*```/.test(text);
      if (!fenced && !/^[\[{"]/.test(text.trimStart()) && /(^|\n)\s*(#{1,6}\s|[-*+]\s|\d+\.\s|>\s)/.test(text)) {
        return `<div class="markdown rendered-output">${markdown(text, attachments)}</div>`;
      }
    }
    if (output.kind === "image" && /^data:image\/png;base64,[A-Za-z0-9+/=]+$/.test(output.data)) return `<img class="plot" src="${output.data}" alt="Python 图像输出">`;
    if (output.kind === "table") return `<div class="table-wrap"><table><thead><tr>${(output.data?.columns || []).map(v=>`<th>${h(v)}</th>`).join("")}</tr></thead><tbody>${(output.data?.rows || []).map(row=>`<tr>${row.map(v=>`<td>${h(typeof v === "object" && v !== null ? JSON.stringify(v) : v)}</td>`).join("")}</tr>`).join("")}</tbody></table>${output.truncated ? '<p class="muted">仅展示前 100 行、30 列。</p>' : ""}</div>`;
    return `<pre class="output-text ${output.kind === "error" || output.kind === "stderr" ? "error-text" : ""}">${h(output.text)}</pre>`;
  }).join("");
}

function orderRunId(cell) {
  return state.live?.cell_id===cell.id && !state.live.operation ? state.live.id : cell.run_ids.at(-1) || cell.last_run_id;
}

// Every Order input is rendered Markdown unless the user is editing it. An
// unrun Order has nothing to execute yet, but its requirement text is already
// content worth reading (and formulas worth typesetting), so it previews too.
function orderInputMode(cell) {
  return state.orderEditing.has(cell.id) ? "edit" : "preview";
}

function inputFileStateHTML(cell) {
  const rid=orderRunId(cell), info=cell.file_input?.run_id===rid?cell.file_input:null;
  const toggle=`<button class="text-button" data-action="${state.orderEditing.has(cell.id)?"done-order":"edit-order"}" data-id="${cell.id}" ${cellLocked(cell.id)?"disabled":""}>${state.orderEditing.has(cell.id)?"完成":"编辑"}</button>`;
  if(!rid)return toggle;
  const label=state.outputDisconnected?"连接中断，显示最近内容":info?.state==="ready"?"input.md · 实时":
    info?.cached?"需求文件暂不可用，显示 Notebook 快照":info?.error||"等待 input.md…";
  return `<span>${h(label)}</span><code title="${h(info?.path||"input.md")}">${h(info?.path||"input.md")}</code>${toggle}`;
}

function inputConflictHTML(cell) {
  if(!state.inputConflicts.has(cell.id))return "";
  const disabled=cellLocked(cell.id)?"disabled":"";
  return `<span>需求文件已更新，当前编辑草稿已保留。</span><button class="text-button" data-action="use-input-file" data-id="${cell.id}" ${disabled}>使用文件版本</button><button class="text-button" data-action="keep-input-draft" data-id="${cell.id}" ${disabled}>用我的版本覆盖文件</button>`;
}

function orderInputContentHTML(cell) {
  if(orderInputMode(cell)==="preview")return `<div class="order-input-preview markdown" data-id="${cell.id}" tabindex="0" title="双击编辑当前需求">${cell.source?markdown(cell.source, cell.attachments):'<p class="muted">需求文件为空。双击编辑。</p>'}</div>`;
  return `<textarea id="source-${cell.id}" class="order-input" data-field="source" data-cell="${cell.id}" aria-label="AI 指令" rows="2" maxlength="32000" placeholder="本步 AI 指令…" ${cellLocked(cell.id)?"disabled":""}>${h(cell.source)}</textarea>`;
}

function orderInputHTML(cell) {
  return `<div id="input-${cell.id}" class="order-input-source" data-input-mode="${orderInputMode(cell)}"><div class="input-file-state">${inputFileStateHTML(cell)}</div><div class="input-file-conflict" ${state.inputConflicts.has(cell.id)?"":"hidden"}>${inputConflictHTML(cell)}</div><div class="order-source-content">${orderInputContentHTML(cell)}</div></div>`;
}

function updateOrderInput(cell) {
  const root=$(`#input-${cell.id}`);if(!root)return;
  const label=$(".input-file-state",root), labelHTML=inputFileStateHTML(cell);
  if(label.innerHTML!==labelHTML)label.innerHTML=labelHTML;
  const conflict=$(".input-file-conflict",root), conflictHTML=inputConflictHTML(cell);
  if(conflict.innerHTML!==conflictHTML)conflict.innerHTML=conflictHTML;
  conflict.hidden=!conflictHTML;
  const content=$(".order-source-content",root), mode=orderInputMode(cell);
  const editorMode=root.dataset.editorVersion==="edit"?"edit":"preview";
  if(root.dataset.inputMode!==mode || editorMode!==mode) {root.dataset.inputMode=mode;content.innerHTML=orderInputContentHTML(cell);}
  else if(mode==="preview") {
    if(root.dataset.sourceVersion!==cell.source)content.innerHTML=orderInputContentHTML(cell);
  } else {
    const editor=$("textarea",content);
    if(editor.value!==cell.source) {
      const start=editor.selectionStart,end=editor.selectionEnd;editor.value=cell.source;
      editor.setSelectionRange(Math.min(start,cell.source.length),Math.min(end,cell.source.length));
    }
  }
  root.dataset.sourceVersion=cell.source;
  // Only this function writes this marker: renderAll rebuilds the content from
  // mode, and a stale edit marker must not survive into the rebuilt element.
  root.dataset.editorVersion=mode;
  autoSize();
}

function receiveOrderInput(cell,item) {
  const base=state.inputEditBases.get(cell.id);
  if(base&&item.file_input.sha256!==base.sha256&&item.source!==cell.source&&state.savingInputs.get(cell.id)!==item.source) {
    state.inputConflicts.set(cell.id,item);
  } else {
    state.inputConflicts.delete(cell.id);
    if(!base)cell.source=item.source;
    else if(item.source===cell.source)state.inputEditBases.set(cell.id,{sha256:item.file_input.sha256});
  }
  cell.file_input=item.file_input;cell.revision=item.revision;cell.updated_at=item.updated_at;
  updateOrderInput(cell);
  const stamp=$(`#cell-${cell.id} .cell-stamp`);if(stamp)stamp.innerHTML=stampHTML(cell);
}

function orderOutputHTML(cell) {
  const rid=orderRunId(cell);
  if(!rid)return outputsHTML(cell.outputs, cell.attachments);
  const info=cell.file_output?.run_id===rid ? cell.file_output : null;
  const outputs=info ? cell.outputs : cell.last_run_id===rid ? cell.outputs : [];
  const label=state.outputDisconnected ? "连接中断，显示最近内容" : info?.state==="ready" ? "output.md · 实时" :
    info?.cached ? "输出文件暂不可用，显示 Notebook 快照" : info?.error || "等待 output.md 更新…";
  return `<div class="file-output-state ${info?.state==="unavailable"||state.outputDisconnected?"error-text":""}"><span>${h(label)}</span><code>${h(info?.path||"output.md")}</code></div>${outputsHTML(outputs, cell.attachments)}`;
}

function updateOrderOutput(cell) {
  const element=$(`#output-${cell.id}`);
  const version=JSON.stringify([cell.file_output,orderRunId(cell),state.outputDisconnected]);
  if(element&&element.dataset.outputVersion!==version){element.innerHTML=orderOutputHTML(cell);element.dataset.outputVersion=version;}
  const block=$(`#cell-${cell.id} .output-block`);
  if(block&&orderRunId(cell))block.hidden=false;
}

async function refreshOrderOutputs() {
  if(!state.doc||state.outputInFlight||document.hidden)return;
  const nid=state.doc.id;
  if(state.outputNotebook!==nid){state.outputNotebook=nid;state.outputFingerprint="";}
  state.outputInFlight=true;
  try {
    const result=await api(`/api/notebooks/${nid}/outputs?after=${encodeURIComponent(state.outputFingerprint)}`);
    if(state.doc?.id!==nid)return;
    const recovered=state.outputDisconnected;
    state.outputDisconnected=false;state.outputFingerprint=result.fingerprint;
    if(result.changed) {
      let contextChanged=false;
      for(const item of result.cells) {
        const cell=state.doc.cells.find(c=>c.id===item.id&&c.type==="order");
        if(!cell||orderRunId(cell)!==item.file_output.run_id)continue;
        contextChanged ||= item.source!==cell.source||item.file_output.sha256!==cell.file_output?.sha256;
        receiveOrderInput(cell,item);
        cell.outputs=item.outputs;cell.file_output=item.file_output;
        updateOrderOutput(cell);
      }
      for(const id of result.stale_cell_ids) {
        const cell=state.doc.cells.find(c=>c.id===id);
        if(!cell||cell.stale)continue;
        cell.stale=true;
        const status=$(`[data-status-id="${id}"]`);if(status)status.innerHTML=tagHTML(cell);
        const block=$(`#cell-${id} .output-block`);if(block) {
          block.classList.add("stale-output");
          if(!$(".stale-label",block))$(".output-heading",block)?.insertAdjacentHTML("afterbegin",'<span class="stale-label">上游内容已更新 · 此结果需重新验证</span>');
        }
      }
      state.doc.updated_at=result.updated_at;
      if(contextChanged)refreshContext();
    }
    if(recovered)state.doc.cells.filter(c=>c.type==="order").forEach(c=>{updateOrderInput(c);updateOrderOutput(c);});
  } catch {
    if(state.doc?.id===nid&&!state.outputDisconnected) {
      state.outputDisconnected=true;
      state.doc.cells.filter(c=>c.type==="order").forEach(c=>{updateOrderInput(c);updateOrderOutput(c);});
    }
  } finally {state.outputInFlight=false;}
}

function startOutputPolling() {
  const tick=async()=>{await refreshOrderOutputs();setTimeout(tick,1000);};
  tick();
}

function pythonHTML(source) {
  const pattern = /#[^\n]*|'(?:\\.|[^'\\\n])*'|"(?:\\.|[^"\\\n])*"|\b(?:import|from|as|def|return|if|else|elif|for|in|while|class|with|try|except|raise|assert|True|False|None|and|or|not|pass|break|continue|lambda|yield)\b|\b\d+(?:\.\d+)?\b/g;
  let result="", offset=0;
  for(const match of source.matchAll(pattern)) {
    result+=h(source.slice(offset,match.index));
    const token=match[0], kind=token.startsWith("#")?"comment":/^["']/.test(token)?"string":/^\d/.test(token)?"number":"keyword";
    result+=`<span class="syntax-${kind}">${h(token)}</span>`;offset=match.index+token.length;
  }
  return result+h(source.slice(offset))+"\n";
}

function shell() {
  $("#app").innerHTML = `<div class="workspace">
    <header class="topbar"><div class="header-inner"><a class="brand" href="/" aria-label="CastleNotebook 首页"><img src="/logo.svg" alt="" width="29" height="29"><span>CastleNotebook</span></a><input id="document-title" class="notebook-title" data-field="title" aria-label="Notebook 标题" maxlength="200"><span id="save-state" class="save-state">已保存</span><span class="app-version">0.9.6</span></div></header>
    <div class="command-area"><nav class="menubar" aria-label="Notebook 菜单"><div class="menu-wrap"><button data-action="file-menu">文件</button><div id="file-menu" class="dropdown" hidden><button data-action="new">新建 Notebook</button><button data-action="init">从项目初始化…</button><a id="export-link" download>下载 Notebook</a><div class="menu-divider"></div><div id="notebook-list"></div></div></div><button data-action="add-default">插入</button><div class="menu-wrap"><button data-action="cell-menu">单元</button><div id="cell-menu" class="dropdown" hidden><button data-action="run-selected">运行当前</button><button data-action="run-all">运行全部</button></div></div><button data-action="reset">内核</button><button data-action="help">帮助</button><span class="menu-spacer"></span><button class="kernel-state" data-action="environment" id="kernel-state">Python 3 <span class="kernel-dot"></span></button></nav>
    <div class="toolbar" role="toolbar" aria-label="Notebook 工具栏"><div class="tool-group"><button class="tool" data-action="save" title="保存 (Ctrl+S)" aria-label="保存 Notebook">${icon("save")}</button></div><div class="tool-group"><button class="tool" data-action="add-default" title="在当前单元下方插入" aria-label="插入单元">${icon("plus")}</button><button class="tool" data-action="delete-selected" title="删除单元" aria-label="删除所选单元">${icon("trash")}</button><button class="tool" data-action="duplicate-selected" title="复制单元" aria-label="复制所选单元">${icon("copy")}</button></div><div class="tool-group"><button class="tool" data-action="up-selected" title="上移" aria-label="上移所选单元">${icon("up")}</button><button class="tool" data-action="down-selected" title="下移" aria-label="下移所选单元">${icon("down")}</button></div><div class="tool-group"><button class="tool run-selected" data-action="run-selected" title="执行 (Shift+Enter)">${icon("play")}<span>运行</span></button><button class="tool" data-action="stop" title="停止" aria-label="停止执行">${icon("stop")}</button><button class="tool" data-action="reset" title="重启内核" aria-label="重启内核">${icon("refresh")}</button></div><select id="cell-type" aria-label="单元类型"><option value="code">Python</option><option value="markdown">Markdown</option><option value="order">Order</option></select><span class="toolbar-spacer"></span><button class="tool inspector-toggle" data-action="inspector" aria-label="切换检查器" title="上下文 / 变量 / 记录">${icon("panel")}</button></div></div>
    <div class="work-content"><main id="notebook" class="notebook" aria-label="Notebook 单元"></main><aside class="inspector" id="inspector" aria-label="调试检查面板"></aside></div></div>`;
  $(".inspector-toggle").insertAdjacentHTML("beforebegin",`<button class="tool project-toggle" data-action="project" id="project-button" title="查看项目文件">${icon("folder")}<span id="project-name">项目</span></button>`);
  $("#cell-menu").insertAdjacentHTML("beforeend",'<div class="menu-divider"></div><button data-action="merge-selected">Merge AI Cells · 合并</button><button data-action="split-selected">Split AI Cells · 拆分</button>');
  renderAll();
}

function renderSidebar() {
  $("#notebook-list").innerHTML = state.boot.notebooks.map(doc=>`<button class="notebook-link ${state.doc?.id===doc.id?"active":""}" data-action="open" data-id="${doc.id}" ${busy()?"disabled":""}>${state.doc?.id===doc.id?"✓ ":""}${h(doc.title)}</button>`).join("");
  if(state.doc){$("#document-title").value=state.doc.title;$("#export-link").href=`/api/notebooks/${state.doc.id}/export`;}
  const editing=busy()||state.sequence;
  document.querySelectorAll('.toolbar [data-action]').forEach(el=>{el.disabled=el.dataset.action==="stop"?!busy():["inspector","save"].includes(el.dataset.action)?false:editing;});
  $("#cell-type").value=currentCell()?.type||"code";$("#cell-type").disabled=editing||!currentCell();
  $("#document-title").disabled=editing;
  for(const operation of ["merge","split"]) {
    const button=$(`[data-action="${operation}-selected"]`);
    button.disabled=editing||currentCell()?.type!=="order"||!providerAvailable(currentCell())||
      (operation==="merge"&&state.doc.cells.filter(c=>c.type==="order").length<2)||
      (operation==="split"&&state.doc.cells.length>=100);
  }
  const project=state.doc?.project_path||state.boot.project_path;
  $("#project-name").textContent=project.split(/[\\/]/).filter(Boolean).at(-1)||"项目";
  $("#project-button").title=`项目目录：${project}`;
  $("#project-button").disabled=!state.doc;
  $("#kernel-state").innerHTML=`Python 3 <span class="kernel-dot ${busy()?"busy":""}" title="${busy()?"执行中":state.doc?.runtime?.state==="ready"?"已就绪":"未启动"}"></span>`;
}

function stampHTML(item) { return `<span title="创建：${h(timeText(item.created_at,true))}">更新 ${h(timeText(item.updated_at))} <span class="dot-separator">·</span> r${item.revision}</span>`; }
function tagHTML(cell) { const stale=cell.stale&&cell.status!=="running";return `<span class="status-tag ${stale ? "stale" : cell.status}">${cell.status === "running" ? '<i class="spinner"></i>' : stale ? icon("refresh") : cell.status === "succeeded" ? icon("check") : ""}${stale ? "结果已失效" : statusLabel[cell.status] || cell.status}</span>`; }

function projectChangesHTML(changes) {
  if(!changes)return "";
  const labels={added:"新增",modified:"修改",deleted:"删除"};
  return `<details class="project-changes" ${changes.files.length?"open":""}><summary>项目文件变更 · ${changes.files.length}${changes.complete?"":" · 部分核验"}</summary>${changes.files.length?`<ul>${changes.files.map(file=>`<li><span class="change-${h(file.change)}">${labels[file.change]||h(file.change)}</span><code>${h(file.path)}</code></li>`).join("")}</ul>`:'<p class="muted">已核验范围内未检测到文件变更。</p>'}${(changes.warnings||[]).map(warning=>`<p class="error-text">${h(warning)}</p>`).join("")}<p class="muted">${h(changes.policy)}</p></details>`;
}

function renderNotebook() {
  const doc = state.doc;
  if (!doc) { $("#notebook").innerHTML=`<div class="empty-workspace">${icon("notebook")}<h1>开始一篇研究笔记</h1><p>定义全局目的，再把问题拆成可验证的步骤。</p><button class="btn primary" data-action="new">${icon("plus")}新建 Notebook</button></div>`; return; }
  const disabled=busy() || state.sequence ? "disabled" : "";
  $("#notebook").innerHTML = `<div class="notebook-inner">
    <section class="global-card" id="global-cell" title="${h(timeText(doc.global.updated_at,true))} · r${doc.global.revision}"><div class="input-prompt global-prompt">Global:</div><div class="global-fields"><label><span>目标</span><textarea data-field="goal" data-global="true" placeholder="项目目标" rows="1" aria-label="项目目标" maxlength="8000">${h(doc.global.goal)}</textarea></label><label><span>约束</span><textarea data-field="constraints" data-global="true" placeholder="全局约束" rows="1" aria-label="全局约束" maxlength="8000">${h(doc.global.constraints)}</textarea></label></div></section>
    <div id="cells">${doc.cells.map(cellHTML).join("")}</div>
    <div class="add-cell-bar"><button data-action="add" data-type="code" ${disabled} title="添加 Python 单元">+ Python</button><button data-action="add" data-type="markdown" ${disabled} title="添加 Markdown 单元">+ Markdown</button><button data-action="add" data-type="order" ${disabled} title="添加 AI Order 单元">+ Order</button></div></div>`;
  autoSize();
}

function orderSessionHTML(session, cellId) {
  const name=session?.provider||providerName(state.doc?.cells.find(c=>c.id===cellId));
  const installed=providerAvailable({provider:name});
  const canOpen=session?.available && installed;
  const reason=session?.reason || (!installed ? `请安装并登录 ${providerLabel(name)} 后重启服务。` : name==="codex" ? "Codex 为流式执行，暂无可打开的后台对话。" : "请先运行此 Order 单元，再打开对话。");
  return `<span class="session-name" title="${h(session?.name || reason)}">${session?.provider?`${providerLabel(session.provider)} · `:""}${session?.name?`Session Name <code>${h(session.name)}</code>`:"运行后可打开对话"}${session?.background_id?` · Background <code>${h(session.background_id)}</code>`:""}${session?.permission_mode?` · ${h(session.permission_mode)}`:""}</span><button class="text-button" data-action="open-session" data-id="${cellId}" title="${h(canOpen?"在新终端查看实时对话并继续交流":reason)}" ${canOpen?"":"disabled"}>${icon("terminal")}打开对话</button>`;
}
function liveSession(run) {
  return {name:run.session_name,id:run.session_id,background_id:run.background_id,provider:run.provider,
          permission_mode:run.provider_permission_mode,available:Boolean(run.background_id),
          reason:run.provider==="codex"?"Codex 为流式执行，无后台会话；执行过程见下方实时输出。":"请等待后台会话启动后再打开对话。"};
}
function refreshSessionButton(cellId, session) {
  const row=$(`#session-${cellId}`);if(!row)return;
  row.innerHTML=orderSessionHTML(session,cellId);
}
function cellHTML(cell,index) {
  const disabled=cellLocked(cell.id) ? "disabled" : "";
  const executionDisabled=busy() || state.sequence ? "disabled" : "";
  const live=state.live?.cell_id===cell.id;
  const editing=state.markdownEditing.has(cell.id);
  const outputExpanded=cell.type==="order"&&state.expandedOrderOutputs.has(cell.id);
  let input="";
  if (cell.type === "code") {
    input=`<div class="code-editor"><pre class="code-highlight" aria-hidden="true">${pythonHTML(cell.source)}</pre><textarea class="code-input" data-field="source" data-cell="${cell.id}" spellcheck="false" wrap="off" aria-label="Python 代码" rows="4" ${disabled}>${h(cell.source)}</textarea></div>`;
  } else if (cell.type === "markdown") {
    input=editing ? `<textarea class="markdown-input" data-field="source" data-cell="${cell.id}" spellcheck="false" aria-label="Markdown 内容" ${disabled}>${h(cell.source)}</textarea>` : `<div class="markdown markdown-preview" data-id="${cell.id}" title="双击编辑 Markdown">${cell.source ? markdown(cell.source, cell.attachments) : '<p class="placeholder">Markdown</p>'}</div>`;
  } else {
    input=`<div class="order-body">${orderInputHTML(cell)}<div class="contract-row"><label for="contract-${cell.id}">预期</label><textarea id="contract-${cell.id}" class="contract-input" data-field="expected_output" data-cell="${cell.id}" placeholder="输出契约" aria-label="预期输出" rows="1" maxlength="8000" ${disabled}>${h(cell.expected_output)}</textarea></div><div class="order-options"><select aria-label="AI 提供商" data-field="provider" data-cell="${cell.id}" ${disabled}>${[["default",`默认（${providerLabel(state.boot.capabilities.default_provider||"claude")}）`],["claude","Claude"],["codex","Codex"]].map(([v,label])=>`<option value="${v}" ${(cell.provider||"default")===v?"selected":""}>${label}</option>`).join("")}</select><select aria-label="输出格式" data-field="output_format" data-cell="${cell.id}" ${disabled}>${["markdown","json","text"].map(v=>`<option value="${v}" ${cell.output_format===v?"selected":""}>${v==="markdown"?"Markdown":v==="json"?"JSON":"Text"}</option>`).join("")}</select><button class="text-button" data-action="context" data-id="${cell.id}">上下文</button></div></div>`;
  }
  if (cell.type === "order") {
    const session=live ? liveSession(state.live) : cell.session;
    input+=`<div class="order-session" id="session-${cell.id}">${orderSessionHTML(session,cell.id)}</div>`;
  }
  const count=cell.execution_count||cell.run_ids.length||" ";
  const hasOutput=live||cell.outputs.length||cell.last_error||(cell.type==="order"&&orderRunId(cell));
  const operations=cell.type==="order"?`<button class="icon-btn" data-action="merge-cells" data-id="${cell.id}" title="Merge AI Cells · 合并两个 Order，清空原对话关联" aria-label="Merge AI Cells" ${executionDisabled||!providerAvailable(cell)||state.doc.cells.filter(c=>c.type==="order").length<2?"disabled":""}>${icon("merge")}</button><button class="icon-btn" data-action="split-cells" data-id="${cell.id}" title="Split AI Cells · 拆分为两个 Order，复制原对话" aria-label="Split AI Cells" ${executionDisabled||!providerAvailable(cell)||state.doc.cells.length>=100?"disabled":""}>${icon("split")}</button>`:"";
  return `<article class="cell ${cell.type}-cell ${state.selected===cell.id?"selected":""}" id="cell-${cell.id}" data-cell-card="${cell.id}" tabindex="0" title="${h(cell.title)} · 更新 ${h(timeText(cell.updated_at,true))}">
    <div class="input-row"><div class="input-prompt">${cell.type==="markdown"?"":`${cell.type==="order"?"AI":"In"} [${live?"*":count}]:`}<button class="gutter-run" data-action="run" data-id="${cell.id}" title="执行单元" aria-label="执行单元" ${cell.type==="order"&&!providerAvailable(cell)?"disabled":executionDisabled}>${icon("play")}</button></div><div class="input-body"><div class="cell-head"><input class="cell-title" data-field="title" data-cell="${cell.id}" aria-label="单元标题" value="${h(cell.title)}" maxlength="200" placeholder="单元标题" ${disabled}><span class="cell-status" data-status-id="${cell.id}">${live&&state.live.operation?'<span class="status-tag running"><i class="spinner"></i>整理中</span>':tagHTML(cell)}</span>${operations}<button class="icon-btn" data-action="history" data-id="${cell.id}" title="执行记录" aria-label="执行记录">${icon("clock")}</button></div>${input}</div></div>
    <div class="output-block ${cell.stale?"stale-output":""} ${outputExpanded?"output-expanded":""}" ${!hasOutput||cell.type==="markdown"?"hidden":""}><div class="output-prompt">Out[${count}]:</div><div class="output-body"><div class="output-heading">${cell.stale?'<span class="stale-label">结果已失效</span>':""}<span class="output-links"></span>${cell.type==="order"?`<button class="output-expand" data-action="toggle-order-output" data-id="${cell.id}" aria-controls="output-${cell.id}" aria-expanded="${outputExpanded}" title="${outputExpanded?"恢复滚动显示":"展开完整输出"}"><span class="output-expand-label">${outputExpanded?"恢复滚动显示":"展开完整输出"}</span></button>`:""}</div><div class="output-content" id="output-${cell.id}">${cell.type==="order"?orderOutputHTML(cell):live?outputsHTML(state.live.outputs):outputsHTML(cell.outputs, cell.attachments)}</div><div class="live-trace" id="trace-${cell.id}"></div>${!live?projectChangesHTML(cell.project_changes):""}${!live&&cell.last_error?`<div class="run-error">${h(cell.last_error)}</div>`:""}${cell.initializer&&cell.status==="succeeded"&&!cell.stale&&cell.applied_run_id!==cell.last_run_id?`<button class="btn primary small" data-action="apply-init" data-id="${cell.last_run_id}">采用拆分结果</button>`:""}</div></div></article>`;
}

function autoSize() {
  document.querySelectorAll("#notebook textarea").forEach(el=>{el.style.height="auto";el.style.height=`${Math.min(540, Math.max(el.scrollHeight, el.classList.contains("code-input")?64:28))}px`;});
}

function inspectorFrame() {
  const cell=currentCell();
  const index=state.doc?.cells.findIndex(c=>c.id===state.selected);
  $("#inspector").innerHTML=`<div class="inspector-heading"><span>调试检查器</span><span class="inspector-symbol">${icon("panel")}</span></div><div class="inspector-tabs" role="tablist" aria-label="检查器面板">${[["project","项目"],["context","上下文"],["variables","变量"],["history","记录"]].map(([key,label])=>`<button role="tab" aria-selected="${state.tab===key}" class="${state.tab===key?"active":""}" data-action="tab" data-tab="${key}">${label}${key==="variables" ? `<span>${state.doc?.runtime?.variables.length || 0}</span>` : ""}</button>`).join("")}</div><div class="inspector-body" id="inspector-body"></div><div class="inspector-footer"><span class="local-dot ${state.doc?.runtime?.state==="cold"?"cold":""}"></span>Python 内核 · ${busy()?"执行中":state.doc?.runtime?.state==="ready"?"已就绪":"尚未启动"}<button class="icon-btn" data-action="environment" aria-label="查看运行环境">${icon("info")}</button></div>`;
  if(state.tab==="project" && state.doc){renderProject();return;}
  if (!cell) { $("#inspector-body").innerHTML='<div class="inspector-empty">选择一个单元，检查它的输入、变量和执行记录。</div>'; return; }
  if(state.tab==="variables") renderVariables();
  if(state.tab==="history") renderHistoryList();
  if(state.tab==="context") {
    $("#inspector-body").innerHTML=`<div class="inspector-selection"><span class="mini-label">当前单元 · r${cell.revision}</span><strong>${String(index+1).padStart(2,"0")} <span>/</span> ${h(cell.title || typeLabel[cell.type])}</strong><small>创建 ${h(timeText(cell.created_at,true))}<br>更新 ${h(timeText(cell.updated_at,true))}</small></div><div id="context-details" class="context-details"><p class="muted">正在准备上下文预览…</p></div><details class="manual-context"><summary>自定义上文摘要</summary><textarea aria-label="精简上下文摘要" data-field="context_note" data-cell="${cell.id}" maxlength="3000" rows="3" ${cellLocked(cell.id)?"disabled":""}>${h(cell.context_note)}</textarea></details>`;
    refreshContext();
  }
}

async function refreshContext() {
  if(!state.doc || !currentCell() || state.tab!=="context") return;
  const target=$("#context-details"); if(!target) return;
  const request=++state.contextRequest, nid=state.doc.id, cid=state.selected;
  if(state.dirty) { target.innerHTML='<p class="muted">保存编辑后更新上下文预览。</p>'; return; }
  try {
    const result=state.live?.cell_id===cid ? state.live.context : await api(`/api/notebooks/${nid}/cells/${cid}/context`);
    if(request!==state.contextRequest || cid!==state.selected || state.tab!=="context") return;
    state.context=result;
    const features=result.config?.features || {global_goal:true,global_constraints:true,expected_output:true};
    const globalParts=[features.global_goal?"目的":"",features.global_constraints?"约束":""].filter(Boolean);
    target.innerHTML=`<div class="context-description">${icon("eye")}每一步的输入，都可以检查。</div>
      <p class="inspector-copy">${state.live?.cell_id===cid?"以下为本次执行的实际输入快照。":"按项目配置组装上下文，点击完整输入查看实际内容。"}</p>
      <div class="context-stat"><div><strong>${result.characters.toLocaleString()}</strong><span>输入字符</span></div><div><strong>~${result.estimated_tokens.toLocaleString()}</strong><span>估计 tokens</span></div></div>
      <div class="budget-bar"><span style="width:${Math.min(100, result.characters/result.budget*100)}%"></span></div>
      <div class="budget-caption">${Math.round(result.characters/result.budget*100)}% 的字符预算 <span>${result.budget.toLocaleString()}</span></div>
      <div class="mini-label context-sources-label">上下文构成</div>
      <div class="context-source"><span class="source-icon">${icon("folder")}</span><div><strong>项目工作区</strong><span>${h(result.project?.project_path||state.doc.project_path)}</span></div></div>
      ${globalParts.length?`<div class="context-source global-source"><span class="source-icon">${icon("target")}</span><div><strong>Global Cell</strong><span>${globalParts.join("与")} · 完整保留</span></div>${icon("check")}</div>`:""}
      ${result.sections.map(s=>`<div class="context-source"><span class="source-icon">${icon(state.doc.cells.find(c=>c.id===s.cell_id)?.type||"code")}</span><div><strong>${h(s.title || "未命名单元")}</strong><span>${s.has_valid_output?"包含有效结果":h(s.state)}${s.manually_summarized?" · 手动摘要":""}</span></div><span class="source-valid ${s.has_valid_output?"valid":""}"></span></div>`).join("")}
      <div class="context-source current-source"><span class="source-icon">${icon("arrow")}</span><div><strong>${features.expected_output?"当前输入与输出契约":"当前输入"}</strong><span>完整保留</span></div></div>
      <button class="btn outline full" data-action="context" data-id="${cid}">${icon("code")}检查完整输入</button>
      <div class="fingerprint"><span>SHA-256 指纹</span><code>${h(result.fingerprint.slice(0,16))}…</code></div>
      <div class="inspector-tip">${icon("info")}${h(result.policy)}</div>`;
  } catch(error) { if(request===state.contextRequest) target.innerHTML=`<p class="error-text">${h(error.message)}</p>`; }
}

async function renderProject() {
  const nid=state.doc.id, request=++state.projectRequest, body=$("#inspector-body");
  body.innerHTML=`<div class="mini-label">项目工作目录</div><code class="project-root">${h(state.doc.project_path)}</code><p class="inspector-copy">Order 在此目录中开发和修改源码，Python 单元可导入项目模块进行验证。</p><button class="btn outline small" data-action="refresh-project">${icon("refresh")}刷新文件</button><div id="project-files"><p class="muted">读取项目文件…</p></div>`;
  try {
    const project=await api(`/api/notebooks/${nid}/project`);
    if(state.doc?.id!==nid || state.tab!=="project" || request!==state.projectRequest)return;
    $("#project-files").innerHTML=`<p class="inspector-copy">${project.files.length} 个文件${project.truncated?" · 概览已截断":""}</p><div class="project-file-list">${project.files.map(path=>`<button data-action="project-file" data-path="${h(path)}" title="预览 ${h(path)}">${icon("code")}<code>${h(path)}</code></button>`).join("")||'<p class="muted">项目中还没有可展示的文件。</p>'}</div>${(project.warnings||[]).map(warning=>`<p class="error-text">${h(warning)}</p>`).join("")}<p class="inspector-copy">${h(project.policy)}</p>`;
  } catch(error) {if(state.doc?.id===nid && state.tab==="project" && request===state.projectRequest)body.textContent=error.message;}
}

function renderVariables() {
  const vars=state.doc.runtime.variables;
  $("#inspector-body").innerHTML=`<div class="mini-label">共享 Python 命名空间</div><p class="inspector-copy">所有 Python 单元共享当前内核的变量。上游重跑后，内存可能仍包含旧变量；重启可从头验证。历史输出会保留。</p>${vars.length?`<div class="variable-list">${vars.map(v=>`<div class="variable"><div><code>${h(v.name)}</code><span>${h(v.type)}</span></div><pre>${h(v.preview)}</pre>${v.shape?`<small>size / shape ${h(v.shape)}</small>`:""}</div>`).join("")}</div>`:`<div class="inspector-empty">${icon("terminal")}<strong>还没有可检查的变量</strong><p>运行 Python 单元后，变量名称、类型和有界预览会显示在这里。</p></div>`}<div class="inspector-tip">${icon("info")}变量预览最多 80 个条目。完整数据保留在本地内核中。</div>`;
}

async function renderHistoryList() {
  const cell=currentCell(), body=$("#inspector-body");
  body.innerHTML=`<div class="mini-label">${h(cell.title)} · ${cell.run_ids.length} 次执行</div><p class="inspector-copy">每次执行都有独立的上下文快照、输出文件与事件记录。</p><div id="history-list">${cell.run_ids.length?'<p class="muted">读取记录…</p>':'<div class="inspector-empty">此单元尚未执行。</div>'}</div>`;
  if(cell.operation_run_id)body.insertAdjacentHTML("afterbegin",`<button class="btn outline small" data-action="view-run" data-id="${cell.operation_run_id}">${icon("eye")}查看 Agent 整理过程</button>`);
  const cid=cell.id;
  try {
    const runs=await Promise.all(cell.run_ids.slice(-20).reverse().map(id=>api(`/api/runs/${id}`)));
    if(state.selected!==cid || state.tab!=="history") return;
    $("#history-list").innerHTML=runs.map((run,i)=>`<button class="history-item" data-action="view-run" data-id="${run.id}"><span class="history-item-head"><strong>执行 #${cell.run_ids.length-i}</strong><span class="status-tag ${run.status}">${statusLabel[run.status]||run.status}</span></span><span>${h(timeText(run.started_at,true))}</span><span class="history-item-foot"><code>${h(run.id)}</code><span>${run.duration_ms===null?"执行中":(run.duration_ms/1000).toFixed(2)+" s"}</span></span></button>`).join("");
  } catch(error) { if($("#history-list")) $("#history-list").textContent=error.message; }
}

function renderAll() {
  const focused=document.activeElement, field=focused?.dataset?.field;
  const selector=field ? focused.dataset.cell ? `[data-cell="${focused.dataset.cell}"][data-field="${field}"]` :
    focused.dataset.global ? `[data-global][data-field="${field}"]` : "#document-title" : null;
  const selection=selector ? {start:focused.selectionStart,end:focused.selectionEnd,top:focused.scrollTop,left:focused.scrollLeft} : null;
  renderSidebar();renderNotebook();inspectorFrame();
  const target=selector&&$(selector);
  if(target&&!target.disabled){const details=target.closest("details");if(details)details.open=true;target.focus({preventScroll:true});if(selection.start!=null)target.setSelectionRange(selection.start,selection.end);target.scrollTop=selection.top;target.scrollLeft=selection.left;}
}
function selectCell(id,scroll=false) {
  if(!state.doc?.cells.some(c=>c.id===id)) return;
  state.selected=id;
  document.querySelectorAll(".cell.selected").forEach(el=>el.classList.remove("selected"));
  $(`#cell-${id}`)?.classList.add("selected");
  renderSidebar(); inspectorFrame();
  if(scroll) $(`#cell-${id}`)?.scrollIntoView({behavior:"smooth",block:"start"});
}

function updateSaveState(label="已保存", error=false) {
  const el=$("#save-state");
  if(el) { el.innerHTML=`${icon(error?"warning":state.dirty?"clock":"check")}${h(label)}`;el.classList.toggle("error-text",error); }
}
function touch(scope="document",field=null) {
  state.dirty=true;state.editSeq++;state.context=null;
  if(field)state.dirtyFields.add(field);
  if(scope==="global")state.globalDirty=true;else state.documentDirty=true;
  updateSaveState("等待保存…");clearTimeout(state.timer);
  state.timer=setTimeout(()=>save().catch(error=>{if(!state.runRefresh)toast(error.message,true);}),600);
}
function editablePayload() {
  const doc=state.doc;
  return {revision:doc.revision,title:doc.title,global:{goal:doc.global.goal,constraints:doc.global.constraints},
    cells:doc.cells.map(c=>({...Object.fromEntries(["id","type","title","source","expected_output","output_format","mode","context_note","provider"].map(k=>[k,k==="provider"?(c.provider||"default"):c[k]])),
      ...(c.type==="order"?{input_edited:state.inputEditBases.has(c.id),input_file_sha256:(state.inputEditBases.get(c.id)||c.file_input)?.sha256}:{} )}))};
}
async function save() {
  if(state.runRefresh) { await state.runRefresh; return save(); }
  if(state.savePromise) { await state.savePromise; if(state.dirty) return save(); return; }
  if(!state.dirty || !state.doc) return;
  const seq=state.editSeq, nid=state.doc.id, globalOnly=state.globalDirty&&!state.documentDirty;
  if(!globalOnly&&state.inputConflicts.size) {updateSaveState("需求文件有更新 · 请选择版本",true);throw new Error("需求文件已更新，草稿已保留。请在 Input Cell 选择使用哪个版本。");}
  const payload=globalOnly?{revision:state.doc.global.revision,goal:state.doc.global.goal,constraints:state.doc.global.constraints}:editablePayload();
  state.savingInputs=new Map((payload.cells||[]).filter(c=>c.type==="order"&&c.input_edited).map(c=>[c.id,c.source]));
  updateSaveState("正在保存…");
  state.savePromise=(async()=>{
    try {
      const doc=await api(`/api/notebooks/${nid}${globalOnly?"/global":""}`,"PUT",payload);
      if(state.doc.id!==nid) return;
      state.outputFingerprint="";
      if(!globalOnly)for(const saved of doc.cells) {
        if(!state.savingInputs.has(saved.id))continue;
        const local=state.doc.cells.find(c=>c.id===saved.id);
        if(local?.source===saved.source)state.inputEditBases.delete(saved.id);
        else if(local)state.inputEditBases.set(saved.id,{sha256:saved.file_input?.sha256});
        state.inputConflicts.delete(saved.id);
      }
      if(seq===state.editSeq) { state.doc=doc;state.dirty=false;state.globalDirty=false;state.documentDirty=false;state.dirtyFields.clear(); }
      else {state.doc.revision=doc.revision;state.doc.global.revision=doc.global.revision;}
      state.doc.cells.filter(c=>c.type==="order").forEach(updateOrderInput);
      const item=state.boot.notebooks.find(n=>n.id===nid);
      if(item) {item.title=doc.title;item.updated_at=doc.updated_at;}
      updateSaveState(state.dirty?"等待保存…":"已保存");
      renderSidebar();
      if(!state.dirty) {
        doc.cells.forEach(c=>{
          const el=$(`[data-status-id="${c.id}"]`);if(el)el.innerHTML=tagHTML(c);
          const card=$(`#cell-${c.id} .output-block`);
          if(card){card.classList.toggle("stale-output",c.stale);if(c.stale&&!$(".stale-label",card)){const label=document.createElement("span");label.className="stale-label";label.textContent="上游已更改 · 此结果需重新验证";$(".duration",card)?.remove();$(".output-heading",card).insertBefore(label,$(".output-links",card));}}
          const stamp=$(`#cell-${c.id} .cell-actions .cell-stamp`);if(stamp)stamp.innerHTML=stampHTML(c);
        });
        const globalStamp=$(".global-foot .cell-stamp");if(globalStamp)globalStamp.innerHTML=stampHTML(doc.global);
        refreshContext();
      }
    } catch(error) {
      updateSaveState("保存失败 · 草稿已保留",true);
      if(error.status===409){state.outputFingerprint="";await refreshOrderOutputs();}
      throw error;
    } finally {state.savingInputs.clear();}
  })();
  try {await state.savePromise;} finally {state.savePromise=null;}
  if(state.dirty) return save();
}
async function flushSave() {clearTimeout(state.timer);await save();}

async function openNotebook(id) {
  await flushSave();
  state.doc=await api(`/api/notebooks/${id}`);state.dirty=false;state.globalDirty=false;state.documentDirty=false;
  state.dirtyFields.clear();
  state.orderEditing.clear();state.inputEditBases.clear();state.inputConflicts.clear();state.expandedOrderOutputs.clear();
  state.selected=state.doc.cells.find(c=>c.type==="order")?.id || state.doc.cells[0]?.id || null;
  state.context=null;state.live=null;
  localStorage.setItem("castle-notebook",id);
  renderAll();
  if(state.doc.runtime.active_run_id) {const run=await api(`/api/runs/${state.doc.runtime.active_run_id}`);watchRun(run).catch(e=>toast(e.message,true));}
}

function rebaseDirtyDraft(latest,draft) {
  for(const key of state.dirtyFields) {
    const [scope,id,field]=key.split("|");
    if(scope==="cell") {
      const source=draft.cells.find(cell=>cell.id===id), target=latest.cells.find(cell=>cell.id===id);
      if(source&&target&&field in source)target[field]=source[field];
    } else if(scope==="global"&&id in draft.global)latest.global[id]=draft.global[id];
    else if(scope==="document"&&id!=="structure"&&id in draft)latest[id]=draft[id];
  }
  return latest;
}

function watchRun(run) {
  state.live=run;state.live.outputs=[];state.doc.runtime.active_run_id=run.id;
  const cell=state.doc.cells.find(c=>c.id===run.cell_id);if(cell&&!run.operation){cell.status="running";cell.execution_count=run.execution_count;if(cell.run_ids.at(-1)!==run.id)cell.run_ids.push(run.id);}
  state.selected=run.cell_id;renderAll();
  state.outputFingerprint="";refreshOrderOutputs();
  let ended=false, lost=false;
  return new Promise((resolve,reject)=>{
    const source=new EventSource(`/api/runs/${run.id}/events`);state.source=source;
    const finish=async final=>{
      if(ended)return;ended=true;source.close();state.source=null;
      try {
        clearTimeout(state.timer);
        // Pause new saves until the final document revision is known. An in-flight
        // save may have raced the run's revision update; keep its local draft.
        state.runRefresh=(async()=>{
          if(state.savePromise){try{await state.savePromise;}catch(error){if(error.status!==409)throw error;}}
          const latest=await api(`/api/notebooks/${run.notebook_id}`);
          state.doc=rebaseDirtyDraft(latest,state.doc);
        })();
        await state.runRefresh;state.runRefresh=null;state.live=null;
        await flushSave();
        if(final.applied_cell_ids?.length)state.selected=final.applied_cell_ids[0];
        final.stale=Boolean(final.stale||state.doc.cells.find(c=>c.id===run.cell_id)?.stale);
        state.boot.notebooks=await api("/api/notebooks");
        renderAll();
        if(final.status!=="succeeded") toast(final.error || "执行未完成，请检查输出与运行记录。",true);
        else if(run.operation) toast(run.operation.kind==="merge"?"Order 已合并，原对话关联已清空。":"Order 已拆分，内容与对话已分别保存。");
        else if(!state.sequence) toast(final.stale?final.stale_reason||"上游输入已变化，本次结果已标记失效。":"本步执行完成，结果与输入快照已保存。");
        resolve(final);
      } catch(error) {state.runRefresh=null;state.live=null;state.doc.runtime.active_run_id=null;renderAll();reject(error);}
    };
    source.onmessage=event=>{
      if(ended || state.live?.id!==run.id)return;
      const message=JSON.parse(event.data), data=message.data;
      if(message.type==="output") {
        if(run.cell_type==="order") {
          state.live.progress=((state.live.progress||"")+(data.text||"")).slice(-500);
          const trace=$(`#trace-${run.cell_id}`);if(trace)trace.textContent=state.live.progress;
          return;
        }
        const previous=state.live.outputs.at(-1);
        if(previous && ["stdout","stderr"].includes(data.kind) && previous.kind===data.kind)previous.text+=data.text||"";
        else state.live.outputs.push(data);
        const el=$(`#output-${run.cell_id}`);if(el)el.innerHTML=outputsHTML(state.live.outputs);
      } else if(message.type==="canonical_output") {
        if(run.cell_type==="order"){state.outputFingerprint="";refreshOrderOutputs();return;}
        state.live.outputs=[data];const el=$(`#output-${run.cell_id}`);if(el)el.innerHTML=outputsHTML([data]);
        typeset(el || document);
      } else if(message.type==="session") {
        // Codex streams without a background session: background_id stays null.
        state.live.background_id=data.background_id;state.live.session_id=data.id;
        if(data.provider)state.live.provider=data.provider;
        state.live.provider_permission_mode=data.permission_mode;
        refreshSessionButton(run.cell_id,liveSession(state.live));
      } else if(message.type==="variables") {
        state.doc.runtime.variables=data.variables;state.doc.runtime.kernel_id=data.kernel_id;
        if(state.tab==="variables")inspectorFrame();
      } else if(message.type==="tool" || message.type==="log" || message.type==="status") {
        const el=$(`#trace-${run.cell_id}`);if(el)el.textContent=message.type==="tool"?`${data.name} · ${data.input}`:data.text||data.message||"";
      } else if(message.type==="finished")finish(data);
    };
    source.onerror=async()=>{
      if(ended)return;
      try {const latest=await api(`/api/runs/${run.id}`);if(latest.status!=="running")await finish(latest);else if(!lost){lost=true;toast("实时连接暂时中断，正在自动重连…");}}
      catch {if(!lost){lost=true;toast("无法连接本地服务。恢复后将自动重连。",true);}}
    };
  });
}

// An Order run always replaces what the cell currently shows, so it gets a
// deliberate, mode-aware confirmation. The switch lives in .castle/settings.json.
function orderRunConfirmationEnabled() {
  return state.boot?.settings?.confirm_order_run !== false;
}
function orderRunModeText(cell) {
  const capabilities=state.boot?.capabilities||{};
  const name=providerName(cell);
  const mode=capabilities.claude_permission_mode;
  const permission=permissionModeLabel[mode]||mode||"默认权限模式";
  // claude_launch_mode only applies to Claude; Codex always streams a subprocess.
  if(name==="codex")return `Codex · 流式执行 · ${codexSandboxLabel[mode]||permission}`;
  const launch=launchModeLabel[capabilities.claude_launch_mode]||capabilities.claude_launch_mode||"默认启动方式";
  return `${providerLabel(name)} · ${launch} · ${permission}`;
}
function orderRunTarget(cell) {
  const title=String(cell.title||"").trim();
  if(title)return title;
  const index=state.doc?.cells.findIndex(item=>item.id===cell.id)??-1;
  return index>=0?`Order ${index+1}`:"当前 Order";
}
function orderRunConfirmBody(cell) {
  const rerun=Boolean(cell.run_ids?.length||cell.last_run_id);
  const software=providerName(cell)==="codex"?"Codex":"Claude Code";
  return `<p class="modal-description">是否采用 <strong>${software}</strong> 的 <strong>${h(orderRunModeText(cell))}</strong> 模式运行「<strong>${h(orderRunTarget(cell))}</strong>」？</p><div class="modal-callout">${icon("warning")}<span>${rerun?"这是覆盖式 AI 重跑：运行会重新生成并覆盖该单元现有的 output.md 与对话关联，下游结果会被标记失效。":"运行会启动一次 AI 会话，并把结果写入该单元的 output.md。"}</span></div><p class="muted">可在启动项目的 <code>.castle/settings.json</code> 中设置 <code>"confirm_order_run": false</code> 关闭此提示。</p><div class="modal-actions"><button class="btn outline" data-action="cancel-order-run">取消</button><button class="btn primary" data-action="confirm-order-run">${icon("play")}继续运行</button></div>`;
}
let pendingOrderRunConfirm=null;
function settleOrderRunConfirm(value) {
  const resolve=pendingOrderRunConfirm;
  pendingOrderRunConfirm=null;
  const modal=$("#modal");
  if(modal?.open)modal.close();
  if(resolve)resolve(value);
}
function confirmOrderRun(cell) {
  if(!orderRunConfirmationEnabled())return Promise.resolve(true);
  if(pendingOrderRunConfirm)settleOrderRunConfirm(false);
  const modal=$("#modal");
  showModal("运行 Order 单元", orderRunConfirmBody(cell));
  return new Promise(resolve=>{
    pendingOrderRunConfirm=resolve;
    modal.addEventListener("close",()=>{
      if(pendingOrderRunConfirm===resolve)settleOrderRunConfirm(false);
    },{once:true});
  });
}

async function runCell(id, options={}) {
  await flushSave();
  if(busy())throw new Error("请先等待或停止当前单元。");
  const cell=state.doc.cells.find(item=>item.id===id);
  if(cell?.type==="order" && options.confirm!==false && !(await confirmOrderRun(cell)))
    return {status:"cancelled",cancelled:true,cell_id:id};
  // A dialog may stay open while output polling refreshes the document.
  await flushSave();
  if(busy())throw new Error("请先等待或停止当前单元。");
  const run=await api(`/api/notebooks/${state.doc.id}/cells/${id}/run`,"POST",{});
  if(run.document_revision)state.doc.revision=run.document_revision;
  state.markdownEditing.delete(id);
  return watchRun(run);
}
async function runAll() {
  await flushSave();
  state.sequence=true;state.sequenceStop=false;
  const ids=state.doc.cells.map(c=>c.id);
  try {
    for(const id of ids) {
      if(state.sequenceStop)break;
      const result=await runCell(id);
      if(result.status!=="succeeded"||result.stale)break;
    }
  } finally {state.sequence=false;renderAll();}
}

function showModal(title,body,wide=false) {
  const modal=$("#modal");if(modal.open)modal.close();
  modal.className=wide?"wide":"";
  modal.innerHTML=`<div class="modal-head"><h2 id="modal-title">${h(title)}</h2><button class="icon-btn" data-action="close-modal" aria-label="关闭窗口">${icon("close")}</button></div><div class="modal-body">${body}</div>`;
  modal.showModal();
}
function newModal() {
  const root=state.boot.project_path, project=state.doc?.project_path||root;
  const relative=project.startsWith(root+"\\")||project.startsWith(root+"/")?project.slice(root.length+1):".";
  showModal("新建 Notebook",`<form id="new-form"><div class="path-block">${icon("folder")}<code>${h(root)}</code></div><label class="modal-label">项目子目录<input name="relative_path" value="${h(relative)}" placeholder="." autocomplete="off"></label><label class="modal-label">标题<input name="title" placeholder="未命名 Notebook" maxlength="200" autocomplete="off"></label><fieldset class="template-grid"><legend>模板</legend><label class="template-option"><input type="radio" name="template" value="blank" checked><span>${icon("notebook")}<strong>空白</strong><small>自定义单元</small></span></label><label class="template-option"><input type="radio" name="template" value="quant"><span>${icon("code")}<strong>金融研究</strong><small>合成数据</small></span></label><label class="template-option"><input type="radio" name="template" value="algorithm"><span>${icon("terminal")}<strong>算法验证</strong><small>边界断言</small></span></label></fieldset><button class="btn primary full" type="submit">创建</button></form>`);
}
function initModal() {
  const name=state.boot.capabilities.default_provider||"claude", label=providerLabel(name), installed=Boolean(state.boot.capabilities[name]);
  showModal("从项目初始化",`<div class="path-block">${icon("folder")}<code>${h(state.boot.project_path)}</code></div><form id="init-form"><label class="modal-label">子目录<input name="relative_path" value="." placeholder="." autocomplete="off"></label><div class="modal-callout">${icon("info")}本次指令要求 AI 只读分析；生成代码需手动执行。</div>${!installed?`<p class="error-text">请先安装并登录 ${name==="codex"?"Codex":"Claude Code"}，再重启服务。</p>`:""}<div class="modal-actions"><button class="btn outline" type="button" data-action="scan-project">文件概览</button><button class="btn primary" type="submit" ${installed?"":"disabled"}>用 ${label} 分析</button></div><div id="scan-result"></div></form>`);
}
async function cellOperationModal(id,operation) {
  await flushSave();
  if(busy()||state.sequence)throw new Error("请等待或停止当前执行后再整理单元。");
  const cell=state.doc.cells.find(c=>c.id===id);
  if(cell?.type!=="order")throw new Error("请选择一个 Order 单元。");
  const merge=operation==="merge", others=state.doc.cells.filter(c=>c.type==="order"&&c.id!==id);
  if(merge&&!others.length)throw new Error("至少需要两个 Order 单元才能合并。");
  if(!merge&&state.doc.cells.length>=100)throw new Error("拆分后会超过 100 个单元。");
  selectCell(id);
  const index=state.doc.cells.indexOf(cell), next=state.doc.cells.slice(index+1).find(c=>c.type==="order")||others[0];
  showModal(merge?"Merge AI Cells · 合并 Order":"Split AI Cells · 拆分 Order",`<form id="cell-operation-form" data-id="${id}" data-operation="${operation}"><p>${merge?"Agent 将归纳两个 Order 的指令、预期输出和已有结论，替换原单元，并清空原 AI 对话关联。":"Agent 将内容按任务边界拆成两个连续 Order，为两者复制原 AI 对话，之后可以分别续聊。"}</p><div class="path-block">${icon("order")}<strong>${h(cell.title||`Order ${index+1}`)}</strong></div>${merge?`<label class="modal-label">合并另一个 Order<select name="other_cell_id" aria-label="选择要合并的 Order">${others.map(other=>`<option value="${other.id}" ${other.id===next?.id?"selected":""}>${state.doc.cells.indexOf(other)+1}. ${h(other.title||other.source.slice(0,50))}</option>`).join("")}</select></label>`:!cell.session?.available?'<p class="muted">原单元暂无可恢复的 AI 对话，将拆分内容并分别保存输入、输出文件。</p>':""}<label class="modal-label">整理要求（可选）<textarea name="instructions" aria-label="整理要求" rows="3" maxlength="4000" placeholder="${merge?"例如：归纳成已完成的版本需求，保留验证结论":"例如：分别整理为功能实现和测试验证两个步骤"}"></textarea></label><p class="muted">完成后自动保存。失败或停止时保留原单元。</p><div class="modal-actions"><button class="btn outline" type="button" data-action="close-modal">取消</button><button class="btn primary" type="submit" ${providerAvailable(cell)?"":"disabled"}>${icon(operation)}调用 Agent ${merge?"合并":"拆分"}</button></div></form>`);
}
async function contextModal(id) {
  await flushSave();selectCell(id);
  const context=state.live?.cell_id===id?state.live.context:await api(`/api/notebooks/${state.doc.id}/cells/${id}/context`);
  state.context=context;
  showModal("执行输入 · 完整上下文",`<p class="modal-description">${h(context.policy)}<br>以下文本与本次状态下发送给所选 AI 的输入一致；执行记录会保存当时的独立快照。${context.config_path?`<br>模板配置：<code>${h(context.config_path)}</code>`:""}</p><div class="context-modal-meta"><span>${context.characters.toLocaleString()} 字符 · ~${context.estimated_tokens.toLocaleString()} tokens</span><button class="btn outline small" data-action="copy-context">${icon("copy")}复制输入</button></div><pre class="context-full">${h(context.prompt)}</pre><div class="context-hash"><span>SHA-256</span><code>${h(context.fingerprint)}</code></div>`,true);
}
async function viewRun(id) {
  const run=await api(`/api/runs/${id}`);const events=await fetch(`/api/runs/${id}/artifact?name=events`).then(r=>r.ok?r.text():"");
  const trace=events.split("\n").filter(Boolean).map(line=>{try{return JSON.parse(line);}catch{return null;}}).filter(e=>e && ["status","tool","tool_result","log","validation","finished"].includes(e.type));
  showModal(`${run.cell_title || "单元"} · 执行记录`,`<div class="run-meta-grid"><div><span>状态</span><strong class="${run.status==="failed"?"error-text":""}">${h(statusLabel[run.status]||run.status)}</strong></div><div><span>执行时间</span><strong>${h(timeText(run.started_at,true))}</strong></div><div><span>输入版本</span><strong>Cell r${run.cell_revision} · Global r${run.global_revision}</strong></div><div><span>运行 ID</span><code>${h(run.id)}</code></div></div>${run.error?`<div class="run-error">${h(run.error)}</div>`:""}<div class="run-downloads">${[["context","输入快照"],["output","输出文件"],["events","事件记录"],["run","完整元数据"]].map(([name,label])=>`<a class="btn outline small" href="/api/runs/${id}/artifact?name=${name}" download>${icon("download")}${label}</a>`).join("")}</div><details class="run-detail" open><summary>最终输出</summary><div class="history-output">${outputsHTML(run.outputs)}</div></details><details class="run-detail"><summary>实际输入 · ${run.context.characters} 字符</summary><pre class="context-full">${h(run.context.prompt)}</pre><code class="full-hash">${h(run.context.fingerprint)}</code></details><details class="run-detail"><summary>运行事件 · ${trace.length} 条</summary><div class="event-list">${trace.map(e=>`<div><time>${h(timeText(e.at))}</time><strong>${h(e.type)}</strong><pre>${h(e.data.text||e.data.message||e.data.name||e.data.status||"")}${e.data.input?"\n"+h(e.data.input):""}</pre></div>`).join("")}</div></details><div class="artifact-path">${icon("folder")}<code>${h(run.artifact)}</code></div>`,true);
  if(run.cell_type==="order")$("#modal .run-downloads").insertAdjacentHTML("afterbegin",`<a class="btn outline small" href="/api/runs/${id}/artifact?name=input" download>${icon("download")}当前需求文件</a>`);
  $("#modal .modal-body").insertAdjacentHTML("beforeend",`<div class="path-block">${icon("folder")}<code>${h(run.project_path)}</code></div>${projectChangesHTML(run.project_changes)}${run.project_changes?`<a class="btn outline small" href="/api/runs/${id}/artifact?name=changes" download>${icon("download")}文件变更记录</a>`:""}`);
}

async function mutateCells(fn) {
  await flushSave();if(busy() || state.sequence)throw new Error("请等待或停止执行后再编辑。");
  fn();touch("document","document|structure");await flushSave();renderAll();
}
async function action(name,el) {
  const id=el.dataset.id;
  if(name==="file-menu"||name==="cell-menu"){const menu=$(`#${name}`),open=menu.hidden;document.querySelectorAll(".dropdown").forEach(el=>el.hidden=true);menu.hidden=!open;return;}
  if(name==="save"){await flushSave();return toast("已保存。");}
  if(name==="run-selected"){if(state.selected)return runCell(state.selected);return;}
  if(name==="merge-selected"||name==="split-selected")return cellOperationModal(state.selected,name==="merge-selected"?"merge":"split");
  if(name==="merge-cells"||name==="split-cells")return cellOperationModal(id,name==="merge-cells"?"merge":"split");
  if(name==="add-default")return action("add",{dataset:{type:currentCell()?.type||"code",insert:true}});
  if(["delete-selected","duplicate-selected","up-selected","down-selected"].includes(name)){
    if(!state.selected)return;
    return action({"delete-selected":"delete","duplicate-selected":"duplicate","up-selected":"move-up","down-selected":"move-down"}[name],{dataset:{id:state.selected}});
  }
  if(name==="new")return newModal();
  if(name==="init")return initModal();
  if(name==="open")return openNotebook(id);
  if(name==="select")return selectCell(id,true);
  if(name==="scroll-global")return $("#global-cell")?.scrollIntoView({behavior:"smooth"});
  if(name==="close-modal")return $("#modal").close();
  if(name==="confirm-order-run")return settleOrderRunConfirm(true);
  if(name==="cancel-order-run")return settleOrderRunConfirm(false);
  if(name==="tab"){state.tab=el.dataset.tab;return inspectorFrame();}
  if(name==="project"){state.tab="project";state.inspectorOpen=true;$(".workspace").classList.add("inspector-open");return inspectorFrame();}
  if(name==="refresh-project")return renderProject();
  if(name==="project-file"){
    const file=await api(`/api/notebooks/${state.doc.id}/project/file?path=${encodeURIComponent(el.dataset.path)}`);
    return showModal(`项目文件 · ${file.path}`,`<p class="modal-description">${h(state.doc.project_path)}<br>当前文件内容</p><pre class="context-full">${h(file.source)}</pre>`,true);
  }
  if(name==="sidebar")return $(".workspace").classList.toggle("sidebar-open");
  if(name==="inspector"){state.inspectorOpen=!state.inspectorOpen;return $(".workspace").classList.toggle("inspector-open",state.inspectorOpen);}
  if(name==="toggle-order-output"){
    const expanded=!state.expandedOrderOutputs.has(id);
    expanded?state.expandedOrderOutputs.add(id):state.expandedOrderOutputs.delete(id);
    el.closest(".output-block")?.classList.toggle("output-expanded",expanded);
    el.setAttribute("aria-expanded",String(expanded));
    el.setAttribute("aria-label",expanded?"恢复滚动显示":"展开完整输出");
    el.title=expanded?"恢复滚动显示":"展开完整输出";
    const label=$(".output-expand-label",el);
    if(label)label.textContent=expanded?"恢复滚动显示":"展开完整输出";
    return;
  }
  if(name==="run")return runCell(id);
  if(name==="run-all")return runAll();
  if(name==="stop"){state.sequenceStop=true;const rid=state.live?.id || state.doc?.runtime.active_run_id;if(rid)return api(`/api/runs/${rid}/cancel`,"POST",{});return;}
  if(name==="context")return contextModal(id);
  if(name==="open-session"){
    el.disabled=true;
    try {const session=await api(`/api/notebooks/${state.doc.id}/cells/${id}/open-session`,"POST",{});toast(`已在新终端打开对话：${session.session_name}`);}
    finally {el.disabled=false;}
    return;
  }
  if(name==="copy-context"){await navigator.clipboard.writeText(state.context.prompt);return toast("完整输入已复制。");}
  if(name==="edit-markdown"){if(cellLocked(id))return;await flushSave();state.selected=id;state.markdownEditing.has(id)?state.markdownEditing.delete(id):state.markdownEditing.add(id);renderAll();$(`#cell-${id} textarea`)?.focus();return;}
  if(name==="edit-order"||name==="done-order") {
    if(cellLocked(id))return;
    await flushSave();state.selected=id;
    name==="edit-order"?state.orderEditing.add(id):state.orderEditing.delete(id);
    // The marker lives on the content root; drop it so updateOrderInput
    // rebuilds the content even when the mode label already matches.
    const root=$(`#input-${id}`); if(root)delete root.dataset.editorVersion;
    updateOrderInput(state.doc.cells.find(c=>c.id===id));
    if(name==="edit-order")$(`#source-${id}`)?.focus();
    return;
  }
  if(name==="use-input-file"||name==="keep-input-draft") {
    if(cellLocked(id))return;
    const cell=state.doc.cells.find(c=>c.id===id), latest=state.inputConflicts.get(id);
    if(!cell||!latest)return;
    if(latest.file_input.state==="unavailable")throw new Error("需求文件暂不可读，请等待恢复后再选择版本。");
    if(name==="use-input-file") {cell.source=latest.source;state.inputEditBases.delete(id);}
    else state.inputEditBases.set(id,{sha256:latest.file_input.sha256});
    state.inputConflicts.delete(id);updateOrderInput(cell);touch("document",`cell|${id}|source`);await flushSave();return;
  }
  if(name==="history"){selectCell(id);state.tab="history";inspectorFrame();state.inspectorOpen=true;$(".workspace").classList.add("inspector-open");return;}
  if(name==="view-run")return viewRun(id);
  if(name==="reset")return showModal("重启 Python 内核",`<p>重启将清空当前共享变量，保留所有历史输出。已有计算结果会标记失效，需从上游重新执行。</p><div class="modal-actions"><button class="btn outline" data-action="close-modal">取消</button><button class="btn primary" data-action="confirm-reset">${icon("refresh")}重启内核</button></div>`);
  if(name==="confirm-reset"){await flushSave();state.doc=await api(`/api/notebooks/${state.doc.id}/kernel/reset`,"POST",{});$("#modal").close();renderAll();return toast("内核已重启，历史输出已保留。");}
  if(name==="add")return mutateCells(()=>{const stamp=new Date().toISOString(),kind=el.dataset.type;const cell={id:newId(),type:kind,title:"",source:"",expected_output:"",context_note:"",mode:"llm",output_format:"markdown",provider:"default",created_at:stamp,updated_at:stamp,revision:1,status:"idle",stale:false,outputs:[],run_ids:[]};const at=el.dataset.insert?state.doc.cells.findIndex(c=>c.id===state.selected)+1:state.doc.cells.length;state.doc.cells.splice(at,0,cell);state.selected=cell.id;if(kind==="markdown")state.markdownEditing.add(cell.id);if(kind==="order")state.orderEditing.add(cell.id);}).then(()=>{$(`#cell-${state.selected}`)?.scrollIntoView({behavior:"smooth",block:"center"});$(`#cell-${state.selected} textarea`)?.focus();});
  if(["move-up","move-down","duplicate"].includes(name))return mutateCells(()=>{const index=state.doc.cells.findIndex(c=>c.id===id);if(name==="duplicate"){const copy=structuredClone(state.doc.cells[index]);Object.assign(copy,{id:newId(),title:copy.title+" · 副本",status:"idle",stale:false,outputs:[],run_ids:[],last_run_id:undefined,last_error:null,initializer:false,applied_run_id:undefined,file_input:undefined,file_output:undefined,input_session_synced:undefined});state.doc.cells.splice(index+1,0,copy);state.selected=copy.id;}else{const to=index+(name==="move-up"?-1:1);if(to>=0 && to<state.doc.cells.length)[state.doc.cells[index],state.doc.cells[to]]=[state.doc.cells[to],state.doc.cells[index]];}});
  if(name==="delete")return showModal("删除这个单元",`<p>将删除当前文档中的「${h(state.doc.cells.find(c=>c.id===id).title)}」。独立运行证据文件仍保存在本地，下游结果会标记失效。</p><div class="modal-actions"><button class="btn outline" data-action="close-modal">取消</button><button class="btn danger" data-action="confirm-delete" data-id="${id}">删除单元</button></div>`);
  if(name==="confirm-delete"){$("#modal").close();return mutateCells(()=>{state.doc.cells=state.doc.cells.filter(c=>c.id!==id);state.expandedOrderOutputs.delete(id);state.selected=state.doc.cells[0]?.id;});}
  if(name==="apply-init"){await flushSave();state.doc=await api(`/api/notebooks/${state.doc.id}/apply-init`,"POST",{run_id:id});renderAll();return toast("项目已拆分为独立单元。检查代码后再逐步运行。");}
  if(name==="scan-project"){el.disabled=true;try{const result=await api("/api/project/scan","POST",{relative_path:$("#init-form input[name=relative_path]").value});$("#scan-result").innerHTML=`<p class="muted">${result.files.length} 个文件${result.truncated?" · 概览已截断":""}</p><pre>${h(result.files.join("\n"))}</pre><p class="muted">${h(result.policy)}</p>`;}finally{el.disabled=false;}return;}
  if(name==="environment"){const caps=state.boot.capabilities,def=caps.default_provider||"claude";return showModal("本地运行环境",`<div class="environment-details"><label>项目工作目录</label><code>${h(state.doc?.project_path||state.boot.project_path)}</code><label>Python 内核</label><code>${h(caps.python)}</code><label>Claude CLI</label><p>${caps.claude?'<span class="status-tag succeeded">已检测到</span> 运行时沿用本机 Claude 的模型、工具、权限与登录配置。':'<span class="status-tag failed">未检测到</span> 安装并登录后，重启本服务。'}${def==="claude"?"当前为默认 AI 提供商。":""}</p><label>Codex CLI</label><p>${caps.codex?'<span class="status-tag succeeded">已检测到</span> 运行时沿用本机 Codex 的模型、沙箱与登录配置，始终流式执行。':'<span class="status-tag failed">未检测到</span> 安装并登录后，重启本服务。'}${def==="codex"?"当前为默认 AI 提供商。":""}</p><label>Order 运行确认</label><p>${state.boot.settings?.confirm_order_run===false?'<span class="status-tag succeeded">已关闭</span> 可在 <code>'+h(state.boot.settings.path)+'</code> 中重新开启。':'<span class="status-tag succeeded">已开启</span> 运行 Order 前询问软件、模式与目标单元。'}${state.boot.settings?.error?`<br><span class="error-text">${h(state.boot.settings.error)}</span>`:""}</p><label>执行上限</label><p>Python ${caps.code_timeout} 秒 · AI ${caps.agent_timeout} 秒</p></div><p class="muted">切换研究环境：启动服务时指定 <code>--python &lt;解释器路径&gt;</code>。切换默认 AI 提供商：使用 <code>--provider claude|codex</code>；指定 Codex CLI 路径：使用 <code>--codex &lt;路径&gt;</code>。初始化其他项目：使用 <code>--project &lt;项目目录&gt;</code>。</p>`);}
  if(name==="help")return showModal("把端到端过程，拆成可控的每一步",`<div class="help-steps"><div><b>01</b><section><h3>用 Global 约束全局</h3><p>写清目的、数据口径和执行边界。修改后，下游旧结果会标记失效。</p></section></div><div><b>02</b><section><h3>用单元推进研究</h3><p>Markdown 记录假设，Python 运行真实代码，Order 把精简上文与输出契约发送给所选 AI。Python 单元共享变量。</p></section></div><div><b>03</b><section><h3>先检查，再决定下一步</h3><p>在检查器查看完整输入、有效结果和变量。每次执行都有独立的输入、输出与事件文件，代码建议由你检查后执行。</p></section></div></div><div class="shortcut-list"><span>执行并选择下一个单元<kbd>Shift + Enter</kbd></span><span>执行当前单元<kbd>Ctrl + Enter</kbd></span><span>立即保存<kbd>Ctrl + S</kbd></span><span>代码缩进<kbd>Tab</kbd></span></div>`);
}

document.addEventListener("click",event=>{
  const el=event.target.closest("[data-action]");
  if(el && !el.disabled){event.preventDefault();if(!["file-menu","cell-menu"].includes(el.dataset.action))document.querySelectorAll(".dropdown").forEach(el=>el.hidden=true);action(el.dataset.action,el).catch?.(error=>toast(error.message,true));return;}
  const card=event.target.closest("[data-cell-card]");if(card && state.selected!==card.dataset.cellCard)selectCell(card.dataset.cellCard);
  if(!event.target.closest(".menu-wrap"))document.querySelectorAll(".dropdown").forEach(el=>el.hidden=true);
});
document.addEventListener("focusin",event=>{const id=event.target.dataset?.cell;if(id&&state.selected!==id)selectCell(id);});
document.addEventListener("dblclick",event=>{const el=event.target.closest(".markdown-preview,.order-input-preview");if(el)action(el.classList.contains("order-input-preview")?"edit-order":"edit-markdown",el).catch(error=>toast(error.message,true));});
document.addEventListener("input",event=>{
  const el=event.target;
  if(!el.dataset.field || !state.doc)return;
  let scope="document",field=`document|${el.dataset.field}`;
  if(el.dataset.global){state.doc.global[el.dataset.field]=el.value;scope="global";field=`global|${el.dataset.field}`;}
  else if(el.dataset.cell){const cell=state.doc.cells.find(c=>c.id===el.dataset.cell);
    if(cell?.type==="order"&&el.dataset.field==="source"&&!state.inputEditBases.has(cell.id))state.inputEditBases.set(cell.id,{sha256:cell.file_input?.sha256});
    if(cell)cell[el.dataset.field]=el.value;
    field=`cell|${el.dataset.cell}|${el.dataset.field}`;
    if(el.classList.contains("code-input"))el.previousElementSibling.innerHTML=pythonHTML(el.value);}
  else state.doc[el.dataset.field]=el.value;
  if(el.tagName==="TEXTAREA"){el.style.height="auto";el.style.height=`${Math.min(540,Math.max(28,el.scrollHeight))}px`;}
  touch(scope,field);
});
document.addEventListener("change",event=>{
  if(event.target.id==="cell-type"){
    const kind=event.target.value;
    mutateCells(()=>{const cell=currentCell();cell.type=kind;if(kind==="markdown")state.markdownEditing.add(cell.id);if(kind==="order")state.orderEditing.add(cell.id);}).catch(e=>toast(e.message,true));return;
  }
  if(event.target.tagName==="SELECT"&&event.target.dataset.field){event.target.dispatchEvent(new Event("input",{bubbles:true}));
    // Provider choice changes CLI availability gates; refresh buttons immediately.
    if(event.target.dataset.field==="provider")renderAll();}
});
document.addEventListener("scroll",event=>{if(event.target.classList?.contains("code-input")){event.target.previousElementSibling.scrollTop=event.target.scrollTop;event.target.previousElementSibling.scrollLeft=event.target.scrollLeft;}},true);
document.addEventListener("submit",async event=>{
  if(!["new-form","init-form","cell-operation-form"].includes(event.target.id))return;
  event.preventDefault();const form=event.target, submit=$("button[type=submit]",form);submit.disabled=true;
  try {
    await flushSave();
    if(busy())throw new Error("请等待或停止当前运行后再创建。");
    const values=Object.fromEntries(new FormData(form));
    if(form.id==="cell-operation-form"){
      const run=await api(`/api/notebooks/${state.doc.id}/cells/${form.dataset.id}/${form.dataset.operation}`,"POST",{...values,revision:state.doc.revision});
      $("#modal").close();await watchRun(run);
    } else if(form.id==="new-form"){
      const doc=await api("/api/notebooks","POST",values);state.boot.notebooks=await api("/api/notebooks");$("#modal").close();await openNotebook(doc.id);
    } else {
      const result=await api("/api/project/initialize","POST",{...values,run:true});state.doc=result.notebook;state.selected=result.run?.cell_id;state.boot.notebooks=await api("/api/notebooks");localStorage.setItem("castle-notebook",state.doc.id);$("#modal").close();renderAll();if(result.run)watchRun(result.run).catch(e=>toast(e.message,true));
    }
  } catch(error){toast(error.message,true);}finally{submit.disabled=false;}
});
document.addEventListener("keydown",event=>{
  if((event.ctrlKey||event.metaKey)&&event.key.toLowerCase()==="s"){event.preventDefault();flushSave().then(()=>toast("已保存到本地。")).catch(e=>toast(e.message,true));return;}
  if($("#modal").open)return;
  if(event.key==="Tab" && event.target.classList?.contains("code-input")){event.preventDefault();const el=event.target;el.setRangeText("    ",el.selectionStart,el.selectionEnd,"end");el.dispatchEvent(new Event("input",{bubbles:true}));return;}
  if(event.key==="Enter"&&(event.shiftKey||event.ctrlKey||event.metaKey)&&currentCell()&&!busy()&&!state.sequence){event.preventDefault();const id=state.selected,advance=event.shiftKey;runCell(id).then(result=>{if(advance&&result.status==="succeeded"&&state.selected===id){const i=state.doc.cells.findIndex(c=>c.id===id);if(state.doc.cells[i+1])selectCell(state.doc.cells[i+1].id,true);}}).catch(e=>toast(e.message,true));}
  if(event.key==="?" && !["INPUT","TEXTAREA","SELECT"].includes(event.target.tagName))action("help",{});
});
window.addEventListener("beforeunload",event=>{if(state.dirty){event.preventDefault();event.returnValue="";}});
document.addEventListener("visibilitychange",()=>{if(!document.hidden){state.outputFingerprint="";refreshOrderOutputs();}});
$("#modal").addEventListener("click",event=>{if(event.target===$("#modal")){const rect=event.target.getBoundingClientRect();if(event.clientX<rect.left||event.clientX>rect.right||event.clientY<rect.top||event.clientY>rect.bottom)event.target.close();}});

(async()=>{
  try {
    state.boot=await api("/api/bootstrap");
    const stored=localStorage.getItem("castle-notebook");
    const first=state.boot.notebooks.find(n=>n.id===stored) || state.boot.notebooks[0];
    if(first){state.doc=await api(`/api/notebooks/${first.id}`);state.selected=state.doc.cells.find(c=>c.type==="order")?.id||state.doc.cells[0]?.id;}
    shell();
    startOutputPolling();
    startMath();
    if(state.doc?.runtime.active_run_id){const run=await api(`/api/runs/${state.doc.runtime.active_run_id}`);watchRun(run).catch(e=>toast(e.message,true));}
  } catch(error){$("#app").innerHTML=`<div class="boot error-text">${icon("warning")}<h1>无法打开研究工作台</h1><p>${h(error.message)}</p><a class="btn outline" href="/">重新连接</a></div>`;}
})();
