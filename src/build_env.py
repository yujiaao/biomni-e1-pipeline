#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
build_env.py — Phase 5：环境构建（工具封装 + 注册表 + 检索索引）

依据聚合结果（data/aggregated/*.json）生成：
  1. 每个工具一个统一接口封装桩 tools/<domain>/<tool>.py
       接口: def tool_name(query, params=None, data_dir=None) -> dict
       返回必须含 LLM 可读的 log 字段（status: success/error/not_implemented）
  2. config/tool_registry.json —— 工具/数据库/任务注册表（供 Agent 检索）
  3. data/aggregated/retrieval_index.jsonl —— 描述向量索引（供语义检索）

domain 推断：按工具名关键词落入 genomics/proteomics/meta/common，缺省 common。

用法：
  python src/build_env.py
"""
from __future__ import annotations

import datetime as dt
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
AGG_DIR = ROOT / "data" / "aggregated"
TOOLS_ROOT = ROOT / "tools"
REGISTRY = ROOT / "config" / "tool_registry.json"
RETRIEVAL = AGG_DIR / "retrieval_index.jsonl"

sys.path.insert(0, str(ROOT / "src"))
from llm_client import LLMClient  # noqa: E402

DOMAIN_KEYWORDS = {
    "genomics": ["genom", "seq", "dna", "rna", "variant", "blast", "alignment", "read"],
    "proteomics": ["protein", "peptide", "mass spec", "ms", "structure", "fold"],
    "meta": ["meta", "pathway", "kegg", "go term", "enrich", "network"],
}


def infer_domain(name: str, desc: str) -> str:
    blob = f"{name} {desc}".lower()
    for dom, kws in DOMAIN_KEYWORDS.items():
        if any(k in blob for k in kws):
            return dom
    return "common"


def safe_func(name: str) -> str:
    s = re.sub(r"[^\w]+", "_", name.lower()).strip("_")
    return s or "tool"


def safe_file(name: str) -> str:
    return safe_func(name) + ".py"


def gen_wrapper(name: str, meta: dict, domain: str) -> Path:
    func = safe_func(name)
    desc = (meta.get("description") or "").replace('"', "'")
    template = f'''"""Auto-generated stub for {name} (Biomni-E1 Phase 5).

由 build_env.py 生成。具体实现按 Phase 4.3 由领域专家 + 软件工程 Agent 完成。
统一接口返回值必须包含 LLM 可读的 log 字段。
"""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))


def {func}(query: str, params: dict = None, data_dir: str = None) -> dict:
    """
    Tool: {name}
    Description: {desc}
    Input: {meta.get('input_format', meta.get('input_type', '见 description'))}
    Output: {meta.get('output_format', meta.get('schema', '见 description'))}
    """
    log = []
    log.append(f"[{name}] received query: {{query!r}}")
    log.append(f"[{name}] params={{params}} data_dir={{data_dir}}")
    # TODO(Phase4.3): 实现具体逻辑（调用 CLI/API/库），把中间结果写入 log
    result = None
    return {{
        "result": result,
        "log": "\\n".join(log),
        "status": "not_implemented",
        "metadata": {{
            "name": "{name}",
            "type": "{meta.get('type', 'unknown')}",
            "url": "{meta.get('url', '')}",
            "version": "{meta.get('version', '')}",
        }},
    }}
'''
    d = TOOLS_ROOT / domain
    d.mkdir(parents=True, exist_ok=True)
    p = d / safe_file(name)
    p.write_text(template, encoding="utf-8")
    return p


def main() -> int:
    client = LLMClient()
    registry = {"schema_version": "biomni-e1/1.0",
                "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
                "stats": {}, "tools": [], "databases": [], "tasks": [], "software": []}

    retrieval_rows = []

    # Tools
    tools_file = AGG_DIR / "tools.json"
    if tools_file.exists():
        tools = json.loads(tools_file.read_text(encoding="utf-8")).get("items", [])
        for t in tools:
            name = t.get("name")
            if not name:
                continue
            domain = infer_domain(name, t.get("description", ""))
            path = gen_wrapper(name, t, domain)
            registry["tools"].append({
                "name": name, "domain": domain, "module": f"{domain}/{path.stem}",
                "type": t.get("type"), "description": t.get("description"),
                "url": t.get("url"), "version": t.get("version"),
                "api_available": t.get("api_available"),
                "frequency": t.get("frequency", 0),
                "status": "pending_implementation",
            })
            retrieval_rows.append({"name": name, "kind": "tool",
                                   "description": t.get("description", ""),
                                   "ref": f"{domain}/{path.stem}"})

    # Databases
    dbs_file = AGG_DIR / "databases.json"
    if dbs_file.exists():
        for d in json.loads(dbs_file.read_text(encoding="utf-8")).get("items", []):
            registry["databases"].append({
                "name": d.get("name"), "access_type": d.get("access_type"),
                "url": d.get("url"), "query_method": d.get("query_method"),
                "group": "api" if d.get("access_type") == "api" else "local_data_lake",
            })
            retrieval_rows.append({"name": d.get("name"), "kind": "database",
                                   "description": d.get("schema", ""),
                                   "ref": d.get("url", "")})

    # Tasks
    tasks_file = AGG_DIR / "tasks.json"
    if tasks_file.exists():
        for t in json.loads(tasks_file.read_text(encoding="utf-8")).get("items", []):
            registry["tasks"].append({
                "name": t.get("name"), "tier": t.get("tier"),
                "frequency": t.get("frequency", 0),
                "difficulty": t.get("difficulty"),
            })

    # Software
    sw_file = AGG_DIR / "software.json"
    if sw_file.exists():
        for s in json.loads(sw_file.read_text(encoding="utf-8")).get("items", []):
            registry["software"].append({
                "name": s.get("name"), "language": s.get("language"),
                "install_cmd": s.get("install_cmd"), "version": s.get("version"),
            })

    registry["stats"] = {
        "tools": len(registry["tools"]),
        "databases": len(registry["databases"]),
        "tasks": len(registry["tasks"]),
        "software": len(registry["software"]),
    }
    REGISTRY.write_text(json.dumps(registry, ensure_ascii=False, indent=2), encoding="utf-8")

    # 检索索引（embedding）
    if retrieval_rows:
        descs = [f"{r['name']}: {r['description']}" for r in retrieval_rows]
        vecs = client.embed(descs)
        with RETRIEVAL.open("w", encoding="utf-8") as f:
            for r, v in zip(retrieval_rows, vecs):
                f.write(json.dumps({"name": r["name"], "kind": r["kind"],
                                    "description": r["description"], "ref": r["ref"],
                                    "vector": v}, ensure_ascii=False) + "\n")

    print(f"[build_env] 生成封装 {len(registry['tools'])} 个，数据库 {len(registry['databases'])} 个")
    print(f"[build_env] 注册表 -> {REGISTRY}")
    print(f"[build_env] 检索索引 {len(retrieval_rows)} 条 -> {RETRIEVAL}")
    print(f"[build_env] embedding 后端: {'live' if client.has_embeddings() else 'stub(占位向量)'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
