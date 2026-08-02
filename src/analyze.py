#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
analyze.py — Biomni-E1 洞察层（Phase 5+ 分析增强）

消费现有聚合结果（data/aggregated/{tasks,tools,databases,software}.json）
+ 原始抽取（data/extractions/*.json 用于 doi->date/category 映射），
产出真正的"分析"而非"计数"：

  1. 规模与质量概览（tier 分布、低置信度占比、待人工复核规模）
  2. 热度 Top 实体（frequency × confidence 作为无引用数据时的代理影响力）
  3. 方法簇：Task→Tool 共现网络 + 社群发现（networkx greedy modularity）
  4. 跨学科技工共性（同一工具出现在 ≥3 个 bioRxiv 子领域）
  5. 近期节奏：月度活跃论文数 + 月度新增唯一工具数
  6. 自动综述段落（可读 Markdown，给必学必会文章用）

输出：
  data/aggregated/analysis.json        结构化结果
  data/aggregated/analysis_report.md   可读报告（供 finalize_article.py 引用）

用法：
  python src/analyze.py
  python src/analyze.py --out-dir data/aggregated
"""
from __future__ import annotations

import argparse
import json
import re
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

import networkx as nx

ROOT = Path(__file__).resolve().parent.parent
AGG = ROOT / "data" / "aggregated"
EXT = ROOT / "data" / "extractions"
REVQ = AGG / "review_queue.jsonl"

CONF_LOW = 0.5  # 低置信度阈值（< 此值标记 needs_review）
TIER_HIGH = 50  # frequency ≥ 此值 -> high
TIER_MID = 10   # frequency ≥ 此值 -> mid，否则 low


# ---------------- 加载 ----------------
def load_agg(name: str) -> list[dict]:
    p = AGG / f"{name}.json"
    if not p.exists():
        return []
    d = json.loads(p.read_text(encoding="utf-8"))
    if isinstance(d, dict):
        return d.get("items", [])
    return d if isinstance(d, list) else []


def build_doi_maps() -> tuple[dict[str, str], dict[str, str]]:
    """doi -> date(YYYY-MM), doi -> category"""
    doi_date: dict[str, str] = {}
    doi_cat: dict[str, str] = {}
    if not EXT.exists():
        return doi_date, doi_cat
    for fp in EXT.glob("*.json"):
        try:
            d = json.loads(fp.read_text(encoding="utf-8"))
        except Exception:
            continue
        doi = d.get("doi")
        if not doi:
            continue
        dt = d.get("date") or ""
        if len(dt) >= 7 and dt[4] == "-":
            doi_date[doi] = dt[:7]
        else:
            doi_date[doi] = ""
        doi_cat[doi] = d.get("category") or "?"
    return doi_date, doi_cat


# ---------------- 分析模块 ----------------
def quality_overview(tasks, tools, dbs, sw) -> dict:
    def tier_of(freq: int) -> str:
        if freq >= TIER_HIGH:
            return "high"
        if freq >= TIER_MID:
            return "mid"
        return "low"

    out = {}
    for key, items in (("tasks", tasks), ("tools", tools), ("databases", dbs), ("software", sw)):
        tiers = Counter()
        low = 0
        for it in items:
            f = int(it.get("frequency", 1) or 1)
            tiers[tier_of(f)] += 1
            if float(it.get("confidence", 1) or 1) < CONF_LOW:
                low += 1
        out[key] = {
            "total": len(items),
            "tier": dict(tiers),
            "low_conf_ratio": round(low / len(items), 3) if items else 0.0,
        }
    # 待人工复核规模
    rq = 0
    if REVQ.exists():
        rq = sum(1 for _ in REVQ.read_text(encoding="utf-8").splitlines() if _.strip())
    total_entities = len(tasks) + len(tools) + len(dbs) + len(sw)
    out["review_queue"] = rq
    out["review_ratio"] = round(rq / total_entities, 3) if total_entities else 0.0
    return out


def heat_top(items: list[dict], k: int = 30) -> list[dict]:
    scored = []
    for it in items:
        f = int(it.get("frequency", 1) or 1)
        c = float(it.get("confidence", 0.5) or 0.5)
        scored.append((it.get("name", "?"), round(f * c, 2), f, c))
    scored.sort(key=lambda x: (-x[1], -x[2]))
    return [{"name": n, "heat": h, "frequency": f, "confidence": c}
            for n, h, f, c in scored[:k]]


def cooccurrence_clusters(tasks: list[dict], tool_freq: dict[str, int], top: int = 12) -> dict:
    """Task.required_tools 两两共现 -> Tool-Tool 网络 -> 社群发现"""
    G = nx.Graph()
    edge_w: dict[tuple[str, str], int] = defaultdict(int)
    tool_type: dict[str, str] = {}
    # 预载 tool type
    for it in load_agg("tools"):
        tool_type[it.get("name", "")] = it.get("type", "")

    for t in tasks:
        req = t.get("required_tools") or []
        # 只保留高频工具（frequency>=2）以抑制噪声爆炸
        req = [r for r in req if tool_freq.get(r, 0) >= 2]
        req = list(dict.fromkeys(req))  # 去重保序
        for i in range(len(req)):
            for j in range(i + 1, len(req)):
                a, b = req[i], req[j]
                edge_w[tuple(sorted((a, b)))] += 1

    for (a, b), w in edge_w.items():
        G.add_edge(a, b, weight=w)
    for n in G.nodes():
        G.nodes[n]["type"] = tool_type.get(n, "")

    communities = []
    if G.number_of_nodes() >= 2:
        try:
            comms = nx.community.greedy_modularity_communities(G, weight="weight")
        except Exception:
            comms = []
        for idx, comm in enumerate(comms):
            members = list(comm)
            # 社群代表工具 = 社群内 frequency 最高的
            ranked = sorted(members, key=lambda m: -tool_freq.get(m, 0))
            types = Counter(tool_type.get(m, "") for m in members)
            communities.append({
                "id": idx,
                "size": len(members),
                "top_tools": ranked[:8],
                "type_mix": dict(types.most_common(5)),
                "density": round(nx.density(G.subgraph(members)), 3) if len(members) > 1 else 0.0,
            })
        communities.sort(key=lambda c: -c["size"])

    # 全局最紧共现对
    top_pairs = sorted(edge_w.items(), key=lambda x: -x[1])[:20]
    return {
        "nodes": G.number_of_nodes(),
        "edges": G.number_of_edges(),
        "top_pairs": [{"a": a, "b": b, "shared_tasks": w} for (a, b), w in top_pairs],
        "clusters": communities[:top],
    }


def cross_discipline_tools(tools: list[dict], doi_cat: dict[str, str], min_cats: int = 3) -> list[dict]:
    rows = []
    for it in tools:
        cats = set()
        for doi in (it.get("source_papers") or []):
            c = doi_cat.get(doi)
            if c and c != "?":
                cats.add(c)
        if len(cats) >= min_cats:
            rows.append({"name": it.get("name"), "n_cats": len(cats),
                         "categories": sorted(cats), "frequency": int(it.get("frequency", 1) or 1)})
    rows.sort(key=lambda x: (-x["n_cats"], -x["frequency"]))
    return rows[:30]


def monthly_rhythm(doi_date: dict[str, str], tools: list[dict]) -> dict:
    # 月度活跃论文数（按 doi 去重落月）
    active = Counter()
    for doi, ym in doi_date.items():
        if ym:
            active[ym] += 1
    # 月度新增唯一工具（按工具首次出现论文的最早 date）
    tool_first = {}
    for it in tools:
        first = None
        for doi in (it.get("source_papers") or []):
            ym = doi_date.get(doi)
            if ym and (first is None or ym < first):
                first = ym
        if first:
            tool_first[it.get("name", "")] = first
    new_tools = Counter(tool_first.values())
    months = sorted(set(list(active) + list(new_tools)))
    series = [{"month": m, "active_papers": active.get(m, 0), "new_tools": new_tools.get(m, 0)}
              for m in months]
    return {"series": series, "months_covered": len(months)}


def build_report(out: dict) -> str:
    L = []
    L.append("# Biomni-E1 洞察分析报告\n")
    L.append(f"> 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}  ")
    L.append("> 数据源：现有聚合结果（tasks/tools/databases/software）+ 原始抽取 doi→date/category 映射；未做新增抓取。\n")

    q = out["quality"]
    L.append("## 1. 规模与质量概览\n")
    L.append(f"- 实体总量：任务 **{q['tasks']['total']}** / 工具 **{q['tools']['total']}** / 数据库 **{q['databases']['total']}** / 软件 **{q['software']['total']}**")
    L.append(f"- 高频实体（frequency≥{TIER_HIGH}）：任务 {q['tasks']['tier'].get('high',0)} / 工具 {q['tools']['tier'].get('high',0)}")
    L.append(f"- 低置信度（confidence<{CONF_LOW}）占比：任务 {q['tasks']['low_conf_ratio']} / 工具 {q['tools']['low_conf_ratio']}")
    L.append(f"- **待人工复核队列：{q['review_queue']} 条（占实体总量 {q['review_ratio']}）** —— 放大抓取前必须先自动化收敛，否则队列会线性膨胀。\n")

    L.append("## 2. 最热方法（热度 = frequency × confidence）\n")
    L.append("> 无引用数据时，以「出现频次 × 抽取置信度」作为影响力代理分。\n")
    for kind, key in (("工具", "tools"), ("任务", "tasks")):
        L.append(f"### Top {kind}\n")
        for r in out["heat"][key][:15]:
            L.append(f"- **{r['name']}**（热度 {r['heat']}，频次 {r['frequency']}，置信度 {r['confidence']}）")
        L.append("")

    cc = out["cooccurrence"]
    L.append("## 3. 方法簇（共现网络 + 社群发现）\n")
    L.append(f"> 由 Task→Tool 依赖构建共现网络：{cc['nodes']} 个工具节点、{cc['edges']} 条边；"
             f"最紧共现对反映方法间的强耦合。\n")
    L.append("### 最紧共现对\n")
    for p in cc["top_pairs"][:10]:
        L.append(f"- {p['a']} ⇄ {p['b']}（共享 {p['shared_tasks']} 个任务）")
    L.append("")
    L.append(f"### 主要方法簇（共 {len(cc['clusters'])} 个社群，按规模列前 {min(8,len(cc['clusters']))}）\n")
    for c in cc["clusters"][:8]:
        L.append(f"- **簇#{c['id']}**（{c['size']} 工具，内部密度 {c['density']}）："
                 + "、".join(c["top_tools"][:6]))
    L.append("")

    L.append("## 4. 跨学科技工共性（出现在 ≥3 个 bioRxiv 子领域）\n")
    L.append("> 这些工具被多个学科独立采用，是方法论收敛的真实信号。\n")
    for r in out["cross"][:15]:
        L.append(f"- **{r['name']}**：跨 {r['n_cats']} 领域（{', '.join(r['categories'][:5])}）")
    L.append("")

    mr = out["monthly"]
    L.append("## 5. 近期节奏（月度）\n")
    L.append(f"> 覆盖 {mr['months_covered']} 个月。注意：date 为抓取/入库时间（近似发表时间），反映近 ~18 个月 bioRxiv 产出节奏。\n")
    L.append("| 月份 | 活跃论文 | 新增唯一工具 |")
    L.append("|------|---------:|-------------:|")
    for s in mr["series"][-14:]:
        L.append(f"| {s['month']} | {s['active_papers']} | {s['new_tools']} |")
    L.append("")

    L.append("## 6. 自动洞察（供文章引用）\n")
    top_tool = out["heat"]["tools"][0]["name"] if out["heat"]["tools"] else "—"
    top_task = out["heat"]["tasks"][0]["name"] if out["heat"]["tasks"] else "—"
    n_cross = len(out["cross"])
    n_clusters = len(out["cooccurrence"]["clusters"])
    L.append(f"- 本批 {q['tasks']['total']} 个研究任务、{q['tools']['total']} 个工具构成的方法环境，"
             f"最热工具为 **{top_tool}**、最热任务为 **{top_task}**。")
    L.append(f"- 共现网络自动识别出 **{n_clusters}** 个方法簇，说明生物医学研究并非散点，"
             f"而是围绕若干方法族收敛（如被高频共引的工具对，往往构成某个子领域的事实标准流程）。")
    L.append(f"- **{n_cross}** 个工具跨越 ≥3 个学科，验证了许多方法（如 {out['cross'][0]['name'] if out['cross'] else '—'}）"
             f"已从单一领域外溢为通用基础设施。")
    L.append(f"- 待人工复核队列高达 {q['review_queue']} 条，提示：在放大抓取前，"
             f"应先以 LLM-judge 自动收敛 + 英文生物医学 embedding 升级去重，否则噪声会随量级线性放大。")
    L.append("")
    L.append("---")
    L.append("_本分析由 `src/analyze.py` 自动生成，消费既有聚合数据，零新增抓取成本。_")
    return "\n".join(L) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-dir", default=str(AGG))
    args = ap.parse_args()
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    print("[analyze] 加载聚合实体 ...")
    tasks = load_agg("tasks")
    tools = load_agg("tools")
    dbs = load_agg("databases")
    sw = load_agg("software")

    tool_freq = {it.get("name", ""): int(it.get("frequency", 1) or 1) for it in tools}

    print("[analyze] 构建 doi 映射（扫 extractions）...")
    doi_date, doi_cat = build_doi_maps()

    print("[analyze] 质量概览 ...")
    quality = quality_overview(tasks, tools, dbs, sw)

    print("[analyze] 热度 Top ...")
    heat = {
        "tools": heat_top(tools),
        "tasks": heat_top(tasks),
        "databases": heat_top(dbs),
        "software": heat_top(sw),
    }

    print("[analyze] 共现网络 + 社群发现 ...")
    cooccurrence = cooccurrence_clusters(tasks, tool_freq)

    print("[analyze] 跨学科技工共性 ...")
    cross = cross_discipline_tools(tools, doi_cat)

    print("[analyze] 月度节奏 ...")
    monthly = monthly_rhythm(doi_date, tools)

    out = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "quality": quality,
        "heat": heat,
        "cooccurrence": cooccurrence,
        "cross": cross,
        "monthly": monthly,
    }
    (out_dir / "analysis.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    report = build_report(out)
    (out_dir / "analysis_report.md").write_text(report, encoding="utf-8")

    print(f"[analyze] 完成：analysis.json + analysis_report.md 已写入 {out_dir}")
    print(f"  实体：任务 {quality['tasks']['total']} / 工具 {quality['tools']['total']} / "
          f"库 {quality['databases']['total']} / 软件 {quality['software']['total']}")
    print(f"  方法簇：{len(cooccurrence['clusters'])} 个；跨学科技工：{len(cross)} 个；"
          f"待复核：{quality['review_queue']} 条")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
