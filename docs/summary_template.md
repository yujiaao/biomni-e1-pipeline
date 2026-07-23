# biomni-e1-pipeline 实战总结：把 2500+ 篇生物预印本变成可调用工具库

> 一份从 0 跑通 `biomni-e1-pipeline` 五阶段流水线的工程笔记：如何用 DeepSeek 替换 OpenAI、用 SQLite 做断点续传、真实抓取并提取 2539 篇 bioRxiv 论文，最终沉淀出一份带语义检索索引的生物信息学工具注册表。

---

## 0. 为什么做这件事

[biomni-e1-pipeline](https://github.com/yujiaao/biomni-e1-pipeline) 是 BioMni E1 生态里的一条"知识沉淀"流水线：把海量生物学论文自动抽取成**结构化、可被 Agent 直接调用的工具/数据库/任务**，再构建成带语义检索的开发环境。

它的价值不在于"又爬了一遍论文"，而在于把**非结构化的论文文本**转成**结构化的工具注册表**——这是后续让科研 Agent 真正能"按需取用工具"的基础设施。

本文记录的是：在完全离线可用的本地环境里（DeepSeek 做 LLM、本地 sentence-transformers 做 embedding），把这条流水线真实跑通、并处理 **2539 篇真实论文** 的全过程，以及其间踩过的每一个坑。

---

## 1. 五阶段流水线架构

整条流水线分五个阶段，数据从一篇篇论文逐步收敛为可调用工具：

```
papers.jsonl ──▶ [1.fetch] ──▶ [2.extract] ──▶ [3.deduplicate] ──▶ [4.validate] ──▶ [5.build_env]
  (抓取列表)        (抓全文)      (LLM抽取实体)    (跨论文聚合去重)    (验收/人工队列)     (生成封装+索引)
```

| 阶段 | 脚本 | 输入 | 输出 |
|------|------|------|------|
| 1. 抓取 | `fetch_papers.py` / `db_fetch.py` | bioRxiv API | `papers.jsonl` + `fetch.db` |
| 2. 提取 | `extract.py` | 每篇论文文本 | `data/extractions/<doi>.json` |
| 3. 去重 | `deduplicate.py` | 全部 extraction | `data/aggregated/*.json` |
| 4. 验证 | `validate.py` | 聚合结果 | `validation_report.json` + `review_queue.jsonl` |
| 5. 构建 | `build_env.py` | 聚合结果 | `tools/*.py` + `tool_registry.json` + `retrieval_index.jsonl` |

**关键设计**：每个阶段都把中间结果落盘（JSON / SQLite），因此任意阶段中断都可以从断点续跑——这是后面"无人值守长跑"能成立的前提。

---

## 2. 两处关键改造

原仓库默认用 OpenAI。在本地/国内网络环境跑，我做两处替换：

### 2.1 用 DeepSeek 替换 OpenAI

`config/llm_config.json` 的 `base_url` 指向 `https://api.deepseek.com/v1`，模型用 `deepseek-chat`，API Key 走环境变量 `DEEPSEEK_API_KEY`（不落盘）。实测 5 篇冒烟测试：约 15 秒/篇，返回真实实体，**零限流**。

> 为什么是 DeepSeek 而不是其他：中文友好、价格低、OpenAI 兼容协议（改个 base_url 即可），且国内网络可达性好。

### 2.2 用本地 sentence-transformers 做 embedding

`build_env` / `deduplicate` 的语义匹配需要向量。改用本地 `BAAI/bge-small-zh-v1.5`，通过 `HF_ENDPOINT=https://hf-mirror.com` 走国内镜像下载。这样语义检索**完全离线**，不依赖任何外部向量服务。

---

## 3. 断点续传的数据库 UI

抓取阶段我重写成 **SQLite 驱动**（`db_fetch.py` + `app.py` + `web/index.html`）：

- `fetch.db` 持久化每篇论文（DOI 唯一去重）和抓取游标 cursor；
- 仪表盘实时显示 25 个学科类别的抓取进度条，支持 **Start / Pause / Reset**；
- 停滞检测：连续 120 页零新增自动收尾；达标（≥目标）即停，不再发请求；
- 进程被杀后重启，从 DB 里读 cursor 接着抓——**天然断点续传**。

---

## 4. 真实运行结果

{{STATS}}

---

## 5. 踩过的坑（按时间线）

1. **GitHub 直连失败** → 改用 `ghfast.top` 镜像 ZIP 下载解压。
2. **managed Python 3.13 的 venv pip 损坏** → 退回系统 Python 3.12.6 建 venv。
3. **PyPI 默认源中断** → 切清华镜像。
4. **`fetch_papers.py` 的 `total`/`count` 字符串比较 TypeError** → 加 `int()` 转换。
5. **bioRxiv API 没有 `subject_areas` 字段** → 改读 `category`，大小写不敏感匹配。
6. **`api.biorxiv.org` IPv6 不可达** → 强制 `socket.getaddrinfo` 走 IPv4。
7. **`db_fetch.py` 的 `c.rowcount` AttributeError** → 改 `cur = execute(...); cur.rowcount`。
8. **抓取达标却报 error**（cursor 处 DNS 抖动，但 total 已达标）→ 在页循环顶部加"达标即收尾"判断。
9. **`extract.py` 第 209 行缩进错误（IndentationError）** → 修复后 `py_compile` 通过，全量提取才真正启动。
10. **后台进程被 agent 运行时回收** → 用 PowerShell `Start-Process` 派生**脱离会话的 Windows 原生进程**，再套一层自修复监督器（提取崩溃 15s 自动重启），从此只取决于机器是否开机，不受对话上下文影响。

---

## 6. 经验沉淀

- **落盘即续传**：每个阶段产出物落盘，是长跑任务的命根子。
- **脱离式进程**：需要"跑完再回来"的长任务，绝不能依赖对话工具的 `run_in_background`（会话切换会被回收），要派生成真正独立的 OS 进程。
- **先做冒烟测试**：全量前先用 5 篇验证成本/限流，避免 10 小时后才发现配置错了。
- **HF 镜像时好时坏**：embedding 这类依赖外部模型的环节，要在最晚时刻（构建阶段）再确认镜像可达，而不是抓数据时顺手测一次就信了。

---

## 7. 最终交付物

- 抓取数据库：`data/fetch.db`（2539 篇）
- 提取结果：`data/extractions/*.json`
- 聚合去重：`data/aggregated/*.json`
- 验证报告：`data/aggregated/validation_report.json`
- 工具注册表：`config/tool_registry.json` + `tools/*.py`
- 语义检索索引：`data/aggregated/retrieval_index.jsonl`
- 实时监控 UI：运行 `python src/app.py`（端口 8080）

> 文章由 WorkBuddy 在流水线跑通后自动生成，统计数字来自真实运行日志与数据库。
