---
name: castle-notebook
description: 用标准 Jupyter Notebook 管理聊天式项目研发：以 Markdown 表达 Global 和 Order，以 Python 单元调用 Claude Code，回写原生输出，编辑、合并、拆分指令，或从已有项目初始化笔记本。适用于 CastleNotebook 的 Notebook 控制面板方案。
---

# CastleNotebook

把 `.ipynb` 当作项目控制面板。项目源码留在原目录；用本技能及其 Python 脚本管理 Global、Order、执行上下文和输出，不需要启动 CastleNotebook Web 服务。

脚本为本技能目录下的 `scripts/notebook.py`，下文以 `HELPER` 表示其实际路径。脚本只需要 Python 3.10+ 标准库；展示、执行需要 Jupyter/IPython，以及已安装并登录的 Claude Code。

## Notebook 约定

- **Global**：一个带角色元数据的 Markdown 单元，写项目目的与约束，可以直接编辑。
- **Order**：一个可编辑的 Markdown 单元，包含指令和必填的预期输出；紧随其后的 Python 单元执行 `!claude`。保留 Markdown 中区分指令和预期输出的 HTML 注释。
- **Output**：该 Python 单元的原生 `outputs`。Agent 把最终结论写到本次运行的 `output.md`，调用脚本回写；执行单元也读取同一文件展示结果。
- **状态**：角色、稳定 ID、时间戳、文件路径和会话引用放在 `metadata.castle_notebook`。普通 Markdown/Python 单元保持原样。
- **输入来源**：已保存的 Notebook Markdown 是当前输入；`inputs/` 是导出的输入文件。Agent 编辑输入应走脚本，让 Notebook 和输入文件同步。

首次使用或涉及结构修改，先阅读 [references/workflow.md](references/workflow.md)，其中有命令、JSON 结构和恢复方法。可用 [references/example-plan.json](references/example-plan.json) 创建演示 Notebook。

## 按用户意图操作

| 意图 | 做法 |
| --- | --- |
| 新建 Notebook | 整理 Global 和首批 Order 为 plan JSON，运行 `init`。所有 Order 都声明预期输出。 |
| 从项目初始化 | 只读查看项目说明、入口和相关文件，生成 plan，再运行 `init`；Order 引用真实项目文件，不把整个项目搬进 Notebook，不自动执行生成的任务。 |
| 编辑 Global | 把 Markdown 写入临时 UTF-8 文件，运行 `set-global`。Global 的目的与约束是整个项目当前版本的定义。 |
| 新增或替换 Order | 生成包含 `title`、`instruction`、`expected_output` 的 JSON，运行 `add-order` 或 `update-order`。 |
| 执行 Order | 常规入口是它的 Python 单元。当前进程收到 `execute` 请求时，按下方协议处理；不要再次运行 `!claude`，避免递归启动 Agent。 |
| 更新输出 | 写当前运行的 `output.md` 和 `summary.md`，运行 `complete`。续聊修改同一输出文件；重新展示用 `sync` 或重新载入 Notebook。 |
| 合并 | 读取两个 Order，归纳为一个新 spec，再运行 `merge`。更新输入文件，清除新单元的对话关联；旧单元和运行证据归档，不删除 Claude 外部会话。 |
| 拆分 | 归纳出两个各自有预期输出的 spec，再运行 `split`。两个结果保留原对话的分支起点，首次运行分别通过 `--resume … --fork-session` 创建独立副本，不能将同一个活动会话当成两个副本。 |
| 打开对话 | `conversation` 返回项目目录与 `claude --resume …` 命令，在该目录的外部终端执行。CLI 会话使用终端入口，不生成虚构的网页链接。 |

合并后的新单元放在两个原单元较早的位置；中间其他单元的相对顺序不变。拆分结果放在原位置。旧结果保留在归档中，新任务不继承“已完成”状态。

## execute 协议

Notebook 的运行单元先调用 `prepare`，生成 `run.json`、`context.md` 和 `request.md`，随后调用 Claude。请求中的文件路径与运行 ID 是本轮写回目标。

1. 读取请求指定的 `run.json` 和 `context.md`。上下文包含完整 Global、每一个上游单元的精简信息、上游输出文件路径、当前指令、预期输出和输出位置。当前任务和 Global 不截断。
2. 只完成当前 Order。开发指令修改真实项目文件；分析指令按其范围分析。不要顺带执行后续 Order。
3. 把最终结果写入 `run.json` 指定的 `output_file`，把供后续步骤使用的简短结论写入 `summary_file`。摘要保留关键决策、改动路径、已做验证、未完成事项；不能把失败或假设写成已验证结论。
4. 运行 `python HELPER complete NOTEBOOK --run RUN_ID`，把结果回写到 Notebook 的原生输出区。脚本会检查执行期间已保存的输入变化，并标记过期结果。
5. 回复已写入的文件路径即可。外层运行单元用 `finish` 检查 CLI 退出状态、实际会话 ID 和写回结果；缺失文件或 CLI 失败不会标为成功。

后续对话修改结论时，同样更新 `output.md`、`summary.md` 并调用 `complete`；已有成功运行的写回保留成功状态。若当前指令或 Global 已变化，先重新运行 Order，不能把旧运行结果冒充新输入的结果。

## 上下文与编辑边界

每次执行都包含全部上游单元的一个条目。优先用 Agent 写出的摘要，否则使用明确标注截断的文本摘录；需要细节时按路径读取原文件。模板和条目长度在生成的 `context.json` 中配置，格式见参考文档。内存中的 Python 变量不能从 `.ipynb` 还原，需要用户显式运行 Python 检查单元。

执行或让 Agent 编辑前，保存 Notebook，并暂停页面自动保存。外部进程修改的是磁盘文件，Jupyter 前端不会自动接收结构更新；结束后使用 **Reload from Disk**，或关闭页面后选择丢弃旧页面改动并重新打开。执行单元能立即展示本次结果，但合并、拆分必须重新载入。不要用旧页面保存覆盖磁盘新版本。

脚本对修改做备份和磁盘版本检查，但不能保护尚未保存的浏览器内存。按单个 Notebook 串行操作，遇到版本冲突先重新读取，不盲目重试覆盖。输出文件保留时，`sync` 可以恢复输出展示；结构误覆盖应从 `backups/` 或 `archive/` 恢复。

只修改本次请求涉及的 Notebook、配套状态目录及用户授权的项目文件。沿用本机 Claude 的模型、工具和权限配置，不自动添加权限绕过参数。
