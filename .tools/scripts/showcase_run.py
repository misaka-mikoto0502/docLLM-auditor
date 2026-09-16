"""showcase_run.py — 展示用全量验证：内嵌示例票证 → 映射示例服务真实单元 → 走主线裁决 → Excel。

从示例票证清单摘取「文档直接对比可发现=是」、且**单条目
规则可直接判**的若干示例服务缺陷，端到端走一遍项目主线流程：

  读产品清洗缓存 → segment 切 API 单元 → 按票证端点定位真实单元 → 每条目×所属单元类别 →
  LLM 并行裁决(adjudicate) → results.json → (gen_results_xlsx 转 Excel)

产物: audit/showcase_sample/{results.json,results.md,run_log.txt,results.xlsx}
并行: ThreadPoolExecutor，默认 8 并发；内嵌示例票证映射到若干单元×类别任务，约 1-3 分钟内跑完。

用法:
  python .tools/scripts/showcase_run.py [--product sample] [--workers 8] [--out audit/showcase_sample] [--dry-run]
"""
import os, sys, json, time, argparse, threading
from concurrent.futures import ThreadPoolExecutor, as_completed

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, ".tools", "scripts"))
from auditlib import pdf_lib, segment, cmmatch

KB = os.path.join(ROOT, "spec", "spec_rules.json")
CONFIG = os.path.join(ROOT, "spec", "rule_categories.json")

# ---------------------------------------------------------------------------
# 内嵌示例票证(示例用途, 直接对比=是, 单条目档可判)
#   issue       : 票证 Issue key
#   title       : 展示用一句话(违规要点)
#   endpoint    : 用于在示例服务单元中定位(路径子串)
#   rules       : 目标规则前缀(用于选类别/展示)
# ---------------------------------------------------------------------------
TICKETS = [
    dict(issue="TICKET-1178", rules=["ARG-010"],
         endpoint="/view/trace/get-trace-events",
         title="获取调用链全量数据: next_spanId/totalTime 应为 snake_case(ARG-010)"),
    dict(issue="TICKET-1180", rules=["ARG-010"],
         endpoint="/view/metric/trend",
         title="获取趋势图: latest_data_Time 大小写违规(ARG-010)"),
    dict(issue="TICKET-1117", rules=["ARG-010", "COM-010"],
         endpoint="/cmdb/tag/get-env-tag-list",
         title="查询环境标签: descp/gmt_create 命名不规范(ARG-010/COM-010)"),
    dict(issue="TICKET-1156", rules=["COM-010", "ARG-010"],
         endpoint="/view/trace/span-search",
         title="查询 Span 数据: biz_id/biz_code 命名怪异(COM-010/ARG-010)"),
    dict(issue="TICKET-1177", rules=["COM-010"],
         endpoint="/view/trace/get-trace-events",
         title="获取调用链全量数据: biz_id/biz_code 字段冗余(COM-010)"),
    dict(issue="TICKET-1104", rules=["COM-010", "URI-050"],
         endpoint="/cmdb/business/get-business-list",
         title="查询应用列表: business 应为 application(COM-010/URI-050)"),
    dict(issue="TICKET-3028", rules=["URI-050"],
         endpoint="/cmdb/business/get-business-list",
         title="应用列表端点过长应简化(URI-050)"),
    dict(issue="TICKET-3208", rules=["STC-010"],
         endpoint="/apm-service/monitor-item-mgr/save-monitor-item-config",
         title="保存/修改操作误用 POST 应 PUT(STC-010)"),
    dict(issue="TICKET-3214", rules=["COM-010"],
         endpoint="/systemmng/get-ak-sk-list",
         title="AK/SK 列表: descp 应为 description(COM-010)"),
    dict(issue="TICKET-3098", rules=["STC-010"],
         endpoint="/view/trace/span-search",
         title="查询类操作误用 POST 应 GET(STC-010)"),
]


def resolve_category(rules, cfg):
    """目标规则前缀 -> 所属单元类别 id 集合(去重)。"""
    ids = set()
    for c in cfg["unit_categories"]:
        if any(r in c["rules"] for r in rules):
            ids.add(c["id"])
    return ids


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--product", default="sample")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--out", default=os.path.join(ROOT, "audit", "showcase_sample"))
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--regen", action="store_true",
                    help="只读已存在的 results.json 重渲染 results.md/stats.json(不调 LLM)")
    ap.add_argument("--all-cats", action="store_true",
                    help="对每个样例单元跑全部单元类别(而非只跑票证指向的类别)=真·全量验证")
    a = ap.parse_args()

    cfg = json.load(open(CONFIG, encoding="utf-8"))
    by_no = pdf_lib.load_rules(KB)["by_no"]
    cats_map = {c["id"]: c["name"] for c in cfg["unit_categories"]}

    if a.regen:
        p = os.path.join(a.out, "results.json")
        if not os.path.exists(p):
            print("[错误] 无 results.json:", p); return 1
        record = json.load(open(p, encoding="utf-8"))
        stats = summary(record)
        json.dump(stats, open(os.path.join(a.out, "stats.json"), "w", encoding="utf-8"),
                  ensure_ascii=False, indent=1)
        tasks = {r.get("path"): r for r in record}   # 仅用于写 md 遍历
        write_md(record, stats, os.path.join(a.out, "results.md"), cats_map, tasks)
        print("[regen] 已重渲染 results.md / stats.json(未调 LLM)")
        return 0


    # 1) 切单元 + 路径索引
    ppath = os.path.join(ROOT, "audit", "work", "m5", a.product, "pages_clean.json")
    if not os.path.exists(ppath):
        print("[错误] 无产品缓存:", ppath); return 1
    units = segment.segment_apis(json.load(open(ppath, encoding="utf-8")))
    by_path = {}
    for u in units:
        p = u.get("path") or ""
        by_path.setdefault(p, u)

    # 2) 票证 -> 定位单元 + 所属类别；聚合 独立(单元×类别) 任务并记录覆盖票证
    tasks = {}              # key=(path,cat_id) -> {...}
    misses = []
    unit_by_ticket = {}
    for t in TICKETS:
        hit = next((u for p, u in by_path.items() if t["endpoint"] in p), None)
        if hit is None:
            misses.append(t["issue"]); continue
        unit_by_ticket[t["issue"]] = hit
        cids = set(cfg_units(cfg)) if a.all_cats else resolve_category(t["rules"], cfg)
        for cid in cids:
            k = (hit["path"], cid)
            d = tasks.setdefault(k, dict(method=hit["method"], path=hit["path"],
                                         start_page=hit["start_page"], title=hit.get("title_cn", ""),
                                         chapter=hit.get("chapter_title", ""), unit=hit, cat_id=cid,
                                         issues=[], rules_sel=set()))
            d["issues"].append(t["issue"]); d["rules_sel"].update(t["rules"])

    if misses:
        print("[警告] 未定位单元的票证:", misses)
    n = len(tasks)
    print(f"产品={a.product} 单元={len(units)} 票证={len(TICKETS)} 独立任务(单元×类别)={n} 并发={a.workers}")

    if a.dry_run:
        for k, d in tasks.items():
            rs = cmmatch.resolve_rules(by_no, cfg_unit_rules(cfg, d["cat_id"]))
            print(f"   {d['method']} {d['path']}  cat{d['cat_id']}({cats_map[d['cat_id']]}) "
                  f"issues={','.join(d['issues'])} 规则={len(rs)}")
        print("[dry-run] 任务校验完成, 未调 LLM。"); return 0

    # 3) 并行裁决
    os.makedirs(a.out, exist_ok=True)
    log = [f"== showcase_run start ==", f"product={a.product} tickets={len(TICKETS)} "
           f"tasks={n} workers={a.workers}"]
    lock = threading.Lock()
    record, fail = [], 0
    done = [False]
    t0 = time.perf_counter()
    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        futs = {ex.submit(cmmatch.adjudicate, d["unit"],
                          cmmatch.resolve_rules(by_no, cfg_unit_rules(cfg, d["cat_id"]))): d
                for d in tasks.values()}
        i = 0
        for fut in as_completed(futs):
            d = futs[fut]
            try:
                rec = fut.result()
            except Exception as e:
                rec = {"ok": False, "path": d["path"], "per_rule": [],
                       "reason_err": str(e)[:200]}
            rec["cat_id"] = d["cat_id"]; rec["cat_name"] = cats_map.get(d["cat_id"], "")
            rec["issues"] = d["issues"]; rec["rules_sel"] = sorted(d["rules_sel"])
            if not rec.get("ok"): fail += 1
            i += 1
            with lock:
                record.append(rec)
                open(os.path.join(a.out, "results.jsonl"), "a", encoding="utf-8").write(
                    json.dumps(rec, ensure_ascii=False) + "\n")
            print(f"  [{i}/{n}] {'OK' if rec.get('ok') else 'FAIL'} cat{d['cat_id']} "
                  f"{d['method']} {d['path']} ({round(rec.get('dt',0),1)}s)", flush=True)
    dur = time.perf_counter() - t0
    print(f"\n[运行] 任务 {n} 用时 {dur:.0f}s | ok={n-fail} fail={fail}")

    # 4) 落盘 + stats
    json.dump(record, open(os.path.join(a.out, "results.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    stats = summary(record)
    stats["wall_s"] = round(dur, 1)
    json.dump(stats, open(os.path.join(a.out, "stats.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    with open(os.path.join(a.out, "run_log.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(log) + "\n")
    write_md(record, stats, os.path.join(a.out, "results.md"), cats_map, tasks)
    print("[产物]", a.out)
    print("  results.json(results.md / stats.json / results.jsonl / run_log.txt)")
    print("  之后: python .tools/scripts/gen_results_xlsx.py <results.json> -o <results.xlsx>")
    return 0


def cfg_units(cfg):
    return [c["id"] for c in cfg["unit_categories"]]


def cfg_unit_rules(cfg, cid):
    for c in cfg["unit_categories"]:
        if c["id"] == cid:
            return c["rules"]
    return []


def summary(recs):
    conf, dedup = [], set()
    for r in recs:
        for p in (r.get("per_rule") or []):
            if p.get("result") == "confirmed":
                conf.append((r.get("path"), p.get("rule_no")))
                dedup.add((r.get("issues") and r["issues"][0], p.get("rule_no")))
    return {"records": len(recs), "confirmed": len(conf), "confirmed_grounded": len(conf),
            "dedup_confirmed_rules": sorted({pre(k) for k in {x[1] for x in dedup}}),
            "confirmed_combos": len(conf)}


def pre(no):
    return "-".join((no or "").split("-")[:2])


def write_md(recs, stats, path, cats_map, tasks):
    L = ["# 示例服务展示用例·全量验证(内嵌示例票证)\n"]
    L.append("> 源: 示例票证清单『文档直接对比可发现=是』单条目规则可直接判的若干条"
             f"；映射示例服务真实单元跑所属类别裁决。确认违规 {stats['confirmed']} 组合 / "
             f"{len(stats['dedup_confirmed_rules'])} 规则。\n")
    L.append("| 票证 | 目标规则 | 单元 | 结果 | 判定理由 |")
    L.append("|------|----------|------|------|----------|")
    for r in recs:
        issues = ",".join(r.get("issues") or [])
        sel = ",".join(r.get("rules_sel") or [])
        L.append(f"| **{issues}** | {sel} | {r.get('method')} {r.get('path')} | - | - |")
        for p in (r.get("per_rule") or []):
            mark = "✅" if p.get("result") == "confirmed" else ("⬜" if p.get("result") == "dismissed" else "❔")
            L.append(f"| | `{pre(p['rule_no'])}` {p['result']} {mark} | | "
                     f"{(p.get('reason') or '')[:70]} |")
    open(path, "w", encoding="utf-8").write("\n".join(L) + "\n")


if __name__ == "__main__":
    sys.exit(main())
