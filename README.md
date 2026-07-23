# Biomni-E1 复现工程

> 按必学必会文章《Biomni 干了什么？怎么干的》中的 **Biomni-E1 方法论**（文献驱动的工具环境构建）复现的完整可运行工程。
> 文章链接：https://bixuebihui.com/content/biomni-gan-le-shen-me-zen-me-gan-de-309

Biomni 本身是一个能自主拆解任务、调用工具的生物医学研究智能体。本文聚焦其核心——**如何不依赖固定工作流模板，从大量论文中自动构建出统一的工具执行环境（E1）**。本工程把文章中描述的 5 个阶段完整实现为可运行代码。

## 五阶段流水线 vs 代码

| 阶段 | 文章目标 | 本工程脚本 | 产出 |
|------|----------|-----------|------|
| Phase 1 论文选取 | 按平台原生子领域取最新论文 | `src/fetch_papers.py` | `data/papers.jsonl` + PDF |
| Phase 2 AI 提取 | LLM 逐块提取 Task/Tool/Database/Software | `src/extract.py` + `src/llm_client.py` | `data/extractions/<doi>.json` |
| Phase 3 去重聚合 | 三层匹配（精确→模糊→语义）收敛冗余 | `src/deduplicate.py` | `data/aggregated/*.json` |
| Phase 4 人工验证 | 五项验收清单 + 测试 | `src/validate.py` | `data/aggregated/validation_report.json` + `review_queue.jsonl` |
| Phase 5 环境构建 | 统一接口封装 + 检索系统 | `src/build_env.py` + `src/retrieve.py` | `tools/**`、`config/tool_registry.json`、`retrieval_index.jsonl` |

## 目录结构

```
d:\ai\bio\
├── config/                    # 配置：子领域、Prompts、LLM、别名、注册表
│   ├── categories.json        # bioRxiv 25 子领域 + 选取参数
│   ├── llm_config.json        # LLM/Embedding 后端（OpenAI 兼容）
│   ├── aliases.json           # 工具别名映射（去重用）
│   ├── prompts/               # 文章中可直接复用的 Prompt 模板
│   └── tool_registry.json     # 最终工具注册表
├── src/                       # 5 阶段脚本 + 可插拔 LLM/Embedding 客户端 + 检索
├── tools/                     # 按 domain 生成的统一接口工具封装
│   ├── genomics/  proteomics/  meta/  common/
│   └── genomics/fasta_stats.py  # 真实可运行示例（带测试）
├── tests/                     # Phase 4 测试用例
├── docker/                    # Dockerfile + 版本锁定的依赖
└── data/                      # 论文/提取/聚合/数据湖（运行产物）
```

## 快速开始

```bash
cd d:\ai\bio
pip install -r docker/requirements.txt

# ① 论文获取（演示：30 天 × 3 篇/类，只拉元数据不下载 PDF）
python src/fetch_papers.py --demo --dry-run
#    真实运行（18 个月 × 100 篇/类，会下载约 50GB PDF）：python src/fetch_papers.py

# ② AI 提取（需配置 OPENAI_API_KEY；无 key 时自动走 stub，流程仍贯通）
export OPENAI_API_KEY=sk-...
python src/extract.py --limit 10

# ③ 去重聚合
python src/deduplicate.py

# ④ 验证
python src/validate.py

# ⑤ 环境构建（生成工具封装 + 注册表 + 检索索引）
python src/build_env.py

# 运行时检索（Agent 选工具）
python src/retrieve.py "蛋白质稳定性优化" --top-k 5
```

## 关于 LLM / Embedding 后端

`config/llm_config.json` 默认走 **OpenAI 兼容接口**。可改 `base_url` 指向任意兼容网关（本地 vLLM、Ollama、第三方）。
**未配置 API key 时自动降级为 stub**：`extract()` 返回空实体、`embed()` 返回确定性占位向量——这条设计让 Phase 1→5 不需付费即可端到端跑通，便于调试。真实去重/检索请配置 embedding 后端（否则 Layer 3 语义匹配会被跳过）。

## 关键设计点（对应文章）

- **统一工具接口**：`def tool_name(query, params, data_dir) -> dict`，返回必含 LLM 可读 `log` 字段（`src/build_env.py` 模板、`tools/genomics/fasta_stats.py` 示例）。
- **幻觉防护**：`extract.py` 对 `confidence<0.5` 标记 `needs_review`，可选 `--no-verify` 关闭 URL 可达性 HEAD 校验。
- **三层去重**：精确（归一化名）→ 模糊（difflib 相似度）→ 语义（embedding 余弦 >0.85）。
- **任务分级**：按出现频次 `frequency` 分高频(≥50)/中频(≥10)/低频(<10)，输出 `tier` 字段。
- **五项验收**：`validate.py` 自动化预检名称/文档/LLM可读/测试/专业性门槛，未自动可判的进入 `review_queue.jsonl` 供人工裁定。

## 成本估算（参考文章附录 B，2500 篇论文）

| 项目 | 估算 |
|------|------|
| 论文 PDF 下载 | ~50GB 存储 |
| LLM 提取调用 | 7500 次（~3 块/篇），约 $300–800 |
| Embedding 生成 | ~10000 次，约 $5–50 |
| 人工验证 | 2–4 人周 |
| 环境构建 | 1–2 人月 |

## 演示数据

`data/` 下若没有真实产出，可放入示例 `papers.jsonl` 与 `extractions/*.json`（见 `tests/demo_extraction_*.json` 思路）直接跑 Phase 3→5，无需联网与 API key 即可看到聚合/验证/封装结果。
