"""recall.py — 阶段一(初筛/召回)修正版：规则分级 + LLM 语义召回 + 接口级结论。

设计原则（与用户确认，修正了“一棍子全打死”与“规则当门禁”两处）：
  * 规则层（确定性探测器 detect.py）：**精度优先，只判“肯定错”的客观项**（camelCase、
    count/total、状态码 200、列表缺分页、URI 风格…），判了就一定是错；不负责证明合规。
  * LLM 层：**召回优先**——负责规则写不死的**语义类规则**（BAS 幂等/原子性/行为、SLA、
    PUB 流程、ERR 错误码语义、ASY、INT…），对**全部接口**逐条判定，且**允许判“干净”**。
  * 客观项**从 LLM 候选集排除**（LLM 不重复判定状态码等规则可判项）——省算力、不重复。
  * 输出：每接口一个结论（通过/可疑），可疑项给判据来源(规则/LLM)/检查点/证据/页码，可回溯原文。
"""
import os, re, json
import urllib.request, urllib.error
from concurrent.futures import ThreadPoolExecutor
from . import detect

SERVICE = "sample"
def set_service(name: str) -> None:
    global SERVICE
    SERVICE = name

# =====================================================================
# LLM 配置（本地 vLLM，OpenAI 兼容；密钥走环境变量 LLM_KEY）
# =====================================================================
LLM_ENABLED   = True
LLM_MODEL     = os.environ.get("LLM_MODEL", "local-model")
LLM_BASE_URL  = os.environ.get("LLM_BASE_URL", "http://127.0.0.1:8900/v1")
LLM_KEY       = os.environ.get("LLM_KEY", "vllm-token")

BATCH_SIZE     = int(os.environ.get("LLM_BATCH", "16"))
INTER_BATCH_GAP = float(os.environ.get("LLM_GAP", "2"))
MAX_FAIL_BATCHES = int(os.environ.get("LLM_MAX_FAIL", "2"))
MAX_RULES_UNIT = 8
# 截断保险：默认 None=不截断（本批 API 平均 ~280 字符，完整原文完全放得下，信息零损失）。
# 仅当显式设置 LLM_CAP 为非空数字时才截取。
API_TEXT_CAP   = os.environ.get("LLM_CAP") or None

_LLM_ABORT = {"flag": False, "fails": 0}
_RAW_SAMPLES = []
def raw_samples():
    return list(_RAW_SAMPLES)

# —— 完整调用日志（供后续优化复盘：每批模型的原始输出、耗时、prompt 长度、解析结果）——
_LLM_CALL_LOG = []
def call_log():
    return list(_LLM_CALL_LOG)
def reset_call_log():
    _LLM_CALL_LOG.clear()


# =====================================================================
# 规则分级
# =====================================================================
def semantic_rules(kb):
    """需 LLM 语义判定的规则 = 全部规则 − 客观/规则可判（在 detect.AUDITED_RULE_IDS 内）的项。

    这些客观规则由规则引擎判定；LLM 不重复判它们 → 状态码/分页/计数等不再出现在 LLM 候选。
    """
    audited = detect.AUDITED_RULE_IDS
    out = []
    for r in (kb.get("list") or []):
        no = r.get("no") or ""
        if any(no.startswith(p) for p in audited):
            continue
        out.append(r)
    return out


# =====================================================================
# LLM 层(语义判定，可判干净；分批 + 严格 JSON)
# =====================================================================
def build_semantic_prompt(batch, kb, sem) -> str:
    lines = []
    for r in sem:
        no = r.get("no")
        if not no:
            continue
        zh = (r.get("zh_summary") or r.get("要点") or r.get("summary") or "").strip()
        if zh:
            lines.append("- %s: %s" % (no, zh))
    rules_block = "\n".join(lines) or "(无语义规则)"
    items = "\n".join(
        "[%d] %s %s (page %s): %s" % (
            i, u.get("method", ""), u.get("path", ""), u.get("start_page", ""),
            ((u.get("text", "")) or "").replace("\n", " ")[:API_TEXT_CAP] if API_TEXT_CAP else (u.get("text", "") or ""))
        for i, u in enumerate(batch))
    return (
        "你是 API 文档合规审计的【粗略规则召回器】——只负责缩小候选范围，不做任何判定。\n"
        "格式/客观类规则(URI 风格、参数命名、状态码、分页、计数等)已由规则引擎单独判定，"
        "**你无需也不应选这些**。\n"
        "下面给出【语义规则目录】(规则号 + 一句话要点)，和 [%d] 条 API。对每一条 API，"
        "**只从目录中挑出它最可能被违反的 top-k 条语义规则**(幂等/原子性/异步/可访问性/"
        "流控/发布流程/错误语义等)。\n"
        "要求：\n"
        "- 只管召回与当前 API 相关的规则，**不做裁判**：不要给理由，不要下『违反/合规』结论，"
        "不要输出证据。判定的唯一权威是第二阶段。\n"
        "- 每条 API 最多 %d 条(按疑似程度从高到低)。\n"
        "- 若某 API 在语义层面**没有可怀疑的规则**，该条的 spef_rules 就输出空 []。\n"
        "- 只输出规则号(no)，一律大写。只输出 JSON，不要任何其它文字。\n"
        'JSON 结构(编号[i] 必须与下方 API 一一对应)：'
        '{"results":[{"idx":0,"spef_rules":["<规则号>", ...]}]}\n\n'
        "语义规则目录(规则号+一句话要点)：\n%s\n\n"
        "待召回的 API：\n%s\n\n"
        "请只输出上述 JSON。" % (len(batch), MAX_RULES_UNIT, rules_block, items)
    )


# Windows 会从注册表自动读系统代理（企业内网代理），urllib 默认走它；
# 该代理对局域网地址解析不稳定。LLM 是局域网 vLLM，须直连，禁用代理。
_NO_PROXY_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def call_small_model(prompt: str, want_json: bool = True):
    """调用 LLM（OpenAI 兼容，urllib 直连零依赖）。返回文本；失败返回 None。429 退避重试。"""
    if not LLM_ENABLED:
        return None
    import time
    url = (LLM_BASE_URL or "http://localhost:11434/v1").rstrip("/") + "/chat/completions"
    key = LLM_KEY or os.environ.get("LLM_KEY", "")
    for attempt in range(3):
        payload = {"model": LLM_MODEL,
                   "messages": [{"role": "system", "content": "You only output JSON."},
                                {"role": "user", "content": prompt}],
                   "temperature": 0}
        if want_json:
            payload["response_format"] = {"type": "json_object"}
        req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers={
            "Content-Type": "application/json", "Authorization": "Bearer " + key}, method="POST")
        try:
            with _NO_PROXY_OPENER.open(req, timeout=300) as r:
                out = json.loads(r.read().decode("utf-8"))
            try:
                return out["choices"][0]["message"]["content"]
            except Exception:
                return json.dumps(out, ensure_ascii=False)
        except urllib.error.HTTPError as e:
            code = e.code
            ra = e.headers.get("Retry-After")
            if code == 429:
                wait = min(float(ra) if ra else 4 * (attempt + 1), 60)
                print(f"      [429 wait {wait:.0f}s]", flush=True)
                time.sleep(wait)
                continue
            if code == 400 and want_json:
                want_json = False
                continue
            print(f"      [LLM HTTP {code} retry]", flush=True)
            time.sleep(3 * (attempt + 1))
        except Exception as e:
            print(f"      [LLM err {e} retry]", flush=True)
            time.sleep(3 * (attempt + 1))
    return None


def _parse_results(raw: str):
    try:
        m = re.search(r"\{.*\}", raw or "", re.S)
        data = json.loads(m.group(0))
        return data.get("results") or []
    except Exception:
        return []


def llm_semantic_batch(batch, kb, sem):
    """对一批单元做规则召回，返回 ( {id(unit): [{"rule_no"},...]}, ok )。

    阶段一只召回“可能相关的 top-k 规则号”，**不做裁判、不给理由**；
    判定唯一权威是第二阶段。留 reason 字段便于回溯，但语义上阶段一不产出判定。
    ok=False 表示本次调用失败(网络/解析错误)——用于熔断；
    ok=True 但结果空，表示模型判定该批“语义层面干净”，是**合法结果**，不算失败。
    """
    result = {id(u): [] for u in batch}
    if not LLM_ENABLED:
        return result, False
    import time as _t
    prompt = build_semantic_prompt(batch, kb, sem)
    rec = {"t": _t.time(), "dt": 0.0, "n_units": len(batch), "prompt_chars": len(prompt),
           "apis": ["%s %s" % (u.get("method", ""), u.get("path", "")) for u in batch],
           "raw": None, "ok": False, "hits": 0, "parse_err": ""}
    t0 = _t.perf_counter()
    raw = call_small_model(prompt, want_json=True)
    rec["dt"] = round(_t.perf_counter() - t0, 3)
    if not raw:
        _LLM_CALL_LOG.append(rec)
        return result, False
    rec["raw"] = raw
    audited = detect.AUDITED_RULE_IDS
    try:
        parsed = _parse_results(raw)
    except Exception as e:
        rec["parse_err"] = str(e)
        parsed = []
    for item in parsed:
        idx = item.get("idx")
        if not isinstance(idx, int) or not (0 <= idx < len(batch)):
            continue
        u = batch[idx]
        for h in (item.get("spef_rules") or item.get("related_rules") or []):
            if isinstance(h, dict):
                no = (h.get("no") or h.get("rule_no") or "").strip().upper()
            else:
                no = str(h).strip().upper()
            if not no or any(no.startswith(p) for p in audited):
                continue            # 客观/规则可判项不进 LLM 候选(兜底)
            if no not in [x["rule_no"] for x in result[id(u)]]:
                result[id(u)].append({"rule_no": no})
    rec["ok"] = True
    rec["hits"] = sum(len(v) for v in result.values())
    if len(_RAW_SAMPLES) < 10:
        _RAW_SAMPLES.append({"api": "batch[%d]" % len(batch), "raw": raw[:600]})
    _LLM_CALL_LOG.append(rec)
    return result, True


# =====================================================================
# 主流程：规则层(精确) + LLM 层(语义) → 每接口结论
# =====================================================================
def run_recall(units, kb, workers=8, llm_enabled=None, batch_size=BATCH_SIZE):
    use_llm = (llm_enabled if llm_enabled is not None else LLM_ENABLED)
    sem = semantic_rules(kb)

    # 规则层：确定性探测器（精度优先，客观发现）
    rule_by = {}
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(detect.detect_unit, u, kb): u for u in units}
        for f in futs:
            rule_by[id(futs[f])] = f.result()

    # LLM 层：语义判定（分批、节流、熔断）
    llm_by = {}
    if use_llm:
        import time as _t
        _LLM_ABORT["flag"], _LLM_ABORT["fails"] = False, 0
        n_batch = (len(units) + batch_size - 1) // batch_size
        for bi in range(n_batch):
            if _LLM_ABORT["flag"]:
                break
            batch = units[bi * batch_size:(bi + 1) * batch_size]
            r, ok = llm_semantic_batch(batch, kb, sem)
            for k, v in r.items():
                llm_by[k] = v
            hits = sum(len(v) for v in r.values())
            # 熔断只看“真实呼叫失败”；0 hits=模型判干净，是合法结果，不算失败
            _LLM_ABORT["fails"] = 0 if ok else _LLM_ABORT["fails"] + 1
            if _LLM_ABORT["fails"] >= MAX_FAIL_BATCHES:
                _LLM_ABORT["flag"] = True
            print(f"      llm batch {bi + 1}/{n_batch}: {hits} semantic hits, "
                  f"ok={ok}, fails={_LLM_ABORT['fails']}", flush=True)
            if bi < n_batch - 1 and not _LLM_ABORT["flag"]:
                _t.sleep(INTER_BATCH_GAP)
    print(f"      llm semantic units covered: {len(llm_by)}/{len(units)}", flush=True)

    # 合并 → 每接口结论
    # 规则层按置信度分桶（见 detect.py）：
    #   high    → rule_findings（明文·说一不二·阶段一直接判为确定发现）
    #   medium  → rule_candidates（模式候选，交阶段二法官式复核，不在此下判定）
    #   low     → 放过：仅计数，不进任何产出、不花阶段二算力
    verds, n_skip_low = [], 0
    for u in units:
        rf = rule_by.get(id(u), [])
        ls = llm_by.get(id(u), [])
        verdict_fs = [f for f in rf if f.get("confidence") == "high"]
        cand_fs    = [f for f in rf if f.get("confidence") == "medium"]
        n_skip_low += sum(1 for f in rf if f.get("confidence") == "low")
        no = str(u.get("chapter", "") + "." + str(u.get("no", "?")))
        title = (f"{u.get('title_cn','')} - {u.get('api_en','')}".strip(" -") or u.get("path", ""))
        verds.append({
            "service": SERVICE,
            "no": no,
            "title": title,
            "chapter": u.get("chapter_title", ""),
            "method": u.get("method", ""),
            "path": u.get("path", ""),
            "api": f"{u.get('method','')} {u.get('path','')}".strip(),
            "start_page": u.get("start_page"),          # 物理页下标(0 起)
            "verdict": "可疑" if (verdict_fs or cand_fs or ls) else "通过",
            "rule_findings": verdict_fs,                # 规则层·高置信·明文确定发现
            "rule_candidates": cand_fs,                 # 规则层·中置信·模式候选 → 阶段二
            "llm_suspects": ls,                         # LLM 层：语义可疑 [{"rule_no","reason"}]
            "text_snippet": (u.get("text", "") or "").strip().replace("\n", " ")[:300],
        })
    print(f"      rule layer: high(判定)={sum(len(v['rule_findings']) for v in verds)}, "
          f"medium(候选→阶段二)={sum(len(v['rule_candidates']) for v in verds)}, "
          f"low(放过)={n_skip_low}", flush=True)
    return verds, sem
