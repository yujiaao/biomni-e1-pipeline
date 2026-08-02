#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
metrics.py — Biomni-E1 精准度 / 质量量化（Phase 5+ 指标层）

消费全链路产物，产出一个机器可读、可纵向对比的精准度仪表盘
data/aggregated/metrics.json：

  1. 规模与覆盖（scale）：论文数 / 来源分布 / 日期覆盖 / 被引覆盖 / 抽取覆盖
  2. 抽取精准度代理（extraction）：stub 比例、置信度分布、文档完整度
  3. 去重收敛（dedup）：原始提及 vs 聚合实体，合并率（目录精准度核心信号）
  4. 验证通过率（validation）：pass / needs_review / fail / reject 分层
  5. 复核收敛（review）：自动接受率、残差率、压缩率
  6. 被引信号覆盖（citation）：带 citation_sum>0 的实体占比、总被引
  7. 数据完整度（recency）：有有效发表月的实体占比、覆盖月数
  8. 综合精准度评分（composite）：上述分量的透明加权（明确标注为启发式）

所有分量均来自既有产物，零新增抓取；可与历史 metrics.json diff 做趋势对比。

用法：
  python src/metrics.py
  python src/metrics.py --out data/aggregated/metrics.json
"""
from __future__ import annotations

import argparse
import json
import statistics
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

# 复用 analyze 的加载器，避免重复实现 & 行为漂移
from analyze import load_agg, build_doi_maps

ROOT = Path(__file__).resolve().parent.parent
AGG = ROOT / "data" / "aggregated"
EXT = ROOT / "data" / "extractions"
PAPERS = ROOT / "data" / "papers.jsonl"
VALIDATION = AGG / "validation_report.json"
AUTO_REVIEW = AGG / "auto_review_metrics.json"

KINDS = ["tasks", "tools", "databases", "software"]
CONF_HIGH = 0.7   # 高置信阈值
CONF_LOW = 0.5    # 低置信阈值


# ---------------- 1. 论文层 ----------------
def papers_metrics() -> dict:
    if not PAPERS.exists():
        return {"total": 0, "by_source": {}, "with_date": 0, "with_citation": 0}
    by_source: Counter = Counter()
    total = 0
    with_date = 0
    with_cit = 0
    total_cit = 0
    for line in PAPERS.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except Exception:
            continue
        total += 1
        by_source[d.get("source", "?")] += 1
        dt = d.get("date") or ""
        if len(dt) >= 7 and dt[4] == "-":
            with_date += 1
        c = int(d.get("citation_count", 0) or 0)
        if c:
            with_cit += 1
            total_cit += c
    return {
        "total": total,
        "by_source": dict(by_source),
        "with_date": with_date,
        "date_coverage": round(with_date / total, 3) if total else 0.0,
        "with_citation": with_cit,
        "citation_coverage": round(with_cit / total, 3) if total else 0.0,
        "total_citations": total_cit,
    }


# ---------------- 2. 抽取层 ----------------
def extraction_metrics() -> dict:
    files = list(EXT.glob("*.json")) if EXT.exists() else []
    total = len(files)
    stub = 0
    empty = 0  # 无实体抽取（extraction 为空）
    ent_counts = []
    for fp in files:
        try:
            d = json.loads(fp.read_text(encoding="utf-8"))
        except Exception:
            continue
        if d.get("stub"):
            stub += 1
        ex = d.get("extraction") or {}
        n = sum(len(v) for v in ex.values() if isinstance(v, list))
        ent_counts.append(n)
        if n == 0:
            empty += 1
    return {
        "total": total,
        "stub": stub,
        "stub_ratio": round(stub / total, 3) if total else 0.0,
        "empty_extractions": empty,
        "empty_ratio": round(empty / total, 3) if total else 0.0,
        "mean_entities_per_paper": round(statistics.mean(ent_counts), 2) if ent_counts else 0.0,
    }


# ---------------- 3. 聚合 / 去重 / 置信 / 文档 / 被引 ----------------
def _conf_stats(confs: list[float]) -> dict:
    if not confs:
        return {"count": 0, "mean": 0.0, "median": 0.0,
                "high_ratio": 0.0, "low_ratio": 0.0}
    return {
        "count": len(confs),
        "mean": round(statistics.mean(confs), 3),
        "median": round(statistics.median(confs), 3),
        "high_ratio": round(sum(1 for c in confs if c >= CONF_HIGH) / len(confs), 3),
        "low_ratio": round(sum(1 for c in confs if c < CONF_LOW) / len(confs), 3),
    }


def aggregated_metrics() -> dict:
    out = {}
    raw_total = 0
    agg_total = 0
    all_confs: list[float] = []
    cit_total = 0
    cit_entities = 0
    for k in KINDS:
        items = load_agg(k)
        mentions = [int(it.get("mentions", 1) or 1) for it in items]
        raw = sum(mentions)
        agg = len(items)
        raw_total += raw
        agg_total += agg
        confs = [float(it.get("confidence", 0) or 0) for it in items]
        all_confs.extend(confs)
        # 文档完整度
        with_desc = sum(1 for it in items if (it.get("description") or it.get("schema")
                                              or it.get("primary_use")))
        with_io = sum(1 for it in items if (it.get("input_format") or it.get("input_type")
                                            or it.get("output_format") or it.get("output_type")))
        # 被引覆盖
        for it in items:
            cs = int(it.get("citation_sum", 0) or 0)
            if cs > 0:
                cit_entities += 1
                cit_total += cs
        out[k] = {
            "raw_mentions": raw,
            "aggregated": agg,
            "merge_rate": round(1 - agg / raw, 3) if raw else 0.0,
            "confidence": _conf_stats(confs),
            "doc_completeness": {
                "with_description": with_desc,
                "with_description_ratio": round(with_desc / agg, 3) if agg else 0.0,
                "with_io": with_io,
                "with_io_ratio": round(with_io / agg, 3) if agg else 0.0,
            },
        }
    out["_overall"] = {
        "raw_mentions": raw_total,
        "aggregated": agg_total,
        "merge_rate": round(1 - agg_total / raw_total, 3) if raw_total else 0.0,
        "confidence": _conf_stats(all_confs),
        "citation_entities": cit_entities,
        "citation_entities_ratio": round(cit_entities / agg_total, 3) if agg_total else 0.0,
        "total_citations": cit_total,
    }
    return out


# ---------------- 4. 验证通过率 ----------------
def validation_metrics() -> dict:
    if not VALIDATION.exists():
        return {"available": False}
    v = json.loads(VALIDATION.read_text(encoding="utf-8"))
    summary = v.get("summary", {})
    total = summary.get("total", 0)
    by_status = summary.get("by_status", {})
    # 逐类型状态分布
    by_kind: dict[str, Counter] = defaultdict(Counter)
    for it in v.get("items", []):
        by_kind[it.get("kind", "?")][it.get("status", "?")] += 1
    return {
        "available": True,
        "total": total,
        "by_status": by_status,
        "pass_rate": round(by_status.get("pass", 0) / total, 3) if total else 0.0,
        # 实质检查通过率：忽略「是否带单元测试」(has_test 是信息项，本工程几乎无测试文件)，
        # 只看 4 项真检查（名称清晰/文档完整/LLM可读/专业性）是否通过 —— 这才是实体质量的真实信号。
        "real_check_pass_rate": round((total - by_status.get("fail", 0)
                                      - by_status.get("reject", 0)) / total, 3) if total else 0.0,
        "reject_rate": round(by_status.get("reject", 0) / total, 3) if total else 0.0,
        "needs_review_rate": round(by_status.get("needs_review", 0) / total, 3) if total else 0.0,
        "fail_rate": round(by_status.get("fail", 0) / total, 3) if total else 0.0,
        "by_kind": {k: dict(c) for k, c in by_kind.items()},
    }


# ---------------- 5. 复核收敛 ----------------
def review_metrics() -> dict:
    if not AUTO_REVIEW.exists():
        return {"available": False}
    a = json.loads(AUTO_REVIEW.read_text(encoding="utf-8"))
    total = a.get("total_queue", 0)
    accepted = a.get("accepted", 0)
    residual = a.get("residual_representatives", 0)
    return {
        "available": True,
        "accept_conf": a.get("accept_conf"),
        "total_queue": total,
        "accepted": accepted,
        "accept_rate": round(accepted / total, 3) if total else 0.0,
        "residual_representatives": residual,
        "residual_rate": round(residual / total, 3) if total else 0.0,
        "reduction_pct": a.get("reduction_pct"),
        "cluster_avg_size": a.get("cluster_avg_size"),
        "scenario_accept_conf_0_5": a.get("scenario_at_accept_conf_0_5"),
    }


# ---------------- 6. 数据完整度（recency）----------------
def recency_metrics(doi_date: dict[str, str]) -> dict:
    # 聚合实体的 source_papers → 发表月；统计有有效月的占比
    covered = 0
    total = 0
    months = set()
    for k in KINDS:
        for it in load_agg(k):
            total += 1
            ym = None
            for doi in (it.get("source_papers") or []):
                if doi_date.get(doi):
                    ym = doi_date[doi]
                    break
            if ym:
                covered += 1
                months.add(ym)
    return {
        "entities_total": total,
        "with_valid_month": covered,
        "month_coverage": round(covered / total, 3) if total else 0.0,
        "months_covered": len(months),
    }


# ---------------- 8. 综合精准度评分（启发式，透明加权）----------------
def composite_score(papers: dict, extr: dict, agg: dict, val: dict,
                    rev: dict, rec: dict) -> dict:
    """透明加权：各分量映射到 [0,1]，再线性加权。

    设计意图（仅作横向对比锚点，不代表绝对"对/错"）：
      - 抽取 live 比例  (1 - stub_ratio)       权重 0.20   —— LLM 真抽而非降级
      - 高置信占比      (conf.high_ratio)       权重 0.20   —— 抽取置信（精准度代理）
      - 去重合并率      (merge_rate)            权重 0.15   —— 目录去冗余（精准度核心）
      - 验证通过率      (pass_rate)             权重 0.15   —— 实体良构+专业门槛
      - 自动接受率      (accept_rate)           权重 0.15   —— 复核端收敛（高质量占比）
      - 月份覆盖        (month_coverage)        权重 0.15   —— 数据完整（趋势可信）
    被引覆盖不计入综合分（样本极小，仅 19/9435 实体有信号，会失真）。
    """
    live = 1 - extr.get("stub_ratio", 1.0)
    high_conf = agg["_overall"]["confidence"].get("high_ratio", 0.0)
    merge = agg["_overall"].get("merge_rate", 0.0)
    real_check = val.get("real_check_pass_rate", 0.0) if val.get("available") else 0.0
    accept = rev.get("accept_rate", 0.0) if rev.get("available") else 0.0
    month = rec.get("month_coverage", 0.0)
    weights = {"live": 0.20, "high_conf": 0.20, "merge": 0.15,
               "real_check": 0.15, "accept": 0.15, "month": 0.15}
    score = (live * weights["live"] + high_conf * weights["high_conf"]
             + merge * weights["merge"] + real_check * weights["real_check"]
             + accept * weights["accept"] + month * weights["month"])
    components = {
        "extraction_live_ratio": round(live, 3),
        "high_conf_ratio": round(high_conf, 3),
        "dedup_merge_rate": round(merge, 3),
        "validation_real_check_pass_rate": round(real_check, 3),
        "auto_accept_rate": round(accept, 3),
        "month_coverage": round(month, 3),
    }
    return {
        "score": round(score, 3),
        "weights": weights,
        "components": components,
        "note": "启发式综合分（0-1），为横向对比锚点；分量见各小节。被引覆盖样本过小未计入。",
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(AGG / "metrics.json"))
    args = ap.parse_args()
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    print("[metrics] 论文层 ...")
    papers = papers_metrics()
    print("[metrics] 抽取层 ...")
    extr = extraction_metrics()
    print("[metrics] 聚合 / 去重 / 置信 ...")
    agg = aggregated_metrics()
    print("[metrics] 验证通过率 ...")
    val = validation_metrics()
    print("[metrics] 复核收敛 ...")
    rev = review_metrics()
    print("[metrics] 数据完整度 ...")
    doi_date, _ = build_doi_maps()
    rec = recency_metrics(doi_date)

    print("[metrics] 综合精准度评分 ...")
    composite = composite_score(papers, extr, agg, val, rev, rec)

    out = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "papers": papers,
        "extraction": extr,
        "aggregated": agg,
        "validation": val,
        "review": rev,
        "recency": rec,
        "composite_precision": composite,
    }
    out_path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[metrics] 完成 -> {out_path}")
    print(f"  论文 {papers['total']} | 抽取 {extr['total']}(stub {extr['stub_ratio']}) "
          f"| 聚合实体 {agg['_overall']['aggregated']}(合并率 {agg['_overall']['merge_rate']})")
    print(f"  验证通过率 {val.get('pass_rate','NA')} | 自动接受率 {rev.get('accept_rate','NA')} "
          f"| 月份覆盖 {rec['month_coverage']}")
    print(f"  ★ 综合精准度评分: {composite['score']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
