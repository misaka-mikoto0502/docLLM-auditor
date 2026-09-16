"""cm_run_product.py — 按产品跑单条目档全量匹配（读产品已有的 pages_clean.json 缓存，不重提 PDF）。

与 cmmatch_run.py（面向示例产品、缓存路径写死）的区别：
  * 输入 = 任意产品已有的清洗页缓存（audit/work/pages_clean.json 或 audit/work/m5/<pid>/pages_clean.json）
  * 类别 = spec/rule_categories.json 全部类别（G1-G6 单元级 + G7 整篇 DOC），不含双路召回目录
  * 断点续跑/并发/熔断/产物 与 cmmatch_run 同构（results.jsonl/json, stats.json, results.md, run_log.txt）

用法:
  python .tools/scripts/cm_run_product.py sample [--out audit/cm_sample] [--workers 6]
      [--max-fail 10] [--cats 1,2,3] [--limit 0] [--dry-run] [--pdf <示例产品 pdf 路径>]
"""
import os, sys, json, time, argparse, threading
from concurrent.futures import ThreadPoolExecutor, as_completed

SCRIPTS = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPTS)
from cmmatch_run import task_key, load_done_keys, adjudicate_task, _annotate_printed_pages
from auditlib import pdf_lib, segment, cmmatch, recall

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
KB = os.path.join(ROOT, "spec", "spec_rules.json")
CONFIG = os.path.join(ROOT, "spec", "rule_categories.json")
WORK = os.path.join(ROOT, "audit", "work")
DEFAULT_OUT = os.path.join(ROOT, "audit", "cm_product")


def product_pages(pid: str) -> str:
    if pid == "sample":
        return os.path.join(WORK, "pages_clean.json")
    return os.path.join(WORK, "m5", pid, "pages_clean.json")


def build_tasks(units, cfg, by_no):
    """枚举任务：每条目 × 每个 unit 类别(G1-G6) + doc 类别(G7)。规则=已解析完整规则。"""
    tasks = []
    for c in cfg["unit_categories"]:
        rules = cmmatch.resolve_rules(by_no, c["rules"])
        for u in units:
            tasks.append({
                "mode": "unit", "cat_id": c["id"], "cat_name": c["name"], "rules": rules,
                "method": u.get("method", ""), "path": u.get("path", ""),
                "start_page": u.get("start_page"), "title": u.get("title_cn", ""),
                "unit": u,
            })
    for c in cfg.get("doc_categories", []):
        rules = cmmatch.resolve_rules(by_no, c["rules"])
        tasks.append({"mode": "doc", "cat_id": c["id"], "cat_name": c["name"],
                      "rules": rules, "method": "", "path": "", "start_page": None,
                      "title": "", "unit": None})
    return tasks


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("product", help="产品 id，如 sample")
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--max-fail", type=int, default=10)
    ap.add_argument("--cats", default="", help="限定类别id逗号分隔,留空=全部(G1-G7)")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--pdf", default="", help="用于标注 PDF 印刷页码(可省)")
    a = ap.parse_args()

    pages_path = product_pages(a.product)
    if not os.path.exists(pages_path):
        print(f"[错误] 无该产品清洗缓存: {pages_path}")
        return 1
    pages = json.load(open(pages_path, encoding="utf-8"))
    units = segment.segment_apis(pages)
    if a.limit:
        units = units[:a.limit]

    cfg = json.load(open(CONFIG, encoding="utf-8"))
    by_no = pdf_lib.load_rules(KB)["by_no"]
    tasks = build_tasks(units, cfg, by_no)
    if a.cats:
        cats = {int(x) for x in a.cats.split(",") if x.strip()}
        tasks = [t for t in tasks if t["cat_id"] in cats]

    n_task = len(tasks)
    n_rules = sum(len(t["rules"]) for t in tasks)
    p0 = cmmatch.build_prompt(tasks[0]["unit"], tasks[0]["rules"]) if tasks else ""
    from collections import Counter
    print(f"产品={a.product} 页={len(pages)} 条目={len(units)} 任务={n_task} 规则判定={n_rules} "
          f"| 首prompt={len(p0)}B")
    print("分类分布:", dict(Counter(t["cat_id"] for t in tasks)))
    cats_map = {c["id"]: c["name"] for c in cfg["unit_categories"] + cfg.get("doc_categories", [])}
    print("类别:", {k: cats_map[k] for k in sorted(Counter(t["cat_id"] for t in tasks))})

    os.makedirs(a.out, exist_ok=True)
    jsonl = os.path.join(a.out, "results.jsonl")
    log = [f"== cm_run_product start {__import__('datetime').datetime.now().isoformat()} ==",
           f"product={a.product} pages={len(pages)} units={len(units)} tasks={n_task} "
           f"rule-judgements={n_rules} workers={a.workers} cats={a.cats or '全部G1-G7'}"]
    open(os.path.join(a.out, "run_log.txt"), "w", encoding="utf-8").write("\n".join(log) + "\n")

    if a.dry_run:
        for c in cfg["unit_categories"] + cfg.get("doc_categories", []):
            rs = cmmatch.resolve_rules(by_no, c["rules"])
            print(f"   cat{c['id']} {c['name']}: {len(c['rules'])}前缀 -> {len(rs)}规则 "
                  f"{'OK' if rs else '!!缺失'}")
        print(f"[dry-run] 任务 {n_task}、规则判定 {n_rules}；校验完成，未调 LLM。")
        return 0

    done = load_done_keys(jsonl)
    todo = [t for t in tasks if task_key(t) not in done]
    log.append(f"resume: skip {len(tasks)-len(todo)} done / run {len(todo)}")
    print(f"resume: skip {len(tasks)-len(todo)} / run {len(todo)}")

    lock = threading.Lock()
    consec_fail, run_fail, abort = 0, 0, {"flag": False}
    record = []

    def emit(t, rec):
        nonlocal consec_fail, run_fail
        rec = dict(rec)
        rec["key"] = task_key(t)
        with lock:
            with open(jsonl, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            record.append(rec)
            if not rec.get("ok"):
                run_fail += 1
            consec_fail = consec_fail + 1 if not rec.get("ok") else 0
            if consec_fail >= a.max_fail:
                abort["flag"] = True

    t0 = time.perf_counter()
    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        futs = {ex.submit(adjudicate_task, t, pages): t for t in todo}
        i = 0
        for fut in as_completed(futs):
            if abort["flag"]:
                break
            t = futs[fut]
            try:
                _, rec = fut.result()
            except Exception as e:
                rec = {"ok": False, "cat_id": t["cat_id"], "key": task_key(t),
                       "method": t["method"], "path": t["path"],
                       "start_page": t.get("start_page"), "title": t.get("title", ""),
                       "per_rule": [], "summary": "", "reason_err": str(e)[:200]}
            i += 1
            emit(t, rec)
            flag = "OK " if rec.get("ok") else "FAIL"
            print(f"  [{i}/{len(todo)}] {flag} cat{t['cat_id']} {t['method']} {t['path']} "
                  f"({round(rec.get('dt',0),1)}s)", flush=True)
    dur = time.perf_counter() - t0
    log.append(f"run {len(todo)} task in {dur:.0f}s | ok={len(todo)-run_fail} fail={run_fail} "
               f"| abort={'熔断' if abort['flag'] else '否'}")
    print(f"\n[运行] {len(todo)} 任务 {dur:.0f}s | ok={len(todo)-run_fail} fail={run_fail}")

    # ---- 产物：全量重读 jsonl(含累计) ----
    allrec = [json.loads(l) for l in open(jsonl, encoding="utf-8") if l.strip()]
    if a.pdf:
        _annotate_printed_pages(allrec, a.pdf)
    allstats = cmmatch.collect_stats(allrec)
    allstats["wall_s"] = round(dur, 1)

    json.dump(allrec, open(os.path.join(a.out, "results.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    json.dump(allstats, open(os.path.join(a.out, "stats.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    open(os.path.join(a.out, "results.md"), "w", encoding="utf-8").write(
        cmmatch.render_md(allrec, allstats))
    with open(os.path.join(a.out, "run_log.txt"), "a", encoding="utf-8") as f:
        f.write("\n" + "\n".join(log) + "\n")
    with open(os.path.join(a.out, "llm_call_log.jsonl"), "w", encoding="utf-8") as f:
        for c in recall.call_log():
            f.write(json.dumps(c, ensure_ascii=False) + "\n")
    print(f"[产物] {a.out}")
    print(f"   results.json({len(allrec)}条) / results.jsonl / results.md / stats.json / run_log.txt / llm_call_log.jsonl")
    print(f"   去重确认违规: {allstats['confirmed_grounded']} 组合 / {len(allstats['dedup_confirmed_rules'])} 规则")
    return 0


if __name__ == "__main__":
    sys.exit(main())
