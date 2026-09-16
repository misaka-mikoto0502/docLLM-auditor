# -*- coding: utf-8 -*-
"""run_all_products.py — 全量跑所有产品单条目档召回（G1-G6 单元级 + G7 整篇 DOC）。

对每个有 pages_clean.json 缓存的产品：
  1) cm_run_product.py <pid> --out audit/cm_ALL/<pid> --workers N   → results.{json,jsonl,md},stats.json,run_log.txt
  2) gen_results_xlsx.py results.json -o results.xlsx               → 三 sheet Excel
带断点续跑/熔断（每个产品独立续跑：已完成的 jsonl 任务自动跳过）。

用法:
  python .tools/scripts/run_all_products.py [--workers 8] [--only sample,svc-a] [--out audit/cm_ALL]
"""
import os, sys, subprocess, argparse

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCRIPTS = os.path.join(ROOT, ".tools", "scripts")
WORK = os.path.join(ROOT, "audit", "work")

def products():
    out = ["sample"]
    m5 = os.path.join(WORK, "m5")
    if os.path.isdir(m5):
        out += sorted(d for d in os.listdir(m5)
                      if os.path.isfile(os.path.join(m5, d, "pages_clean.json")))
    return out

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--only", default="", help="逗号分隔产品 id，留空=全部")
    ap.add_argument("--out", default=os.path.join(ROOT, "audit", "cm_ALL"))
    a = ap.parse_args()

    plist = products()
    if a.only:
        keep = {x.strip() for x in a.only.split(",") if x.strip()}
        plist = [p for p in plist if p in keep]
    print(f"[run_all] 产品 {len(plist)} 个: {', '.join(plist)} | workers={a.workers} | out={a.out}")

    fail = []
    for pid in plist:
        out = os.path.join(a.out, pid)
        print(f"\n=====>> {pid} 开始 {__import__('datetime').datetime.now().isoformat()}")
        r1 = subprocess.run([sys.executable, os.path.join(SCRIPTS, "cm_run_product.py"),
                             pid, "--out", out, "--workers", str(a.workers)],
                            cwd=ROOT)
        if r1.returncode != 0:
            print(f"[run_all] {pid} cm_run_product 退出码 {r1.returncode}，跳过转 Excel"); fail.append(pid); continue
        r2 = subprocess.run([sys.executable, os.path.join(SCRIPTS, "gen_results_xlsx.py"),
                             os.path.join(out, "results.json"), "-o", os.path.join(out, "results.xlsx")],
                            cwd=ROOT)
        if r2.returncode != 0:
            print(f"[run_all] {pid} gen xlsx 退出码 {r2.returncode}"); fail.append(pid)
        print(f"=====>> {pid} 完成 {__import__('datetime').datetime.now().isoformat()}")

    print(f"\n[run_all] 结束 | fail={fail or '无'}")
    return 1 if fail else 0

if __name__ == "__main__":
    sys.exit(main())
