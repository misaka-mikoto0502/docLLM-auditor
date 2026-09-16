"""detect.py — 规则召回 + 检查点判定层。

* 对单个 API 单元召回候选规则，并用“客观探测器”判定是否违规。
* 每个探测器:  doc-level(只读文档即可发现)  —— 与历史问题单里的“文档直接对比可发现=是”对齐。
* 产出 finding(dict)，字段与历史问题单同构：
    service, no, title, method, path, rule_no,
    category(问题分类), priority(Blocker/High/Medium/Low),
    check(命中的检查点), evidence(原文证据), desc(中文问题描述),
    concern(本质/实际/期望), confidence(high/medium)

设计目标：探测器彼此独立、可热插拔（在 DETECTORS 列表增删即可），故易复用/泛化。
"""
import re

# 服务名：可由 audit_run 通过 set_service() 覆盖(泛化到其它云服务文档)
SERVICE = "sample"
def set_service(name: str) -> None:
    global SERVICE
    SERVICE = name

# ---------------- finding 构造 ----------------
def F(unit, rule_no, category, priority, check, evidence, desc, concern, conf="high"):
    no = str(unit.get("chapter", "") + "." + str(unit.get("no", "?")))
    title = f"{unit.get('title_cn','')} - {unit.get('api_en','')}".strip(" -") or unit.get("path", "")
    return {
        "service": SERVICE,
        "no": no,
        "title": title,
        "chapter": unit.get("chapter_title", ""),
        "method": unit.get("method", ""),
        "path": unit.get("path", ""),
        "api": f"{unit.get('method','')} {unit.get('path','')}".strip(),
        "start_page": unit.get("start_page"),
        "rule_no": rule_no,
        "category": category,
        "priority": priority,
        "check": check,
        "evidence": evidence,
        "desc": desc,
        "concern": concern,
        "confidence": conf,
    }

# ---------------- path 工具 ----------------
def path_segments(path):
    """返回非空段列表；{placeholders} 段保持原样。剔除查询串(供 URI 类探测用)，
    避免示例查询串把资源段污染(教训：unit path 本身干净，但仍防御)。"""
    p = (path or "").split("?", 1)[0].rstrip("?")
    return [s for s in p.split("/") if s]

def _is_placeholder(seg):
    return seg.startswith("{") and seg.endswith("}")

def _resource_segs(path):
    return [s for s in path_segments(path) if not _is_placeholder(s)
            and not re.fullmatch(r"v\d+", s)]

# ================= 探测器 =================

def d_uri_style(unit):
    """URI-020: 资源名须全小写、连字符分隔(禁下划线)。"""
    fs = []
    for seg in _resource_segs(unit.get("path", "")):
        if re.search(r"[A-Z]", seg):
            fs.append(F(unit, "URI-020-01-2507-2507-M", "接口设计不合规", "High",
                        "URI 资源名全小写(禁大写)",
                        seg, f"URI 资源段 `{seg}` 含大写字母",
                        "问题本质：URI 资源名含大写字母，违反 URI 全小写规范。实际：路径中 `{seg}` 非全小写。期望：资源名全小写。", "high"))
            break
    for seg in _resource_segs(unit.get("path", "")):
        if "_" in seg:
            fs.append(F(unit, "URI-020-01-2507-2507-M", "接口设计不合规", "High",
                        "URI 资源名用连字符分隔(禁下划线)",
                        seg, f"URI 资源段 `{seg}` 含下划线",
                        "问题本质：URI 资源名使用下划线而非连字符。实际：`{seg}` 中含 `_`。期望：资源名以连字符(-)分隔。", "high"))
            break
    return fs


def d_uri_version(unit):
    """VER-010: URI 须带 v<N> 版本号。"""
    path = unit.get("path", "")
    if path and not re.search(r"/v\d+(/|$)", path):
        return [F(unit, "VER-010-01-2507-2507-M", "接口设计不合规", "High",
                  "URI 带 vN 版本号", unit.get("path"),
                  f"URI 缺少版本号",
                  "问题本质：URI 未包含 vN 版本号。实际：路径中无 `/v<N>/`。期望：URI 带整数版本号 vN。", "high")]
    return []


def d_pagination(unit):
    """FIL-020: 资源列表接口必须支持分页(param 含 limit/marker/offset 之一)。"""
    method = unit.get("method", "").upper()
    title = unit.get("title_cn", "") or ""
    en = unit.get("api_en", "") or ""
    path = unit.get("path", "")
    if method != "GET":
        return []
    # 单资源查询：路径末段为 {占位}（如 .../apps/{app_id}）→ 非列表，跳过
    last_raw = path.rstrip("/").split("/")[-1] if path else ""
    if "{" in last_raw:
        return []
    # 判定“列表接口”：中文标题含“列表/查询所有/批量查询”，或英文名含 List(如 ListWorkspace)
    is_list = ("列表" in title) or ("查询所有" in title) or ("批量查询" in title) \
              or bool(re.search(r"\bList\w*\b", en))
    if not is_list:
        return []
    text = unit.get("text", "")
    if re.search(r"\b(limit|marker|offset)\b", text):
        return []
    return [F(unit, "FIL-020-01-2507-2507-M", "接口设计不合规", "High",
              "列表接口必须支持分页(limit/marker 或 offset/limit)",
              unit.get("path"),
              "列表接口未见 limit/marker/offset 分页参数",
              "问题本质：资源列表接口未提供分页参数。实际：`{}` 为列表接口，但请求参数/路径中未见 limit、marker、offset。期望：按规范提供 marker/limit 或 offset/limit 分页，并用 page_info 返回指针。".format(unit.get("path")),
              # 置信度 medium：规则“必须分页”是明文，但“元(未)含分页参数”靠否定检测，
              # 分页可能声明在公共参数区而在单单元外 → 交给阶段二复核，不直接判定
              "medium")]


def d_count(unit):
    """QUE-040: 计数字段用 count，禁止 total/total_count。"""
    text = unit.get("text", "")
    bad = []
    for ln in (text or "").splitlines():
        s = ln.strip()
        if s in ("total", "total_count", "total_number", "totalCount", "totalcount"):
            bad.append(s)
    if bad:
        names = "、".join(sorted(set(bad)))
        return [F(unit, "QUE-040-01-2507-2507-M", "接口设计不合规", "High",
                  "列表响应计数用 count(禁 total/total_count)",
                  names, f"响应/参数中出现计数名 `{names}`",
                  "问题本质：列表响应计数字段命名不符合规范。实际：使用 `{}`，规范要求用 `count`。期望：统一用 `count`，禁用 `total`/`total_count`。".format(names), "high")]
    return []


def d_time_naming(unit):
    """TIM-010/COM-010: 时间参数命名用 created_at/updated_at(禁 create_time/update_time)。"""
    text = unit.get("text", "")
    bad = []
    for ln in (text or "").splitlines():
        s = ln.strip()
        if s in ("create_time", "update_time", "delete_time", "createdTime", "updatedTime"):
            bad.append(s)
    if bad:
        names = "、".join(sorted(set(bad)))
        return [F(unit, "TIM-010-01-2507-2507-M", "接口设计不合规", "Medium",
                  "时间参数命名遵循通用参数表(created_at/updated_at)",
                  names, f"时间参数名为 `{names}`",
                  "问题本质：时间参数命名不符合通用参数表。实际：使用 `{}`，规范通用参数表用 `created_at`/`updated_at`(RFC3339 UTC)。期望：改用 *created_at*/*updated_at* 命名。".format(names), "medium")]
    return []


def _query_names(unit, cap=8):
    """收集 query 参数名：① URI path 内的 query(unit path 干净,几乎为空)；
    ② 正文**示例请求 URL** 的 query(?k=v&...)。
    返回驼峰风格(非全大写)的参数名列表，去重。cap 防单表爆刷。"""
    seen = []
    pool = [unit.get("path") or ""]
    pool += re.findall(r"\?([A-Za-z0-9_\-=&%.]+)", unit.get("text") or "")
    for chunk in pool:
        q = chunk.split("?", 1)[-1]
        for kv in q.split("&"):
            name = kv.split("=", 1)[0].strip()
            if not name:
                continue
            if re.search(r"[A-Z]", name[1:]) and not re.search(r"[A-Z]{2,}", name):
                if name not in seen:
                    seen.append(name)
            if len(seen) >= cap:
                return seen
    return seen


def d_query_camel(unit):
    """ARG-010: query 参数名 snake_case(禁 camelCase)。
    来源一：URI path 内 query；来源二：正文**示例请求 URL** 的 query —— 主查此处，
    否则 ARG-010 对 query 名形同虚设(unit path 干净、几乎无 query，见教训)。
    (注: body 字段名 camelCase 亦可审，但扁平文本提取会因表格换行产生大量断行碎片假阳性，
     暂不在自动探测中开启，见 README 已知局限。)"""
    params = _query_names(unit)
    if params:
        names = "、".join(sorted(set(params)))
        return [F(unit, "ARG-010-01-2507-2507-M", "接口设计不合规", "High",
                  "query 参数名 snake_case(禁 camelCase)",
                  names, f"query 参数名采用驼峰式 `{names}`",
                  "问题本质：query 参数名使用 camelCase，违反 snake_case 约定。实际：URI/示例请求 query 中 `{}` 非下划线小写。期望：统一 snake_case(如 `server_id`)。".format(names), "high")]
    return []


def d_auth_header(unit):
    """HDR-010/文档：请求参数若含认证需求应带 X-Auth-Token 说明。
    仅当该 API 有明显“请求参数/请求Header”且整体未出现 X-Auth-Token 时，作候选提示。"""
    text = unit.get("text", "")
    if "请求" not in text:
        return []
    if "X-Auth-Token" in text or "Authorization" in text:
        return []
    # 避免大段介绍页误报：必须有请求头/请求参数表信号
    if not re.search(r"请求Header|请求参数|请求Body", text):
        return []
    return [F(unit, "HDR-010-01-2507-2507-M", "文档错误", "Medium",
              "请求头应含认证头(X-Auth-Token 等)说明",
              unit.get("path"), "未见 X-Auth-Token/Authorization 认证头",
              "问题本质：该接口请求参数区未标注认证头(可能为候选：若属 APP 认证等例外可忽略)。实际：文本中未出现 X-Auth-Token/Authorization。期望：请求头中明确标注认证方式及 Token 头。",
              # 置信度 low：认证方式可能统一声明在文档级而非每接口；缺席是弱信号 → 放过
              "medium", "low")]


def d_plural(unit):
    """URI-040: 单资源操作路径应带 {resource_id}；集合用复数(启发式，候选)。"""
    path = unit.get("path", "")
    method = unit.get("method", "").upper()
    res = _resource_segs(path)
    if not res:
        return []
    last = res[-1]
    # 修改/删除/查详情若最后一段不是占位 {id}，则资源标识疑似未进路径
    if method in ("PUT", "DELETE", "PATCH") and not ("{" in path):
        return [F(unit, "URI-040-01-2507-2507-M", "接口设计不合规", "Medium",
                  "指定单资源的操作路径带 {resource_id}",
                  last, f"`{method}` 操作路径末段无资源 ID 占位",
                  f"问题本质：对单资源的 {method} 操作，URI 末段未带 {{resource_id}}。实际：`{path}` 末段为 `{last}`。期望：追加 `{{resource_id}}` 定位资源。",
                  # 置信度 low：REST 资源标识是否必须进路径属设计判断，且无 {id} 也可能是集合操作 → 放过
                  "low")]
    return []


def d_create_status(unit):
    """STC-010/STC-040: 创建类 POST 成功状态码应 201；若文档标注 200 且无 201 → 违规。"""
    method = unit.get("method", "").upper()
    if method != "POST":
        return []
    title = unit.get("title_cn", "") or ""
    if not re.search(r"创建|新建|注册|开通", title):
        return []
    text = unit.get("text", "")
    has_200 = re.search(r"状态码\s*[:：]?\s*200", text)
    has_201 = re.search(r"状态码\s*[:：]?\s*201", text)
    if has_200 and not has_201:
        return [F(unit, "STC-010-01-2507-2507-M", "接口设计不合规", "Medium",
                  "创建类 POST 成功状态码用 201",
                  unit.get("path"), "创建接口响应状态码标注为 200(应为 201)",
                  "问题本质：创建类 POST 接口成功状态码应按规范用 201。实际：文档响应标注 200 且未标 201。期望：创建成功返回 201 Created；如需无内容成功可 204，应避免成功掩用 200。", "medium")]
    return []


# ---------- 文档级审计(整篇一次性) ----------
def doc_level_findings(pages: list[dict] | None, service=None) -> list:
    """对整篇文档跑一次的可对比判定(修订历史、废弃标记等)。返回 doc 级 finding。"""
    if not pages:
        return []
    service = service or SERVICE
    front = "\n".join(" ".join(p["lines"][:80]) for p in pages[:16])
    alltext = "\n".join(" ".join(p["lines"]) for p in pages)
    out = []
    # DOC-030 修订历史表：需有多行“版本号 + 日期 + 变更说明”
    rows = re.findall(r"(?:文档版本|版本)[ ：]?\s*(?:V?\d[\d.]*|0?\d)\s*(?:发布日期|日期)[ ：]?[\d\-/]+", front)
    multi_row = len([r for r in rows if r]) >= 2
    if not multi_row and not re.search(r"修订历史|版本记录|修订记录|Revision", front):
        out.append({"doc_level": True, "service": service, "no": "-", "title": "参考文档修订历史缺失 - DOC-030",
                    "chapter": "文档规范", "method": "", "path": "-", "api": "-", "start_page": None,
                    "rule_no": "DOC-030-01-2507-2507-M", "category": "文档错误", "priority": "Medium",
                    "check": "API 参考文档每次变更须记录到修订历史表(日期/版本/说明/作者)",
                    "evidence": "仅见文档版本/发布日期单行，未见修订历史表", "confidence": "medium",
                    "desc": "问题本质：API 参考文档未提供修订历史表。实际：文档只给出单个“文档版本/发布日期”，无多行(日期/版本/变更说明)修订记录。期望：按 DOC-030 维护修订历史表，变更说明精确到参数级。"})
    # DOC-020 废弃标记：存在“历史API”章节时应标注已废弃/@deprecated
    if re.search(r"历史API|历史接口|已废弃|deprecated", alltext, re.I):
        if not re.search(r"@deprecated|已废弃|Deprecated|废弃", alltext):
            out.append({"doc_level": True, "service": service, "no": "-",
                        "title": "历史 API 章节缺少废弃标注 - DOC-020", "chapter": "文档规范",
                        "method": "", "path": "-", "api": "-", "start_page": None,
                        "rule_no": "DOC-020-01-2507-2507-M", "category": "文档错误", "priority": "Medium",
                        "check": "不支持的接口须提前公告并在文档标注 @deprecated",
                        "evidence": "存在历史API章节但未见 @deprecated/已废弃 标注", "confidence": "medium",
                        "desc": "问题本质：历史/废弃 API 应在文档标注 @deprecated。实际：文档含历史 API 内容但未见 @deprecated 标注。期望：对不再支持的接口标注 @deprecated 并给出替换方案。"})
    return out


DETECTORS = [d_uri_style, d_uri_version, d_pagination, d_count,
             d_time_naming, d_query_camel, d_auth_header, d_plural, d_create_status]

# 被本套自动探测器覆盖(绑定过的)规则号前缀 —— 用于报告覆盖矩阵。
# 收敛原则：仅保留 **阶段一直接判为判定(高置信·明文·说一不二)** 的规则前缀，
# 以及文档级(DOC-*)明文核对项。TIM-010/FIL-020/STC-010/HDR-010/URI-040 等
# 属于“模式候选(中/低置信)”，不在此列 → 让 LLM 语义层召回、交由阶段二复核。
AUDITED_RULE_IDS = {
    "URI-020",                       # d_uri_style  明文：全小写/连字符(禁下划线)
    "VER-010",                       # d_uri_version 明文：URI 带 vN 版本
    "QUE-040",                       # d_count      明文：计数统一 count(禁 total/total_count)
    "ARG-010",                       # d_query_camel 明文：query 参数 snake_case
    "DOC-020", "DOC-030",            # 文档级明文核对
}


def audit_coverage(rules: list[dict]) -> dict:
    """按大节(category)汇总规则总数 / 已自动审计 / 待人工，支撑覆盖矩阵。"""
    from collections import Counter
    total = Counter(r.get("category", "") for r in rules if r.get("no"))
    audited = Counter()
    for r in rules:
        no = r.get("no") or ""
        if any(no.startswith(p) for p in AUDITED_RULE_IDS):
            audited[r.get("category", "")] += 1
    return {"total": dict(total), "audited": dict(audited)}


def detect_unit(unit, kb) -> list:
    fs = []
    for fn in DETECTORS:
        try:
            fs += fn(unit)
        except Exception:
            continue
    return fs


def run_all(units, kb, pages=None) -> list:
    findings, seen = [], set()
    for u in units:
        for f in detect_unit(u, kb):
            key = (f["path"], f["rule_no"], f.get("evidence"))
            if key in seen:
                continue
            seen.add(key)
            findings.append(f)
    findings += [f for f in doc_level_findings(pages) if
                 (f.get("path"), f.get("rule_no")) not in seen]
    return findings
