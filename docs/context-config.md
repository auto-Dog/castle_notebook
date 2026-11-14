# 上下文与 harness prompt 配置

编辑研究项目中的 `.castle/context.json`。服务首次启动时生成默认配置；保存后，下次点击“上下文”或运行单元就会重新读取，无需重启。正在执行的单元继续使用启动时的配置快照。

配置分为 `features`、`limits`、`templates` 三部分。所有开关默认开启；未填写的配置项使用默认值。JSON 使用 `true` / `false`，不支持注释。

## 功能开关

| `features` 配置项 | 控制的上下文内容或处理方式 |
| --- | --- |
| `instructions` | 是否加入 harness 的角色、行为说明文字 |
| `global_goal` | 是否加入 Global 目标 |
| `global_constraints` | 是否加入 Global 约束 |
| `previous_inputs` | 是否加入前置单元输入 |
| `previous_outputs` | 是否加入前置单元的有效输出 |
| `manual_summaries` | 是否用手写摘要替代对应前置单元的原始输入 |
| `variables` | 是否加入当前 Python 变量的有界预览 |
| `expected_output` | 是否把当前单元的预期输出契约写入上下文 |
| `output_format` | 是否加入当前单元的输出格式说明 |
| `compact_history` | 是否按预算精简前置单元的输入和输出 |

例如，只关闭变量预览和历史输出，可以将对应项改为：

```json
{
  "features": {
    "variables": false,
    "previous_outputs": false
  }
}
```

这些开关控制上下文的组装。单元的必填项和 JSON 输出校验仍按单元设置执行。失败或失效的历史输出不会作为有效证据加入。

## 编辑 harness prompt

`templates.instructions` 是可直接编辑的 harness 说明正文，包括角色、只处理当前单元等要求；这段文字可以自由重写。它支持 `{run_dir}`、`{output_file}`、`{run_id}`、`{session_name}`、`{prompt_file}` 运行变量，其余内容为普通文本，可以直接包含 JSON 花括号。

`templates.prompt` 控制完整 prompt 的拼接顺序，支持下列区块占位符：

```text
{project}
{instructions}
{global}
{previous_steps}
{variables}
{current_step}
{expected_output}
{output_format}
```

各段格式也可独立修改：

`{project}` 由服务生成，包含当前 Notebook 的真实项目目录、有限文件概览，以及在项目源码中完成开发和验证的要求。它始终进入输入，不受 `features.instructions` 开关控制。旧模板未包含 `{project}` 时，服务将此区块补在输入开头；已有配置文件保留原内容。自定义模板可通过 `{project}` 调整其位置。文件列表只是数据，不能覆盖 Global 或当前步骤；初始化和明确的只读任务仍按只读要求处理。

| 模板 | 可用占位符 |
| --- | --- |
| `global` | `{revision}`、`{goal}`、`{constraints}`；后两项为已按开关渲染的子区块 |
| `global_goal` | `{goal}` |
| `global_constraints` | `{constraints}` |
| `previous_steps` | `{steps}` |
| `previous_step` | `{id}`、`{title}`、`{type}`、`{state}`、`{input}`、`{output}` |
| `previous_input` | `{source}` |
| `previous_output` | `{output}` |
| `variables` | `{variables}` |
| `current_step` | `{id}`、`{revision}`、`{title}`、`{type}`、`{source}` |
| `expected_output` | `{expected_output}` |
| `output_format` | `{output_format}` |
| `invocation` | `{prompt_file}`、`{run_id}`、`{session_name}`、`{run_dir}`、`{output_file}` |

`templates.invocation` 是后台传给 Claude 的简短启动指令，例如：

```json
{
  "templates": {
    "instructions": "你是一名无线通信研究助手。给出来源，明确区分已核验与未核验结论。\n",
    "invocation": "请读取 {prompt_file}，完成文件中的当前任务。"
  }
}
```

完整输入仍写入 `.castle/prompts/input<运行ID>.md`，命令行只传渲染后的 `invocation`。Claude 的工具、权限与模型使用本机配置。

输出路径在 `templates.instructions` 的输出要求段落中展开，例如：

```text
最终回答按照jupyter notebook cell支持的形式（文本、格式文本、图片、widget）写入本次运行目录 `{run_dir}` 的 output.md（相对项目目录的完整路径：`{output_file}`）。注意：此后的每一轮对话，都要按照最初的格式需求更新这个output文件。
```

`{run_dir}` 默认展开为 `.castle/runs/<运行ID>`，`{output_file}` 默认展开为 `.castle/runs/<运行ID>/output.md`，统一使用 `/` 并相对于项目工作目录解析。指定自定义 `--data-dir` 时按真实位置计算相对路径；同驱动器下的项目外目录使用 `../`，跨驱动器时明确报错。启动指令不再自动追加输出路径；需要时仍可在 `invocation` 中主动引用这些变量。

预览会预分配本次运行 ID，实际执行复用该 ID，因此预览、输入 Markdown 文件和运行记录中的输出路径一致。执行后下一次预览会分配新的 ID。配置快照保留模板原文，输入快照保存已经展开的相对路径。当前自动执行的最终回答仍由后台收集并保存到 `output.md`。

除 `instructions` 外，模板中的字面花括号写成 `{{` 和 `}}`；用户指令及研究数据作为占位符的值插入，不会被二次格式化。JSON 字符串中的换行写成 `\n`。总模板必须保留 `{current_step}`，当前单元模板必须保留 `{source}`，启动指令必须保留 `{prompt_file}`。

## 长度与运行记录

| `limits` 配置项 | 默认值 | 含义 |
| --- | ---: | --- |
| `max_characters` | 48000 | 完整 prompt 的字符预算 |
| `history_cell_characters` | 1900 | 开启历史精简时，单个前置单元的预算上限；总预算不足时会进一步缩减 |
| `variables_characters` | 2400 | Python 变量预览的字符上限 |

当前指令及启用的 Global 内容完整保留。关闭历史精简后，历史内容超过总预算会明确报错。配置语法、占位符或开关类型错误也会明确报错，文件不会被默认配置覆盖。

运行记录的 `context.config` 保存实际使用的配置，`context.config_fingerprint` 保存配置指纹；`context.md` 和 prompt 文件保存当次组装后的完整输入。修改配置不会覆盖旧运行的输入和配置快照。
