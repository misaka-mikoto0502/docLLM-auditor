"""cmmatch_run.py — A 档完整匹配 · 生产运行入口。

用法:
  python .tools/scripts/cmmatch_run.py [--config spec/rule_categories_ABC.json]
      [--out audit/cm] [--workers 6] [--max-fail 10]
      [--cats 1,2,3,4,5,6,8] [--limit 0] [--dry-run] [--force-extract]

特性:
  * 断点续跑：每个任务完成即追加写 results.jsonl(key=cat|method|path)；重跑跳过已有 key。
  * 并发 + 熔断：连续失败 >= max-fail 则提前终止(保留已跑部分)。
  * dry-run：只枚举任务、生成 prompt(不打 LLM)，校验任务数/规则解析/prompt 长度。
  * 产物：results.jsonl(追加) / results.json / stats.json / results.md / run_log.txt / llm_call_log.jsonl
"""
import os, sys, json, time, argparse, threading
from concurrent.futures import ThreadPoolExecutor, as_completed


def _annotate_printed_pages(records, pdf_path):
    """给记录补 PDF 印刷页码(页脚整行数字)。best-effort：失败则置 None，不中断主流程。"""
    for r in records:
        r["printed_page"] = None
    try:
        import fitz
        doc = fitz.open(pdf_path)
        cache = {}
        for r in records:
            sp = r.get("start_page")
            if not isinstance(sp, int):
                continue
            if sp not in cache:
                txt = doc[sp].get_text("text")
                nums = [l.strip() for l in txt.split("\n")
                        if l.strip().isdigit() and len(l.strip()) <= 4]
                cache[sp] = int(nums[-1]) if nums else None
            r["printed_page"] = cache[sp]
    except Exception:
        pass

SCRIPTS = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPTS)
sys.path.insert(0, os.path.join(SCRIPTS, "auditlib"))
sys.path.insert(0, os.path.join(SCRIPTS, "..", "pylibs"))

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEFAULT_PDF = os.path.join(ROOT, "reference", "ModelArts_API参考-pdf.pdf")
DEFAULT_OUT = os.path.join(ROOT, "audit", "cm")
CONFIG = os.path.join(ROOT, "spec", "rule_categories_ABC.json")
KB = os.path.join(ROOT, "spec", "spec_rules.json")

from auditlib import pdf_lib, segment, recall
from auditlib import cmmatch


def load_config(path):
    return json.load(open(path, encoding="utf-8"))


def build_tasks(units, cfg, by_no):
    """枚举任务：每条目 × 每个 per-unit 类别 + doc 类别。rules=已解析的完整规则。"""
    tasks = []
    cat_by_id = {c["id"]: c for c in cfg["unit_categories"]}
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


def task_key(t):
    if t["mode"] == "doc":
        return f"doc|{t['cat_id']}"
    # 含 start_page 消歧：同一 method+path 可能对应多个章节条目
    return f"unit|{t['cat_id']}|{t['method']}|{t['path']}|{t.get('start_page')}"


def load_done_keys(jsonl):
    """只把「成功」的任务视为已完成(续跑时跳过)；失败/解析失败的记录不算完成，下次自动重试。
    教训(2026-08-13)：若把 fail 也当 done，几小时运行中偶发的 LLM 失败将永远被跳过、形成数据空洞。"""
    done = set()
    if os.path.exists(jsonl):
        for line in open(jsonl, encoding="utf-8"):
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except Exception:
                continue
            if r.get("ok") and r.get("per_rule"):
                done.add(r.get("key"))
    return done


def adjudicate_task(t, pages):
    """执行单个任务。unit 模式=条目×类别；doc 模式=整篇文档一次。"""
    if t["mode"] == "doc":
        rec = cmmatch.doc_adjudicate(pages, t["rules"])
    else:
        rec = cmmatch.adjudicate(t["unit"], t["rules"])
    for k in ("cat_id", "cat_name", "key"):
        rec[k] = t.get(k)
    return t, rec


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=CONFIG)
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--pdf", default=DEFAULT_PDF)
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--max-fail", type=int, default=10, help="连续失败熔断阈值")
    ap.add_argument("--cats", default="", help="限定类别id逗号分隔,留空=全部unit类别")
    ap.add_argument("--limit", type=int, default=0, help="条目上限(0=全部)")
    ap.add_argument("--dry-run", action="store_true", help="只枚举+生成prompt,不调LLM")
    ap.add_argument("--force-extract", action="store_true")
    a = ap.parse_args()

    os.makedirs(a.out, exist_ok=True)
    run_log = os.path.join(a.out, "run_log.txt")
    jsonl = os.path.join(a.out, "results.jsonl")
    log = []
    log.append(f"== cmmatch_run start {__import__('datetime').datetime.now().isoformat()} ==")

    cfg = load_config(a.config)
    by_no = pdf_lib.load_rules(KB)["by_no"]

    pages = pdf_lib.extract_pages_clean(
        a.pdf, os.path.join(ROOT, "audit", "work", "pages_clean.json"),
        force=a.force_extract)
    units = segment.segment_apis(pages)
    if a.limit:
        units = units[:a.limit]

    cats = {int(x) for x in a.cats.split(",") if x.strip()}
    tasks = build_tasks(units, cfg, by_no)
    if cats:
        tasks = [t for t in tasks if t["cat_id"] in cats]

    n_task = len(tasks)
    n_rules = sum(len(t["rules"]) for t in tasks)
    prompt0 = cmmatch.build_prompt(tasks[0]["unit"], tasks[0]["rules"]) if tasks else ""
    log.append(f"units={len(units)} tasks={n_task} rule-judgements={n_rules}")
    log.append(f"config categories: {[c['id'] for c in cfg['unit_categories']]} "
               f"+ doc {[c['id'] for c in cfg.get('doc_categories',[])]}")
    log.append(f"avg prompt(first) = {len(prompt0)}B")
    print(f"tasks={n_task} | rule-judgements={n_rules} | prompt(first)={len(prompt0)}B")
    print(f"task breakdown by cat:")
    from collections import Counter
    for k, v in Counter(t["cat_id"] for t in tasks).items():
        print(f"   cat{k}: {v}")

    if a.dry_run:
        # 校验：每类至少解析出规则、prompt 非空
        print("\n[dry-run] 规则解析校验:")
        bad = 0
        for c in cfg["unit_categories"] + cfg.get("doc_categories", []):
            rs = cmmatch.resolve_rules(by_no, c["rules"])
            ok = "OK" if rs else "!! 未解析到规则"
            if not rs:
                bad += 1
            print(f"   cat{c['id']} {c['name']}: {len(c['rules'])}前缀 -> {len(rs)}规则 {ok}")
        print(f"\n[dry-run] 总任务 {n_task}，若执行约 {n_task} 次 LLM 调用。")
        print(f"[dry-run] 校验 {'通过' if bad==0 else '存在未解析类别'}")
        open(run_log, "w", encoding="utf-8").write("\n".join(log))
        return

    # ---------- 真实运行 ----------
    done = load_done_keys(jsonl)
    todo = [t for t in tasks if task_key(t) not in done]
    log.append(f"resume: skip {len(tasks)-len(todo)} done / run {len(todo)}")
    print(f"resume: skip {len(tasks)-len(todo)} done, run {len(todo)}")

    lock = threading.Lock()
    consec_fail = 0
    abort = {"flag": False}
    records = []
    run_fail = 0

    def emit(t, rec):
        nonlocal consec_fail, run_fail
        rec = dict(rec)
        rec["key"] = task_key(t)
        with lock:
            with open(jsonl, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            records.append(rec)
            if not rec.get("ok"):
                run_fail += 1
            consec_fail = consec_fail + 1 if not rec.get("ok") else 0
            if consec_fail >= a.max_fail:
                abort["flag"] = True

    t_start = time.perf_counter()
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

    dur = time.perf_counter() - t_start
    log.append(f"run {len(todo)} task in {dur:.0f}s | ok={len(todo)-run_fail} fail={run_fail} "
               f"| abort={'是(熔断)' if abort['flag'] else '否'}")

    # ---------- 统计与产物 ----------
    stats = cmmatch.collect_stats(records)
    stats["wall_s"] = round(dur, 1)
    stats["task_probed"] = len(todo)
    md = cmmatch.render_md(records[-len(todo):], stats) if todo else "(本次无新增任务)"
    # 全量重读(results.jsonl 含历史), 保证 stats 覆盖累计
    allrec = [json.loads(l) for l in open(jsonl, encoding="utf-8") if l.strip()]
    allstats = cmmatch.collect_stats(allrec)
    allstats["wall_s"] = stats["wall_s"]

    # 补 PDF 印刷页码(供人工按原文核对)，best-effort
    _annotate_printed_pages(allrec, a.pdf)

    json.dump(allrec, open(os.path.join(a.out, "results.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    json.dump(allstats, open(os.path.join(a.out, "stats.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    open(os.path.join(a.out, "results.md"), "w", encoding="utf-8").write(
        cmmatch.render_md(allrec, allstats))
    open(run_log, "a", encoding="utf-8").write("\n" + "\n".join(log[-3:]) + "\n")
    cl = recall.call_log()
    with open(os.path.join(a.out, "llm_call_log.jsonl"), "w", encoding="utf-8") as f:
        for c in cl:
            f.write(json.dumps(c, ensure_ascii=False) + "\n")
    print(f"\n[结果] worst: 本段 ok={len(todo)-run_fail}/fail={run_fail}；累计去重确认违规 "
          f"{allstats['confirmed_grounded']} 组合/{len(allstats['dedup_confirmed_rules'])} 规则")
    print(f"[产物] {os.path.join(a.out)} (results.json/.jsonl/.md, stats.json, run_log.txt, llm_call_log.jsonl)")


if __name__ == "__main__":
    main()
