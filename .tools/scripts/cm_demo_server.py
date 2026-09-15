"""cm_demo_server.py — 单网页 demo：选产品(API 文档)→ 选/输一个 API 条目 → 判定违规。

判定集 = A 档 6 单元级类别(G1-G6, 33条, spec/rule_categories_A7.json)；B/C 档与
全文档(DOC) 已剔除，不在 demo 执行集内（2026-08-19 复核：ASY-010 归 B，单条目 A 由 34 调为 33）。底层完全复用现有逻辑：
  * cmmatch.adjudicate(unit, rules) —— 路1：一条目×一类别 → per_rule 判定(走本地 vLLM)
  * cmmatch.catalog_adjudicate(unit, rules) —— 路2：整目录召回，条目×整个规则目录一次调入
  * pdf_lib.load_rules / segment.segment_apis —— 规则库与条目
仅 stdlib(http.server)，无第三方依赖。产物：audit/cm_demo/index.html。

产品数据源（本地文件，懒加载并缓存）：
  * ModelArts  → audit/work/pages_clean.json
  * MaaS/TaurusDB/gaussdb/dws/ecs/aom/codearts → audit/work/m5/<id>/pages_clean.json

用法: python .tools/scripts/cm_demo_server.py [--port 8000]
浏览器打开 http://localhost:8000
"""
import os, sys, json, time, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs
from concurrent.futures import ThreadPoolExecutor, as_completed

SCRIPTS = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPTS)
ROOT = os.path.dirname(os.path.dirname(SCRIPTS))
DEMO_DIR = os.path.join(ROOT, "audit", "cm_demo")

CONFIG = os.path.join(ROOT, "spec", "rule_categories_A7.json")
KB = os.path.join(ROOT, "spec", "spec_rules.json")
INDEX = os.path.join(DEMO_DIR, "index.html")

from auditlib import pdf_lib, segment, cmmatch, deprec

# ---------- 产品注册表 + 懒加载缓存 ----------
PRODUCTS = [{"id": "ModelArts", "label": "ModelArts",
             "path": os.path.join(ROOT, "audit", "work", "pages_clean.json")}]
for _n in ["MaaS", "TaurusDB", "gaussdb", "dws", "ecs", "aom", "codearts",
           "APM", "DBSS"]:
    PRODUCTS.append({"id": _n, "label": _n,
                     "path": os.path.join(ROOT, "audit", "work", "m5", _n, "pages_clean.json")})

_prod = {}          # id -> {id,pages,units,n_units,path}
_prod_lock = threading.Lock()


def product(name):
    """懒加载某产品的 pages+units，进程内缓存；name 缺失默认 ModelArts。"""
    name = name or "ModelArts"
    with _prod_lock:
        if name in _prod:
            return _prod[name]
        p = next((x for x in PRODUCTS if x["id"] == name), None)
        if p is None or not os.path.exists(p["path"]):
            raise KeyError(f"未知产品 '{name}' 或数据文件缺失: {p['path'] if p else name}")
        pages = json.load(open(p["path"], encoding="utf-8"))
        units = segment.segment_apis(pages)
        active, dep = deprec.partition_units(units)
        _prod[name] = {"id": name, "label": p["label"], "path": p["path"],
                       "pages": pages, "units": active, "all_units": units,
                       "n_units": len(active), "n_total": len(units), "n_dep": len(dep)}
        return _prod[name]


# ---------- 一次性加载(规则库/类别，全局通用) ----------
_by_no = pdf_lib.load_rules(KB)["by_no"]
_cfg = json.load(open(CONFIG, encoding="utf-8"))
_UNIT_CATS = _cfg["unit_categories"]                 # 路1：A 档 6 单元级类别(G1-G6, 34条)
# 全文档剔除：整篇级(G7/DOC-010..030)与 B/C 档均不在 demo 执行集内
_DOC_CATS = []
# 路2·整目录召回的规则目录 = 全部 A 档单元级规则(跨 6 类合一锅，一次调入判定)
_CATALOG_ALL = [r for c in _UNIT_CATS for r in c["rules"]]
_lock = threading.Lock()

_METHOD_OK = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}


def _unit_label(u, i):
    return {"i": i, "method": u.get("method", ""), "path": u.get("path", ""),
            "title": u.get("title_cn", ""), "start_page": u.get("start_page"),
            "chapter": u.get("chapter_title", "")}


def _resolve_rules(c):
    return cmmatch.resolve_rules(_by_no, c["rules"])


def _judge_all(prod, unit):
    """单条目 全量并行 = 双路召回：路1 = A 档 6 个单元级类别(G1-G6)，路2 = 整目录召回。
    全文档(G7/DOC)与 B/C 档不在执行集内。"""
    tasks = [(c, "unit") for c in _UNIT_CATS] + [
        ({"id": 99, "name": "整目录召回", "rules": _CATALOG_ALL}, "catalog"),
    ]

    def run(cat, mode):
        rules = _resolve_rules(cat)
        rec = (cmmatch.catalog_adjudicate(unit, rules) if mode == "catalog"
               else cmmatch.adjudicate(unit, rules))
        rec = dict(rec)
        rec["cat_id"], rec["cat_name"], rec["mode"] = cat["id"], cat["name"], mode
        rec["per_rule"] = [r for r in rec.get("per_rule", []) if r.get("rule_no")]
        return rec

    t0 = time.perf_counter()
    with ThreadPoolExecutor(max_workers=len(tasks)) as ex:
        futs = {ex.submit(run, c, m): c["id"] for c, m in tasks}
        recs = [fut.result() for fut in as_completed(futs)]
    return {"wall_s": round(time.perf_counter() - t0, 2),
            "n_cats": len(tasks), "categories": sorted(recs, key=lambda r: r["cat_id"])}


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass  # 静默访问日志

    def _send(self, code, payload, ctype="application/json; charset=utf-8"):
        body = payload if isinstance(payload, bytes) else json.dumps(
            payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path == "/" or u.path == "/index.html":
            try:
                with open(INDEX, "rb") as f:
                    self._send(200, f.read(), "text/html; charset=utf-8")
            except FileNotFoundError:
                self._send(500, {"error": f"index.html 缺失: {INDEX}"})
            return
        if u.path == "/api/meta":
            pid = q.get("product", [""])[0] or "ModelArts"
            try:
                prod = product(pid)
            except KeyError as e:
                self._send(400, {"error": str(e)})
                return
            units = [_unit_label(x, i) for i, x in enumerate(prod["units"])]
            cats = [{"id": c["id"], "name": c["name"], "mode": "unit",
                     "rules": [r["no"] for r in _resolve_rules(c)]} for c in _UNIT_CATS]
            plist = [{"id": p["id"], "label": p["label"],
                      "n_units": _prod[p["id"]]["n_units"] if p["id"] in _prod else None,
                      "n_total": _prod[p["id"]]["n_total"] if p["id"] in _prod else None,
                      "n_dep": _prod[p["id"]]["n_dep"] if p["id"] in _prod else None}
                     for p in PRODUCTS]
            self._send(200, {"product": pid, "products": plist, "units": units,
                             "categories": cats, "doc_categories": [],
                             "catalog": {"name": "整目录召回", "id": 99,
                                         "rules": _CATALOG_ALL,
                                         "n_rules": len(_CATALOG_ALL)},
                             "n_units": len(units), "n_total": prod["n_total"],
                             "n_dep": prod["n_dep"]})
            return
        if u.path == "/api/prompt":
            # 预览用：纯拼 prompt(不调 LLM)。i=条目序号, cat_id=类别 id, product=产品
            try:
                i = int(q.get("i", [""])[0])
                cat_id = int(q.get("cat_id", [""])[0])
            except Exception:
                self._send(400, {"error": "need i & cat_id"})
                return
            pid = q.get("product", [""])[0] or "ModelArts"
            try:
                prod = product(pid)
            except KeyError as e:
                self._send(400, {"error": str(e)})
                return
            if not (0 <= i < len(prod["units"])):
                self._send(400, {"error": f"条目序号无效: {i}"})
                return
            unit = prod["units"][i]
            if cat_id == 99:
                # 路2·整目录召回 预览：条目 × 整目录(提示词骨架与 build_prompt 一致)
                rules = cmmatch.resolve_rules(_by_no, _CATALOG_ALL)
                prompt = cmmatch.build_prompt(unit, rules)
                self._send(200, {
                    "api": f"{unit.get('method')} {unit.get('path')}".strip(),
                    "cat": "整目录召回", "cat_id": 99, "mode": "catalog",
                    "prompt": prompt, "prompt_chars": len(prompt),
                    "rules": [r["no"] for r in rules],
                    "unit_text": unit.get("text", ""),
                    "start_page": unit.get("start_page"),
                })
                return
            cat = next((c for c in _UNIT_CATS + _DOC_CATS if c["id"] == cat_id), None)
            if cat is None:
                self._send(400, {"error": f"类别 id 无效: {cat_id}"})
                return
            rules = _resolve_rules(cat)
            if cat.get("mode") == "doc":
                prompt = cmmatch.build_doc_prompt(prod["pages"], rules)
            else:
                prompt = cmmatch.build_prompt(unit, rules)
            self._send(200, {
                "api": f"{unit.get('method')} {unit.get('path')}".strip(),
                "cat": cat["name"], "cat_id": cat_id, "mode": cat.get("mode", "unit"),
                "prompt": prompt, "prompt_chars": len(prompt),
                "rules": [r["no"] for r in rules],
                "unit_text": unit.get("text", ""),
                "start_page": unit.get("start_page"),
            })
            return
        self._send(404, {"error": "not found"})

    def do_POST(self):
        u = urlparse(self.path)
        if u.path not in ("/api/judge", "/api/judge_all", "/api/judge_catalog"):
            self._send(404, {"error": "not found"})
            return
        try:
            n = int(self.headers.get("Content-Length", 0))
            data = json.loads(self.rfile.read(n) or b"{}")
        except Exception as e:
            self._send(400, {"error": f"bad request: {e}"})
            return
        i = data.get("i")
        pid = data.get("product") or "ModelArts"
        try:
            prod = product(pid)
        except KeyError as e:
            self._send(400, {"error": str(e)})
            return
        if not (0 <= i < len(prod["units"])):
            self._send(400, {"error": f"条目序号无效: {i} (产品 {pid} 共 {prod['n_units']} 条)"})
            return
        if u.path == "/api/judge_all":
            self._send(200, _judge_all(prod, prod["units"][i]))
            return
        if u.path == "/api/judge_catalog":
            # 路2·整目录召回 单次调用：条目 × 整个规则目录(全部 A 单元级规则一次调入)
            rules = cmmatch.resolve_rules(_by_no, _CATALOG_ALL)
            with _lock:
                rec = cmmatch.catalog_adjudicate(prod["units"][i], rules)
            rec = dict(rec)
            rec["cat_id"], rec["cat_name"] = 99, "整目录召回"
            rec["per_rule"] = [r for r in rec.get("per_rule", []) if r.get("rule_no")]
            self._send(200, rec)
            return
        cat_id = data.get("cat_id")
        cat = next((c for c in _UNIT_CATS + _DOC_CATS if c["id"] == cat_id), None)
        if cat is None:
            self._send(400, {"error": f"类别 id 无效: {cat_id}"})
            return
        unit = prod["units"][i]
        rules = _resolve_rules(cat)
        if not rules:
            self._send(200, {"method": unit.get("method"), "path": unit.get("path"),
                             "cat_id": cat_id, "cat_name": cat["name"], "mode": cat.get("mode", "unit"),
                             "ok": False, "per_rule": [], "summary": "该类别未解析到任何规则"})
            return
        with _lock:  # 单条判定串行，避免对本机 LLM 并发打爆
            if cat.get("mode") == "doc":
                rec = cmmatch.doc_adjudicate(prod["pages"], rules)
            else:
                rec = cmmatch.adjudicate(unit, rules)
        rec = dict(rec)
        rec["cat_id"], rec["cat_name"] = cat_id, cat["name"]
        rec["per_rule"] = [r for r in rec.get("per_rule", []) if r.get("rule_no")]
        self._send(200, rec)


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8000)
    a = ap.parse_args()
    print(f"[cm_demo] 已注册 {len(PRODUCTS)} 个产品(API 文档)，懒加载缓存：")
    for p in PRODUCTS:
        mark = "●" if p["id"] in _prod else "○"
        if p["id"] in _prod:
            x = _prod[p["id"]]
            cnt = f"{x['n_units']} 有效 / 废弃 {x['n_dep']} / 共 {x['n_total']}"
        else:
            cnt = "?"
        print(f"          {mark} {p['id']:<10} {cnt:<24} {p['path']}")
    os.makedirs(DEMO_DIR, exist_ok=True)
    print(f"[cm_demo] 打开 http://localhost:{a.port}")
    ThreadingHTTPServer(("0.0.0.0", a.port), H).serve_forever()


if __name__ == "__main__":
    main()
