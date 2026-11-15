# 命令与使用流程

以下命令在目标项目根目录运行。`HELPER` 替换为技能目录下 `scripts/notebook.py` 的路径；本项目中是 `.claude/skills/castle-notebook/scripts/notebook.py`。路径含空格时加引号。

## 创建与使用

把整个 `castle-notebook` 文件夹放入目标项目的 `.claude/skills/`，Claude Code 即可发现本技能。也可以放进其个人技能目录。不要只复制 `SKILL.md` 而漏掉脚本和参考文件。用户不需要安装 CastleNotebook Python 包或启动其服务器。

在 Claude Code 中可以直接请求：

```text
/castle-notebook 从当前项目初始化 research.ipynb，整理 Global 和第一批 Order，不执行任务。
/castle-notebook 修改 research.ipynb 的 Global，补充“所有改动必须保留现有 API”。
/castle-notebook 将 research.ipynb 中的 order-... 和 order-... 合并。
/castle-notebook 将 research.ipynb 的 order-... 拆成实现和验证两个 Order。
```

Notebook 展示使用已有的 JupyterLab、Jupyter Notebook 或 VS Code Notebook。没有环境时可以安装：

```text
python -m pip install jupyterlab
python -m jupyterlab
```

创建计划 JSON，全部使用 UTF-8；也可以直接采用本目录下的 `example-plan.json`：

```json
{
  "global": "# Global\n\n## 目的\n实现可检查的项目研发过程。\n\n## 约束\n修改真实源码；每次报告验证结果。",
  "orders": [
    {
      "title": "分析当前结构",
      "instruction": "只读查看项目入口和测试，归纳模块职责与下一步建议。",
      "expected_output": "Markdown 报告，包含真实文件路径、已知问题和待验证假设。"
    }
  ]
}
```

```text
python HELPER init research.ipynb --project . --plan plan.json
python HELPER inspect research.ipynb
```

`init` 返回实际 Order ID。空白初始化可省略 `--plan`。已有普通 `.ipynb` 不会被静默转换或覆盖；可以创建新 Notebook，再将选定的普通单元原样复制进去。

打开生成的 Notebook，保存后暂停该编辑器的自动保存，再运行 Order 后面的 Python 单元。运行单元会：

1. 从已保存 Notebook 导出输入，构造上下文和独立运行目录。
2. 在项目目录执行 `!claude -p … --output-format json`，提示中要求读取技能并完成当前任务。
3. 检查 Agent 已写出结果和摘要、完成写回，并检查 CLI 真实退出状态。
4. 在 Python 的原生输出区展示 `output.md`。

生成的运行单元支持内核位于 Notebook 所在目录或项目根目录。移动 Notebook 或技能安装位置后，应调整运行单元中的定位路径。

## 文件与属性

```text
research.ipynb
.castle-notebook/<notebook-id>/
  context.json            # 可编辑的上下文模板
  global.md               # 最近导出的 Global
  inputs/order-....md     # 最近导出的 Order 指令及预期输出
  runs/run-.../
    request.md            # Claude 启动请求，明确技能、Notebook、run ID
    context.md            # 完整实际上下文
    run.json              # 输入指纹、状态、输出路径、会话
    output.md             # Agent 写入的最终结论
    summary.md            # Agent 写入的后续上下文摘要
    response.json         # CLI 返回值，包含真实 session_id
  backups/                # 每次改写前的完整 Notebook
  archive/                # 合并、拆分前的原始单元
```

Notebook 元数据中的 `project_root`、`state_dir`、`helper` 相对 Notebook 所在目录；Windows 上技能与项目跨盘时，`helper` 使用绝对路径。Order 的 `input_file` 相对项目根目录。运行元数据中的路径为绝对路径。分发技能只需复制技能文件夹；迁移一个已执行的 Notebook 时应连同状态目录迁移，并调整内部路径。

Order Markdown 的 `metadata.castle_notebook` 包含 `order_id`、`title`、`input_file`、`conversation`、`latest_run`、`status` 和时间戳。预期输出在 Markdown 的 `castle:expected-output` 注释后，直接编辑可见内容即可。Python 单元保存关联 ID、角色和原生输出。

`context.json` 的 `history_characters` 是每个历史输入或输出摘要的字符上限，默认 1400；不会直接丢弃较早的单元。`template` 可以调整拼接方式，可用变量：`{project}`、`{global}`、`{history}`、`{current}`、`{expected_output}`、`{writeback}`。普通花括号用 `{{` 和 `}}` 转义。保留全部变量以维持输入和输出契约。未执行的示例 Notebook 可以在状态目录缺失时按默认配置重新生成配套文件。

原生 Python 单元的已保存输出会明确标成“新鲜度未知”，不假装脚本能追踪内核变量或证明旧代码结果仍然有效。上游 Order 已失败、输入变化或输出不可用时，其路径仍进入上下文，但旧结果不作为有效证据。

## 编辑、合并和拆分

```text
python HELPER set-global research.ipynb --file global-new.md
python HELPER add-order research.ipynb --spec order.json
python HELPER update-order research.ipynb --cell order-... --spec order-new.json
python HELPER merge research.ipynb --cells order-A order-B --spec merged.json
python HELPER split research.ipynb --cell order-A --spec split.json
python HELPER conversation research.ipynb --cell order-A
python HELPER sync research.ipynb
```

`order.json`、`order-new.json`、`merged.json` 使用上面的单个 Order 结构。`split.json` 是恰好两个 Order 对象组成的数组。指令语义的归纳由 Agent 完成，脚本执行结构变换。

Global 或 Order 改动后，旧输出保留并标为 `stale`。执行时也会按当前保存内容比较指纹，因此直接编辑 Markdown 后不会把旧上游结果当成有效结果。合并、拆分后的下游已执行结果也标为过期。新合并/拆分单元没有已完成输出，历史保留在归档和原运行目录。

合并清除会话绑定，不删除外部 Claude 历史。拆分只保存 `fork_from`，不立即调用模型；两个新 Order 首次运行时分别分叉，并把 CLI 返回的实际新会话 ID 写回。普通重跑默认创建新会话，使用完整当前上下文；继续旧对话可运行 `conversation` 返回的命令。对尚未执行的分支，`conversation` 返回的命令会分叉供终端查看；分支与 Order 的正式绑定仍以运行单元首次执行后的 CLI 返回值为准。

Jupyter 页面加载的是内存副本。完成后需要 **Reload from Disk**；若编辑器提示磁盘文件变化，选择磁盘版本。结构编辑后不要按旧页面的保存按钮。

## Agent 执行与故障恢复

运行单元自动调用以下协议；当前 Agent 收到 execute 请求时只执行当前任务和 `complete`，不要再调用 `prepare` 或启动另一个 Claude：

```text
python HELPER prepare research.ipynb --cell order-...
# Agent 读取 run.json 与 context.md，写 output.md 和 summary.md。
python HELPER complete research.ipynb --run run-...
# 外层运行单元写好 response.json，再做 finish。
python HELPER finish research.ipynb --run run-... --exit-code 0
```

状态依次为 `idle → running → written → succeeded`；CLI 错误或缺失写回为 `failed`，执行期间输入变化为 `stale`。`complete` 不根据 CLI 的最终回复伪造输出。调用 `finish` 时必须传真实退出码。

停止或内核中断后，确认 Claude 已退出，再用 `finish … --exit-code 130` 将遗留运行标记为失败，之后可重新执行。权限不允许写文件时，处理当前 Claude 权限问题再重跑；技能不会自动绕过权限，也不会在失败时无限重试。

续聊更新同一 `output.md` 与 `summary.md` 后调用 `complete`；`sync` 可以从这些文件恢复 Notebook 的输出展示。若旧页面覆盖了合并或拆分，从 `backups/` 找到所需版本恢复；不要简单用 `sync` 重建已经丢失的结构。

生成单元通常使用标准 `!claude` 和文件重定向。Windows 路径含 `%` 等无法可靠通过 shell 引用的字符时，运行单元自动改用 Python `subprocess.run(argv, input=...)` 调用同一个 Claude CLI；输入内容仍通过 UTF-8 文本传入。

接口依据：[Claude Code Skills](https://code.claude.com/docs/en/skills)、[CLI 参数](https://code.claude.com/docs/en/cli-reference)、[Jupyter Notebook 格式](https://nbformat.readthedocs.io/en/latest/format_description.html)。
