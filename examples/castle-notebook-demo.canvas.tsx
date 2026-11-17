import { useEffect, useRef, useState, type CSSProperties, type ReactNode } from "react";
import { useCanvasState, type CanvasRuntimeConfig } from "qoder/canvas";

export const canvasConfig: CanvasRuntimeConfig = {
  shell: { mode: "full-bleed", padding: 0, background: "#f5f5f5", color: "#222" },
};

/* ============ 数据：案例《CastleNotebook演示 —— 以项目自身为例》 ============ */

type Block =
  | { t: "md"; src: string }
  | { t: "code"; lang: string; src: string }
  | { t: "wireframe" };

type CellStatus = "idle" | "running" | "succeeded" | "failed";

interface CellDef {
  id: string;
  kind: "order" | "code";
  title: string;
  ts: string;
  status: CellStatus;
  runId?: string;
  session?: string;
  chatUrl?: string;
  contract?: string;
  input: Block[];
  output?: Block[];
  outputNote?: string;
  stale?: boolean;
  mergedFrom?: string[];
}

const GLOBAL_PURPOSE =
  "解决 LLM Agent 聊天式研发中「信息展示不直观、全局信息不透明」的问题：避免需求重复提出、修改困难、复现不稳定。参考 Jupyter Notebook 核心哲学——实时展示、全局变量。";
const GLOBAL_CONSTRAINT =
  "核心结构：Global: Do What → STEP What (AI Process) → Out What。所有 Cell 带时间戳；每个 Step 实际给 AI 的输入都是它的全部精简版上文。";

const CHAT_1 = "https://chatgpt.com/s/cx_6af89257acd881918ea83b331838382f";
const CHAT_2 = "https://chatgpt.com/s/cx_6af8961716108191ad63a2a0bb4617b1";

const V09_SPEC = `## 版本0.9===

**界面层：**

- 一个Notebook文档展示界面，可以展示Project Global Cell、Input Cell、Output Cell
- 类似于Jupyter Notebook Server的极简版实现，前端界面可以用网页展示

**功能层：**

- Global Cell: 存放核心目的
  - 实时可编辑的全局项目目的、约束
- Input Cell：可以是代码类型，可以是Markdown类型，可以是Order类型（需要指定预期Output）
  - Run Button：Order Input类型，把上文的内容连同本次指令一起发给LLM，harness要求LLM将本次对话结论输出到xx文件，这个文件将提供Output Cell展示内容
  - 每一个step，实际给AI的输入都是他的全部精简版上文
- Output Cell：同Jupyter Notebook
- 所有cell要带时间戳
- 要加一个Init from project功能，调用AI Agent从已有项目初始化成这种Notebook`;

const MERGED_SPEC = `## 实现功能 · 版本0.9===

**界面层：**

- [x] 一个Notebook文档展示界面，可以展示Project Global Cell、Input Cell、Output Cell
- [x] 类似于Jupyter Notebook Server的极简版实现，前端界面可以用网页展示【jupyter二次开发可忽略】

**功能层：**

- [x] Global Cell: 存放核心目的
  - [x] 实时可编辑的全局项目目的、约束
- [x] Input Cell：可以是代码类型，可以是Markdown类型，可以是Order类型（需要指定预期Output）
  - [x] Run Button：Order Input类型，把上文的内容连同本次指令一起发给LLM，harness要求LLM将本次对话结论输出到xx文件，这个文件将提供Output Cell展示内容
  - [x] 可切换到LLM Agent 运行Order Input Cell，目前采用 \`claude "PROMPT" -n "ID NAME"\`
  - [x] 每一个step，实际给AI的输入都是他的全部精简版上文
- [x] Output Cell：同Jupyter Notebook
- [x] 所有cell要带时间戳
- [x] 要加一个Init from project功能，调用AI Agent从已有项目初始化成这种Notebook`;

const CELLS: Record<string, CellDef> = {
  c1: {
    id: "c1", kind: "order", title: "Input Cell 1 · 版本0.9 需求", ts: "11-10 09:12:44",
    status: "succeeded", runId: "a3f8c21d90ab", session: "castle-0.9-init", chatUrl: CHAT_1,
    contract: ".castle/outputs/output-a3f8c21d90ab.md",
    input: [{ t: "md", src: V09_SPEC }],
    output: [
      { t: "md", src: "做好了，如下：Notebook 展示界面、Global / Input / Output 三类 Cell、Run Button 与时间戳均已实现。" },
      { t: "wireframe" },
    ],
    outputNote: "界面初版截图（示意）",
  },
  c2: {
    id: "c2", kind: "order", title: "Input Cell 2 · BUG 检查", ts: "11-10 14:37:05",
    status: "failed", runId: "d016b87badef", session: "castle-bugfix", chatUrl: CHAT_2,
    contract: ".castle/outputs/output-d016b87badef.md",
    input: [{ t: "md", src: "运行ID：**d016b87badef** 看起来不是很完整啊，根本没跑通的样子" }],
    output: [
      { t: "md", src: "已改好：完整输入保存到 [.castle/prompts/inputc0a1218fdf04.md](.castle/prompts/inputc0a1218fdf04.md)，后台 Claude 自行读取，…" },
    ],
    outputNote: "运行 d016b87badef 首次执行未跑通，修复后结论已回写",
    stale: true,
  },
  c2u: {
    id: "c2u", kind: "order", title: "Input Cell 2 (Update 1) · BUG修复 + 上下文模板", ts: "11-11 10:02:18",
    status: "succeeded", runId: "d016b87badef", session: "castle-bugfix", chatUrl: CHAT_2,
    contract: ".castle/context.json",
    input: [{ t: "md", src: "解决没跑通问题，运行ID：**d016b87badef**；随后又新增**上下文模板可编辑**功能" }],
    output: [
      { t: "md", src: "已改好：完整输入保存到 [.castle/prompts/inputc0a1218fdf04.md](.castle/prompts/inputc0a1218fdf04.md)，后台 Claude 自行读取，…\n\n编辑方面，直接编辑 [.castle/context.json](.castle/context.json)：\n\n- `templates.instructions`：harness prompt 正文\n- `templates.prompt`：上下文结构和拼接顺序\n- `templates.invocation`：Claude 启动时的读取指令\n- `features`：各项开关；`limits`：字符预算\n\n保存后下次预览或运行立即生效，无需重启；每次运行保存配置快照。**57 项测试通过。**" },
    ],
    outputNote: "同一对话追加提问，Cell 自动更新（Update 1）",
  },
  c3: {
    id: "c3", kind: "order", title: "Input Cell 3 · IPython 接入", ts: "11-12 16:20:41",
    status: "succeeded", runId: "e77c04f1c2aa", session: "castle-ipython",
    contract: ".castle/outputs/output-e77c04f1c2aa.md",
    input: [{ t: "md", src: "看起来目前和真正的jupyter notebook还有区别，比如不支持 **!命令**" }],
    output: [
      { t: "md", src: "已接入 IPython，现在支持：\n\n- `!命令`、`files = !命令`、变量插值\n- `%pip`、`%cd`、`%time`、`%%capture`\n- 流式输出、Windows 中文编码及停止/超时清理\n\n**82 项测试全部通过。重启当前内核后生效**\n\n**目前仍不支持 widgets 和完整 Jupyter 协议**" },
    ],
  },
  m1: {
    id: "m1", kind: "order", title: "Input Cell 1 · 0.9.1 合并版", ts: "11-14 09:00:12",
    status: "succeeded", runId: "f09d31b7e6c4", session: "castle-merge-0.9.1",
    contract: ".castle/outputs/output-f09d31b7e6c4.md",
    mergedFrom: ["Input Cell 1", "Input Cell 2", "Input Cell 3"],
    input: [{ t: "md", src: MERGED_SPEC }],
    output: [
      { t: "md", src: "上述功能均已实现。值得注意的是，Order Input Cell模板编辑方面，直接编辑 [.castle/context.json](.castle/context.json)；Jupyter方面，**目前仍不支持 widgets 和完整 Jupyter 协议**。\n\n如需运行项目，执行命令：" },
    ],
    outputNote: "由一个 Claude 会话将 Cell 1 / 2 / 3 合并而成",
  },
  m2: {
    id: "m2", kind: "code", title: "Cell 2 · Python Cell", ts: "11-14 09:04:56",
    status: "succeeded",
    input: [{ t: "code", lang: "PowerShell", src: "!cd E:\\code\\CastleNotebook\n!python -m pip install -e .\n!python -m castle_notebook" }],
    output: [
      { t: "md", src: "内核已重启，CastleNotebook 服务启动，浏览器打开研究工作台。" },
    ],
  },
};

interface StageDef { key: string; title: string; date: string; version: string; desc: string; cells: string[]; fresh: string[]; updated: string[]; }

const STAGES: StageDef[] = [
  { key: "s0", title: "提出 0.9 需求", date: "11-10", version: "0.9", desc: "Global Cell 定下核心目的，Input Cell 1 以 Order 类型发出首条指令，Run Button 把精简版上文连同指令交给 LLM。", cells: ["c1"], fresh: ["c1"], updated: [] },
  { key: "s1", title: "发现 BUG", date: "11-10", version: "0.9", desc: "运行 d016b87badef 没跑通。新开对话要求检查，Input Cell 2 记录问题与修复结论，失败状态可追溯。", cells: ["c1", "c2"], fresh: ["c2"], updated: [] },
  { key: "s2", title: "上下文模板可编辑", date: "11-11", version: "0.9", desc: "同一对话继续追问：模板放进配置文件。Cell 2 自动多出 Update 1 内容——无需新开零散对话。", cells: ["c1", "c2u"], fresh: [], updated: ["c2u"] },
  { key: "s3", title: "IPython 接入", date: "11-12", version: "0.9", desc: "与此同时新功能并行开发：Input Cell 3 补齐 !命令 与魔法命令，82 项测试通过。", cells: ["c1", "c2u", "c3"], fresh: ["c3"], updated: [] },
  { key: "s4", title: "0.9.1 合并", date: "11-14", version: "0.9.1", desc: "推出 0.9.1：三个实现需求的 Cell 由一个 Claude 会话合并为带勾选清单的 Input Cell 1，并附运行命令的 Python Cell。", cells: ["m1", "m2"], fresh: ["m1", "m2"], updated: [] },
];

/* ============ 图标（与主分支 app.js 同款线稿） ============ */

const PATHS: Record<string, string> = {
  play: "m8 5 11 7-11 7V5Z", stop: "M6 6h12v12H6z", plus: "M12 5v14M5 12h14",
  order: "m12 3 2.5 6.5L21 12l-6.5 2.5L12 21l-2.5-6.5L3 12l6.5-2.5L12 3Z",
  code: "m8 7-5 5 5 5m8-10 5 5-5 5m-3-12-2 14",
  markdown: "M4 6v12M4 6l5 7 5-7v12m3-4 3 4 3-4m-3-8v12",
  target: "M21 12a9 9 0 1 1-9-9m0 4a5 5 0 1 0 5 5m-5 0 9-9m-4 0h4v4",
  clock: "M12 8v5l3 2m6-3a9 9 0 1 1-18 0 9 9 0 0 1 18 0",
  check: "m5 12 4 4L19 6", close: "m6 6 12 12M6 18 18 6",
  link: "m10 13 4-4m-6 7-2 2a4 4 0 0 1-6-6l5-5a4 4 0 0 1 6 0m2-2 2-2a4 4 0 0 1 6 6l-5 5a4 4 0 0 1-6 0",
  save: "M4 3h13l4 4v14H3V3h1Zm3 0v6h10V3M7 21v-8h10v8",
  eye: "M2 12s4-7 10-7 10 7 10 7-4 7-10 7S2 12 2 12Zm13 0a3 3 0 1 1-6 0 3 3 0 0 1 6 0",
  panel: "M3 4h18v16H3V4Zm12 0v16", refresh: "M20 8a8 8 0 1 0 0 8m0-8V3m0 5h-5",
  merge: "m4 4 6 6m10-6-6 6m-2 0v11m-4-4 4 4 4-4",
  spark: "m12 3 2 7 7 2-7 2-2 7-2-7-7-2 7-2 2-7",
  info: "M12 11v6m0-10v.1m9 4.9a9 9 0 1 1-18 0 9 9 0 0 1 18 0",
  chevron: "m9 5 7 7-7 7", down: "m5 9 7 7 7-7",
  terminal: "m5 6 6 6-6 6m8 0h6", folder: "M3 7V5h6l2 2h10v13H3V7Z",
  zoom: "M11 4a7 7 0 1 0 0 14 7 7 0 0 0 0-14Zm9 16-5-5m-4-6v6m-3-3h6",
};

function Icon({ name, size = 14 }: { name: string; size?: number }) {
  return (
    <svg className="cnb-icon" style={{ width: size, height: size }} viewBox="0 0 24 24" fill="none"
      stroke="currentColor" strokeWidth={1.6} strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d={PATHS[name] || PATHS.info} />
    </svg>
  );
}

function CastleLogo({ size = 29 }: { size?: number }) {
  return (
    <svg style={{ width: size, height: size, flexShrink: 0 }} viewBox="0 0 40 40" fill="none" aria-hidden="true">
      <path d="M7 9h6v6h4V9h6v6h4V9h6v25H7V9Z" stroke="#e87c23" strokeWidth="2.3" />
      <path d="M17 34V23h6v11" stroke="#e87c23" strokeWidth="2.3" />
      <path d="M4 4h32" stroke="#a6a6a6" strokeWidth="2" />
    </svg>
  );
}

/* ============ 迷你 Markdown / 代码渲染（案例内容专用） ============ */

function inlineMd(text: string, keyBase: string): ReactNode[] {
  const out: ReactNode[] = [];
  const re = /(`[^`]+`)|(\*\*[^*]+\*\*)|(\[([^\]]+)\]\(([^)]+)\))/g;
  let last = 0, m: RegExpExecArray | null, k = 0;
  while ((m = re.exec(text))) {
    if (m.index > last) out.push(text.slice(last, m.index));
    if (m[1]) out.push(<code key={`${keyBase}-${k++}`}>{m[1].slice(1, -1)}</code>);
    else if (m[2]) out.push(<strong key={`${keyBase}-${k++}`}>{m[2].slice(2, -2)}</strong>);
    else if (m[3]) {
      if (/^https?:\/\//.test(m[5])) out.push(<a key={`${keyBase}-${k++}`} href={m[5]} target="_blank" rel="noopener noreferrer">{m[4]}</a>);
      else out.push(<code key={`${keyBase}-${k++}`}>{m[4]}</code>);
    }
    last = m.index + m[0].length;
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}

function Md({ src }: { src: string }) {
  const lines = src.replace(/\r/g, "").split("\n");
  const nodes: ReactNode[] = [];
  let i = 0, key = 0;
  while (i < lines.length) {
    const line = lines[i];
    const trimmed = line.trim();
    if (!trimmed) { i++; continue; }
    if (trimmed.startsWith("```")) {
      const lang = trimmed.slice(3); const buf: string[] = []; i++;
      while (i < lines.length && !lines[i].trim().startsWith("```")) buf.push(lines[i++]);
      i++;
      nodes.push(
        <div className="cnb-md-code" key={key++}>
          <span>{lang || "code"}</span>
          <pre><code>{buf.join("\n")}</code></pre>
        </div>
      );
      continue;
    }
    const h = /^(#{1,4})\s+(.*)$/.exec(trimmed);
    if (h) {
      const level = Math.min(h[1].length + 1, 4);
      const content = inlineMd(h[2], `h${key}`);
      nodes.push(level === 2 ? <h2 key={key++}>{content}</h2> : level === 3 ? <h3 key={key++}>{content}</h3> : <h4 key={key++}>{content}</h4>);
      i++; continue;
    }
    if (/^[-*]\s+/.test(trimmed)) {
      const items: { text: string; indent: number; checked: boolean | null }[] = [];
      while (i < lines.length && /^\s*[-*]\s+/.test(lines[i])) {
        const indent = lines[i].match(/^\s*/)![0].length;
        let text = lines[i].replace(/^\s*[-*]\s+/, "");
        let checked: boolean | null = null;
        const cm = /^\[( |x)\]\s*/.exec(text);
        if (cm) { checked = cm[1] === "x"; text = text.slice(cm[0].length); }
        items.push({ text, indent, checked });
        i++;
      }
      nodes.push(
        <ul key={key++}>
          {items.map((it, j) => (
            <li key={j} className={it.checked !== null ? "cnb-task" : undefined}
              style={it.indent >= 2 ? { marginLeft: 18 } : undefined}>
              {it.checked !== null && <input type="checkbox" checked={it.checked} readOnly tabIndex={-1} />}
              {inlineMd(it.text, `li${key}-${j}`)}
            </li>
          ))}
        </ul>
      );
      continue;
    }
    const para: string[] = [trimmed]; i++;
    while (i < lines.length && lines[i].trim() && !/^(#{1,4}\s|[-*]\s|```)/.test(lines[i].trim())) {
      para.push(lines[i].trim()); i++;
    }
    nodes.push(<p key={key++}>{inlineMd(para.join(" "), `p${key}`)}</p>);
  }
  return <div className="cnb-markdown">{nodes}</div>;
}

function hlCode(src: string): ReactNode[] {
  const out: ReactNode[] = [];
  const re = /(#.*$)|("[^"]*")|(!|%pip|%cd|%time|%%capture|\bpython\b|\bpip\b|\binstall\b|\bcd\b)/gm;
  let last = 0, m: RegExpExecArray | null, k = 0;
  while ((m = re.exec(src))) {
    if (m.index > last) out.push(src.slice(last, m.index));
    const cls = m[1] ? "cnb-syn-comment" : m[2] ? "cnb-syn-string" : "cnb-syn-keyword";
    out.push(<span key={k++} className={cls}>{m[0]}</span>);
    last = m.index + m[0].length;
  }
  if (last < src.length) out.push(src.slice(last));
  return out;
}

function Wireframe() {
  return (
    <svg className="cnb-wireframe" viewBox="0 0 560 240" role="img" aria-label="Notebook 界面初版示意图">
      <rect x="1" y="1" width="558" height="238" fill="#ffffff" stroke="#ddd" />
      <rect x="1" y="1" width="558" height="34" fill="#ffffff" stroke="#ddd" />
      <g transform="translate(10,7) scale(0.5)">
        <path d="M7 9h6v6h4V9h6v6h4V9h6v25H7V9Z" stroke="#e87c23" strokeWidth="2.3" fill="none" />
        <path d="M17 34V23h6v11" stroke="#e87c23" strokeWidth="2.3" fill="none" />
      </g>
      <text x="36" y="23" fontSize="12" fill="#333" fontFamily="Arial">CastleNotebook</text>
      <rect x="150" y="10" width="180" height="15" fill="#f5f5f5" stroke="#ddd" />
      <rect x="1" y="35" width="558" height="22" fill="#f5f5f5" stroke="#e5e5e5" />
      <text x="12" y="50" fontSize="9" fill="#777" fontFamily="Arial">文件  编辑  运行  帮助</text>
      <circle cx="520" cy="46" r="4" fill="#fff" stroke="#555" />
      <rect x="24" y="70" width="512" height="34" fill="#fafafa" stroke="#ccc" />
      <text x="30" y="84" fontSize="9" fill="#666" fontFamily="Consolas,monospace">Global</text>
      <text x="86" y="84" fontSize="9" fill="#777" fontFamily="Arial">目的 · 解决聊天式研发的全局信息不透明</text>
      <text x="86" y="97" fontSize="9" fill="#777" fontFamily="Arial">约束 · Global: Do What → STEP What → Out What</text>
      <text x="24" y="126" fontSize="9" fill="#303f9f" fontFamily="Consolas,monospace">In [1]:</text>
      <rect x="72" y="112" width="464" height="40" fill="#fcfaf6" stroke="#d5c7b5" />
      <rect x="72" y="112" width="464" height="14" fill="#f5f1e9" stroke="#e4ded4" />
      <text x="80" y="122" fontSize="8" fill="#9c5c17" fontFamily="Arial">ORDER · 版本0.9 需求</text>
      <text x="80" y="140" fontSize="9" fill="#555" fontFamily="Arial">界面层 + 功能层：Global / Input / Output Cell …</text>
      <text x="24" y="172" fontSize="9" fill="#b54e0b" fontFamily="Consolas,monospace">Out[1]:</text>
      <rect x="72" y="160" width="464" height="34" fill="#fff" stroke="#eee" />
      <text x="80" y="174" fontSize="9" fill="#333" fontFamily="Arial">做好了，如下：三类 Cell、Run Button、时间戳均已实现。</text>
      <rect x="80" y="180" width="90" height="8" fill="#eef3f8" stroke="#c9d8e8" />
      <text x="280" y="222" fontSize="9" fill="#999" fontFamily="Arial">+ 代码  + Markdown  + Order</text>
    </svg>
  );
}

function Blocks({ blocks }: { blocks: Block[] }) {
  return (
    <>
      {blocks.map((b, i) =>
        b.t === "md" ? <Md key={i} src={b.src} />
          : b.t === "code" ? (
            <div className="cnb-md-code" key={i}>
              <span>{b.lang}</span>
              <pre><code>{hlCode(b.src)}</code></pre>
            </div>
          ) : <Wireframe key={i} />
      )}
    </>
  );
}

/* ============ Cell 视觉（与主分支 styles.css 对齐） ============ */

function StatusTag({ status }: { status: CellStatus }) {
  if (status === "succeeded" || status === "idle") return null;
  if (status === "running") return <span className="cnb-status running"><span className="cnb-spinner" />执行中</span>;
  return <span className="cnb-status failed"><Icon name="close" size={11} />执行失败</span>;
}

function CellVisual({ cell, updated, inFocus }: { cell: CellDef; updated?: boolean; inFocus?: boolean }) {
  const isOrder = cell.kind === "order";
  return (
    <div className={`cnb-cell ${isOrder ? "cnb-order" : "cnb-code-cell"} ${updated ? "cnb-updated" : ""}`}>
      <div className="cnb-input-row">
        <div className="cnb-input-prompt">{isOrder ? "Order:" : "In [ ]:"}</div>
        <div className="cnb-input-body">
          <div className="cnb-cell-head">
            <Icon name={isOrder ? "order" : "code"} size={12} />
            <span className="cnb-cell-title">{cell.title}</span>
            {cell.mergedFrom && (
              <span className="cnb-merge-badge"><Icon name="merge" size={11} />合并自 {cell.mergedFrom.length} 个 Cell</span>
            )}
            {updated && <span className="cnb-update-badge">Update 1</span>}
            <StatusTag status={cell.status} />
            <span className="cnb-cell-ts"><Icon name="clock" size={11} />{cell.ts}</span>
            {!inFocus && <span className="cnb-zoom-hint"><Icon name="zoom" size={12} />点击放大</span>}
          </div>
          {isOrder ? (
            <div className="cnb-order-body">
              <div className="cnb-order-preview"><Blocks blocks={cell.input} /></div>
              {cell.contract && (
                <div className="cnb-contract-row">
                  <label>预期Output</label>
                  <span className="cnb-contract-input"><code>{cell.contract}</code></span>
                </div>
              )}
              <div className="cnb-order-options">
                <select defaultValue="claude" tabIndex={-1} aria-label="执行方式">
                  <option value="claude">Claude Agent</option>
                  <option value="print">print 非交互</option>
                </select>
                <span className="cnb-muted cnb-tiny">精简版上文 + 本次指令 → LLM</span>
                {cell.chatUrl && (
                  <a className="cnb-text-button" href={cell.chatUrl} target="_blank" rel="noopener noreferrer"
                    onClick={e => e.stopPropagation()}>
                    <Icon name="link" size={12} />进入对话
                  </a>
                )}
              </div>
              {cell.session && (
                <div className="cnb-order-session">
                  <span className="cnb-session-name">会话 <code>{cell.session}</code></span>
                  {cell.runId && <span className="cnb-session-name">运行ID <code>{cell.runId}</code></span>}
                </div>
              )}
            </div>
          ) : (
            <div className="cnb-code-wrap">
              <pre className="cnb-code-pre"><code>{hlCode(cell.input[0].t === "code" ? cell.input[0].src : "")}</code></pre>
            </div>
          )}
        </div>
      </div>
      {cell.output && (
        <div className={`cnb-output-block ${cell.stale ? "cnb-stale" : ""}`}>
          <div className="cnb-output-prompt">Out [ ]:</div>
          <div className="cnb-output-body">
            {cell.stale && <div className="cnb-stale-label"><Icon name="clock" size={11} />历史输出 · 修复前</div>}
            {cell.outputNote && <div className="cnb-output-note">{cell.outputNote}</div>}
            <div className="cnb-output-content"><Blocks blocks={cell.output} /></div>
          </div>
        </div>
      )}
    </div>
  );
}

/* ============ 聚焦视图：上下文检查器 ============ */

function ContextInspector({ cellId, stageIdx }: { cellId: string; stageIdx: number }) {
  const stage = STAGES[stageIdx];
  const idx = stage.cells.indexOf(cellId);
  const sources = stage.cells.slice(0, Math.max(idx, 0)).map(id => CELLS[id]);
  const chars = 1280 + sources.length * 2140 + (cellId === "m1" ? 3620 : 860);
  const budget = 16000;
  const pct = Math.min(100, Math.round((chars / budget) * 100));
  return (
    <aside className="cnb-inspector">
      <div className="cnb-inspector-heading">
        <span><Icon name="panel" size={13} /> 上下文检查器</span>
        <span className="cnb-mini-label">只读演示</span>
      </div>
      <p className="cnb-inspector-copy">
        每一个 Step，实际给 AI 的输入都是它的<strong>全部精简版上文</strong>。下方列出构成本次运行上下文的全部来源。
      </p>
      <div className="cnb-context-stat">
        <div><strong>{chars.toLocaleString()}</strong><span>上下文字符</span></div>
        <div><strong>{sources.length + 1}</strong><span>来源（含 Global）</span></div>
      </div>
      <div className="cnb-budget-bar"><span style={{ width: `${pct}%` }} /></div>
      <div className="cnb-budget-caption"><span>字符预算</span><span>{chars.toLocaleString()} / {budget.toLocaleString()}</span></div>
      <div className="cnb-context-sources-label cnb-mini-label">上下文来源</div>
      <div className="cnb-context-source">
        <span className="cnb-source-icon"><Icon name="target" size={14} /></span>
        <div><strong>Project Global Cell</strong><span>核心目的与约束 · 始终置顶</span></div>
        <span className="cnb-source-valid" />
      </div>
      {sources.map(s => (
        <div className="cnb-context-source" key={s.id}>
          <span className="cnb-source-icon"><Icon name={s.kind === "order" ? "order" : "code"} size={14} /></span>
          <div><strong>{s.title}</strong><span>{s.ts} · 精简摘要注入</span></div>
          <span className="cnb-source-valid" />
        </div>
      ))}
      <div className="cnb-context-source cnb-current-source">
        <span className="cnb-source-icon"><Icon name="spark" size={14} /></span>
        <div><strong>{CELLS[cellId].title}</strong><span>本次指令 · 完整注入</span></div>
        <span className="cnb-source-valid" />
      </div>
      <div className="cnb-fingerprint"><span>上下文指纹</span><code>ctx-{cellId}-{stageIdx}9f3e2a</code></div>
      <div className="cnb-inspector-tip">
        <Icon name="info" size={12} />
        <span>Order Cell 运行时，harness 要求 LLM 将结论写入预期 Output 文件，该文件回写为 Output Cell 展示内容。</span>
      </div>
    </aside>
  );
}

/* ============ 主组件 ============ */

interface FocusState { id: string; rect: { top: number; left: number; width: number; height: number }; moved: boolean; }

export default function CastleNotebookDemo() {
  const [stageIdx, setStageIdx] = useCanvasState<number>("cnb-demo-stage", 0);
  const [playing, setPlaying] = useState(false);
  const [focus, setFocus] = useState<FocusState | null>(null);
  const [detail, setDetail] = useState(false);
  const [closing, setClosing] = useState(false);
  const [toast, setToast] = useState<{ msg: string; err?: boolean } | null>(null);
  const [helpOpen, setHelpOpen] = useState(false);
  const [banner, setBanner] = useState(true);
  const [kernelBusy, setKernelBusy] = useState(false);
  const cellEls = useRef<Record<string, HTMLDivElement | null>>({});
  const toastTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const focusTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const busyTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const stage = STAGES[stageIdx];
  const visibleCells = stage.cells.map(id => CELLS[id]);
  const focusCell = focus ? CELLS[focus.id] : null;

  const showToast = (msg: string, err = false) => {
    setToast({ msg, err });
    if (toastTimer.current) clearTimeout(toastTimer.current);
    toastTimer.current = setTimeout(() => setToast(null), 3200);
  };

  const pulseKernel = () => {
    setKernelBusy(true);
    if (busyTimer.current) clearTimeout(busyTimer.current);
    busyTimer.current = setTimeout(() => setKernelBusy(false), 1300);
  };

  const gotoStage = (i: number, announce = true) => {
    const clamped = Math.max(0, Math.min(STAGES.length - 1, i));
    setStageIdx(clamped);
    pulseKernel();
    if (announce) {
      const s = STAGES[clamped];
      if (clamped === 4) showToast("0.9.1 发布：Cell 1 / 2 / 3 已由一个 Claude 会话合并");
      else if (s.updated.length) showToast("Input Cell 2 自动更新：新增上下文模板可编辑内容（Update 1）");
    }
  };

  const togglePlay = () => {
    if (!playing && stageIdx >= STAGES.length - 1) gotoStage(0, false);
    setPlaying(p => !p);
  };

  useEffect(() => {
    if (!playing) return;
    if (stageIdx >= STAGES.length - 1) { setPlaying(false); return; }
    const t = setTimeout(() => gotoStage(stageIdx + 1), 6000);
    return () => clearTimeout(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [playing, stageIdx]);

  useEffect(() => () => {
    if (toastTimer.current) clearTimeout(toastTimer.current);
    if (focusTimer.current) clearTimeout(focusTimer.current);
    if (busyTimer.current) clearTimeout(busyTimer.current);
  }, []);

  const openFocus = (id: string) => {
    if (focus || closing) return;
    const el = cellEls.current[id];
    if (!el) return;
    const r = el.getBoundingClientRect();
    setPlaying(false);
    setDetail(false);
    setFocus({ id, rect: { top: r.top, left: r.left, width: r.width, height: r.height }, moved: false });
    requestAnimationFrame(() => requestAnimationFrame(() => {
      setFocus(f => (f ? { ...f, moved: true } : f));
    }));
    focusTimer.current = setTimeout(() => setDetail(true), 480);
  };

  const closeFocus = () => {
    if (!focus || closing) return;
    setClosing(true);
    setDetail(false);
    setFocus(f => (f ? { ...f, moved: false } : f));
    focusTimer.current = setTimeout(() => {
      setFocus(null);
      setClosing(false);
    }, 440);
  };

  const navFocus = (dir: 1 | -1) => {
    if (!focus) return;
    const ids = stage.cells;
    const i = ids.indexOf(focus.id);
    const next = ids[(i + dir + ids.length) % ids.length];
    setFocus(f => (f ? { ...f, id: next } : f));
  };

  useEffect(() => {
    if (!focus) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") closeFocus();
      else if (e.key === "ArrowRight") navFocus(1);
      else if (e.key === "ArrowLeft") navFocus(-1);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [focus, closing, stageIdx]);

  const focusTarget = (() => {
    if (!focus || typeof window === "undefined") return null;
    const w = Math.min(920, window.innerWidth - 40);
    const h = Math.min(window.innerHeight - 72, Math.max(focus.rect.height, 320));
    return { top: Math.max(28, (window.innerHeight - h) / 2), left: (window.innerWidth - w) / 2, width: w, height: h };
  })();

  const cloneStyle: CSSProperties | undefined = focus && focusTarget ? {
    position: "fixed",
    top: focus.moved ? focusTarget.top : focus.rect.top,
    left: focus.moved ? focusTarget.left : focus.rect.left,
    width: focus.moved ? focusTarget.width : focus.rect.width,
    height: focus.moved ? focusTarget.height : focus.rect.height,
    transition: closing || focus.moved ? "top .42s cubic-bezier(.22,.9,.3,1), left .42s cubic-bezier(.22,.9,.3,1), width .42s cubic-bezier(.22,.9,.3,1), height .42s cubic-bezier(.22,.9,.3,1)" : "none",
    zIndex: 60, overflow: "hidden", background: "#fff",
    boxShadow: "0 18px 70px rgba(0,0,0,.35)", border: "1px solid #b7cfe2",
    pointerEvents: "none",
  } : undefined;

  const updatedIds = new Set(stage.updated);

  return (
    <div className="cnb-root">
      <style>{CSS}</style>

      {/* 顶栏 */}
      <header className="cnb-topbar">
        <div className="cnb-header-inner">
          <span className="cnb-brand"><CastleLogo />CastleNotebook</span>
          <span className="cnb-notebook-title">CastleNotebook演示 —— 以项目自身为例</span>
          <span className="cnb-save-state"><Icon name="check" size={12} />演示快照 · 已保存</span>
          <span className="cnb-app-version">v{stage.version}</span>
        </div>
      </header>

      {/* 命令区 */}
      <div className="cnb-command-area">
        <div className="cnb-menubar">
          <button onClick={() => showToast("演示模式：文件菜单已简化，完整功能见主分支实现")}>文件</button>
          <button onClick={() => showToast("演示模式：编辑菜单已简化")}>编辑</button>
          <button onClick={togglePlay}>{playing ? "暂停回放" : "运行回放"}</button>
          <button onClick={() => setHelpOpen(true)}>帮助</button>
          <span className="cnb-menu-spacer" />
          <span className="cnb-kernel-state">
            <span className={`cnb-kernel-dot ${kernelBusy || playing ? "busy" : ""}`} />
            {kernelBusy ? "内核忙碌" : playing ? "回放中" : "内核空闲"} · Python 3
          </span>
        </div>
        <div className="cnb-toolbar">
          <span className="cnb-tool-group">
            <button className="cnb-tool" title="保存" onClick={() => showToast("演示快照已保存")}><Icon name="save" /></button>
            <button className="cnb-tool" title="添加 Cell" onClick={() => showToast("演示模式：Cell 由开发时间轴驱动")}><Icon name="plus" /></button>
          </span>
          <span className="cnb-tool-group">
            <button className="cnb-tool cnb-run-selected" onClick={togglePlay}>
              <Icon name={playing ? "stop" : "play"} />{playing ? "停止" : "运行"}
            </button>
            <button className="cnb-tool" title="重启内核并回到起点" onClick={() => { setPlaying(false); gotoStage(0, false); showToast("已回到版本 0.9 起点"); }}>
              <Icon name="refresh" />
            </button>
          </span>
          <select className="cnb-type-select" defaultValue="order" tabIndex={-1} aria-label="Cell 类型"
            onChange={e => showToast(`演示模式：新建 ${e.target.value === "order" ? "Order" : e.target.value === "code" ? "代码" : "Markdown"} Cell 见主分支`)}>
            <option value="code">代码</option>
            <option value="markdown">Markdown</option>
            <option value="order">Order</option>
          </select>
          <button className="cnb-tool cnb-inspector-toggle" title="聚焦当前 Cell 查看上下文"
            onClick={() => openFocus(stage.cells[stage.cells.length - 1])}>
            <Icon name="panel" />上下文
          </button>
        </div>

        {banner && (
          <div className="cnb-demo-banner">
            <Icon name="spark" size={14} />
            <span>
              动态演示 · 以项目自身为例：<strong>点击任意 Cell 放大聚焦</strong>，底部时间轴回放 CastleNotebook 自身的 5 个开发阶段；「运行」自动播放。
            </span>
            <button className="cnb-icon-btn" title="关闭提示" onClick={() => setBanner(false)}><Icon name="close" size={12} /></button>
          </div>
        )}

        {/* 开发时间轴 */}
        <div className="cnb-timeline">
          <button className={`cnb-play-btn ${playing ? "playing" : ""}`} onClick={togglePlay}>
            <Icon name={playing ? "stop" : "play"} size={13} />{playing ? "暂停" : "播放"}
          </button>
          <div className="cnb-stages">
            {STAGES.map((s, i) => (
              <button key={s.key} className={`cnb-stage-chip ${i === stageIdx ? "active" : ""} ${i < stageIdx ? "done" : ""}`}
                onClick={() => { setPlaying(false); gotoStage(i); }} title={s.desc}>
                <span className="cnb-stage-num">{i < stageIdx ? <Icon name="check" size={11} /> : i + 1}</span>
                <span className="cnb-stage-text">
                  <strong>{s.title}</strong>
                  <small>{s.date} · v{s.version}</small>
                </span>
                {i === stageIdx && playing && <span className="cnb-stage-progress" />}
              </button>
            ))}
          </div>
          <button className="cnb-reset-btn" onClick={() => { setPlaying(false); gotoStage(0, false); showToast("已重置到版本 0.9 起点"); }}>
            <Icon name="refresh" size={12} />重置
          </button>
        </div>
      </div>

      {/* Notebook 主体 */}
      <div className="cnb-work-content">
        <div className="cnb-notebook">
          <div className="cnb-notebook-inner">
            <div className="cnb-stage-desc" key={stage.key}>
              <span className="cnb-stage-desc-tag">阶段 {stageIdx + 1} / {STAGES.length} · v{stage.version}</span>
              {stage.desc}
            </div>

            <div className="cnb-global-card">
              <div className="cnb-global-prompt">Global:</div>
              <div className="cnb-global-fields">
                <label><span>目的</span><textarea readOnly rows={2} value={GLOBAL_PURPOSE} /></label>
                <label><span>约束</span><textarea readOnly rows={2} value={GLOBAL_CONSTRAINT} /></label>
              </div>
            </div>

            {visibleCells.map(cell => (
              <div key={`${stage.key}-${cell.id}`}
                ref={el => { cellEls.current[cell.id] = el; }}
                className={`cnb-cell-wrap ${stage.fresh.includes(cell.id) ? "cnb-enter" : ""}`}
                onClick={() => openFocus(cell.id)}
                role="button" tabIndex={0}
                onKeyDown={e => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); openFocus(cell.id); } }}>
                <CellVisual cell={cell} updated={updatedIds.has(cell.id)} />
              </div>
            ))}

            <div className="cnb-add-bar">
              <button onClick={() => showToast("演示模式：Cell 由开发时间轴驱动")}>+ 代码</button>
              <button onClick={() => showToast("演示模式：Cell 由开发时间轴驱动")}>+ Markdown</button>
              <button onClick={() => showToast("演示模式：Cell 由开发时间轴驱动")}>+ Order</button>
            </div>
          </div>
        </div>
        <div className="cnb-foot-note">
          案例来源：CastleNotebook演示 —— 以项目自身为例.md · UI 依据 main 分支 castle_notebook/static 实现复刻 · 数据为演示快照
        </div>
      </div>

      {/* 聚焦遮罩与放大动画 */}
      {focus && focusTarget && (
        <>
          <div className={`cnb-backdrop ${focus.moved && !closing ? "on" : ""}`} onClick={closeFocus} />
          {!detail && (
            <div style={cloneStyle}>
              <div className="cnb-clone-inner" style={{ width: focus.rect.width }}>
                <CellVisual cell={focusCell!} updated={updatedIds.has(focus.id)} inFocus />
              </div>
            </div>
          )}
          {detail && focusCell && (
            <div className="cnb-detail" role="dialog" aria-modal="true" aria-label={`${focusCell.title} 聚焦视图`}>
              <div className="cnb-detail-head">
                <span className="cnb-detail-type">
                  <Icon name={focusCell.kind === "order" ? "order" : "code"} size={14} />
                  {focusCell.kind === "order" ? "ORDER CELL" : "PYTHON CELL"}
                </span>
                <strong>{focusCell.title}</strong>
                <StatusTag status={focusCell.status} />
                <span className="cnb-detail-ts"><Icon name="clock" size={12} />{focusCell.ts}</span>
                <span className="cnb-menu-spacer" />
                <button className="cnb-icon-btn" title="上一个 Cell（←）" onClick={() => navFocus(-1)}><Icon name="chevron" size={13} /></button>
                <button className="cnb-icon-btn cnb-flip" title="下一个 Cell（→）" onClick={() => navFocus(1)}><Icon name="chevron" size={13} /></button>
                <button className="cnb-icon-btn" title="关闭（Esc）" onClick={closeFocus}><Icon name="close" size={14} /></button>
              </div>
              <div className="cnb-detail-body">
                <div className="cnb-detail-main">
                  <CellVisual cell={focusCell} updated={updatedIds.has(focusCell.id)} inFocus />
                  {focusCell.mergedFrom && (
                    <div className="cnb-merge-note">
                      <Icon name="merge" size={13} />
                      <span>本 Cell 由一个 Claude 会话合并自：{focusCell.mergedFrom.join("、")}。推出 0.9.1 时，同属实现需求的零散对话不再分散，统一归档为可勾选的需求清单。</span>
                    </div>
                  )}
                </div>
                <ContextInspector cellId={focusCell.id} stageIdx={stageIdx} />
              </div>
              <div className="cnb-detail-foot">
                <span><kbd>Esc</kbd> 关闭</span>
                <span><kbd>←</kbd><kbd>→</kbd> 切换 Cell</span>
                <span className="cnb-menu-spacer" />
                <span>点击遮罩亦可返回 Notebook</span>
              </div>
            </div>
          )}
        </>
      )}

      {/* 帮助弹窗 */}
      {helpOpen && (
        <div className="cnb-modal-backdrop" onClick={() => setHelpOpen(false)}>
          <div className="cnb-modal" role="dialog" aria-modal="true" aria-label="演示帮助" onClick={e => e.stopPropagation()}>
            <div className="cnb-modal-head">
              <h2>演示说明</h2>
              <button className="cnb-icon-btn" onClick={() => setHelpOpen(false)}><Icon name="close" size={14} /></button>
            </div>
            <div className="cnb-modal-body">
              <p className="cnb-modal-description">
                本演示以 CastleNotebook 自身的开发过程为案例，复刻 main 分支研究工作台 UI，展示「全局信息透明」的 Notebook 如何记录一次完整的 AI 辅助研发。
              </p>
              <div className="cnb-help-steps">
                <div><b>01</b><div><h3>Global Cell 定目的</h3><p>核心目的与约束实时可见，所有 Step 共享同一份全局上文。</p></div></div>
                <div><b>02</b><div><h3>Order Cell 发指令</h3><p>Run Button 把精简版上文连同本次指令发给 LLM，结论写入预期 Output 文件并回写展示。</p></div></div>
                <div><b>03</b><div><h3>点击放大聚焦</h3><p>点击任意 Cell 放大查看完整输入 / 输出、运行ID与会话链接，右侧检查器展示上下文来源与字符预算。</p></div></div>
                <div><b>04</b><div><h3>时间轴回放</h3><p>5 个阶段：0.9 需求 → BUG 检查 → 上下文模板可编辑 → IPython 接入 → 0.9.1 合并。同一对话的追问自动更新原 Cell。</p></div></div>
              </div>
              <div className="cnb-shortcut-list">
                <span>放大聚焦 <span><kbd>点击 Cell</kbd></span></span>
                <span>关闭聚焦 <span><kbd>Esc</kbd></span></span>
                <span>切换 Cell <span><kbd>←</kbd> <kbd>→</kbd></span></span>
              </div>
            </div>
          </div>
        </div>
      )}

      {/* Toast */}
      <div className={`cnb-toast ${toast ? "visible" : ""} ${toast?.err ? "error" : ""}`} role="status">
        {toast?.msg ?? ""}
      </div>
    </div>
  );
}

/* ============ 样式：依据 main 分支 styles.css 复刻 ============ */

const CSS = `
.cnb-root{--ink:#222;--muted:#777;--line:#ddd;--blue:#303f9f;--orange:#e87c23;
  --sans:Arial,"Microsoft YaHei",sans-serif;--mono:"Cascadia Code",Consolas,"Liberation Mono",monospace;
  background:#f5f5f5;color:var(--ink);font:13px/1.55 var(--sans);min-height:100vh;padding-bottom:48px}
.cnb-root *{box-sizing:border-box}
.cnb-root button{cursor:pointer;font:inherit;color:inherit;border-radius:0}
.cnb-root a{color:#337ab7;text-decoration:none}
.cnb-root a:hover{text-decoration:underline}
.cnb-root ::selection{background:#cce2fa}
.cnb-root ::-webkit-scrollbar{width:10px;height:10px}
.cnb-root ::-webkit-scrollbar-track{background:#f0f0f0}
.cnb-root ::-webkit-scrollbar-thumb{background:#c9c9c9;border:2px solid #f0f0f0}
.cnb-root ::-webkit-scrollbar-thumb:hover{background:#337ab7}
.cnb-icon{display:inline-block;vertical-align:middle;flex-shrink:0}
.cnb-icon-btn{display:inline-flex;align-items:center;justify-content:center;width:26px;height:25px;border:0;background:transparent;padding:4px;color:#777}
.cnb-icon-btn:hover{background:#eee;color:#333}
.cnb-muted{color:#777}.cnb-tiny{font-size:10px}
.cnb-root kbd{font:11px var(--mono);background:#f4f4f4;border:1px solid #ccc;padding:2px 5px}
.cnb-root code{font-family:var(--mono)}
.cnb-menu-spacer{flex:1}

.cnb-topbar{background:#fff;border-bottom:1px solid #ddd;box-shadow:0 1px 4px #00000012}
.cnb-header-inner{width:min(1180px,100%);margin:auto;display:flex;align-items:center;gap:20px;height:65px;padding:0 24px}
.cnb-brand{display:flex;align-items:center;gap:8px;color:#333;white-space:nowrap;font-size:20px;letter-spacing:-.7px}
.cnb-notebook-title{border:1px solid transparent;background:transparent;min-width:80px;flex:1;max-width:550px;font-size:18px;padding:3px 6px;color:#333;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.cnb-notebook-title:hover{border-color:#ddd}
.cnb-save-state{font-size:11px;color:#777;white-space:nowrap;margin-left:auto;display:inline-flex;align-items:center;gap:4px}
.cnb-app-version{font-size:11px;color:#888;border:1px solid #ddd;padding:1px 5px}

.cnb-command-area{width:min(1180px,100%);margin:auto;padding:0 24px;background:#f5f5f5;position:sticky;top:0;z-index:15}
.cnb-menubar{display:flex;align-items:center;height:35px;border-bottom:1px solid #ddd}
.cnb-menubar>button{background:transparent;border:0;padding:7px 14px;font-size:12px}
.cnb-menubar>button:hover{background:#e5e5e5}
.cnb-menubar>button:first-child{padding-left:10px}
.cnb-kernel-state{display:flex;align-items:center;gap:8px;font-size:12px;padding-right:4px;color:#444}
.cnb-kernel-dot{width:10px;height:10px;display:inline-block;border:1px solid #555;border-radius:100%;background:#fff}
.cnb-kernel-dot.busy{background:#555}
.cnb-toolbar{display:flex;align-items:center;gap:7px;height:42px;padding:6px 0}
.cnb-tool-group{display:inline-flex;align-items:center}
.cnb-tool{border:1px solid #ccc;background:linear-gradient(#fff,#f2f2f2);height:28px;min-width:31px;padding:5px 8px;display:inline-flex;align-items:center;justify-content:center;gap:6px;color:#444;margin-left:-1px}
.cnb-tool:first-child{margin-left:0}
.cnb-tool:hover{background:#e6e6e6;border-color:#aaa}
.cnb-run-selected{font-size:12px;padding:5px 11px}
.cnb-type-select{height:28px;min-width:115px;font-size:12px;background:#fff;padding:2px 8px;border:1px solid #ccc}
.cnb-inspector-toggle{margin-left:auto;font-size:12px}

.cnb-demo-banner{display:flex;align-items:center;gap:8px;border:1px solid #c9d8e8;background:#f2f7fb;color:#3d5a73;font-size:11px;line-height:1.7;padding:7px 10px;margin:2px 0 6px}
.cnb-demo-banner>.cnb-icon{color:#337ab7}
.cnb-demo-banner strong{color:#2e6da4}
.cnb-demo-banner .cnb-icon-btn{margin-left:auto;width:20px;height:20px}

.cnb-timeline{display:flex;align-items:center;gap:10px;padding:8px 0 10px;border-bottom:1px solid #ddd}
.cnb-play-btn{display:inline-flex;align-items:center;gap:6px;border:1px solid #2e6da4;background:#337ab7;color:#fff;font-size:12px;padding:6px 13px;flex-shrink:0}
.cnb-play-btn:hover{background:#286090}
.cnb-play-btn.playing{background:#fff;color:#337ab7;border-color:#337ab7}
.cnb-stages{display:flex;gap:6px;flex:1;min-width:0;overflow-x:auto;padding-bottom:2px}
.cnb-stage-chip{position:relative;display:flex;align-items:center;gap:8px;border:1px solid #ccc;background:#fff;padding:5px 11px;min-width:128px;text-align:left;overflow:hidden;flex-shrink:0}
.cnb-stage-chip:hover{border-color:#96b8d1;background:#f2f7fb}
.cnb-stage-chip.active{border-color:#337ab7;background:#f2f7fb;box-shadow:inset 0 0 0 1px #337ab7}
.cnb-stage-chip.done .cnb-stage-num{background:#639361;border-color:#639361;color:#fff}
.cnb-stage-num{display:inline-flex;align-items:center;justify-content:center;width:19px;height:19px;border:1px solid #bbb;border-radius:100%;font:10px var(--mono);color:#777;flex-shrink:0;background:#fff}
.cnb-stage-chip.active .cnb-stage-num{background:#337ab7;border-color:#337ab7;color:#fff}
.cnb-stage-text{display:flex;flex-direction:column;gap:1px;min-width:0}
.cnb-stage-text strong{font-size:11px;font-weight:600;color:#333;white-space:nowrap}
.cnb-stage-text small{font:9px var(--mono);color:#888;white-space:nowrap}
.cnb-stage-progress{position:absolute;left:0;bottom:0;height:2px;background:#337ab7;animation:cnb-progress 6s linear forwards}
@keyframes cnb-progress{from{width:0}to{width:100%}}
.cnb-reset-btn{display:inline-flex;align-items:center;gap:5px;border:1px solid #ccc;background:#fff;font-size:11px;color:#555;padding:6px 10px;flex-shrink:0}
.cnb-reset-btn:hover{background:#e9e9e9}

.cnb-work-content{width:min(1180px,100%);margin:0 auto;position:relative}
.cnb-notebook{background:#fff;min-height:480px;box-shadow:0 1px 8px #00000018;padding:25px 25px 40px 12px;margin:14px 24px 0}
.cnb-notebook-inner{max-width:1080px;margin:0 auto}
.cnb-stage-desc{font-size:12px;color:#555;line-height:1.8;background:#fafafa;border:1px solid #e5e5e5;border-left:3px solid #e87c23;padding:9px 12px;margin:0 0 18px 88px;animation:cnb-fade .4s ease}
.cnb-stage-desc-tag{display:inline-block;font:10px var(--mono);color:#9c5c17;background:#f5f1e9;border:1px solid #e4ded4;padding:1px 6px;margin-right:9px}
@keyframes cnb-fade{from{opacity:0;transform:translateY(-4px)}to{opacity:1;transform:none}}

.cnb-global-card,.cnb-input-row,.cnb-output-block{display:grid;grid-template-columns:88px minmax(0,1fr)}
.cnb-global-card{margin:0 0 20px;padding:5px;border:1px solid transparent}
.cnb-global-prompt{font:12px/1.55 var(--mono);text-align:right;padding:7px 10px 0 0;color:#666;white-space:nowrap}
.cnb-global-fields{border:1px solid #ccc;background:#fafafa;padding:7px 10px}
.cnb-global-fields>label{display:flex;align-items:flex-start;gap:10px}
.cnb-global-fields>label+label{border-top:1px solid #e7e7e7;margin-top:5px;padding-top:5px}
.cnb-global-fields label>span{font-size:11px;color:#777;padding-top:4px;white-space:nowrap}
.cnb-global-fields textarea{border:0;background:transparent;width:100%;font-size:12px;padding:2px 0;resize:none;line-height:1.6;color:#444;font-family:var(--sans)}

.cnb-cell-wrap{outline:none;cursor:zoom-in;animation:cnb-enter .5s ease}
.cnb-cell-wrap:focus-visible{outline:2px solid #5a9ecb;outline-offset:2px}
@keyframes cnb-enter{from{opacity:0;transform:translateY(10px)}to{opacity:1;transform:none}}
.cnb-cell{position:relative;border:1px solid transparent;border-left:3px solid transparent;padding:5px 4px 5px 0;margin:0 0 12px;transition:border-color .15s,box-shadow .15s}
.cnb-cell-wrap:hover .cnb-cell{border-color:#b7cfe2;border-left-color:#428bca;box-shadow:0 2px 10px #337ab71a}
.cnb-cell-wrap:hover .cnb-zoom-hint{opacity:1}
.cnb-input-prompt,.cnb-output-prompt{font:12px/1.55 var(--mono);text-align:right;padding:8px 10px 0 0;color:#303f9f;white-space:nowrap}
.cnb-input-body{min-width:0;border:1px solid #cfcfcf;background:#f7f7f7}
.cnb-cell-head{display:flex;align-items:center;gap:8px;min-height:25px;padding:1px 6px 0;border-bottom:1px solid #e8e8e8;background:#f3f3f3;color:#888}
.cnb-cell-title{font-size:11px;color:#777;flex:1;min-width:40px;padding:1px 3px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.cnb-cell-ts{display:inline-flex;align-items:center;gap:4px;font:10px var(--mono);color:#999;white-space:nowrap}
.cnb-zoom-hint{display:inline-flex;align-items:center;gap:4px;font-size:10px;color:#337ab7;opacity:0;transition:opacity .15s;white-space:nowrap}
.cnb-status{display:inline-flex;align-items:center;gap:4px;font-size:10px;white-space:nowrap}
.cnb-status.failed{color:#b42525}
.cnb-status.running{color:#337ab7}
.cnb-spinner{display:inline-block;height:10px;width:10px;border:1.5px solid #ccc;border-top-color:#337ab7;border-radius:100%;animation:cnb-spin .8s linear infinite}
@keyframes cnb-spin{to{transform:rotate(360deg)}}
.cnb-update-badge{font:9px var(--mono);color:#2e6da4;background:#dceaf4;border:1px solid #96b8d1;padding:1px 6px;animation:cnb-pulse 1.6s ease infinite}
@keyframes cnb-pulse{0%,100%{box-shadow:0 0 0 0 #337ab733}50%{box-shadow:0 0 0 4px #337ab700}}
.cnb-merge-badge{display:inline-flex;align-items:center;gap:4px;font:9px var(--mono);color:#3b7c40;background:#eef5ee;border:1px solid #bcd6bc;padding:1px 6px;white-space:nowrap}
.cnb-updated{border-left-color:#e87c23}

.cnb-order .cnb-input-prompt{color:#9c5c17}
.cnb-order .cnb-input-body{border-color:#d5c7b5;background:#fcfaf6}
.cnb-order .cnb-cell-head{background:#f5f1e9;border-color:#e4ded4;color:#9c5c17}
.cnb-order-body{padding:9px 11px}
.cnb-order-preview{min-height:32px;padding:1px 0 3px}
.cnb-contract-row{display:flex;gap:9px;margin-top:8px;padding-top:8px;border-top:1px solid #e7dfd2}
.cnb-contract-row>label{font-size:11px;color:#777;padding-top:3px;white-space:nowrap}
.cnb-contract-input{font-size:12px;line-height:1.6;color:#666;min-width:0;overflow-wrap:anywhere}
.cnb-order-options{display:flex;align-items:center;gap:7px;padding-top:9px}
.cnb-order-options select{font-size:11px;height:25px;background:#fff;border:1px solid #d5cbbd;padding:2px 5px}
.cnb-text-button{border:0;background:transparent;font-size:11px;padding:0;color:#337ab7;display:inline-flex;align-items:center;gap:4px;margin-left:auto}
.cnb-order-session{display:flex;align-items:center;gap:12px;border-top:1px solid #e7dfd2;padding:8px 0 0;margin-top:9px;flex-wrap:wrap}
.cnb-session-name{display:flex;align-items:baseline;gap:7px;color:#888;font-size:10px;white-space:nowrap}
.cnb-session-name code{font:10px var(--mono)}

.cnb-code-wrap{position:relative}
.cnb-code-pre{font:13px/1.55 var(--mono);white-space:pre;padding:10px 12px;margin:0;overflow:auto;color:#333;min-height:64px}
.cnb-syn-keyword{color:#008000;font-weight:600}
.cnb-syn-string{color:#ba2121}
.cnb-syn-comment{color:#408080;font-style:italic}

.cnb-output-block{margin-top:5px}
.cnb-output-prompt{color:#b54e0b;padding-top:4px}
.cnb-output-body{min-width:0;padding:0 5px 0 10px}
.cnb-output-note{font-size:10px;color:#888;margin:2px 0 6px}
.cnb-stale-label{display:inline-flex;align-items:center;gap:4px;font-size:10px;color:#946216;margin-bottom:5px}
.cnb-stale .cnb-output-content{border-left:2px solid #dbbe82;padding-left:8px}
.cnb-output-content{font-size:13px;overflow:auto}

.cnb-markdown{font-size:13px;line-height:1.65;overflow-wrap:anywhere;color:#333}
.cnb-markdown h2,.cnb-markdown h3,.cnb-markdown h4{font-weight:600;color:#111;line-height:1.4;margin:12px 0 8px}
.cnb-markdown h2{font-size:19px}.cnb-markdown h3{font-size:16px}.cnb-markdown h4{font-size:14px}
.cnb-markdown p{margin:7px 0}
.cnb-markdown ul{padding-left:24px;margin:8px 0}
.cnb-markdown li{margin:3px 0}
.cnb-markdown li.cnb-task{list-style:none}
.cnb-markdown li.cnb-task>input{width:13px;height:13px;margin:0 7px 0 -20px;vertical-align:middle;accent-color:#538954}
.cnb-markdown code{font:12px/1.6 var(--mono);background:#f1f1f1;padding:1px 3px}
.cnb-markdown blockquote{border-left:4px solid #ddd;color:#666;padding:5px 14px;margin:10px 0;background:#fafafa}
.cnb-md-code{border:1px solid #ddd;background:#f7f7f7;margin:10px 0}
.cnb-md-code>span{display:block;font:10px var(--mono);color:#777;padding:4px 10px;border-bottom:1px solid #e4e4e4}
.cnb-md-code>pre{font:12px/1.6 var(--mono);overflow:auto;margin:0;padding:10px}
.cnb-md-code code{padding:0;background:transparent}
.cnb-wireframe{display:block;max-width:100%;height:auto;margin:10px 0;border-radius:4px}

.cnb-add-bar{display:flex;justify-content:center;gap:15px;margin:25px 0 0 88px;padding:10px;border-top:1px solid #eee;opacity:.6}
.cnb-add-bar:hover{opacity:1}
.cnb-add-bar>button{border:0;background:transparent;color:#777;font:11px var(--mono);padding:4px}
.cnb-add-bar>button:hover{color:#337ab7}
.cnb-foot-note{width:min(1180px,100%);margin:12px auto 0;padding:0 24px;font-size:10px;color:#999;text-align:center}

.cnb-backdrop{position:fixed;inset:0;background:#1c2430;opacity:0;transition:opacity .4s ease;z-index:50}
.cnb-backdrop.on{opacity:.55}
.cnb-clone-inner{padding:10px 12px;background:#fff}
.cnb-detail{position:fixed;top:36px;left:50%;transform:translateX(-50%);width:min(920px,calc(100vw - 40px));max-height:calc(100vh - 72px);background:#fff;border:1px solid #aaa;box-shadow:0 10px 60px #0003;z-index:61;display:flex;flex-direction:column;animation:cnb-detail-in .25s ease}
@keyframes cnb-detail-in{from{opacity:0;transform:translateX(-50%) scale(.97)}to{opacity:1;transform:translateX(-50%) scale(1)}}
.cnb-detail-head{display:flex;align-items:center;gap:10px;padding:12px 16px;border-bottom:1px solid #ddd;background:#fff;flex-shrink:0}
.cnb-detail-head strong{font-size:14px;font-weight:600;color:#222;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.cnb-detail-type{display:inline-flex;align-items:center;gap:5px;font:10px var(--mono);color:#9c5c17;background:#f5f1e9;border:1px solid #e4ded4;padding:2px 7px;white-space:nowrap}
.cnb-detail-ts{display:inline-flex;align-items:center;gap:4px;font:10px var(--mono);color:#999;white-space:nowrap}
.cnb-flip{transform:scaleX(-1)}
.cnb-detail-body{display:grid;grid-template-columns:minmax(0,1fr) 280px;gap:0;overflow:auto;flex:1}
.cnb-detail-main{padding:16px 14px 20px 10px;min-width:0}
.cnb-detail-main .cnb-cell{cursor:default;margin-bottom:0}
.cnb-merge-note{display:flex;gap:8px;align-items:flex-start;border:1px solid #bcd6bc;background:#eef5ee;color:#3b7c40;font-size:11px;line-height:1.8;padding:9px 11px;margin:14px 0 0 88px}
.cnb-merge-note>.cnb-icon{margin-top:3px}
.cnb-detail-foot{display:flex;align-items:center;gap:16px;border-top:1px solid #ddd;padding:8px 16px;font-size:10px;color:#777;flex-shrink:0;background:#fafafa}

.cnb-inspector{border-left:1px solid #ddd;background:#fff;padding:16px;display:flex;flex-direction:column}
.cnb-inspector-heading{display:flex;align-items:center;justify-content:space-between;font-size:12px;color:#555}
.cnb-inspector-heading>span{display:inline-flex;align-items:center;gap:6px}
.cnb-mini-label{font-size:10px;color:#777}
.cnb-inspector-copy{font-size:11px;line-height:1.8;color:#777;margin:10px 0 14px}
.cnb-inspector-copy strong{color:#555}
.cnb-context-stat{display:grid;grid-template-columns:1fr 1fr;margin:0 0 12px}
.cnb-context-stat>div{display:flex;flex-direction:column;gap:4px}
.cnb-context-stat strong{font:20px var(--mono);color:#555}
.cnb-context-stat span{font-size:10px;color:#888}
.cnb-budget-bar{height:3px;background:#eee}
.cnb-budget-bar>span{height:100%;display:block;background:#6e9bc1;transition:width .4s ease}
.cnb-budget-caption{font-size:9px;color:#888;display:flex;justify-content:space-between;margin-top:5px}
.cnb-context-sources-label{margin:18px 0 4px}
.cnb-context-source{display:flex;align-items:center;gap:9px;border-bottom:1px solid #eee;padding:9px 0}
.cnb-source-icon{width:23px;display:flex;align-items:center;justify-content:center;color:#888;flex-shrink:0}
.cnb-context-source>div{flex:1;min-width:0}
.cnb-context-source strong{display:block;font-size:11px;font-weight:500;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.cnb-context-source>div>span{display:block;font-size:10px;color:#888;margin-top:3px;line-height:1.6}
.cnb-source-valid{width:5px;height:5px;border:1px solid #639361;border-radius:100%;background:#639361;flex-shrink:0}
.cnb-current-source{border:0;background:#f2f7fb;padding:9px 7px;margin-top:4px}
.cnb-fingerprint{display:flex;justify-content:space-between;gap:8px;font-size:9px;color:#999;margin:12px 0 14px}
.cnb-fingerprint code{font-size:9px}
.cnb-inspector-tip{display:flex;align-items:flex-start;gap:6px;font-size:10px;line-height:1.8;background:#f7f7f7;border:1px solid #e5e5e5;padding:9px;color:#777;margin-top:auto}
.cnb-inspector-tip>.cnb-icon{margin-top:3px}

.cnb-modal-backdrop{position:fixed;inset:0;background:#0004;z-index:70;display:flex;align-items:flex-start;justify-content:center;padding:8vh 16px 16px;overflow:auto}
.cnb-modal{border:1px solid #aaa;background:#fff;color:#333;box-shadow:0 10px 60px #0003;width:min(570px,100%);animation:cnb-fade .2s ease}
.cnb-modal-head{display:flex;align-items:center;justify-content:space-between;padding:15px 20px;border-bottom:1px solid #ddd}
.cnb-modal-head h2{font-size:16px;font-weight:500;margin:0}
.cnb-modal-body{padding:20px;font-size:12px}
.cnb-modal-description{font-size:12px;line-height:1.8;color:#777;margin:0 0 20px}
.cnb-help-steps>div{display:flex;gap:14px;margin-bottom:18px}
.cnb-help-steps b{font:13px var(--mono);color:#aaa;padding-top:3px}
.cnb-help-steps h3{font-size:13px;margin:0 0 6px;font-weight:600}
.cnb-help-steps p{font-size:12px;color:#777;line-height:1.8;margin:0}
.cnb-shortcut-list{border-top:1px solid #ddd;padding-top:12px}
.cnb-shortcut-list>span{display:flex;justify-content:space-between;align-items:center;font-size:11px;color:#777;margin:8px 0}

.cnb-toast{position:fixed;bottom:22px;left:50%;transform:translate(-50%,10px);max-width:90vw;padding:9px 16px;background:#333;color:#fff;box-shadow:0 2px 12px #0002;font-size:12px;opacity:0;pointer-events:none;transition:.15s;z-index:100}
.cnb-toast.visible{opacity:1;transform:translate(-50%,0)}
.cnb-toast.error{background:#9e3333}

@media(max-width:900px){
  .cnb-detail-body{grid-template-columns:1fr}
  .cnb-inspector{border-left:0;border-top:1px solid #ddd}
}
@media(max-width:760px){
  .cnb-header-inner{height:56px;padding:0 12px;gap:9px}
  .cnb-brand{font-size:15px;letter-spacing:-.5px}
  .cnb-notebook-title{font-size:14px;max-width:100%;min-width:50px}
  .cnb-save-state{display:none}
  .cnb-command-area{padding:0 10px}
  .cnb-menubar{height:31px}
  .cnb-menubar>button{font-size:11px;padding:5px 11px}
  .cnb-toolbar{height:39px;gap:5px}
  .cnb-type-select{min-width:85px;width:85px;font-size:11px}
  .cnb-notebook{margin:10px 8px 0;padding:17px 8px 30px 0}
  .cnb-global-card,.cnb-input-row,.cnb-output-block{grid-template-columns:65px minmax(0,1fr)}
  .cnb-input-prompt,.cnb-output-prompt{font-size:10px;padding-right:6px}
  .cnb-stage-desc{margin-left:65px}
  .cnb-add-bar{margin-left:65px}
  .cnb-merge-note{margin-left:65px}
  .cnb-cell-ts{display:none}
  .cnb-stage-chip{min-width:110px;padding:4px 8px}
  .cnb-detail{top:16px;max-height:calc(100vh - 32px)}
}
@media(prefers-reduced-motion:reduce){
  .cnb-root *,.cnb-root *:before,.cnb-root *:after{animation:none!important;transition:none!important}
}
`;
