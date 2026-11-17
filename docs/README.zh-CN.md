# CastleNotebook 0.9 — 完整中文文档

> 英文版见仓库根目录 [README.en.md](../README.en.md)。
> 快速上手见根目录 [README.md](../README.md)。

在使用 LLM Agent 进行聊天式项目研发时，通常存在信息展示不直观、全局信息不透明的问题。该问题造成了需求的重复提出、修改困难、复现不稳定，因此推出 CastleNotebook。
项目参考 Jupyter Notebook 的核心哲学：实时展示
界面采用经典 Jupyter 布局

## 运行

需要 Python 3.10+。Python 内核使用 IPython 执行单元；网页不需要 Node 或前端构建。

```bash
pip install -e .
python -m castle_notebook
```
使用时，
```bash
cd 你的项目目录
castle-notebook
```
然后浏览器访问打开 <http://127.0.0.1:8765>。首次启动创建合成数据示例；也可从“文件”菜单新建空白、金融研究或算法验证 Notebook。
如果同时打开多个项目，请指定端口
```bash
cd 你的项目目录
castle-notebook --port 指定数字
```


指定研究项目和 Python 环境：

```powershell
python -m castle_notebook --project E:\research\my_model --python E:\conda\envs\research\python.exe
```

Python 和 Claude 都在指定项目目录运行；Python 内核将该目录加入模块搜索路径，可直接导入项目源码。指定 `--python` 时，该解释器也需要安装 IPython（`"路径/python.exe" -m pip install "ipython>=8.18"`）。NumPy、pandas、Matplotlib 使用该解释器已有的安装；它们是可选依赖。调用 `display(df)` 展示表格，`display_markdown(text)` 展示 Markdown；Matplotlib 图像会在执行完成时自动收集。

Markdown 渲染支持单元格内公式：`$…$`、`\(…\)` 为行内公式，`$$…$$`、`\[…\]` 为行间公式（两个 `$$` 可以单独占行）。KaTeX 首次使用时由本地服务在后台从镜像下载（优先 npmmirror，失败回退 jsdelivr、unpkg），页面不会等待下载，就绪前公式暂时显示源码；资源只从 `127.0.0.1` 提供，不向页面注入任何第三方脚本。已下载的文件缓存在安装目录的 `static/math/`，离线时沿用上次下载的副本。

Markdown 预览也支持图片。`![说明](路径)` 的相对路径按 Notebook 所在项目目录解析；保存时，项目内的 PNG/JPEG/GIF/WebP/AVIF/BMP 图片会读取为 base64 附件写入 Notebook（Jupyter attachments 格式），源码改写为 `attachment:image-<hash>.png`，之后即使原文件移动也能显示。单张内嵌图片上限 5 MB，单个 Notebook 合计上限 100 MB；超过限制、位于项目外或读取失败的引用保持原样，仍可通过本地项目图片端点显示。`https://` 远程图片和 `data:image` 内联图片可直接显示（CSP 只放行这两类外部图像源，页面脚本仍只允许同源）。

Python 单元支持 [IPython 语法](https://ipython.readthedocs.io/en/stable/interactive/reference.html#system-shell-access)：`!命令`、`files = !命令` 捕获输出、`$变量` / `{表达式}` 插值，以及 `%pwd`、`%cd`、`%time`、`%%time`、`%%capture`、`%pip` 等魔法命令。普通 Python 与 `!` 可以混写，字符串和注释中的 `!` 不受影响：

```python
import sys
!"{sys.executable}" --version
files = !dir
%pwd
```

命令使用当前系统的 shell（Windows 为 `cmd.exe`，Linux/macOS 为 `/bin/sh`），例如 Windows 使用 `!dir`，Linux/macOS 使用 `!ls`。`!cd` 只改变子 shell 的目录，持续切换内核目录应使用 `%cd`。安装包建议用 `%pip install 包名`，以使用当前内核解释器。命令输出会流式进入 Output；与 IPython 一致，普通 `!` 命令的非零退出码保存在 `_exit_code`，不会单独使 Python 单元失败。停止或超时会结束内核及其命令子进程。当前仍使用 Castle 自己的通信、输出展示与 Notebook 文件格式；不支持 Jupyter widgets、交互式终端输入或完整 Jupyter 协议。

工具栏的项目名称显示当前 Notebook 绑定的目录，点击后可查看项目文件与当前源码预览。新建 Notebook 可选择启动目录下的项目子目录；新建不依赖“从项目初始化”，也不需要复制项目实现到单元中。

## 工作方式

1. 在 **Global** 写目的与约束。默认完整进入每次 AI 调用，可在上下文配置中分别开关。
2. 添加 **Python / Markdown / Order** 单元。Order 发起项目开发、修复或分析步骤，开发任务直接修改项目源码和测试；Python 单元导入项目模块、提供交互输入并检查结果，共享当前 Notebook 的内核变量。Order 需要明确预期输出。
3. 选择单元，点击“运行”，或按 `Ctrl+Enter`。`Shift+Enter` 执行并选择下一单元；Markdown 双击编辑、执行后预览。
4. 点击右上角检查器图标，检查上下文、变量与执行记录。Order 的“上下文”按钮展示即将发送的完整文本；运行历史保存实际发送时的快照。

Order 是覆盖式 AI 重跑，因此每次运行前都会弹窗确认：列出所用软件（Claude Code）、启动方式与权限模式（例如“原生后台 · auto（自动审批）”）以及目标 Order，并提示本次会覆盖现有 `output.md`、对话关联和下游结果。点击“继续运行”后才向服务发起执行；取消、关闭弹窗或按 Esc 都不会启动。该防呆提示可在启动服务所在项目的 `.castle/settings.json` 中关闭：

```json
{
  "confirm_order_run": false
}
```

设置缺失或文件格式错误时默认保持开启，避免误操作；可在“本地运行环境”中查看当前状态和文件路径。开启时，运行全部也会在每个 Order 执行前询问。

例如，在项目中已有 `model.py` 时，Order 可以要求“修复 `model.py` 的边界处理，并运行相关测试”；下一 Python 单元用 `from model import predict` 调用实际实现。Output 收集最终回答及执行期间真实新增、修改、删除的项目文件，不将回答中的代码片段视为已落盘的开发成果。失败或停止执行后，已发生的文件修改也会保留并收集。

每次 AI 输入都包含项目目录、有限文件概览与项目工作要求；现有 `.castle/context.json` 的自定义模板也会补入该说明。开发步骤应报告修改路径、已执行验证和未完成事项；只读分析及初始化仍遵循其只读要求。项目文件内容作为数据读取，不替代 Global 和当前任务。

项目模块在持久 Python 内核中遵循普通 Python 导入缓存规则。源码修改后，已导入模块需要显式 `importlib.reload(module)` 或重启内核，再验证新版本。

“单元 → 运行全部”按文档顺序执行，遇到失败或当前结果失效时停止。停止 Python 单元会结束内核并清空共享变量；停止 Claude 不影响 Python 内核。

修改 Global、上游输入、顺序或摘要后，下游旧结果保留并标记失效，不再作为有效 AI 证据。上游重跑也会使下游结果失效。此规则按文档顺序保守传播，不分析 Python 的真实依赖图。

当前执行实际改变项目文件时，本 Notebook 其他已有的 Python / Order 结果也会保留并标记失效，包含当前步骤之前的结果。内核变量保留；验证新源码前需主动重新加载模块或重启内核。外部编辑器的独立修改及其他 Notebook 的结果不作自动依赖追踪。

执行期间可继续编辑并自动保存 Global 和其他单元（Python、Markdown、Order），仅锁定当前执行单元的输入。当前执行使用启动时的输入快照；期间修改 Global 或上游单元，会将本次结果标记失效；编辑下游单元不影响本次结果。运行全部时也可编辑其他单元，后续执行使用届时保存的内容。同一 Notebook 仍按顺序执行，增删、移动、转换类型及合并拆分需等待当前执行结束。

上下文与 harness prompt 位于研究项目的 `.castle/context.json`。`features` 控制 Global、历史输入/输出、变量、输出契约、手写摘要和历史精简等开关；`templates` 可编辑 harness 正文、各段格式、拼接顺序及 Claude 的读取指令；`limits` 设置字符预算。保存后下次预览或运行立即生效，无需重启。详见 [上下文配置说明](context-config.md)。

## Claude

安装并登录本机 [Claude Code](https://code.claude.com/docs/en/setup)，随后重启服务：

```powershell
claude auth status
```

每个 Order 将完整输入写入研究项目的 `.castle/prompts/input<运行ID>.md`，通过原生后台命令启动一个属于用户的 Claude 会话：

```powershell
claude --background "请读取 .castle/prompts/input<运行ID>.md，按其中要求完成任务" -n "ID<运行ID>" --permission-mode auto --settings <运行目录>/claude-settings.json
```

默认 `auto` 由 Claude 的审批模型检查工具调用，减少日常确认，需要用户处理的提问或权限请求可以进入实时对话处理。`bypassPermissions` 会跳过常规审批，等价于 `--dangerously-skip-permissions`；只有显式传入 `--claude-permission-mode bypassPermissions` 才启用。也可选择 `acceptEdits`、`default`、`plan`、`dontAsk`。模型、工具、MCP 和登录继续使用本机配置。首次使用原生后台前，在项目目录运行一次 `claude` 完成目录信任确认；未完成时网页显示 CLI 的具体错误。auto 可能因模型或设置限制退回 Manual，界面展示实际权限模式，不自动切换到 bypassPermissions。参考 [CLI 参数](https://code.claude.com/docs/en/cli-reference)与[权限模式](https://code.claude.com/docs/en/permission-modes)。

Notebook 只负责触发与收集，不占有会话。`claude agents` 可查看后台列表，`claude agents --cwd <项目目录>` 只看当前项目，`claude attach <Background ID>` 可随时进入同一个实时对话，处理提问、权限或继续交流；退出终端后会话仍保留。关闭网页或停止 Notebook 服务不会终止原生后台会话，重启服务会继续收集原来的运行，不重复发起任务。只有明确点击“停止”才调用 `claude stop`。原生后台不受 `--agent-timeout` 限制，等待用户处理时持续监听。参考 [Claude 原生后台会话](https://code.claude.com/docs/en/agent-view)。

每次运行的 `claude-settings.json` 只附加记录事件的命令 Hook，将工具活动、实际权限模式和最终回答保存到运行目录。后台状态来自 `claude agents --json --all`，可见进度来自 `claude logs`；不解析 Claude 私有状态或 transcript。完整输入与 `context.md` 内容及 SHA-256 指纹一致，命令行只传文件路径和固定读取指令。

旧版 CLI 或无原生后台能力的环境可显式使用 `--claude-launch-mode print`，此时调用 `claude -p ... --permission-mode auto --output-format stream-json --verbose --include-partial-messages`。`--background` 与 `-p` 不能混用；print 会话不出现在原生后台列表，执行完成后按 UUID 恢复。出现工具权限拒绝时，保留原始回答并标记执行失败。
`templates.instructions` 通过 `{input_file}`、`{run_dir}`、`{output_file}` 告知 Claude 当前需求和输出的位置，默认展开为 `.castle/runs/<运行ID>/input.md`、`.castle/runs/<运行ID>` 和 `.castle/runs/<运行ID>/output.md`，相对于项目工作目录。预览与执行复用同一运行 ID，输入文件和上下文快照中的路径一致。`templates.invocation` 也支持这些变量，启动指令不自动追加路径。

Windows 和 Linux 下，Order 单元底部显示 **Session Name**、**Background ID** 和实际权限模式。原生后台会话启动后，“打开对话”立即可用，通过 `claude attach <id>` 在新终端进入同一实时对话，执行中也能处理问题；历史 print 会话使用 `claude --resume <session-id> --permission-mode <配置值>`。Linux 优先使用 `TERMINAL`，再自动查找常用终端；无桌面的服务器可直接在自己的终端运行 `claude attach`。未执行或未生成会话时按钮禁用。续聊对 `input.md`、`output.md` 的更新继续同步到对应单元。
Order 的 Out Cell 以最近一次运行的 `output.md` 为来源，在页面可见时约每秒检查一次变化，实时渲染文件；执行中以及执行完成后的续聊、外部编辑、文件替换都会同步。CLI 进度保留在运行日志中。模型已写出 `output.md` 时，后台直接采用文件，执行结束不会用 CLI 的确认消息覆盖它；模型未写文件时，后台用最终回答创建该文件。

Order 的 Input Cell 同样实时渲染该运行的 `input.md`，支持 Markdown 和任务清单。尚未运行的 Order 也会以 Markdown 预览当前需求（含单元格内公式），不会先显示原始源码。运行时先将当前指令写入该文件，harness 要求 Claude 在每轮对话结束前更新当前完整需求摘要、已确认约束和完成状态，可替换上一版，因此追加或调整的需求会回到对应 Input Cell。点击“编辑”或双击需求正文可直接修改，保存时同步写回文件；编辑期间文件被模型更新时保留草稿，并提供“使用文件版本”和“用我的版本覆盖文件”。无关字段的自动保存会保留最新文件内容。

续聊的最新输入也会缓存到 Notebook，并用于后续上下文和单元合并、拆分。输入更新会使下游结果失效；初始 `.castle/prompts/input<运行ID>.md`、`context.md` 和运行记录中的 `input_snapshot` 保留原始内容。`input.md` 上限为 32000 字符；文件丢失或暂不可读时明确显示 Notebook 快照，恢复后继续同步。旧运行首次读取时自动补建需求文件，通过“打开对话”重新进入旧会话时会一次性告知 Claude 新的文件维护要求。已打开的旧终端需重新打开对话后接收该说明。

Order 标题栏及“单元”菜单支持 **Merge AI Cells** 和 **Split AI Cells**，可补充整理要求。Agent 会重写指令、预期输出、摘要和已有 Out 结论，完成后自动替换原单元并保存：

- **合并**：选择另一个 Order，归纳为一个单元，放在两者较早的位置；中间其他单元保持原有顺序。合并后的单元清空原 AI 对话关联，下次运行开启新会话。
- **拆分**：按两个任务边界拆成连续的 Order，并通过 Claude 原生 `--resume <原会话ID> --fork-session` 为两者复制独立会话（[会话分叉说明](https://code.claude.com/docs/en/sessions#branch-a-session)）。原单元没有可恢复对话时只拆分内容。每份对话使用独立的输入文件和 `output.md`，可以分别续聊。

整理只改 Notebook 内容，不重新执行原任务。新单元分别保存更新后的 `.castle/prompts/input<运行ID>.md` 和输出文件；原输入快照与运行证据保留归档，合并后不再关联原对话，拆分后保留原执行历史。下游旧结果标记失效；原结论已失效或执行失败时，整理不会使它重新有效。整理过程可从新单元的记录中查看。失败、取消、超时、复制会话失败，或整理期间 Global / 原输出发生变化时，保留原单元，避免覆盖新内容。

最新文件内容同时缓存到 Notebook，下载 Notebook 时也会读取当前文件。文件暂不可用时会明确提示并展示已保存的快照；空文件会清空显示，恢复后继续同步。实时显示上限为 2 MB。输出改变会使下游已有结果失效，后续 AI 上下文使用当前文件内容。运行历史中的原始输出和指纹保留首次执行时的记录。

未完成的工具协议文本、截断回答和失败状态不会被标为成功，原始输出仍保留；启动时会校验旧 Order 的错误成功记录并追加纠正事件。选择 JSON 时对首次执行采用的文件或最终回答执行严格语法校验；旧版项目初始化的 JSON 结果另做结构校验。自然语言输出契约仍需研究者检查。

从项目初始化：“文件 → 从项目初始化”，可先预览目录。确认后生成一个初始化 Order 单元并提醒手动运行：指令动态写入当前项目路径，要求 Claude 读取 `~/.claude/projects/` 下对应目录的历史对话 `*.jsonl`（本次对话除外），按时间、目的归类需求，合并成 ≤5 个 AI Order 板块并直接改写本 notebook；Input Cell 高度概括，需求运行结果详情放 Output Cell。旧版初始化生成的 JSON 拆分单元仍可通过“采用拆分结果”采用。界面支持启动目录及其子目录；其他项目使用 `--project` 启动。

## 保存与复现

Notebook 文档和运行证据默认位于启动项目的 `.castle/`，已加入本仓库忽略规则。绑定子项目时，文档仍由当前服务的存储目录管理；子项目自己的 `.castle/` 保存上下文配置和 Claude 输入文件。

```text
.castle/
  context.json              # 上下文功能开关、harness 模板与字符预算
  settings.json             # 运行确认等界面安全开关
  prompts/input<run-id>.md   # Claude 自行读取的完整输入
  notebooks/<notebook-id>.json
  runs/<run-id>/
    context.md       # 启动时的完整输入快照，SHA-256 指纹
    input.md         # 随当前对话更新的需求摘要，Input Cell 实时读取
    output.md        # 随当前对话更新的输出，Out Cell 实时读取
    events.jsonl     # 时间戳、流式输出、工具事件、终态
    project-before.json / project-after.json  # 执行前后项目文件内容指纹
    changes.json     # 实际新增、修改和删除的文件清单
    run.json         # 输入版本、配置快照、执行器、耗时、输出校验值、错误等
```

文档自动保存，使用原子替换与版本冲突检查。“文件 → 下载 Notebook”导出文档与最近输出；独立执行记录可在检查器下载。“文件 → 从文件导入…”读取一份导出的 Notebook JSON，在当前项目下建立一个副本（新文档与单元 ID，绑定当前项目）；副本不携带原执行记录，单元需重新运行，原始笔记本文件不会被改动，如需导出仍用“下载 Notebook”。UTC 时间戳在界面按浏览器本地时区显示。

文件变更通过执行前后的内容指纹比较收集，跳过隐藏文件、依赖、构建、临时目录和运行证据；最多扫描 2000 个文件、20 层目录，单文件读取上限 8 MiB、总计 64 MiB。覆盖不完整会明确显示。清单反映执行期间观测到的变化，同时使用编辑器或其他工具修改的文件也可能计入；它不自动回滚文件，也不等同于源码版本管理。

关闭服务会清空内核内存。重启后历史输出仍可查看，但相关计算结果会标记失效；重新运行上游即可恢复。运行快照用于追踪证据，不等于完整环境、依赖或外部数据的冻结。

## 参数

```text
--project PATH        项目根目录，默认当前目录
--port 8765           本机端口
--python PATH         内核解释器，默认启动服务的 Python
--claude PATH         Claude 可执行文件或 npm cli.js
--claude-launch-mode background   原生后台（默认）；print 为旧版非交互模式
--claude-permission-mode auto   Claude 自动审批（默认），需要时进入终端处理
--data-dir PATH       文档与证据目录，默认 <project>/.castle
--code-timeout 60     Python 执行秒数上限
--agent-timeout 240   print 模式 Claude 执行秒数上限；原生后台不设期限
--empty               不自动创建首次示例
```

## 验证

```powershell
python -m unittest discover -s tests -v
node --check castle_notebook/static/app.js
node --test tests/test_app.js
```

测试包含真实 Python 子进程、项目模块导入、代理写入实际项目文件、变更回收、共享变量、取消/超时、失效传播、精简上下文、实际 HTTP/SSE 通信、输出契约和初始化。Claude 测试使用子进程协议夹具，不产生模型费用；这与真实 Claude 云端调用的验证不同。

服务仅绑定 `127.0.0.1`，限制 Host / Origin，并使用页面会话令牌保护写操作。Python 拥有当前用户的文件访问权限；Claude 默认使用 auto 自动审批，可用启动参数调整。0.9 不提供操作系统级沙箱或多人认证，不适合暴露为远程公共服务。

详细范围与设计见 [0.9 开发说明](0.9-plan.md)。