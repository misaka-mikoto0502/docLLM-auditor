# -*- coding: utf-8 -*-
"""gen_rule_doc.py — 生成「单条目规则整合分组」审阅文档。

标准（近期严格化复核）：单条目档 = 仅凭【一条 API 条目 + 该规则】即可直接判
（方法/路径/参数/头/状态码/响应字段），证据可锚定该条目原文 ⇒ 逐条审计。
据此剔出三类不构成"单条目直判"的规则：
  * DOC-010/020/030 —— 整篇(文档)级：单条目永远给不出证据（模板/修订历史/跨版本迁移）。
  * ASY-010 —— 需跨接口聚合：要求"另提供按标识查状态的接口"，单条目无法证伪 ⇒ 归聚合档。

单条目直判规则全量，压缩整合为若干均衡新类（每类 5-6 条）。
产物 spec/规则归类整合.md。

用法: python .tools/scripts/gen_rule_doc.py
"""
import os, sys, json, datetime
from collections import OrderedDict

SCRIPTS = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(SCRIPTS))
KB = os.path.join(ROOT, "spec", "spec_rules.json")
OUT = os.path.join(ROOT, "spec", "规则归类整合.md")

kb = json.load(open(KB, encoding="utf-8"))
rules = [r for r in kb["rules"] if r.get("option") in ("Mandatory", "Optional")
         and r.get("no")]
assert rules, "规则库为空"

# 单条目档(源)全量；其中 DOC×3 整篇级、ASY-010 跨接口聚合 → 不入单条目直判
A_FULL = ["URI-010","URI-020","URI-030","URI-040","URI-050","VER-010",
          "COD-010","COD-020","COD-030","COD-040",
          "STC-010","STC-020","STC-030","STC-040","ASY-010",
          "HDR-010","HDR-020","HDR-030","HDR-040","HDR-050",
          "ARG-010","ARG-040","ARG-050","ARG-060",
          "BAS-070","BAS-100","QUE-040","QUE-060","COM-010","TIM-010",
          "FIL-010","FIL-020","FIL-030","ERR-010",
          "DOC-010","DOC-020","DOC-030"]
assert len(set(A_FULL)) == len(A_FULL), "A_FULL 前缀须唯一"

DOC_RULES = ["DOC-010", "DOC-020", "DOC-030"]     # 整篇级，非单条目
ASY_TO_B = ["ASY-010"]                            # 跨接口聚合 -> 聚合档
A = [p for p in A_FULL if p not in DOC_RULES and p not in ASY_TO_B]

# 单条目直判规则 -> 若干个均衡新类（每类 5-6 条），不再含 DOC / ASY
GROUP = [
    ("请求 URI 与版本号", "G1",
     ["URI-010", "URI-020", "URI-030", "URI-040", "URI-050", "VER-010"]),
    ("HTTP 动词、状态码与错误码", "G2",
     ["STC-010", "STC-020", "STC-030", "STC-040", "ERR-010"]),
    ("传输协议、编码与安全", "G3", ["COD-010", "COD-020", "COD-030", "COD-040", "BAS-100"]),
    ("请求/响应消息头", "G4", ["HDR-010", "HDR-020", "HDR-030", "HDR-040", "HDR-050"]),
    ("参数命名、通用词表与易用性", "G5",
     ["ARG-010", "ARG-040", "ARG-050", "ARG-060", "COM-010", "BAS-070"]),
    ("查询契约：分页、过滤、排序、计数与时间", "G6",
     ["FIL-010", "FIL-020", "FIL-030", "QUE-040", "QUE-060", "TIM-010"]),
]
g2rules = {}
for name, gid, gset in GROUP:
    g2rules[gid] = set(gset)
flat = [r for gset in g2rules.values() for r in gset]
assert len(flat) == len(set(flat)), "规则前缀须无重叠"
assert set(flat) == set(A), set(flat) ^ set(A)
for gid, gset in g2rules.items():
    assert 3 <= len(gset) <= 7, f"{gid} 长度须在 3-7: {len(gset)}"

by_no = {r["no"]: r for r in rules}
by_base = {p: next(r for no, r in by_no.items() if no.startswith(p)) for p in A + DOC_RULES}
mo = lambda r: "M" if r["option"] == "Mandatory" else "O"

# 原生分类 -> 目标组 映射（展示压缩；DOC/ASY 两类移出）
native_map = []
for p in A:
    gid = next(gid for gid, gset in g2rules.items() if p in gset)
    native_map.append((by_base[p]["category"], gid))
nat2g = OrderedDict()
for c, g in native_map:
    nat2g.setdefault(c, {}).setdefault(g, 0)
    nat2g[c][g] += 1

L = []
L.append("# 单条目规则整合分组\n")
L.append("> 标准：**单条目档** = 仅凭【一条 API 条目 + 该规则】即可直接判（方法/路径/参数/头/"
         "状态码/响应字段），证据可锚定该条目原文。逐条审计。\n")
L.append("> 已剔出（近期复核）：**DOC-010/020/030**（整篇/文档级，单条目给不出证据）；"
         "**ASY-010**（需跨接口聚合『另提供查状态的接口』→ 聚合档）。全部 A 条压缩整合"
         "为若干均衡新类（每类 5–6 条）。\n")
L.append(f"> 生成 {datetime.datetime.now().isoformat()} ｜ 数据源 `spec/spec_rules.json` ｜ "
         f"单条目档规则分组整合（每类 "
         f"{min(len(s) for _, _, s in GROUP)}–{max(len(s) for _, _, s in GROUP)} 条）\n")
L.append("> **图例**：M=Mandatory(强制) · O=Optional(建议) ｜「来源分类」列 = 源文档原生分类\n")
L.append("## 判定轴\n")
L.append("单条目档：从一条 API 条目（方法/路径/参数/头/状态码/响应字段）**直接可判**，"
         "证据锚定该条目原文 ⇒ 逐条审计。DOC(整篇级)×3 与 ASY(跨接口→聚合档) 不在本集合内。\n")
L.append("## 整合映射（原生分类 → 新分组；DOC/ASY 两原生类移出）\n")
L.append("| 原生分类 | 并入新类 | 条数 |")
L.append("|----------|---------|------|")
for c, gd in nat2g.items():
    cells = "、".join(f"{g}({n})" for g, n in gd.items())
    L.append(f"| {c} | {cells} | {sum(gd.values())} |")
L.append("| ~~Asynchronous operations~~ | → 聚合档（ASY-010，需跨接口聚合） | 0 |")
L.append("| ~~API Reference Document Specifications~~ | → 整篇级（DOC-010/020/030） | 0 |")

for idx, (name, gid, gset) in enumerate(GROUP, 1):
    L.append(f"\n---\n\n## {idx}. {gid} · {name}（{len(gset)} 条）\n")
    L.append("| # | 规则号 | M/O | 来源分类 | 规则标题（EN） | 中文要点 |")
    L.append("|---|--------|----|----------|----------------|----------|")
    rs = sorted((by_base[p] for p in gset), key=lambda r: r.get("seq", 0))
    for n, r in enumerate(rs, 1):
        title = (r["title"] or "").replace("|", "\\|")
        zs = (r.get("zh_summary") or "").replace("|", "\\|")
        L.append(f"| {n} | `{r['no']}` | {mo(r)} | {r['category']} | {title} | {zs} |")

L.append("\n---\n\n## 附：档位划分依据\n")
L.append("- **DOC-010/020/030**：判定须整篇文档（统一模板/修订历史/跨版本迁移与 @deprecated），"
         "单条 API 条目无证据 → 整篇级单独处理，不入单条目档。")
L.append("- **ASY-010**：『异步接口须另提供按标识查状态/结果的接口』需跨接口聚合证伪 "
         "→ 归聚合档。")
L.append("- 其余各条：核心条款均可在**单条目内**读判（如 VER-010 的 URI 带 vX、STC-030 的"
         "批量动词/207/resources 数组、BAS-070 的入参易获取）；跨接口/流程次条款留 "
         "inapplicable/uncertain，不降级。")
L.append("- 新分组内规则**全部不同、无重叠**；每类落在 5-6 条均衡区间，DOC/ASY 已不占位。")

out = "\n".join(L) + "\n"
os.makedirs(os.path.dirname(OUT), exist_ok=True)
open(OUT, "w", encoding="utf-8").write(out)
print("written:", OUT, "|", len(out), "chars")
print("各新类条数:", {gid: len(gset) for _, gid, gset in GROUP})
