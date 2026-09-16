"""m5_parse_all.py — API 条目解析（从 PDF 起）：对 API 参考 PDF 解析(pages_clean) + 切分(segment)。

这是「API 条目解析」完整流程的**入口层**：
  ① PDF → pages_clean.json  （pdf_lib.extract_pages_clean：提取文本层 + 通用清洗）
  ② pages_clean.json → API 条目（auditlib/segment.segment_apis）
之后 cm_run_product / demo 直接吃 pages_clean 缓存即可。

依赖：pip install pymupdf（fitz，文本层提取，仅此一个第三方）；PDF 自备放 reference/API/。
本文件为 portable 版：ROOT 取本包根，PDF 放 <portable>/reference/API/<关键词>.pdf。

用法:
  python .tools/scripts/m5_parse_all.py            # 全部(已缓存则秒回)
  python .tools/scripts/m5_parse_all.py --pdfs svc-a  # 只跑指定(关键词匹配文件名)
  python .tools/scripts/m5_parse_all.py --pdfs svc-a --force  # 强制重解析(忽略缓存)

产物: <portable>/audit/work/m5/<svc>/{pages_clean.json, units.json} + m5_report.md
"""
import os, sys, json, argparse

sys.stdout.reconfigure(encoding="utf-8")
SCRIPTS = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPTS)
sys.path.insert(0, os.path.join(SCRIPTS, "auditlib"))

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
API_DIR = os.path.join(ROOT, "reference", "API")
OUT_DIR = os.path.join(ROOT, "audit", "work", "m5")

from auditlib import pdf_lib, segment

# 服务名 -> 文件名片段；新增文档在此追加。解析有缓存，重复跑只重切分。
TARGETS = {
    "svc-a": "svc-a",
    "svc-b": "svc-b",
    "svc-c": "svc-c",
    "svc-d": "svc-d",
    "svc-e": "svc-e",
    "svc-f": "svc-f",
    "svc-g": "svc-g",
    "svc-h": "svc-h",
    "svc-i": "svc-i",
    "sample": "sample",  # 示例产品 sample 放 audit/work/pages_clean.json（顶层），此映射可选
}


def find_pdf(keyword):
    if not os.path.isdir(API_DIR):
        return None
    for f in sorted(os.listdir(API_DIR)):
        if keyword in f and f.lower().endswith(".pdf"):
            return os.path.join(API_DIR, f)
    return None


def count_isolated_boolean(pages):
    import re
    n = 0
    lone = {"是", "否", "必选", "可选", "是可选", "否必选", "必选否", "可选是"}
    for p in pages:
        for ln in p.get("lines", []):
            s = ln.strip()
            if s in lone:
                n += 1
    return n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pdfs", default="", help="逗号分隔服务名关键词,留空=全部")
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()

    want = [x.strip() for x in a.pdfs.split(",") if x.strip()] or list(TARGETS)
    os.makedirs(OUT_DIR, exist_ok=True)
    report = []
    report.append("# M5 全量解析报告（API 参考 PDF）\n")
    report.append(f"> 生成 {__import__('datetime').datetime.now().isoformat()}\n")

    for svc in want:
        if svc not in TARGETS:
            print(f"[skip] 未知服务 {svc} (合法: {list(TARGETS)})")
            continue
        pdf = find_pdf(TARGETS[svc])
        if not pdf:
            report.append(f"\n## {svc}: **未找到 PDF**（请放 reference/API/）")
            print(f"[ERR] {svc}: 找不到 PDF（reference/API/{TARGETS[svc]}*）"); continue
        svc_out = os.path.join(OUT_DIR, svc)
        os.makedirs(svc_out, exist_ok=True)
        cache = os.path.join(svc_out, "pages_clean.json")
        try:
            pages = pdf_lib.extract_pages_clean(pdf, cache, force=a.force)
            units = segment.segment_apis(pages)
            no_title = sum(1 for u in units if not (u.get("title_cn") or u.get("title_en")))
            booleans = count_isolated_boolean(pages)
            n_url = sum(1 for u in units if u.get("path"))
            json.dump(units, open(os.path.join(svc_out, "units.json"), "w", encoding="utf-8"),
                      ensure_ascii=False, indent=1)
            line = (f"## {svc}\n"
                    f"- PDF: {os.path.basename(pdf)} | 页 {len(pages)}\n"
                    f"- 单元数: **{len(units)}** | 有URI: {n_url} | 无标题: {no_title}\n"
                    f"- 孤立是/否行: {booleans}\n"
                    f"- pages_clean: `audit/work/m5/{svc}/pages_clean.json` | units: `audit/work/m5/{svc}/units.json`\n")
            report.append(line)
            print(line, flush=True)
        except Exception as e:
            import traceback; traceback.print_exc()
            report.append(f"\n## {svc}: **解析失败** {e}")

    md = "\n".join(report) + "\n"
    open(os.path.join(OUT_DIR, "m5_report.md"), "w", encoding="utf-8").write(md)
    print("\n[report] audit/work/m5/m5_report.md")


if __name__ == "__main__":
    main()
