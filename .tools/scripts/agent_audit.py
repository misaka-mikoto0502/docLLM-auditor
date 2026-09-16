# AI生成
"""agent_audit.py — Agent-driven audit bridge (Agent 自主推理模式)。

将原本委托外部 vLLM 的 LLM 推理环节改为由**当前使用技能的模型**自主完成。
流水线拆分为三阶段：

  Phase 1 (build)   : PDF → pages_clean → segment → 构建所有 prompt → _agent_prompts.json
  Phase 2 (agent)   : Agent 读取 prompt，自主推理生成 JSON 响应 → _agent_responses.json
  Phase 3 (process) : 读取响应 → grounding → 统计 → 渲染结果

用法:
  # Phase 1: 构建所有 prompt（纯 Python，不调 LLM）
  python agent_audit.py build --pdf "<PDF路径>" --out audit/cm_agent [--cats 1,2,3] [--limit 0]
  python agent_audit.py build --product sample --out audit/cm_agent   # 从已有缓存构建

  # Phase 2: Agent 自主推理（由技能 SKILL.md 指导 Agent 执行，非命令行）
  #   → 读取 _agent_prompts.json
  #   → 逐条推理，生成 JSON 响应
  #   → 写入 _agent_responses.json

  # Phase 3: 处理响应 + 生成报告（纯 Python）
  python agent_audit.py process --out audit/cm_agent [--pdf "<PDF路径>"]
"""
import os, sys, json, time, argparse

SCRIPTS = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPTS)
sys.path.insert(0, os.path.join(SCRIPTS, "auditlib"))

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
KB = os.path.join(ROOT, "spec", "spec_rules.json")
CONFIG = os.path.join(ROOT, "spec", "rule_categories.json")
WORK = os.path.join(ROOT, "audit", "work")

from auditlib import pdf_lib, segment, cmmatch


# =====================================================================
# Phase 1: build — 构建所有 prompt
# =====================================================================
def product_pages_path(pid: str) -> str:
    if pid == "sample":
        return os.path.join(WORK, "pages_clean.json")
    return os.path.join(WORK, "m5", pid, "pages_clean.json")


def build_tasks(units, cfg, by_no):
    """枚举任务：每条目 × 每个 unit 类别(G1-G6) + doc 类别(G7)。"""
    tasks = []
    for c in cfg["unit_categories"]:
        rules = cmmatch.resolve_rules(by_no, c["rules"])
        for u in units:
            tasks.append({
                "mode": "unit", "cat_id": c["id"], "cat_name": c["name"],
                "rules": rules,
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
    return f"unit|{t['cat_id']}|{t['method']}|{t['path']}|{t.get('start_page')}"


def cmd_build(args):
    """Phase 1: 解析 PDF/缓存 → 分割条目 → 构建所有 prompt → 输出 _agent_prompts.json"""
    cfg = json.load(open(args.config, encoding="utf-8"))
    by_no = pdf_lib.load_rules(args.kb)["by_no"]

    # 获取 pages
    if args.pdf:
        cache = os.path.join(ROOT, "audit", "work", "pages_clean.json")
        pages = pdf_lib.extract_pages_clean(args.pdf, cache, force=args.force)
    elif args.product:
        pp = product_pages_path(args.product)
        if not os.path.exists(pp):
            print(f"[错误] 无该产品清洗缓存: {pp}")
            return 1
        pages = json.load(open(pp, encoding="utf-8"))
    else:
        print("[错误] 需指定 --pdf 或 --product")
        return 1

    units = segment.segment_apis(pages)
    if args.limit:
        units = units[:args.limit]

    tasks = build_tasks(units, cfg, by_no)
    if args.cats:
        cats = {int(x) for x in args.cats.split(",") if x.strip()}
        tasks = [t for t in tasks if t["cat_id"] in cats]

    # 为每个任务构建 prompt
    prompts = []
    for i, t in enumerate(tasks):
        if t["mode"] == "doc":
            prompt_text = cmmatch.build_doc_prompt(pages, t["rules"])
        else:
            prompt_text = cmmatch.build_prompt(t["unit"], t["rules"])

        rule_nos = [r.get("no", "") for r in t["rules"]]
        prompts.append({
            "idx": i,
            "key": task_key(t),
            "mode": t["mode"],
            "cat_id": t["cat_id"],
            "cat_name": t["cat_name"],
            "method": t["method"],
            "path": t["path"],
            "start_page": t.get("start_page"),
            "title": t.get("title", ""),
            "rule_nos": rule_nos,
            "prompt": prompt_text,
        })

    os.makedirs(args.out, exist_ok=True)
    out_file = os.path.join(args.out, "_agent_prompts.json")
    json.dump(prompts, open(out_file, "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)

    # 概况
    from collections import Counter
    cat_dist = Counter(p["cat_id"] for p in prompts)
    print(f"[build] 条目={len(units)} 任务={len(prompts)} "
          f"规则判定={sum(len(p['rule_nos']) for p in prompts)}")
    print(f"[build] 类别分布: {dict(sorted(cat_dist.items()))}")
    print(f"[build] prompt 平均长度: "
          f"{sum(len(p['prompt']) for p in prompts)//max(len(prompts),1)} 字符")
    print(f"[build] 产物: {out_file}")
    print(f"[build] 下一步: Agent 读取该文件，逐条推理生成响应 → _agent_responses.json")
    return 0


# =====================================================================
# Phase 3: process — 处理 Agent 响应 + 生成报告
# =====================================================================
def cmd_process(args):
    """Phase 3: 读取 Agent 响应 → grounding → 统计 → 渲染结果"""
    prompts_file = os.path.join(args.out, "_agent_prompts.json")
    responses_file = os.path.join(args.out, "_agent_responses.json")

    if not os.path.exists(prompts_file):
        print(f"[错误] 未找到 prompt 文件: {prompts_file}")
        return 1
    if not os.path.exists(responses_file):
        print(f"[错误] 未找到响应文件: {responses_file}")
        print("[提示] Agent 应先读取 _agent_prompts.json，逐条推理后写入 _agent_responses.json")
        return 1

    prompts = json.load(open(prompts_file, encoding="utf-8"))
    responses = json.load(open(responses_file, encoding="utf-8"))

    # 按 idx 建索引
    resp_by_idx = {r.get("idx"): r for r in responses}

    records = []
    n_ok, n_fail, n_skip = 0, 0, 0

    for p in prompts:
        idx = p["idx"]
        resp = resp_by_idx.get(idx)

        if resp is None:
            n_skip += 1
            records.append({
                "ok": False, "key": p["key"], "cat_id": p["cat_id"],
                "cat_name": p["cat_name"], "mode": p["mode"],
                "method": p["method"], "path": p["path"],
                "start_page": p["start_page"], "title": p["title"],
                "per_rule": [], "summary": "",
                "parse_err": "Agent 未生成响应",
            })
            continue

        raw = resp.get("response", "")
        obj = cmmatch.parse(raw)

        # 获取条目原文（用于 grounding）
        if p["mode"] == "doc":
            text = ""  # doc 级不做 grounding
        else:
            # 从 prompt 中无法直接拿到原文，需要从缓存重新加载
            text = resp.get("source_text", "")

        per = []
        for r in (obj.get("per_rule") or []):
            rno = (r.get("rule_no") or "").strip().upper()
            res = (r.get("result") or "").strip().lower()
            ev = (r.get("evidence") or "").strip()

            if p["mode"] != "doc" and text:
                g = cmmatch.grounded(ev, text)
            else:
                g = True  # doc 级或无原文时跳过 grounding

            final = res if (res != "confirmed" or g) else "uncertain"
            per.append({
                "rule_no": rno, "result": final,
                "confidence": r.get("confidence"),
                "grounded": bool(g),
                "evidence": ev[:2000],
                "reason": (r.get("reason") or "")[:2000],
            })

        rec = {
            "ok": bool(per), "key": p["key"],
            "cat_id": p["cat_id"], "cat_name": p["cat_name"],
            "mode": p["mode"],
            "method": p["method"], "path": p["path"],
            "start_page": p["start_page"], "title": p["title"],
            "per_rule": per, "summary": obj.get("summary", ""),
            "parse_err": "" if per else "per_rule 为空/格式异常",
            "prompt_chars": len(p["prompt"]),
            "dt": resp.get("dt", 0),
        }
        if per:
            n_ok += 1
        else:
            n_fail += 1
        records.append(rec)

    # 统计
    stats = cmmatch.collect_stats(records)
    stats["agent_mode"] = True
    stats["task_skip"] = n_skip

    # 补 PDF 印刷页码
    if args.pdf and os.path.exists(args.pdf):
        _annotate_printed_pages(records, args.pdf)

    # 写产物
    json.dump(records, open(os.path.join(args.out, "results.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    json.dump(stats, open(os.path.join(args.out, "stats.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    open(os.path.join(args.out, "results.md"), "w", encoding="utf-8").write(
        cmmatch.render_md(records, stats))

    # JSONL（每行一条记录）
    with open(os.path.join(args.out, "results.jsonl"), "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    print(f"[process] ok={n_ok} fail={n_fail} skip={n_skip} / 共 {len(prompts)} 任务")
    print(f"[process] confirmed&grounded 去重违规: {stats['confirmed_grounded']} "
          f"组合 / {len(stats['dedup_confirmed_rules'])} 规则")
    print(f"[产物] {args.out}")
    print(f"   results.json / results.jsonl / results.md / stats.json")
    return 0


def _annotate_printed_pages(records, pdf_path):
    """给记录补 PDF 印刷页码。"""
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


# =====================================================================
# Phase 2 辅助: 为 Agent 提供 source_text（grounding 所需的条目原文）
# =====================================================================
def cmd_extract_text(args):
    """辅助命令：从缓存中提取每个 unit 任务的条目原文，供 grounding 使用。

    Agent 在 Phase 2 生成响应时，需要将原文一并写入 _agent_responses.json
    的 source_text 字段。本命令把原文提取到 _agent_source_text.json，
    Agent 可按 idx 查找。
    """
    prompts_file = os.path.join(args.out, "_agent_prompts.json")
    if not os.path.exists(prompts_file):
        print(f"[错误] 未找到 prompt 文件: {prompts_file}")
        return 1

    prompts = json.load(open(prompts_file, encoding="utf-8"))

    # 需要重新加载 units 以获取原文
    if args.pdf:
        cache = os.path.join(ROOT, "audit", "work", "pages_clean.json")
        pages = pdf_lib.extract_pages_clean(args.pdf, cache, force=False)
    elif args.product:
        pp = product_pages_path(args.product)
        pages = json.load(open(pp, encoding="utf-8"))
    else:
        print("[错误] 需指定 --pdf 或 --product 以加载条目原文")
        return 1

    units = segment.segment_apis(pages)
    # 按 (method, path, start_page) 建索引
    unit_by_key = {}
    for u in units:
        k = (u.get("method", ""), u.get("path", ""), u.get("start_page"))
        unit_by_key[k] = u.get("text", "")

    source_texts = []
    for p in prompts:
        if p["mode"] != "unit":
            source_texts.append({"idx": p["idx"], "source_text": ""})
            continue
        k = (p["method"], p["path"], p["start_page"])
        source_texts.append({"idx": p["idx"], "source_text": unit_by_key.get(k, "")})

    out_file = os.path.join(args.out, "_agent_source_text.json")
    json.dump(source_texts, open(out_file, "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    print(f"[extract-text] {len(source_texts)} 条目原文 → {out_file}")
    return 0


# =====================================================================
# main
# =====================================================================
def main():
    ap = argparse.ArgumentParser(description="Agent-driven audit bridge")
    sub = ap.add_subparsers(dest="cmd")

    # build
    pb = sub.add_parser("build", help="Phase 1: 构建所有 prompt")
    pb.add_argument("--pdf", default="", help="PDF 路径")
    pb.add_argument("--product", default="", help="产品 ID（从缓存加载）")
    pb.add_argument("--out", default=os.path.join(ROOT, "audit", "cm_agent"))
    pb.add_argument("--config", default=CONFIG)
    pb.add_argument("--kb", default=KB)
    pb.add_argument("--cats", default="", help="限定类别 ID，逗号分隔")
    pb.add_argument("--limit", type=int, default=0, help="条目上限(0=全部)")
    pb.add_argument("--force", action="store_true", help="强制重新解析 PDF")

    # process
    pp = sub.add_parser("process", help="Phase 3: 处理 Agent 响应")
    pp.add_argument("--out", default=os.path.join(ROOT, "audit", "cm_agent"))
    pp.add_argument("--pdf", default="", help="PDF 路径（用于标注印刷页码）")

    # extract-text
    pe = sub.add_parser("extract-text", help="提取条目原文（grounding 辅助）")
    pe.add_argument("--out", default=os.path.join(ROOT, "audit", "cm_agent"))
    pe.add_argument("--pdf", default="", help="PDF 路径")
    pe.add_argument("--product", default="", help="产品 ID")

    a = ap.parse_args()
    if a.cmd == "build":
        return cmd_build(a)
    elif a.cmd == "process":
        return cmd_process(a)
    elif a.cmd == "extract-text":
        return cmd_extract_text(a)
    else:
        ap.print_help()
        return 0


if __name__ == "__main__":
    sys.exit(main())
