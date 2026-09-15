"""cmmatch.py — A 档「一条 API 条目 × 一个规则类别」完整匹配：核心逻辑(不含入口编排)。

设计(2026-08-13 定稿, 方案 A)：
  * 每条目 × 每个 A 档类别(该类全部规则全文) 单次调用 → 每规则 verdict。
  * 目标=错杀一千不放过一个：召回重；同一违规常被多条规则冗余覆盖(如 STC-010/040)。
  * grounding：confirmed 的 evidence 必须逐字落地条目原文(整段 → token 级回退)，否则降 uncertain。
  * 断点续跑由入口 cmmatch_run.py 的 results.jsonl 追加机制实现(本模块不带 IO 状态)。
"""
import re
import json
import os

_WS = re.compile(r"[\s　]+")

# 条目原文输入上限(字符)。超长条目按关键字窗口+首尾截断，防上下文爆破/拖慢。
# 默认 28000：本 PDF 仅 ~10 条超上限，牺牲极小完整性换单提示词上界(~38KB, ~9.5k token，
# 远低于阶段一实测 82KB，安全)。可 CMM_CAP 环境变量覆盖。
CONTEXT_CAP = int(os.environ.get("CMM_CAP", "28000"))


def _cap_text(text, rules, cap):
    """信息完整性优先的截断：保留【首】+【各规则关键字命中窗口】+【尾】。
    教训(2026-08-13)：不能只取开头——违规证据常在中部/尾部(状态码/响应字段)。
    被截部分不送模型→grounding 只对送去的内容生效，安全；代价=截断处的违规可能漏(可控)。"""
    if len(text) <= cap:
        return text
    low = text.lower()
    wins = []
    for rc in (rules or []):
        for kw in (rc.get("keywords") or []):
            k = str(kw).lower()
            if k and k in low:
                pos = low.find(k)
                wins.append((max(0, pos - 400), min(len(text), pos + len(k) + 400)))
    wins.sort()
    merged = []
    for a, b in wins:
        if merged and a <= merged[-1][1] + 200:
            merged[-1] = (merged[-1][0], max(merged[-1][1], b))
        else:
            merged.append((a, b))
    # 装配：首 + 各窗口 + 尾
    head_n = int(cap * 0.35)
    tail_n = int(cap * 0.15)
    parts = []
    pos = 0
    for a, b in merged:
        if a < head_n or b > len(text) - tail_n:
            continue                       # 与首/尾区重叠则并入首尾
        if a > pos:
            parts.append(text[pos:a])
            parts.append("\n…[截断]…\n")
        parts.append(text[a:b])
        pos = b
    if pos < len(text) - tail_n:
        parts.append("\n…[截断]…\n")
        parts.append(text[-tail_n:])
    out = text[:head_n] + "".join(parts)
    return out[:cap]


def norm(s):
    return _WS.sub("", s or "")


def grounded(ev, text):
    """evidence 是否落地于条目原文。先整段(剥空后)子串；失败再 token 级：
    逗号/顿号/分号/空格 分隔的每片(去空)都必须命中。
    教训(2026-08-13)：LLM 常给"字段名枚举"作证据(update_time,create_time,status)，
    整段子串必失败→把真违规降级。用 token 回退保住召回(错杀不漏)。"""
    e, t = norm(ev), norm(text)
    if not e:
        return False
    if e in t:
        return True
    toks = [norm(x) for x in re.split(r"[,，、;；。．.\s]+", ev) if len(norm(x)) >= 2]
    return bool(toks) and all(x in t for x in toks)


# =====================================================================
# 规则解析：前缀 -> 完整规则(按 spec_rules.json)
# =====================================================================
def resolve_rules(by_no, prefixes):
    """给定 {no: rule} 与 前缀列表，返回匹配的完整规则 list(按前缀输入顺序)。
    兼容多种后缀(如 ARG-010-01-2507-2509-M)。未匹配者跳过。"""
    out = []
    for p in prefixes:
        hit = next((r for no, r in by_no.items() if no.startswith(p)), None)
        if hit:
            out.append(hit)
    return out


# =====================================================================
# Prompt
# =====================================================================
def _rule_example(rc):
    """抽出规则示例区(blocks 中 role=example 标记及其后 content 块, 到下个非 content 标记止)。
    规范原文示例常写作: [Example] + 若干内容行；单独抽出便于在提示词中强调, 辅助模型理解判例。"""
    blocks = rc.get("blocks") or []
    out, taking = [], False
    for b in blocks:
        role = b.get("role", "")
        txt = (b.get("text") or "").strip()
        if not txt:
            continue
        if role == "example":
            taking = True
            if txt not in ("Example:", "Example", "举例", "示例"):
                out.append(txt)
            continue
        if role == "content" and taking:
            # 遇"(N)" 开头的下一条款(如 URI-020 的 "(5) CCE..."), 示例区结束
            if re.match(r"^\(\d+\)\s", txt):
                break
            out.append(txt)
            continue
        if taking:
            break  # 遇到 option/no 等新块, 示例区结束
    return " ".join(out).strip()


def _rule_cards(rules):
    """规则卡只送 规则号+正文原文+规范示例——不送中文要点/检查点(它们是转述, 与原文非100%一致,
    喂模型会叠误差)。
    数据不改，仅调整传给模型的参数。"""
    card = []
    for rc in rules:
        ex = _rule_example(rc)
        body = f"正文：{rc.get('text','')}"
        if ex:
            body += f"\n规范示例：{ex}"
        card.append(f"【规则 {rc['no']}】\n{body}")
    return card


# ---------------------------------------------------------------------
# 共享提示词骨架（unit 与 doc 两类复用）：任务 | 铁律 | 判型示例 | [每模式:输入+规则] | 输出契约
# 装配顺序固定=任务→铁律→示例→输入→规则→输出契约。关键指令放首尾、输入/规则带中间，
# 减小长文信息丢失；示例不绑定具体规则号(用 甲/乙/丙 占位)→任何类别复用不误导；
# 输出契约单一副本→消除两处重复与漂移。
# 2026-08-14 由用户审核 + 全量误判(反着判/脑补)驱动重构。
# ---------------------------------------------------------------------
TASK = """你是华为云 API 文档**完整匹配审计**。给定【一个输入】与【一个类别全部规则全文】，
逐一条判定【输入】在**每条规则**下是否违规（可在规则间并行，逐条给 verdict）。"""

IRON_RULES = """## 铁律（违反其一即算不合格）
1.【唯一判据=下方【规则】正文原文】判别只能依据下方每条规则的正文原文。
   **禁止**调用你记忆里的任何其它知识/其它服务实现/HTTP通用惯例/RFC/业界常识/主观印象。
   规则没写的检查点，一律不得引入。
2.【读数 ≠ 推断】输入里【明写的具体值】（状态码数字、HTTP动词、路径、参数名、字段名、描述原句），
   "把它摘出来、与规则明文对照"叫**读数**，是真证据，不是推断，不违反铁律1。
   规则禁止的只是"输入没写、你自造"。所以：别把"输入明说了 200"当成需要小心翼翼怀疑的事——它就是证据。
3.【证据逐字来自输入】凡判 confirmed：evidence 必须是"去掉多余空白后仍逐字出现在【输入】原文里"的原句；
   且 reason 必须抄出【规则要求Y】与【输入值X】两处真实原文。缺其一，就不是 confirmed（降级）。
4.【不漏报 > 不错杀】本审计取向=宁可多报可疑、不可放过真违规。
   只要输入里读出了与规则强制要求**相冲突**的相关值，**必须判 confirmed**；
   不得因"可能规则不适用/可能我理解偏"而滑向 dismissed/uncertain（那是漏报）。
   只有输入确实没给该值、或该条款不针对本输入时，才判 uncertain/dismissed。

## 判别步骤（对每条规则，严格按次序，六级每步都做）
  第1步·拆条款：把规则正文每条强制要求拆成条件式「当[触发场景]成立 → 必须[要求]」。
      只保留能落到本输入**具体值**的条款；流程/审批/跨版本类(本输入无法证伪)标 not_applicable。
  第2步·读数：在本输入里找对应条款的【具体值/明说】，抄下逐字原文 → 记作 V。找不到 → uncertain。
  第3步·对照（真值表，硬规定，不许含糊）：
        V 与规则要求一致        → dismissed（reason 写"输入X = 规则要求X"）
        V 与规则要求冲突        → confirmed（reason 写"输入X，规则要求Y，X≠Y，明文不符"）
        输入无该值              → uncertain（reason 写明"输入未涉及该值：…"）
        条款不适用于本输入(流程类) → dismissed（reason 指明是哪条不适用）
  第4步·自检：判 confirmed 前，(规则条文原文 + 输入值原文)必须都能逐字列出；缺一即降级。
  第5步·产出：该规则一条 JSON 记录；reason ≤2 句中文。"""

EXAMPLES = """## 判型示例（规则名用 甲/乙/丙 占位，规则正文引自 spec 规范原文）
# A=真实缺陷单 OTCPAAS-3030；B/C=对照构造用例，仅示判定边界。
【A·confirmed】来源=真实单 OTCPAAS-3030（APM 删除AK/SK误用POST）
输入：删除 AK/SK（V2）
POST /v2/systemmng/access-ak-sk/delete-ak-sk
规则甲（STC-010 类）：[Rule] The DELETE operation is used to delete resources. [Rule] The POST operation is applicable to create or non-CRUD scenarios.
正确产出：{"per_rule":[{"rule_no":"规则甲","result":"confirmed","evidence":"POST /v2/systemmng/access-ak-sk/delete-ak-sk","reason":"标题“删除 AK/SK”=删除语义（X），方法行却写 POST（Y），规则要求删除用 DELETE、POST 仅创建/非CRUD，X≠Y，明文冲突。"}],"summary":"违规。"}
要点：删除却 POST → confirmed。POST 是创建/非CRUD，DELETE 才删资源，动词用错即违规。

【B·dismissed】来源=对照构造（合规查询接口）
输入：查询工作空间详情
GET /v1/{project_id}/workspaces/{workspace_id}
| workspace_id | 是 | String | 工作空间ID |
| create_time | 否 | String | 创建时间 |
| update_time | 否 | String | 更新时间 |
状态码：200
规则乙（STC-010 类）：[Rule] The GET operation is used to obtain list of resources or single resource. [Rule] If the GET operation succeeds, 200 is returned.
正确产出：{"per_rule":[{"rule_no":"规则乙","result":"dismissed","evidence":"","reason":"查询用 GET（=要求X）、状态码 200（=要求X，条目“状态码：200”与规则“GET 成功返 200”对应），参数均 snake_case，一致，合规。"}],"summary":"合规。"}
要点：GET 查资源、状态码 200 均与规则相符（200 在条目与规则两处都有原文）→ 敢判 dismissed。

【C·uncertain】来源=对照构造（运行时规则）
输入：查询实例列表
GET /v1/{project_id}/instances
| page | 是 | Integer | 页码 |
| status | 否 | String | 状态 |
状态码：200
规则丙（SLA-010 类）：[Rule] The response time for an API call does not exceed 3s.
正确产出：{"per_rule":[{"rule_no":"规则丙","result":"uncertain","evidence":"","reason":"规则要求响应时间≤3s（运行时性能），条目仅有接口结构、无实际耗时数据，无法据条目判定。"}],"summary":"无法判定。"}
要点：C 是运行时规则——条目不记录真实响应时长，条目给全也判不出，故必然 uncertain（非“信息没给全”）。"""


def _output_contract(n, src):
    """输出契约（单一副本）；src=证据来源名（unit=条目全文, doc=文档摘要原文）。"""
    return f"""## 输出契约（严格按此 JSON，不要输出任何解释或前后缀）
{{"per_rule":[
  {{"rule_no":"<对应规则号>","result":"confirmed|dismissed|uncertain",
    "evidence":"<逐字来自【{src}】的字段/原句；判 confirmed 必须非空，其余可为空>",
    "reason":"<≤2 句中文>"}}
  ...共 {n} 条...
],"summary":"<一句话总述本输入有无违规>"}}
- confirmed=确实违反（须有逐字证据，且违反的是规则明文，不是猜测）；
- dismissed=符合/未违反；uncertain=输入未涉及该值/说不清。
- evidence 必须逐字取自【{src}】；找不到就降级，绝不编造。
- 每条 reason 必须给出【输入值X】对【规则要求Y】的对照（X=Y 合规 / X≠Y 违规 / 输入无X）；不给对照即不合格。"""


def build_prompt(unit, rules):
    """单元级：条目全文 × 类别规则。骨架=TASK→IRON→EXAMPLES→输入→规则→输出契约。"""
    card = _rule_cards(rules)
    api = f"{unit.get('method','')} {unit.get('path','')}".strip()
    src = "条目全文"
    return "\n\n".join([
        TASK,
        IRON_RULES,
        EXAMPLES,
        f"# 输入（{src}）\n要判定条目：{api}（第 {unit.get('start_page')} 页）\n"
        "(超长可能截断，含首/关键字命中窗口/尾)\n"
        + _cap_text(unit.get('text') or '', rules, CONTEXT_CAP),
        f"# 规则（本轮全部 {len(rules)} 条，逐条判定）\n" + "\n".join(card),
        _output_contract(len(rules), src),
    ])


def build_doc_prompt(pages, rules):
    """整篇文档级(DOC 类)提示词：复用共享骨架，输入=文档结构摘要(而非 2.4MB 全量)。"""
    import re as _re
    alltext = "\n".join(" ".join(p.get("lines", [])) for p in pages)
    markers = ["修订历史", "修订记录", "Revision", "@deprecated", "已废弃", "废弃",
               "工作空间管理", "删除", "创建", "请求参数", "响应参数", "状态码", "错误码",
               "文档版本", "发布日期", "变更说明", "迁移", "升级"]
    present = {m: (m in alltext) for m in markers}
    heads = [ln.strip() for p in pages[:40] for ln in p.get("lines", [])[:3]]
    heads = [h for h in heads if _re.match(r"^\d{1,2}\s+\S", h.strip())][:20]
    # 修订历史样例行
    hist = [ln.strip() for p in pages[:16] for ln in p.get("lines", [])
            if _re.search(r"版本|日期|修订|发布", ln)][:8]
    card = _rule_cards(rules)
    src = "文档摘要原文"
    return "\n\n".join([
        TASK,
        IRON_RULES,
        EXAMPLES,
        f"# 输入（{src}）——整篇文档的结构摘要\n文档总页数：{len(pages)}\n"
        "章节标题样例(前40页)：\n" + ("\n".join(heads) or "(无)") + "\n"
        "关键结构标记命中与否：\n"
        + "\n".join(f"- {k}: {'有' if v else '无'}" for k, v in present.items()) + "\n"
        "修订/版本相关行样例：\n" + ("\n".join(hist) or "(无)"),
        f"# 规则（整篇级，共 {len(rules)} 条，逐条判定）\n" + "\n".join(card),
        _output_contract(len(rules), src),
    ])


def doc_adjudicate(pages, rules):
    """整篇文档级(DOC 类)一次判定。返回与 adjudicate 同构的记录。"""
    from . import recall
    prompt = build_doc_prompt(pages, rules)
    import time as _t
    t0 = _t.perf_counter()
    raw = recall.call_small_model(prompt, want_json=True)
    dt = round(_t.perf_counter() - t0, 3)
    rec = {"method": "", "path": "", "start_page": None, "title": "", "mode": "doc",
           "prompt_chars": len(prompt), "dt": dt, "ok": raw is not None,
           "per_rule": [], "summary": "", "parse_err": ""}
    if not raw:
        return rec
    obj = parse(raw)
    for r in (obj.get("per_rule") or []):
        rno = (r.get("rule_no") or "").strip().upper()
        res = (r.get("result") or "").strip().lower()
        rec["per_rule"].append({
            "rule_no": rno, "result": res, "confidence": r.get("confidence"),
            "grounded": True, "evidence": (r.get("evidence") or "")[:2000],
            "reason": (r.get("reason") or "")[:2000],
        })
    rec["summary"] = obj.get("summary", "")
    if not rec["per_rule"]:
        rec["parse_err"] = "文档级 per_rule 为空"
    rec["ok"] = bool(raw) and bool(rec["per_rule"])
    _log_call(rec, raw)
    return rec


def parse(raw):
    try:
        m = re.search(r"\{.*\}", raw or "", re.S)
        return json.loads(m.group(0)) or {}
    except Exception:
        return {}


def _log_call(rec, raw):
    """把本次 LLM 调用追加进 recall._LLM_CALL_LOG(与阶段一一致,供 llm_call_log.jsonl)。"""
    from . import recall
    import time as _t
    recall._LLM_CALL_LOG.append({
        "t": _t.time(), "dt": rec.get("dt", 0.0),
        "cat": rec.get("cat_id"), "api": f"{rec.get('method')} {rec.get('path')}".strip(),
        "prompt_chars": rec.get("prompt_chars", 0), "n_rules": len(rec.get("per_rule", [])),
        "ok": bool(rec.get("ok")), "raw": (raw or "")[:4000],
    })


# =====================================================================
# 单任务判定：一个 (unit, category) 的完整过程
# =====================================================================
def adjudicate(unit, rules):
    """调用 LLM 判一个条目在一个类别下的违规。返回 dict(记录)。
    LLM 失败 / 解析失败 -> ok=False(供入口熔断判断 + 续跑重试)。"""
    from . import recall
    prompt = build_prompt(unit, rules)
    import time as _t
    t0 = _t.perf_counter()
    raw = recall.call_small_model(prompt, want_json=True)
    dt = round(_t.perf_counter() - t0, 3)
    rec = {
        "method": unit.get("method", ""), "path": unit.get("path", ""),
        "start_page": unit.get("start_page"), "title": unit.get("title_cn", ""),
        "chapter": unit.get("chapter_title", ""), "no": unit.get("no", ""),
        "api": f"{unit.get('method','')} {unit.get('path','')}".strip(),
        "prompt_chars": len(prompt), "dt": dt, "ok": raw is not None, "mode": "unit",
        "per_rule": [], "summary": "", "parse_err": "",
    }
    if not raw:
        _log_call(rec, None)
        return rec
    obj = parse(raw)
    text = unit.get("text") or ""
    per = []
    for r in (obj.get("per_rule") or []):
        rno = (r.get("rule_no") or "").strip().upper()
        res = (r.get("result") or "").strip().lower()
        ev = (r.get("evidence") or "").strip()
        g = grounded(ev, text)
        # 与原型一致：confirmed 必须 grounding，否则降 uncertain
        final = res if (res != "confirmed" or g) else "uncertain"
        per.append({
            "rule_no": rno, "result": final, "confidence": r.get("confidence"),
            "grounded": bool(g), "evidence": ev[:2000],
            "reason": (r.get("reason") or "")[:2000],
        })
    rec["per_rule"] = per
    rec["summary"] = obj.get("summary", "")
    if not per:
        rec["parse_err"] = "per_rule 为空/格式异常"
    rec["ok"] = bool(raw) and bool(per)
    _log_call(rec, raw)
    return rec


# =====================================================================
# 路2·整目录召回：一条目 × 整个规则目录(非单车类别)
# =====================================================================
def catalog_adjudicate(unit, rules):
    """路2·整目录召回：输入 = 一条 API 条目 + 整个规则目录(全部 A 规则一次调入)。

    与路1(adjudicate = 一条目×单类别)的唯一区别是规则集=整目录(跨类别全量)，
    提示词骨架与 adjudicate 完全共用 build_prompt(其他提示词不变)→同为 per_rule 判定。
    「双路召回」= 路1(逐类别, adjudicate) + 路2(整目录, 本函数) 各自独立运行、结果可
    对照/合并；单跑本函数只是其中一路。返回与 adjudicate 同构的记录，仅
    mode='catalog' 以资区分。"""
    rec = adjudicate(unit, rules)
    rec = dict(rec)
    rec["mode"] = "catalog"
    return rec


# =====================================================================
# 统计
# =====================================================================
def collect_stats(records):
    """records: list of task-records。产出按类别/按规则的 confirmed/dismissed/uncertain
    汇总 + grounding + 去重违规(DISTINCT (unit_key, rule_no) confirmed&grounded)。"""
    from collections import Counter
    per_cat = {}
    per_rule = Counter()
    rule_confirmed_dedup = {}          # rule_no -> set(unit_key)
    units_touched = set()
    confirmed_grounded = 0
    n_bad = 0
    n_ok = 0
    for rec in records:
        cid = rec.get("cat_id")
        if not rec.get("ok"):
            n_bad += 1
            per_cat.setdefault(str(cid), Counter())["call_fail"] += 1
            continue
        n_ok += 1
        c = per_cat.setdefault(str(cid), Counter())
        unit_key = f"{rec.get('method')} {rec.get('path')}"
        units_touched.add(unit_key)
        for r in rec.get("per_rule", []):
            rno = r.get("rule_no") or "?"
            per_rule[rno] += 1
            c[r.get("result")] += 1
            if r.get("result") == "confirmed" and r.get("grounded"):
                confirmed_grounded += 1
                rule_confirmed_dedup.setdefault(rno, set()).add(unit_key)
    # 去重违规计数(一条规-接口组合只算一次)
    dedup = {k: len(v) for k, v in rule_confirmed_dedup.items()}
    return {
        "task_ok": n_ok, "task_fail": n_bad,
        "units_touched": len(units_touched),
        "confirmed_grounded": confirmed_grounded,
        "per_category": {k: dict(v) for k, v in per_cat.items()},
        "per_rule": dict(per_rule),
        "dedup_confirmed_rules": dedup,
    }


def render_md(records, stats, cat_names=None):
    L = []
    A = lambda s="": L.append(s)
    A("# A 档完整匹配 · 运行结果")
    A()
    A(f"- 任务 ok={stats['task_ok']} / fail={stats['task_fail']} ｜ 触及条目 {stats['units_touched']} ｜ "
      f"confirmed&grounded 去重违规组合 {stats['confirmed_grounded']}({len(stats['dedup_confirmed_rules'])} 个规则)")
    A()
    A("## 去重确认违规(rule→接口数)")
    A()
    if not stats["dedup_confirmed_rules"]:
        A("（无）")
    for rno, n in sorted(stats["dedup_confirmed_rules"].items(), key=lambda x: -x[1]):
        A(f"- `{rno}` × {n}")
    A()
    A("## 逐条记录")
    A()
    for rec in records:
        if not rec.get("ok"):
            A(f"### ⚠️ 调用失败 {rec.get('method')} {rec.get('path')} (cat{rec.get('cat_id')})")
            A()
            continue
        con = [r for r in rec["per_rule"] if r["result"] == "confirmed"]
        if not con:
            continue
        A(f"### {rec.get('cat_id')}·{rec.get('method')} {rec.get('path')} ｜ {rec.get('title')} ｜ 第{rec.get('start_page')}页")
        for r in con:
            A(f"- **{r['rule_no']}** confirmed (grounded={r['grounded']})：{r['reason']}")
            if r["evidence"]:
                A(f"  - 证据：`{r['evidence']}`")
        A()
    return "\n".join(L)
