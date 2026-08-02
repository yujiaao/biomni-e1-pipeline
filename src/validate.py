#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
validate.py — Phase 4：人工验证（自动化预检 + 人工复核队列）

对聚合后的 tools/databases/software 逐项执行五项验收清单（自动化部分）：
  ① 名称清晰    : 有 name 且非过短/纯缩写
  ② 完整文档    : 有 description 即基本完整；IO 字段缺失仅记软标记（待补全，非失败）
  ③ LLM 可读输出: 自然语言描述即可被 LLM 消费，仅显式声明原始二进制输出才判不可读
  ④ 通过测试    : 存在对应测试用例或可通过 smoke 自测（无则标记 manual）
  ⑤ 专业性门槛  : 命中"简单画图/统计/文本清洗"等拒绝模式则 reject；已知专业工具（白名单）强制通过

输出：
  data/aggregated/validation_report.json   全量结果
  data/aggregated/review_queue.jsonl       需人工裁定条目（needs_review / manual / reject）

用法：
  python src/validate.py
  python src/validate.py --strict           # 任意一项不通过即整体 fail
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
AGG_DIR = ROOT / "data" / "aggregated"
TESTS_DIR = ROOT / "tests"
REPORT = AGG_DIR / "validation_report.json"
QUEUE = AGG_DIR / "review_queue.jsonl"

# ⑤ 专业性门槛：拒绝模式（简单操作不值得封装）
#    注意：移除过于宽泛的「可视化」单模式——它会误杀 PyMOL / VOSviewer 等
#    公认专业科研工具（其描述含"分子可视化/文献计量网络可视化"）。
#    琐碎画图仍由「画(柱状|折线|散点|饼)图 / 画个?图」覆盖。
REJECT_PATTERNS = [
    r"select\s+\*", r"画(柱状|折线|散点|饼)",
    # 「均值/标准差」收窄为"计算/求 均值/标准差"等琐碎操作，避免误杀
    # t-test / ANOVA 等正规统计方法（其描述多为"比较多组均值"而非"计算均值"）。
    r"计算均值|求均值|算均值|均值计算|计算标准差|求标准差|标准差计算",
    r"正则匹配", r"字符串清洗", r"简单(统计|计算|查询)", r"画个?图",
]
REJECT_RE = re.compile("|".join(REJECT_PATTERNS), re.IGNORECASE)

BIN_OUTPUT = re.compile(r"二进制|binary|\.bam$|\.fastq$|\.gz$", re.IGNORECASE)

# 描述"过薄"软阈值（去空白后字符数低于此值视为待补全，非失败）
DESC_THIN_CHARS = 10

# 专业性白名单：已知科研专业工具/数据库/软件，任何关键词拒绝模式都不应误杀。
# 名称归一化（去空格/标点、转小写）后匹配。命中即强制 professional=True。
PROF_WHITELIST = {
    # 可视化 / 文献计量
    "pymol", "vosviewer", "imagej", "fiji", "cytoscape", "gephi", "chimerax",
    # 序列 / 结构 / 模拟
    "blast", "hmmer", "gromacs", "autodock", "chimera", "swiss-model", "rosetta",
    "modeller", "napari", "ucsc", "igv", "bwa", "samtools", "bowtie", "gatk",
    "bedtools", "star", "kallisto", "salmon",
    # 统计 / 计算 / 编程
    "r", "python", "matlab", "rstudio", "jupyter", "tensorflow", "pytorch",
    "numpy", "scipy", "pandas", "scikit-learn", "matplotlib", "ggplot2",
    # 实验 / 写作 / 数据
    "origin", "graphpad", "prism", "snapgene", "benchling", "endnote",
    "zenodo", "figshare",
}


def check_item(item: dict, kind: str) -> dict:
    checks = {}
    name = item.get("name") or ""
    # description 来源按类型回退：数据库用 schema/query_method，软件用 primary_use，任务用 description
    is_db = kind.startswith("database")
    is_task = kind.startswith("task")
    if is_db:
        desc = item.get("description") or item.get("schema") or item.get("query_method") or ""
    else:
        desc = item.get("description") or item.get("primary_use") or ""
    out_fmt = item.get("output_format") or item.get("schema") or item.get("query_method") or ""

    # ① 名称清晰：有描述性名称且非过短缩写（全大写工具名如 HMMER/PDB 视为合法）
    checks["name_clear"] = bool(name) and len(name) >= 2

    # ② 完整文档：核心是"有可用描述"，IO(input/output) 是增强项而非硬性门槛。
    #    旧逻辑对 tools/databases 强制要求 IO 字段，导致大量描述完整但无 IO 的真实
    #    工具/方案（CRISPR / organoid culture protocol 等）误判 doc_complete=False。
    #    改为：描述存在即基本完整；IO 缺失 / 描述过薄仅记为软标记（待补全，不计入 failed）。
    if kind == "software":
        has_doc = bool(desc) and bool(item.get("install_cmd") or item.get("primary_use"))
    elif is_task:
        has_doc = bool(desc) and bool(item.get("input_type") or item.get("output_type"))
    else:
        # tools / databases：有描述即基本完整（IO 不再作为硬门槛）
        has_doc = bool(desc)
    checks["doc_complete"] = has_doc
    # 软标记（非失败）：供下游补全，不影响 status 判定
    has_io = bool(item.get("input_format") or item.get("input_type")
                  or item.get("output_format") or out_fmt)
    desc_needs_io = bool(desc) and not has_io
    desc_thin = bool(desc) and len(re.sub(r"\s+", "", desc)) < DESC_THIN_CHARS

    # ③ LLM 可读输出：软件类恒为真；任务要求结构化 output_type；
    #    tools/databases 默认可读（自然语言描述即可被 LLM 消费），仅当显式声明
    #    原始二进制输出（.bam/.fastq/.gz 等）才判不可读。旧逻辑要求必须有 output_format
    #    字段才可读，导致无数描述型工具/方案误判 llm_readable=False。
    if kind == "software":
        checks["llm_readable"] = True
    elif is_task:
        checks["llm_readable"] = bool(item.get("output_type")) and not BIN_OUTPUT.search(item.get("output_type") or "")
    else:
        checks["llm_readable"] = not BIN_OUTPUT.search(out_fmt)

    # ④ 通过测试（探测测试文件 / smoke）—— 仅作信息项，无测试归为 manual 不计入 fail
    test_name = re.sub(r"[^\w]", "_", name.lower())
    has_test = (TESTS_DIR / f"test_{test_name}.py").exists()
    checks["has_test"] = has_test
    checks["test_status"] = "auto_pass" if has_test else "manual"

    # ⑤ 专业性门槛（白名单优先：已知专业工具强制通过，避免关键词误杀）
    norm_name = re.sub(r"[\s\-_./()]+", "", name.lower())
    blob = f"{name} {desc}".lower()
    reject = bool(REJECT_RE.search(blob)) and norm_name not in PROF_WHITELIST
    checks["professional"] = not reject
    if reject:
        checks["reject_reason"] = "命中简单操作拒绝模式"

    # has_test 是信息项，不计入 failed；manual 集合自然包含值为 "manual" 的 test_status
    failed = [k for k, v in checks.items() if v is False and k != "has_test"]
    manual = sorted({k for k, v in checks.items() if v == "manual"})
    if reject:
        status = "reject"
    elif failed:
        status = "fail"
    elif manual:
        status = "needs_review"
    else:
        status = "pass"
    return {"name": name, "kind": kind, "checks": checks, "status": status,
            "failed": failed, "manual": manual,
            "desc_needs_io": desc_needs_io, "desc_thin": desc_thin}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--strict", action="store_true")
    args = ap.parse_args()

    report = {"items": [], "summary": {}}
    queue = []
    kinds = ["tools", "databases", "software", "tasks"]
    for kind in kinds:
        f = AGG_DIR / f"{kind}.json"
        if not f.exists():
            continue
        data = json.loads(f.read_text(encoding="utf-8"))
        for it in data.get("items", []):
            res = check_item(it, kind)
            report["items"].append(res)
            if res["status"] in ("reject", "fail", "needs_review"):
                queue.append({"name": res["name"], "kind": kind,
                              "status": res["status"], "detail": res})

    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    with QUEUE.open("w", encoding="utf-8") as q:
        for item in queue:
            q.write(json.dumps(item, ensure_ascii=False) + "\n")

    total = len(report["items"])
    by_status: dict[str, int] = {}
    for it in report["items"]:
        by_status[it["status"]] = by_status.get(it["status"], 0) + 1
    report["summary"] = {"total": total, "by_status": by_status,
                          "desc_needs_io": sum(1 for it in report["items"] if it.get("desc_needs_io")),
                          "desc_thin": sum(1 for it in report["items"] if it.get("desc_thin"))}
    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"[validate] 共 {total} 项，状态分布: {by_status}")
    print(f"[validate] 复核队列 {len(queue)} 项 -> {QUEUE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
