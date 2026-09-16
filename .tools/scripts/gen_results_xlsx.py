"""gen_results_xlsx.py — 把 cm_run_product / cmmatch_run 的 results.json 转成易筛选的 Excel。

版式（3 个 sheet）：
  * 判定明细  —— 每行 = 一条「接口 × 规则 × 结果」（per_rule 拍平），带自动筛选/冻结首行
  * confirmed违规 —— 只留 confirmed（&grounded）的行，人工定案用
  * 违规汇总 —— 按规则聚合：confirmed 去重接口数 + 接口列举(换行)

用法:
  python .tools/scripts/gen_results_xlsx.py audit/cm_sample/results.json -o audit/cm_sample/results.xlsx
"""
import os, sys, json, argparse
from collections import OrderedDict, Counter


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("in_json")
    ap.add_argument("-o", "--out", default="")
    a = ap.parse_args()

    recs = json.load(open(a.in_json, encoding="utf-8"))
    out = a.out or (os.path.splitext(a.in_json)[0] + ".xlsx")

    from openpyxl import Workbook
    from openpyxl.styles import Font, Alignment, PatternFill, Border, Side
    from openpyxl.utils import get_column_letter

    HEAD = ["类别", "类别名", "模式", "方法", "路径", "接口", "标题", "章节",
            "起始页", "印刷页", "规则号", "结果", "grounded", "置信度", "证据", "原因", "接口摘要"]
    WIDTHS = [6, 22, 6, 7, 34, 42, 20, 16, 8, 9, 22, 10, 9, 7, 34, 40, 22]

    def pre(no):
        return "-".join((no or "").split("-")[:2])

    def row_of(r, p):
        api = r.get("api") or f"{r.get('method','')} {r.get('path','')}".strip()
        # 摘要只写给「本轮确认违规」的那一行，且按【本规则 + 理由】定位到具体一条错误；
        # dismissed/uncertain 行留空，避免"结果=dismissed 却带'存在违规'摘要"的自相矛盾。
        summ = ""
        if p.get("result") == "confirmed":
            summ = f"{pre(p.get('rule_no',''))} 违规：{(p.get('reason') or '').strip()[:60]}"
        return [r.get("cat_id", ""), r.get("cat_name", ""), r.get("mode", ""),
                r.get("method", ""), r.get("path", ""), api, r.get("title", ""),
                r.get("chapter", ""), r.get("start_page", ""), r.get("printed_page", ""),
                p.get("rule_no", ""), p.get("result", ""), p.get("grounded", ""),
                p.get("confidence", ""), p.get("evidence", ""), p.get("reason", ""),
                summ]

    # ---------- 明细行 ----------
    detail = []
    confirmed = []
    for r in recs:
        for p in (r.get("per_rule") or []):
            row = row_of(r, p)
            detail.append(row)
            if p.get("result") == "confirmed":
                confirmed.append(row)

    # ---------- 违规汇总：按规则聚合 (去重 接口, 页) ----------
    agg = OrderedDict()          # rule -> {'apis':OrderedDict(api->set(pages)), 'cats':Counter}
    for r in recs:
        api = r.get("api") or f"{r.get('method','')} {r.get('path','')}".strip()
        pg = r.get("printed_page") or r.get("start_page") or ""
        for p in (r.get("per_rule") or []):
            if p.get("result") != "confirmed":
                continue
            key = p.get("rule_no", "?")
            a_ = agg.setdefault(key, {"apis": OrderedDict(), "cats": Counter()})
            a_["apis"].setdefault(api, set()).add(pg)
            a_["cats"][r.get("cat_id", "")] += 1
    summary = []
    for rno, a_ in sorted(agg.items(), key=lambda kv: -len(kv[1]["apis"])):
        apis = "；".join(f"{a}（p{','.join(str(x) for x in sorted(ps))}）"
                          for a, ps in a_["apis"].items())
        summary.append([rno, len(a_["apis"]), sum(a_["cats"].values()),
                        ", ".join(str(c) for c in a_["cats"]), apis])
    SUMMARY_HEAD = ["规则号", "confirmed 接口数(去重)", "confirmed 判定次数", "涉及类别", "接口列举(印刷页)"]

    # ---------- 样式 ----------
    wb = Workbook()
    thin = Side(style="thin", color="D9D9D9")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    hdr_fill = PatternFill("solid", fgColor="1F3864")
    hdr_font = Font(color="FFFFFF", bold=True)
    res_fill = {
        "confirmed": PatternFill("solid", fgColor="F8CBAD"),
        "dismissed": PatternFill("solid", fgColor="E2EFDA"),
        "uncertain": PatternFill("solid", fgColor="DDEBF7"),
    }
    wrap = Alignment(vertical="top", wrap_text=False)

    def fill_sheet(ws, head, rows, width, color_res=True, freeze="A2"):
        ws.append(head)
        for c in range(1, len(head) + 1):
            cell = ws.cell(row=1, column=c)
            cell.fill, cell.font, cell.border = hdr_fill, hdr_font, border
            cell.alignment = Alignment(vertical="center")
        for row in rows:
            ws.append(row)
        # 列宽 + 边框 + 结果着色
        for r_idx in range(2, ws.max_row + 1):
            for c_idx in range(1, len(head) + 1):
                cell = ws.cell(row=r_idx, column=c_idx)
                cell.border = border
                cell.alignment = wrap
                if color_res and c_idx == 12:
                    f = res_fill.get(cell.value)
                    if f:
                        cell.fill = f
        for i, w in enumerate(width, start=1):
            ws.column_dimensions[get_column_letter(i)].width = w
        ws.freeze_panes = freeze
        ws.auto_filter.ref = f"A1:{get_column_letter(len(head))}{ws.max_row}"

    ws1 = wb.active
    ws1.title = "判定明细"
    fill_sheet(ws1, HEAD, detail, WIDTHS)

    ws2 = wb.create_sheet("confirmed违规")
    fill_sheet(ws2, HEAD, confirmed, WIDTHS)

    ws3 = wb.create_sheet("违规汇总")
    fill_sheet(ws3, SUMMARY_HEAD, summary, [22, 18, 18, 12, 80], color_res=False)

    wb.save(out)
    print(f"[ok] {out}")
    print(f"  判定明细 {len(detail)} 行 / confirmed违规 {len(confirmed)} 行 / 违规汇总 {len(summary)} 规则")
    print(f"  top规则: " + "、".join(s[0] for s in summary[:12]))


if __name__ == "__main__":
    main()
