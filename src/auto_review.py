#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
auto_review.py — Phase 4b：复核队列自动化收敛

背景：validate.py 生成的 review_queue.jsonl 共 10569 条，100% 仅因「缺单元测试」
(test_status=manual) 被标 needs_review；它们五项真检查（名称清晰 / 文档完整 /
LLM 可读 / 专业性 / 通过测试）全部通过，test_status 是信息项不计入 failed。
—— 这是一个「默认未验证噪音」队列，不是真实质检问题，放大抓取前必须收敛。

策略（确定性、零外部依赖）：
  ① 自动接受：only_test_missing 且聚合 confidence >= --accept-conf（默认 0.7）。
     这些实体通过全部真检查 + 高抽取置信，直接标 auto_validated，不再占人工队列。
  ② 残差聚类：其余（中置信 0.5–accept_conf、低置信 <0.5、真实 failed）按
     (kind, 词族首 token) 确定性分组，只留每族代表 + cluster_size/members 进人工队列。
  ③ LLM-judge（可选，需 DEEPSEEK_API_KEY）：对「中置信」残差做二次 yes/no 判定，
     命中 yes 即升级为接受。本次未设 key 时该路径自动跳过。

输出：
  data/aggregated/cleaned_review_queue.jsonl   收敛后人工队列（仅残差代表）
  data/aggregated/auto_review_metrics.json     收敛统计（含 accept-conf=0.5 情景对比）
  回写聚合实体文件的 review_status / auto_validated 字段（非破坏性，仅新增）

用法：
  python src/auto_review.py
  python src/auto_review.py --accept-conf 0.5
  python src/auto_review.py --llm-judge
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
AGG_DIR = ROOT / "data" / "aggregated"
QUEUE = AGG_DIR / "review_queue.jsonl"
CLEANED = AGG_DIR / "cleaned_review_queue.jsonl"
METRICS = AGG_DIR / "auto_review_metrics.json"
KINDS = ["tasks", "tools", "databases", "software"]

# 词族分组：取归一化名称的首个字母数字 token（去大小写/标点），稳健且可复现
_TOKEN_RE = re.compile(r"[a-z0-9]+")


def family_key(name: str) -> str:
    norm = re.sub(r"[\s\-_./()]+", "", name.lower())
    m = _TOKEN_RE.search(norm)
    return m.group(0) if m else norm or "unknown"


def load_queue() -> list[dict]:
    if not QUEUE.exists():
        return []
    out = []
    for line in QUEUE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def load_aggregated() -> tuple[dict, dict, dict]:
    """返回 (lookup[key] = entity, files[(kind)] = path, data_by_kind[(kind)] = 解析后的完整 dict)。

    注意：lookup 中的 entity 与 data_by_kind[k]['items'] 中的对象是同一引用，
    因此就地修改 entity 后直接写回 data_by_kind[k] 即可持久化（切勿从磁盘重新 load 覆盖）。
    """
    lookup: dict[tuple[str, str], dict] = {}
    files: dict[str, Path] = {}
    data_by_kind: dict[str, dict] = {}
    for k in KINDS:
        p = AGG_DIR / f"{k}.json"
        if not p.exists():
            continue
        files[k] = p
        data = json.loads(p.read_text(encoding="utf-8"))
        data_by_kind[k] = data
        for it in data.get("items", []):
            nm = (it.get("name") or "").strip()
            if nm:
                lookup[(k, nm.lower())] = it
    return lookup, files, data_by_kind


def llm_judge_batch(client, items: list[dict]) -> dict[tuple[str, str], bool]:
    """对 (kind,name) 列表做 LLM 二次判定，返回 {key: accept_bool}。无 key 时返回空。"""
    from llm_client import LLMClient  # 延迟导入，避免无 key 环境也强依赖
    if client is None or not getattr(client, "_using_stub", True):
        # 注意：_using_stub 为 False 才表示 live；None 时视为不可用语
        pass
    result: dict[tuple[str, str], bool] = {}
    if client is None or client._using_stub:
        return result
    sys.stderr.write(f"[auto_review] LLM-judge 启用，二次判定 {len(items)} 条中置信残差...\n")
    for it in items:
        key = (it["kind"], it["name"].lower())
        desc = it.get("_desc", "")
        prompt = (
            f"实体名：{it['name']}\n类型：{it['kind']}\n描述：{desc[:500]}\n"
            f"该实体是否是一个真实存在、值得收录到生物学研究工具/数据库/软件/任务目录中的条目？"
            f"只回答 JSON：{{\"verdict\":\"yes|no\",\"reason\":\"一句话\"}}"
        )
        try:
            resp = client.extract("你是生物学文献抽取质量仲裁，严谨判断实体是否真实。", prompt)
            v = str(resp.get("verdict", "")).strip().lower()
            result[key] = v == "yes"
        except Exception as e:  # noqa: BLE001
            sys.stderr.write(f"[auto_review] judge fail {it['name']}: {e}\n")
            result[key] = False
    return result


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--accept-conf", type=float, default=0.7,
                    help="自动接受阈值：only_test_missing 且 confidence>=该值即接受（默认 0.7）")
    ap.add_argument("--llm-judge", action="store_true",
                    help="对中置信残差启用 DeepSeek 二次判定（需 DEEPSEEK_API_KEY）")
    args = ap.parse_args()

    queue = load_queue()
    lookup, files, data_by_kind = load_aggregated()
    total = len(queue)
    if total == 0:
        print("[auto_review] review_queue.jsonl 为空，无需收敛。")
        return 0

    # LLM 客户端（可选）
    client = None
    if args.llm_judge:
        try:
            sys.path.insert(0, str(ROOT / "src"))
            from llm_client import LLMClient
            client = LLMClient()
            if client._using_stub:
                sys.stderr.write("[auto_review] 警告：未检测到 DEEPSEEK_API_KEY，LLM-judge 跳过。\n")
                client = None
        except Exception as e:  # noqa: BLE001
            sys.stderr.write(f"[auto_review] LLM 初始化失败：{e}\n")
            client = None

    accepted: list[dict] = []
    residual: list[dict] = []          # 残差候选（含待 LLM 判定的中置信）
    mid_for_judge: list[dict] = []     # 中置信，待 LLM 判定
    by_kind_acc = defaultdict(int)
    by_kind_res = defaultdict(int)
    reason_count = defaultdict(int)
    scenario_at_05 = {"accept": 0, "residual": 0}  # accept-conf=0.5 情景对比

    for q in queue:
        name = q.get("name", "")
        kind = q.get("kind", "")
        detail = q.get("detail", {}) or {}
        checks = detail.get("checks", {}) or {}
        manual = detail.get("manual", []) or []
        real_failed = [k for k, v in checks.items()
                       if v is False and k != "has_test"]
        only_test_missing = (not real_failed) and manual == ["test_status"]

        ent = lookup.get((kind, name.lower()))
        conf = float(ent.get("confidence", 0)) if ent else 0.0
        # 描述用于 LLM-judge
        desc = ""
        if ent:
            desc = ent.get("description") or ent.get("schema") or ent.get("primary_use") or ""

        key = (kind, name.lower())
        if only_test_missing and conf >= args.accept_conf:
            decision = "accept"
        elif only_test_missing and 0.5 <= conf < args.accept_conf:
            decision = "mid"
        elif only_test_missing and conf < 0.5:
            decision = "low"
        elif real_failed:
            decision = "fail"
        else:
            decision = "other"

        # accept-conf=0.5 情景：中置信也接受
        if only_test_missing and conf >= 0.5:
            scenario_at_05["accept"] += 1
        else:
            scenario_at_05["residual"] += 1

        if decision == "accept":
            accepted.append(q)
            by_kind_acc[kind] += 1
            if ent is not None and not ent.get("auto_validated"):
                ent["review_status"] = "accepted"
                ent["auto_validated"] = True
                ent["auto_review_policy"] = f"check_pass+conf>={args.accept_conf}"
                ent["auto_review_ts"] = datetime.now(timezone.utc).isoformat()
        else:
            residual.append({"q": q, "kind": kind, "name": name, "conf": conf,
                             "decision": decision, "_desc": desc, "key": key})
            by_kind_res[kind] += 1
            reason_count[decision] += 1
            if decision == "mid" and client is not None:
                mid_for_judge.append({"kind": kind, "name": name, "_desc": desc})

    # 可选 LLM-judge：中置信升级
    llm_accepted = 0
    if mid_for_judge:
        verdicts = llm_judge_batch(client, mid_for_judge)
        still_residual = []
        for r in residual:
            if r["decision"] == "mid" and verdicts.get(r["key"]):
                # 升级为接受
                ent = lookup.get(r["key"])
                if ent is not None and not ent.get("auto_validated"):
                    ent["review_status"] = "accepted"
                    ent["auto_validated"] = True
                    ent["auto_review_policy"] = "check_pass+llm_judge"
                    ent["auto_review_ts"] = datetime.now(timezone.utc).isoformat()
                accepted.append(r["q"])
                by_kind_acc[r["kind"]] += 1
                llm_accepted += 1
            else:
                still_residual.append(r)
        residual = still_residual
        by_kind_res.clear()
        reason_count.clear()
        for r in residual:
            by_kind_res[r["kind"]] += 1
            reason_count[r["decision"]] += 1

    # 回写聚合实体文件（非破坏性，仅新增 review_status / auto_validated 等字段）
    # 必须用内存中已就地修改过的 data_by_kind，切勿从磁盘重新 load（否则覆盖修改）
    for k, p in files.items():
        data = data_by_kind[k]
        p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    # 残差按 (kind, 词族) 聚类，只留代表
    families: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for r in residual:
        fk = family_key(r["name"])
        families[(r["kind"], fk)].append(r)

    cleaned = []
    cluster_sizes = []
    for (kind, fk), members in families.items():
        members_sorted = sorted(members, key=lambda x: x["conf"], reverse=True)
        rep = members_sorted[0]
        cluster_sizes.append(len(members_sorted))
        rec = {
            "name": rep["name"],
            "kind": kind,
            "status": "needs_review",
            "family_key": fk,
            "cluster_size": len(members_sorted),
            "cluster_members": [m["name"] for m in members_sorted],
            "representative_conf": rep["conf"],
            "decisions": sorted({m["decision"] for m in members_sorted}),
            "detail": rep["q"].get("detail", {}),
        }
        cleaned.append(rec)

    # 写出收敛后人工队列
    CLEANED.parent.mkdir(parents=True, exist_ok=True)
    with CLEANED.open("w", encoding="utf-8") as f:
        for rec in cleaned:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    num_repr = len(cleaned)
    reduction = (1 - num_repr / total) * 100 if total else 0
    metrics = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "accept_conf": args.accept_conf,
        "llm_judge_enabled": client is not None,
        "llm_accepted": llm_accepted,
        "total_queue": total,
        "accepted": len(accepted),
        "residual_representatives": num_repr,
        "residual_raw": len(residual),
        "reduction_pct": round(reduction, 2),
        "accepted_by_kind": dict(by_kind_acc),
        "residual_by_kind": dict(by_kind_res),
        "residual_reason": dict(reason_count),
        "cluster_max_size": max(cluster_sizes) if cluster_sizes else 0,
        "cluster_avg_size": round(sum(cluster_sizes) / len(cluster_sizes), 2) if cluster_sizes else 0,
        "scenario_at_accept_conf_0_5": scenario_at_05,
    }
    METRICS.write_text(json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"[auto_review] 队列总数 {total}")
    print(f"[auto_review] 自动接受 {len(accepted)}（按 conf>={args.accept_conf}），"
          f"残差代表 {num_repr}（原始残差 {len(residual)}），人工队列压缩 {reduction:.1f}%")
    if client is not None:
        print(f"[auto_review] LLM-judge 升级接受 {llm_accepted} 条")
    print(f"[auto_review] 对比：若 accept-conf=0.5，将接受 {scenario_at_05['accept']}，"
          f"残差 {scenario_at_05['residual']}")
    print(f"[auto_review] -> {CLEANED}")
    print(f"[auto_review] -> {METRICS}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
