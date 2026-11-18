# Castle Notebook

<!-- Language: English | [简体中文](README.md) -->

Language: English | [简体中文](README.md)

When using LLM agents for chat-driven project development, information is often displayed unintuitively and global context is opaque. This leads to repeated requests, difficult modifications, and unstable reproducibility — which is we present Castle Notebook.

The project follows the core philosophy of Jupyter Notebook: **live, real-time display**. The interface uses a classic Jupyter layout.

## Quick start

Requires Python 3.10+. Python cells execute via IPython; the web UI requires no Node or frontend build step.

Install: 

Open this repo folder, and

```bash
pip install -e .
```

To use it:

```bash
cd your-project-directory
castle-notebook
```

Then open <http://127.0.0.1:8765> in your browser. A synthetic-data example is created on first launch; you can also create blank, finance-research, or algorithm-verification notebooks from the **File** menu. If you open several projects at once, specify a port:

```bash
cd your-project-directory
castle-notebook --port <port-number>
```

Specify the research project and Python environment:

```powershell
python -m castle_notebook --project E:\research\my_model --python E:\conda\envs\research\python.exe
```

## Options

```text
--project PATH        Project root directory (defaults to current directory)
--port 8765           Local port
--python PATH         Kernel interpreter (defaults to the Python running the server)
--claude PATH         Claude executable or npm cli.js
--codex PATH          Codex executable or npm codex.js
--provider claude     Default AI provider (claude, codex or custom); each Order cell can override
--claude-launch-mode background   Native background (default); "print" is the legacy non-interactive mode
--claude-permission-mode auto     Claude auto-approval (default); escalate in terminal when needed; Codex maps this to its sandbox
--data-dir PATH       Document & evidence directory (defaults to <project>/.castle)
--code-timeout 60     Max seconds for Python execution
--agent-timeout 240   Max seconds for print-mode Claude and Codex; native background has no limit
--empty               Do not auto-create the first example
```

### Custom provider (external AI chat)

`--provider custom` launches no AI process: running an Order only writes `input.md` and the context snapshot, then waits for you to chat in an external AI (e.g. Qoder or Claude web) and write the result into that run's `output.md`. Castle collects it as the canonical output automatically. Stopping the wait never deletes files already written.

The Order's "Open chat" button opens a dialog where you bind one sessions directory (e.g. Qoder's `~/.qoder-cn/projects/<project>`), search raw transcripts by chat keyword and date, and bind a session to the cell; afterwards the full transcript is shown directly. Nothing is bound by default and no external files are readable — only the directory explicitly bound in the UI can be read.

## Documentation

| Topic | 中文 | English |
| --- | --- | --- |
| Full documentation | [docs/README.zh-CN.md](docs/README.zh-CN.md) | This README (English) |
| Workflow & Claude integration | In full doc | See sections below |
| Context configuration | [docs/context-config.md](docs/context-config.md) (zh) | Same file (Chinese) |
| 0.9 development plan | [docs/0.9-plan.md](docs/0.9-plan.md) (zh) | Same file (Chinese) |

For the complete Chinese documentation (workflow, Claude integration, save & reproduce, validation, security), see [docs/README.zh-CN.md](docs/README.zh-CN.md). The full English version below mirrors it.

---

## How to use

1. Write your goal and constraints in **Global**. By default this is included in every AI call; you can toggle pieces individually in the context configuration.
2. Add **Python / Markdown / Order** cells. An Order dispatches project development, fixes, or analysis steps and directly modifies project source and tests; Python cells import project modules, provide interactive input, and check results, sharing the current notebook's kernel variables. Orders require a clear expected output.
3. Select a cell and click **Run**, or press `Ctrl+Enter`. `Shift+Enter` runs and selects the next cell; Markdown cells are edited by double-clicking and previewed after execution.
4. Click the inspector icon in the top-right to inspect context, variables, and execution history. An Order's **Context** button shows the full text that will be sent; run history stores a snapshot of what was actually sent.

Because an Order run overwrites AI output, every run first opens a confirmation dialog naming the software (Claude Code), the configured launch and permission modes (for example "native background · auto (auto-approval)"), and the target Order, and warning that the existing `output.md`, conversation link, and downstream results will be replaced. Execution starts only after **Continue**; cancelling, closing the dialog, or pressing Esc starts nothing. Disable this guard in the launched project's `.castle/settings.json`:

```json
{
  "confirm_order_run": false
}
```

A missing or malformed file keeps the prompt enabled to stay fail-safe; the current state and file path appear in **Local runtime environment**. While enabled, Run All also asks before each Order.

For example, if `model.py` already exists in your project, an Order can ask to "fix boundary handling in `model.py` and run the relevant tests"; the next Python cell calls the real implementation via `from model import predict`. Output collects the final answer plus project files that were genuinely added, modified, or deleted during execution — code snippets in an answer are not treated as landed development. File modifications that occurred before a failure or stop are retained and collected.

Every AI input includes the project directory, a bounded file overview, and project work guidelines; any custom template in the existing `.castle/context.json` is merged in as well. Development steps should report modified paths, validations performed, and outstanding items; read-only analysis and initialization still honor their read-only requirements. Project file contents are read as data and do not replace Global or the current task.

Project modules in the persistent Python kernel follow normal Python import caching rules. After source changes, re-imported modules require an explicit `importlib.reload(module)` or a kernel restart before re-verification.

Markdown previews render inline math (`$…$`, `\(…\)`) and display math (`$$…$$`, `\[…\]`; the two `$$` may sit on their own lines). KaTeX is fetched once in the background by the local service (npmmirror first, falling back to jsdelivr and unpkg), so the page never waits on the network: formulas show their source until the bundle is ready, then typeset in place. Assets are served only from `127.0.0.1` — no third-party script ever runs in the page — and are cached under the install's `static/math/` for offline reuse.

Markdown previews also render images. A relative target in `![alt](path)` is resolved against the notebook's project directory; on save, PNG/JPEG/GIF/WebP/AVIF/BMP files inside the project are read as base64 attachments and written into the notebook (Jupyter attachment format), rewriting the source to `attachment:image-<hash>.png`, so the image survives the original file moving. The limit is 5 MB per embedded image and 100 MB per notebook; references that exceed a limit, point outside the project or cannot be read are left as written and still display through the local project image endpoint. Remote `https://` images and inline `data:image` images also display (the CSP allows only those external image sources; page scripts remain same-origin).

**Cell → Run All** executes in document order and stops on failure or when the current result becomes stale. Stopping a Python cell terminates the kernel and clears shared variables; stopping Claude does not affect the Python kernel.

After you modify Global, upstream inputs, order, or summaries, downstream old results are retained but marked stale and no longer count as valid AI evidence. Re-running upstream also invalidates downstream. This rule propagates conservatively by document order and does not analyze Python's true dependency graph.

When the current execution actually changes project files, other existing Python / Order results in this notebook are also retained but marked stale, including results from before the current step. Kernel variables are kept; proactively reload modules or restart the kernel before verifying new source. Independent edits from external editors and results from other notebooks are not tracked automatically.

While an execution is running you can keep editing and auto-saving Global and other cells (Python, Markdown, Order); only the currently executing cell's input is locked. The current execution uses the input snapshot from launch time; editing Global or upstream cells mid-run marks this result stale; editing downstream cells does not affect it. When running all, you can also edit other cells; subsequent runs use whatever is saved at that time. The same notebook still executes sequentially — add/move/convert-type/merge/split operations wait for the current execution to finish.

Context and harness prompt live in the research project's `.castle/context.json`. `features` controls Global, history inputs/outputs, variables, output contracts, handwritten summaries, and history compaction; `templates` edits the harness body, per-section formats, concatenation order, and Claude's read instructions; `limits` sets character budgets. Changes take effect on the next preview or run — no restart needed. See [Context configuration](docs/context-config.md) (Chinese) for details.

## Claude

Install and sign in to [Claude Code](https://code.claude.com/docs/en/setup) on your machine, then restart the service:

```powershell
claude auth status
```

Each Order writes the full input to `.castle/prompts/input<run-ID>.md` in the research project and starts a Claude session owned by you via a native background command:

```powershell
claude --background "请读取 .castle/prompts/input<run-ID>.md，按其中要求完成任务" -n "ID<run-ID>" --permission-mode auto --settings <run-dir>/claude-settings.json
```

The default `auto` mode has Claude's approval model check tool calls, reducing routine confirmations; questions or permission requests that need you can be handled in the live conversation. `bypassPermissions` skips normal approvals and is equivalent to `--dangerously-skip-permissions`; it is enabled only if you explicitly pass `--claude-permission-mode bypassPermissions`. You may also choose `acceptEdits`, `default`, `plan`, or `dontAsk`. Models, tools, MCP, and login continue to use your local configuration. Before using native background for the first time, run `claude` once in the project directory to complete directory-trust confirmation; if incomplete, the web UI shows the CLI's exact error. `auto` may fall back to Manual due to model or setting restrictions — the UI shows the actual permission mode and never auto-switches to `bypassPermissions`. Reference: [CLI reference](https://code.claude.com/docs/en/cli-reference) and [permission modes](https://code.claude.com/docs/en/permission-modes).

The notebook only triggers and collects; it does not own the session. `claude agents` lists background sessions; `claude agents --cwd <project-dir>` shows only the current project; `claude attach <Background ID>` enters the same live conversation anytime to handle questions, permissions, or continue chatting; exiting the terminal keeps the session alive. Closing the web page or stopping the Notebook service does not terminate native background sessions; restarting the service resumes collection of the original runs without re-dispatching tasks. Only an explicit **Stop** calls `claude stop`. Native background is not limited by `--agent-timeout` and keeps listening while waiting for user input. Reference: [Claude native background sessions](https://code.claude.com/docs/en/agent-view).

Each run's `claude-settings.json` only attaches a command hook that records events, saving tool activity, the actual permission mode, and the final answer to the run directory. Background status comes from `claude agents --json --all`; visible progress comes from `claude logs`; no Claude private state or transcript is parsed. The full input is identical to `context.md` and its SHA-256 fingerprint; the command line only passes the file path and fixed read instructions.

Legacy CLIs or environments without native background can explicitly use `--claude-launch-mode print`, which calls `claude -p ... --permission-mode auto --output-format stream-json --verbose --include-partial-messages`. `--background` and `-p` cannot be mixed; print sessions do not appear in the native background list and are resumed by UUID after completion. On tool-permission denial, the original answer is kept and the execution is marked failed.
`templates.instructions` tells Claude where the current requirements and output live via `{input_file}`, `{run_dir}`, `{output_file}`; by default these expand to `.castle/runs/<run-ID>/input.md`, `.castle/runs/<run-ID>`, and `.castle/runs/<run-ID>/output.md` relative to the project working directory. Preview and execution share the same run ID, and paths in the input file and context snapshot stay consistent. `templates.invocation` also supports these variables; the launch command does not auto-append paths.

On Windows and Linux, an Order cell's footer shows **Session Name**, **Background ID**, and the actual permission mode. Once a native background session starts, **Open conversation** is immediately available and runs `claude attach <id>` in a new terminal to enter the same live conversation, where you can handle issues mid-execution; historical print sessions use `claude --resume <session-id> --permission-mode <configured value>`. Linux prefers `$TERMINAL`, then auto-detects common terminals; on headless servers, run `claude attach` in your own terminal. The button is disabled when no session has run or been created. Follow-up chat updates to `input.md` and `output.md` continue to sync to the corresponding cell.
An Order's Out Cell is sourced from the latest run's `output.md`, checked for changes about once per second while the page is visible and re-rendered live; mid-execution follow-ups, external edits, and file replacements all sync. CLI progress stays in the run log. If the model already wrote `output.md`, the background adopts the file and does not overwrite it with the CLI's confirmation message when execution ends; if the model did not write the file, the background creates it from the final answer.

An Order's Input Cell also renders that run's `input.md` live, supporting Markdown and task lists. An Order that has not run yet previews its current requirement as Markdown too (formulas included) instead of showing raw source first. At run start the current instruction is written to the file; the harness requires Claude to update a current full requirement summary, confirmed constraints, and completion status before the end of each conversation turn — replacing the prior version — so appended or adjusted requirements flow back into the Input Cell. Click **Edit** or double-click the requirement body to modify directly; saves sync back to the file. If the model updates the file while you are editing, your draft is kept and you are offered **Use file version** and **Overwrite file with my version**. Auto-saves of unrelated fields preserve the latest file contents.

The latest follow-up input is also cached to the notebook and used for subsequent context and cell merge/split. Input updates invalidate downstream results; the initial `.castle/prompts/input<run-ID>.md`, `context.md`, and the run record's `input_snapshot` keep the original content. `input.md` is capped at 32,000 characters; if the file is missing or temporarily unreadable, the UI explicitly shows the notebook snapshot and resumes syncing once it recovers. Old runs auto-create the requirements file on first read; re-entering an old session via **Open conversation** tells Claude the new file-maintenance requirements once — already-open old terminals must reopen the conversation to receive this note.

Order title bar and the **Cell** menu support **Merge AI Cells** and **Split AI Cells**, with an optional organizing instruction. The agent rewrites instructions, expected outputs, summaries, and existing Out conclusions, then automatically replaces and saves the original cell:

- **Merge**: Select another Order, combine into one cell placed at the earlier of the two positions; other cells between them keep their order. The merged cell clears the original AI conversation link; the next run starts a new session.
- **Split**: Divide into two consecutive Orders by two task boundaries, and fork independent sessions for each via Claude native `--resume <original-session-id> --fork-session` (see [session forking](https://code.claude.com/docs/en/sessions#branch-a-session)). If the original cell has no recoverable conversation, only content is split. Each conversation uses its own input file and `output.md` and can be continued separately.

Organizing only changes notebook content and does not re-execute the original task. New cells save the updated `.castle/prompts/input<run-ID>.md` and output files respectively; original input snapshots and run evidence remain archived — merge no longer links the original conversation, split keeps the original execution history. Downstream old results are marked stale; organizing does not re-validate results whose conclusions are already stale or whose execution failed. The organizing process can be reviewed in the new cell's records. On failure, cancellation, timeout, session-fork failure, or changes to Global / original output during organizing, the original cell is kept to avoid overwriting new content.

The latest file content is also cached to the notebook and read when downloading the notebook. If the file is temporarily unavailable, the UI clearly flags it and shows the saved snapshot; an empty file clears the display and resumes syncing after recovery. The live display cap is 2 MB. Output changes invalidate existing downstream results, and subsequent AI context uses the current file content. Run history's original output and fingerprints retain the record from first execution.

Incomplete tool-protocol text, truncated answers, and failure states are not marked successful; raw output is retained. On startup, legacy Order records with erroneous success are validated and corrective events are appended. When JSON is chosen, strict syntax validation is applied to the file or final answer adopted on first execution; legacy project-init JSON results additionally undergo structural validation. Natural-language output contracts still require researcher review.

**Initialize from project**: **File → Initialize from project** lets you preview the directory first. Confirming creates an initialization Order cell and prompts you to run it manually: the instruction embeds the current project path and asks Claude to read the historical conversation `*.jsonl` files (excluding the current conversation) under the matching `~/.claude/projects/` directory, group requirements by time and purpose, merge them into ≤5 AI Order sections, and rewrite this notebook directly. Input cells stay high-level; requirement and run details go into Output cells. Legacy JSON split results can still be adopted via **Adopt split result**. The UI supports the launch directory and its subdirectories; for other projects, start with `--project`.

## Save & reproduce

Notebook documents and run evidence default to the `.castle/` directory of the launched project, already in this repo's ignore rules. When bound to a subproject, documents are still managed by the current service's storage directory; the subproject's own `.castle/` holds context configuration and Claude input files.

```text
.castle/
  context.json              # Context feature flags, harness templates & character budgets
  settings.json             # UI safety switches such as run confirmation
  prompts/input<run-id>.md   # Full input Claude reads itself
  notebooks/<notebook-id>.json
  runs/<run-id>/
    context.md       # Full input snapshot at launch, SHA-256 fingerprint
    input.md         # Requirement summary updated with the live conversation, read live by Input Cell
    output.md        # Output updated with the live conversation, read live by Out Cell
    events.jsonl     # Timestamps, streamed output, tool events, terminal state
    project-before.json / project-after.json  # Pre/post execution project file fingerprints
    changes.json     # Actual list of files added, modified, deleted
    run.json         # Input version, config snapshot, executor, duration, output validation, errors, etc.
```

Documents auto-save using atomic replacement with version-conflict checks. **File → Download notebook** exports the document and recent outputs; individual run records can be downloaded in the inspector. **File → Import from file…** reads an exported notebook JSON and creates a copy under the current project (new document and cell IDs, bound to the current project); prior outputs are preserved and Order input/output content is written immediately into the copy's board files, while run evidence is not migrated and re-running produces new results; the original notebook file is never modified, and exporting still uses **Download notebook**. UTC timestamps display in the browser's local timezone.

File changes are collected by comparing pre/post execution content fingerprints, skipping hidden files, dependencies, builds, temp directories, and run evidence; at most 2,000 files / 20 directory levels are scanned, with an 8 MiB per-file and 64 MiB total read cap. Incomplete coverage is clearly indicated. The manifest reflects changes observed during execution; files modified by editors or other tools may also be included. It does not auto-rollback files and is not equivalent to source version control.

Closing the service clears kernel memory. After restart, historical outputs remain viewable, but related computed results are marked stale; re-run upstream to restore. Run snapshots track evidence and are not a full environment/dependency/external-data freeze.

## Validation

```powershell
python -m unittest discover -s tests -v
node --check castle_notebook/static/app.js
node --test tests/test_app.js
```

Tests cover real Python subprocesses, project module imports, agents writing actual project files, change collection, shared variables, cancel/timeout, invalidation propagation, context compaction, real HTTP/SSE communication, output contracts, and initialization. Claude tests use subprocess protocol fixtures and incur no model cost; this differs from real Claude cloud-call validation.

The service binds only to `127.0.0.1`, restricts Host / Origin, and protects write operations with a page-session token. Python has the current user's file access; Claude defaults to auto-approval, adjustable via launch flags. 0.9 provides no OS-level sandbox or multi-user authentication and is not suitable for exposure as a remote public service.

For detailed scope and design, see the [0.9 development plan](docs/0.9-plan.md) (Chinese).