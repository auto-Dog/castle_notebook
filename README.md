# Castle Notebook

Language: [English](README.en.md) | 简体中文

## 快速开始

需要 Python 3.10+。Python 内核使用 IPython 执行单元；网页不需要 Node 或前端构建。

安装：

打开本文件夹，并执行：

```bash
pip install -e .
```

使用时：

```bash
cd 你的项目目录
castle-notebook
```

然后浏览器访问 <http://127.0.0.1:8765>。首次启动创建合成数据示例；也可从“文件”菜单新建空白、金融研究或算法验证 Notebook。
如果同时打开多个项目，请指定端口：

```bash
cd 你的项目目录
castle-notebook --port 指定数字
```

指定研究项目和 Python 环境：

```powershell
python -m castle_notebook --project E:\research\my_model --python E:\conda\envs\research\python.exe
```

## 参数

```text
--project PATH        项目根目录，默认当前目录
--port 8765           本机端口
--python PATH         内核解释器，默认启动服务的 Python
--claude PATH         Claude 可执行文件或 npm cli.js
--codex PATH          Codex 可执行文件或 npm codex.js
--provider claude     默认 AI 提供商（claude、codex 或 custom）；Order 单元可单独覆盖
--claude-launch-mode background   原生后台（默认）；print 为旧版非交互模式
--claude-permission-mode auto   Claude 自动审批（默认），需要时进入终端处理；Codex 按此映射沙箱
--data-dir PATH       文档与证据目录，默认 <project>/.castle
--code-timeout 60     Python 执行秒数上限
--agent-timeout 240   print 模式 Claude 与 Codex 执行秒数上限；原生后台不设期限
--empty               不自动创建首次示例
```

### Custom 提供商（外部 AI 对话）

`--provider custom` 不启动任何 AI 进程：运行 Order 只生成 `input.md` 与上下文快照，然后等待你在外部 AI（如 Qoder、Claude 网页版）中完成对话并把结果写入该运行的 `output.md`，Castle 自动收集为正式输出。停止等待不会删除已写入的文件。

点击 Order 的「打开对话」可在弹窗中绑定一个会话目录（例如 Qoder 的 `~/.qoder-cn/projects/<项目>`），按聊天关键词与日期搜索原始会话并绑定到单元，之后直接查看完整对话记录。默认不绑定任何目录，也不读取任何外部文件；只允许读取界面中明确绑定的目录。

## 文档

| 内容 | 中文 | English |
| --- | --- | --- |
| 完整说明 | [docs/README.zh-CN.md](docs/README.zh-CN.md) | [README.en.md](README.en.md) |
| 工作方式与 Claude 集成 | 见完整说明 | See full README |
| 上下文配置 | [docs/context-config.md](docs/context-config.md) | 同左（中文） |
| 0.9 开发说明 | [docs/0.9-plan.md](docs/0.9-plan.md) | 同左（中文） |

## 链接

- 中文完整文档：[docs/README.zh-CN.md](docs/README.zh-CN.md)
- 英文完整文档：[README.en.md](README.en.md)