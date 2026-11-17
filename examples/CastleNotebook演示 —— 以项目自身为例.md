# CastleNotebook演示 —— 以项目自身为例

以下过程模拟了一个Castle Notebook动态展示全局信息的方式，展示的案例是castle notebook自身的开发过程

假设以下文本就是Global Cell
>>>
---

## 项目背景

在使用LLM Agent进行聊天式项目研发时，通常存在信息展示不直观、全局信息不透明的问题。该问题造成了需求的重复提出、修改困难、复现不稳定，因此提出CastleNotebook。

项目参考Jupyter Notebook的核心哲学：实时展示、全局变量

> *Jupyter Notebook 是一个基于 Web 的交互式计算环境，支持多种编程语言，包括 Python、R、Julia 等。它的主要功能是将代码、文本、数学方程式、可视化和其他相关元素组合在一起，创建一个动态文档，用于数据分析、机器学习 、科学计算和数据可视化等方面。Jupyter Notebook 提供了一个交互式的界面，使用户能够以增量和可视化的方式构建和执行代码，同时支持 Markdown 格式的文本和 LaTeX 数学符号。*
> 
> 

## 项目实现

核心：

> \> Global:Do What
> 
> \> STEP What \(AI Process\)
> 
> \> Out What 
> 
> 

---
<<<

假设以下文本就是Input Cell 1
>>>
---

Input



```Plain Text
版本0.9===
界面层：
一个Notebook文档展示界面，可以展示Project Global Cell、Input Cell、Output Cell
类似于Jupyter Notebook Server的极简版实现，前端界面可以用网页展示
功能层：
Global Cell: 存放核心目的
    实时可编辑的全局项目目的、约束
Input Cell：可以是代码类型，可以是Markdown类型，可以是Order类型（需要指定预期Output）
    Run Button：Order Input类型，把上文的内容连同本次指令一起发给LLM，harness要求LLM将本次对话结论输出到xx文件，这个文件将提供Outpu Cell展示内容
    每一个step，实际给AI的输入都是他的全部精简版上文
Output Cell：同Jupyter Notebook
    所有cell要带时间戳
要加一个Init from project功能，调用AI Agent从已有项目初始化成这种Notebook
```

【对话：https://chatgpt\.com/s/cx\_6af89257acd881918ea83b331838382f。实际笔记本中有个按钮，点下后直接进入这个聊天】

Output:

做好了，如下，xxx。

![Image](xxx)



---
<<<

发现BUG，去开了个对话要求检查：

假设以下文本就是Input Cell 2
>>>
---

Input: 

运行ID：d016b87badef 看起来不是很完整啊，根本没跑通的样子

【对话：https://chatgpt\.com/s/cx\_6af8961716108191ad63a2a0bb4617b1】

Output: 

已改好：完整输入保存到 \[\.castle/prompts/inputc0a1218fdf04\.md\]\(E:/code/CastleNotebook/\.castle/prompts/inputc0a1218fdf04\.md\)，后台 Claude 自行读取，\.\.\.



---
<<<

然后在这个对话又提了个问题：关于上下文模板，可否放在配置文件里，这样我可以编辑哪些功能是开启/关闭。AI答复：“

可以，已实现。直接编辑 \[context\.json\]\(E:/code/CastleNotebook/\.castle/context\.json\)：

- `templates.instructions`：harness prompt 正文

- `templates.prompt`：上下文结构和拼接顺序

- `templates.invocation`：Claude 启动时的读取指令

- `features`：各项开关；\`limits\`：字符预算

保存后下次预览或运行立即生效，无需重启；每次运行保存配置快照。57 项测试通过。

”

此时，cell 2中应自动多出内容，假设以下文本就是Input Cell 2\(Update 1\)：

>>>
---

Input: 解决没跑通问题，运行ID：d016b87badef ；随后又新增上下文模板可编辑功能

Output: 已改好：完整输入保存到 \[\.castle/prompts/inputc0a1218fdf04\.md\]\(E:/code/CastleNotebook/\.castle/prompts/inputc0a1218fdf04\.md\)，后台 Claude 自行读取，\.\.\.；

编辑方面，直接编辑 \[context\.json\]\(E:/code/CastleNotebook/\.castle/context\.json\)：

- `templates.instructions`：harness prompt 正文

- `templates.prompt`：上下文结构和拼接顺序

- `templates.invocation`：Claude 启动时的读取指令

- `features`：各项开关；\`limits\`：字符预算



---
<<<

与此同时，新功能也在开发：

假设以下文本就是Input Cell 3：
>>>
---

Input:

看起来目前和真正的jupyter notebook还有区别，比如不支持 \!命令

Output:

已接入 IPython，现在支持：

- `!命令`、\`files = \!命令\`、变量插值

- `%pip`、\`%cd\`、\`%time\`、\`%%capture\`

- 流式输出、Windows 中文编码及停止/超时清理

**82 项测试全部通过。重启当前内核后生效**

**目前仍不支持 widgets 和完整 Jupyter 协议**

---
<<<


最后，在推出0\.9\.1版本时，以上的cell由于均属于实现需求，在项目管理中不需要零散分布成多个对话。值得被合并：

假设以下是Input Cell 1，由原始的Input Cell 1，2，3合并，合并过程由一个claude会话实现
>>>
---

Input: 

实现功能

#### 版本0\.9===

界面层：

* [x] 一个Notebook文档展示界面，可以展示Project Global Cell、Input Cell、Output Cell

* [x] 类似于Jupyter Notebook Server的极简版实现，前端界面可以用网页展示【jupyter二次开发可忽略】

功能层：

* [x] Global Cell: 存放核心目的

    * [x] 实时可编辑的全局项目目的、约束

* [x] Input Cell：可以是代码类型，可以是Markdown类型，可以是Order类型（需要指定预期Output）

    * [x] Run Button：Order Input类型，把上文的内容连同本次指令一起发给LLM，harness要求LLM将本次对话结论输出到xx文件，这个文件将提供Outpu Cell展示内容

    * [x] 可切切换到LLM Agent 运行Order Input Cell，目前采用\`claude "PROMPT" \-n "ID NAME"\`

    * [x] 每一个step，实际给AI的输入都是他的全部精简版上文。

* [x] Output Cell：同Jupyter Notebook

* [x] 所有cell要带时间戳

* [x] 要加一个Init from project功能，调用AI Agent从已有项目初始化成这种Notebook

Output:

上述功能均已实现。值得注意的是，Order Input Cell模板编辑方面，直接编辑 \[context\.json\]; Jupyter方面，**目前仍不支持 widgets 和完整 Jupyter 协议**

如需运行项目，执行命令：

---
<<<

Cell 2 \(Python Cell\)
>>>
---

```PowerShell
!cd E:\code\CastleNotebook
!python -m pip install -e .
!python -m castle_notebook
```

---
<<<


\(完\)





