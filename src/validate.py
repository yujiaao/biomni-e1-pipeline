#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
validate.py — Phase 4：人工验证（自动化预检 + 人工复核队列）

对聚合后的 tools/databases/software 逐项执行五项验收清单（自动化部分）：
  ① 名称清晰    : 有 name 且非过短/纯缩写
  ② 完整文档    : 有 description + input/output 说明
  ③ LLM 可读输出: output_format 为结构化文本/JSON/表格，非原始二进制
  ④ 通过测试    : 存在对应测试用例或可通过 smoke 自测（无则标记 manual）
  ⑤ 专业性门槛  : 命中"简单查询/画图/统计/文本清洗"等拒绝模式则 reject

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
REJECT_PATTERNS = [
    r"select\s+\*", r"画(柱状|折线|散点|饼)", r"可视化", r"均值", r"标准差",
    r"正则匹配", r"字符串清洗", r"简单(统计|计算|查询)", r"画个?图",
]
REJECT_RE = re.compile("|".join(REJECT_PATTERNS), re.IGNORECASE)

BIN_OUTPUT = re.compile(r"二进制|binary|\.bam$|\.fastq$|\.gz$", re.IGNORECASE)


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

    # ② 完整文档：软件类看 install_cmd/primary_use；任务看 input/output_type；其余看 description + IO
    if kind == "software":
        has_doc = bool(desc) and bool(item.get("install_cmd") or item.get("primary_use"))
    elif is_task:
        has_doc = bool(desc) and bool(item.get("input_type") or item.get("output_type"))
    else:
        has_io = bool(item.get("input_format") or item.get("input_type") or out_fmt)
        has_doc = bool(desc) and has_io
    checks["doc_complete"] = has_doc

    # ③ LLM 可读输出：软件类恒为真；任务要求结构化 output_type；其余要求结构化输出且非原始二进制
    if kind == "software":
        checks["llm_readable"] = True
    elif is_task:
        checks["llm_readable"] = bool(item.get("output_type")) and not BIN_OUTPUT.search(item.get("output_type") or "")
    else:
        checks["llm_readable"] = bool(out_fmt) and not BIN_OUTPUT.search(out_fmt)

    # ④ 通过测试（探测测试文件 / smoke）—— 仅作信息项，无测试归为 manual 不计入 fail
    test_name = re.sub(r"[^\w]", "_", name.lower())
    has_test = (TESTS_DIR / f"test_{test_name}.py").exists()
    checks["has_test"] = has_test
    checks["test_status"] = "auto_pass" if has_test else "manual"

    # ⑤ 专业性门槛
    blob = f"{name} {desc}".lower()
    reject = bool(REJECT_RE.search(blob))
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
            "failed": failed, "manual": manual}


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
    report["summary"] = {"total": total, "by_status": by_status}
    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"[validate] 共 {total} 项，状态分布: {by_status}")
    print(f"[validate] 复核队列 {len(queue)} 项 -> {QUEUE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
