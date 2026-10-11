# AEGIS-Agent

> 一个会读论文的 LLM Agent —— 边学 agent 开发边做的个人项目。

给它一个装满论文的文件夹，它会自己把 PDF 解析成 Markdown、按章节拆开、挑重点读完，然后交给你一份结构化总结。除此之外，它还能联网搜索、读写文件、开子代理并行干活。

项目跑在本地，模型接口走 OpenAI 兼容格式，可以接任何兼容的服务。

---

## 它能做什么

### 读论文

把论文 PDF 丢进一个文件夹，跟它说一声就行。它会走完整个流程：

**解析**（PDF → Markdown，含图片）
→ **切分**（按章节拆成小文件，附一份章节清单）
→ **精读**（第一遍只读标题 / 摘要 / 介绍 / 方法 / 实验这五类）
→ **总结**（输出结构化的论文摘要）

全文始终留在磁盘上，想深挖哪部分再按需取回，不会丢内容。

### 批量写报告

论文多的时候，可以一次性把整个文件夹批量解析、逐篇读完，最后汇总成一份综述报告。

### 联网搜索

接入 Tavily，能查实时信息。

### 文件操作

可以在工作区里读、写、追加、替换文件。

### 子代理

复杂任务可以开子代理并行推进，结果通过消息队列回传给主代理。

---

## 快速开始

### 环境要求

- Python 3.10+（开发环境为 3.12）

### 安装

```bash
git clone https://github.com/A51662B/AEGIS-Agent.git
cd AEGIS-Agent
pip install -r requirements.txt
```

### 配置

在项目根目录建一个 `.env` 文件：

```ini
# 必填：模型接口（OpenAI 兼容格式）
OPENAI_API_KEY=your-api-key
OPENAI_BASE_URL=https://your-endpoint/v1

# 可选：模型名，不填则回落到主模型
CHAT_MODEL=deepseek-flash
REPORT_MODEL=
TITLE_MODEL=

# 用联网搜索时需要
TAVILY_API_KEY=

# 用 PDF 解析 / 报告工作流时需要
MINERU_API_TOKEN=

# 可选：显式指定上下文窗口大小（token），用于用量统计
CONTEXT_WINDOW=
```

| 变量 | 必填 | 说明 |
|---|---|---|
| `OPENAI_API_KEY` | ✅ | 模型 API Key |
| `OPENAI_BASE_URL` | ✅ | 模型 API 地址，需兼容 OpenAI 格式 |
| `CHAT_MODEL` | | 主对话模型，默认 `deepseek-flash` |
| `REPORT_MODEL` | | 报告工作流模型，缺省回落到 `CHAT_MODEL` |
| `TITLE_MODEL` | | 会话标题模型，缺省回落到 `CHAT_MODEL` |
| `TAVILY_API_KEY` | | 联网搜索（Tavily） |
| `MINERU_API_TOKEN` | | PDF 解析（MinerU Standard API） |
| `CONTEXT_WINDOW` | | 上下文窗口大小，用于 token 用量统计 |

### 运行

**Web 界面**（推荐）

```bash
python server.py
```

打开 <http://127.0.0.1:8000> —— 支持流式输出、多会话管理、中断生成、一键压缩上下文。

**命令行**

```bash
python main.py        # 同步版
python async_main.py  # 异步版，支持子代理
```

---

## 内置工具

模型可以自主调用下面这些工具：

| 工具 | 作用 |
|---|---|
| `pdfToMarkdown` | 调用 MinerU 把目录下的 PDF 批量解析成 Markdown（完整保留正文与图片） |
| `splitPaper` | 把解析出的 `full.md` 按章节切分，并生成章节清单（类别 + 标题 + 字符数） |
| `reportWorkflow` | 批量读论文并自动汇总成报告 |
| `skilltool` | 加载 `skills/` 下的技能手册，按手册里的流程执行 |
| `web_search` | 联网搜索（Tavily） |
| `subAgent` | 开一个异步子代理，并行处理子任务 |
| `list_directory` | 列出目录内容 |
| `read_file` | 读取文件内容 |
| `write_file` | 创建或覆盖文件 |
| `append_file` | 追加内容到文件末尾 |
| `replace_in_file` | 替换文件中的一段文字 |
| `get_current_time` | 获取当前日期、时间和星期 |

---

## 项目结构

```
AEGIS-Agent/
├── server.py                  Web 服务入口（FastAPI + SSE 流式输出）
├── main.py                    命令行入口（同步）
├── async_main.py              命令行入口（异步，支持子代理）
├── ChatModel.py               模型调用 + 工具调度（同步）
├── async_ChatModel.py         模型调用 + 工具调度（异步）
├── Memory.py                  会话记忆：持久化、压缩、检查点回滚
├── Message.py                 消息类型定义与格式转换
├── session_store.py           会话目录管理
├── session_context.py         当前会话的上下文变量
├── SubAgentMessageQueue.py    子代理结果队列
├── config.py                  模型名配置
│
├── tools/                     内置工具
├── workflow/                  LangGraph 工作流（PDF 解析 / 报告生成）
├── skills/                    技能手册（Markdown，模型按需加载）
├── utils/                     分词计数、标题生成、章节切分等
├── static/                    Web 前端
│
├── memory/                    会话数据 ← 运行时生成，不入库
└── workspace/                 工作区 ← 运行时生成，不入库
```

---

## 技能（skills）

`skills/` 目录下每个 `.md` 文件就是一个**技能** —— 一份写给模型看的操作手册。

模型不会一次性把所有手册读完，而是通过 `skilltool` 按需加载：真要用到读论文时，才把 `read_paper` 的完整流程取出来照着做。

新增技能只要往 `skills/` 里丢一个 `.md` 文件：

```markdown
# 这里写这个技能是干什么的

（下面写具体流程和规则）
```

第一行 `#` 后面的文字会自动成为这个技能的描述，模型据此判断什么时候该用它。

目前内置：`read_paper` —— 论文阅读流程。

---

## 说明

- `memory/` 和 `workspace/` 是运行时目录，程序会自行创建，不纳入版本控制。
- 模型接口走 OpenAI 兼容格式，理论上可以接任何兼容服务。
- 个人学习项目，还在持续完善中。
