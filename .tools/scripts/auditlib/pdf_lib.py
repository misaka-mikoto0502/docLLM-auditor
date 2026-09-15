"""pdf_lib.py — 通用 API 参考文档 PDF 的文本提取 / 清洗 / 缓存。

设计目标(可复用/可泛化)：
  * 不依赖任何厂商特定内容，仅按通用版面特征清洗页眉页脚。
  * 将 页文本 -> 清洗文本 缓存为 json，避免每次重解析 3663 页大 PDF。
  * 提供“单条规则库加载”“关键词召回”等通用工具。

只处理文本层，不渲染、不读图；若某文档为扫描版(无可提取文本)，调用方可改用 OCR 管道。
"""
import os, re, json, hashlib

DEFAULT_FOOTER_MARKS = ("版权所有", "ModelArts\nAPI 参考", "文档版本", "API 参考")

def clean_page_text(text: str) -> list[str]:
    """清洗单页文本：去掉页眉/页脚/页码，返回非空、裁剪后的行列表。

    通用规则(不针对 ModelArts 定制，仅针对这类 Antenna House 版式的云厂商 API 文档)：
      - 移除单独的页码行(整行仅数字)
      - 移除页脚段：从含“版权所有”的行往后(版权/章节名/页码尾块)
      - 移除“文档版本 ... / 日期”行与 “ModelArts/API 参考” 头
    """
    lines = text.split("\n")
    out = []
    for ln in lines:
        s = ln.strip()
        if not s:
            continue
        # 纯页码行
        if re.fullmatch(r"\d{1,4}", s):
            continue
        # 页脚尾块起点
        if s.startswith("版权所有") or re.search(r"版权.*", s) and "©" in s:
            break
        # 常规页脚/版本标记
        if re.match(r"^文档版本\s*\d", s):
            continue
        if re.fullmatch(r"(ModelArts|API参考|API 参考)", s):
            continue
        out.append(s)
    # 再去掉末尾仍可能残留的“章节名 + 页码”型页脚(最后一个非空行如果是孤立章节标题行，保留无妨)
    return out


def extract_pages(pdf_path: str, cache_path: str | None = None, force: bool = False) -> list[dict]:
    """提取 PDF 全文(按页)，清洗后缓存。返回 [{page, lines, text}]。"""
    if cache_path and os.path.exists(cache_path) and not force:
        with open(cache_path, encoding="utf-8") as f:
            return json.load(f)
    import fitz
    doc = fitz.open(pdf_path)
    pages = []
    for i in range(doc.page_count):
        raw = doc[i].get_text("text")
        lines = clean_page_text(raw)
        pages.append({"page": i, "lines": lines, "text": "\n".join(lines)})
    if cache_path:
        os.makedirs(os.path.dirname(cache_path), exist_ok=True)
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump(pages, f, ensure_ascii=False)
    return pages


# =====================================================================
# 坐标级页眉/页脚预处理（供 segment 之前的单元切分使用）
# 判据=纯位置带(与内容/重复性无关)：顶部 9%、底部 90% 的 y 带视为页眉/页脚。
# 实测(ModelArts, h=842)：页眉 y≈42-67、页脚 y≈773-786、正文 y≈83-727，
#   带边界(75/757)与正文间有安全间距 → 不会误伤正文。
# =====================================================================
PAGE_TOP_BAND = 0.09   # y0 < 9%·h → 页眉
PAGE_BOT_BAND = 0.90   # y0 > 90%·h → 页脚

def _page_margin_rows(page):
    """逐行提取 (y0, text)，按 y 升序排序（利用坐标而非文本平铺顺序）。"""
    rows = []
    h = page.rect.height
    for blk in page.get_text("dict")["blocks"]:
        for ln in blk.get("lines", []):
            x0, y0, x1, y1 = ln["bbox"]
            txt = "".join(sp["text"] for sp in ln.get("spans", [])).strip()
            if txt:
                rows.append((y0, h, txt))
    rows.sort(key=lambda r: r[0])
    return rows

def _cell_text(page, rect, gap: float = 2.5, line_tol: float = 3.0) -> str:
    """用 word 级精确bbox裁剪重建单个单元格文本。

    关键修复:get_text("dict") 的 line 平铺会把表内各列文本交错混排(孤立"是/否"行)，
    find_tables().extract() 会把 project_id 拆成 "project id\n_"(下划线错位)。
    这里按单元格 rect 精确裁取 word(坐标级)，行内相邻词间距>=gap 才插空格，
    跨行用 '' 拼回(中文无空格需求) → 输出干净、完整、无交错。
    """
    import fitz
    r0 = fitz.Rect(rect)
    ws = [w for w in page.get_text("words", clip=r0)]
    if not ws:
        return ""
    ws.sort(key=lambda w: (w[1], w[0]))
    lines = []
    cur, cury = [], None
    for w in ws:
        if cury is None or abs(w[1] - cury) <= line_tol:
            cur.append(w)
            cury = w[1] if cury is None else min(cury, w[1])
        else:
            lines.append(cur)
            cur, cury = [w], w[1]
    if cur:
        lines.append(cur)
    out = []
    for ln in lines:
        ln.sort(key=lambda w: w[0])
        s, prev = "", None
        for w in ln:
            if prev is not None and (w[0] - prev) >= gap:
                s += " "
            s += w[4]
            prev = w[2]
        out.append(s)
    return "".join(out)


def _cluster_rows(cells, tol: float = 5.0) -> list[list]:
    """按单元格 y0 聚类成表格行(合并单元格也自然归入首行)，行内原序。"""
    sorted_cells = sorted(cells, key=lambda c: (c[1], c[0]))
    groups = []
    rep = None
    for c in sorted_cells:
        if rep is None or abs(c[1] - rep) > tol:
            groups.append([c])
            rep = c[1]
        else:
            groups[-1].append(c)
    return groups


def _table_lines(page, t) -> tuple[float, list[str]]:
    """重建一张表为单行管道行列表，返回 (顶y锚点, [行文本...])。

    不用 flat 列主序索引(合并单元格时 cells 长度 != nr*nc)；改用几何 y0 聚类分
    行、行内按 x0 排序，对含合并单元格的表格同样稳健。行首带 '|' 防误命中分段正则。
    """
    cells = [c for c in t.cells if c]
    if not cells:
        return 0.0, []
    top = min(c[1] for c in cells)
    lines = []
    for row in _cluster_rows(cells):
        row_sorted = sorted(row, key=lambda c: c[0])
        lines.append("| " + " | ".join(_cell_text(page, c) for c in row_sorted) + " |")
    return top, lines


def _body_rows(page, table_rects) -> list[tuple[float, float, str]]:
    """取正文行(纯位置带裁页眉页脚)，剔除落入任一表格 rect 的行(避免与重建表重复)。"""
    import fitz
    h = page.rect.height
    rows = []
    for blk in page.get_text("dict")["blocks"]:
        for ln in blk.get("lines", []):
            x0, y0, x1, y1 = ln["bbox"]
            txt = "".join(sp["text"] for sp in ln.get("spans", [])).strip()
            if not txt:
                continue
            if y0 < h * PAGE_TOP_BAND or y0 > h * PAGE_BOT_BAND:
                continue
            rr = fitz.Rect(x0, y0, x1, y1)
            if any(rr.intersects(tr) for tr in table_rects):
                continue
            rows.append((y0, y1, txt))
    rows.sort(key=lambda r: r[0])
    return rows


def extract_pages_clean(pdf_path: str, clean_cache: str | None = None, force: bool = False) -> list[dict]:
    """坐标级提取:裁页眉页脚 + 表格结构重建，产出 [{page, lines, text}]，缓存到 clean_cache。

    与旧版(纯位置带剔行)的区别:表格用 find_tables 结构 + word 级单元格重建输出为
    单行管道块(行首带 '|' 防误命中 CHAP/API/URI 正则)，消除表内文本交错与下划线错位。
    表格与正文按 y 序合并保持阅读顺序。segment.segment_apis 可直接消费本结果(schema 不变)。
    """
    import fitz
    if clean_cache and os.path.exists(clean_cache) and not force:
        with open(clean_cache, encoding="utf-8") as f:
            return json.load(f)
    clean_cache = clean_cache or (os.path.splitext(pdf_path)[0] + "_clean.json")
    doc = fitz.open(pdf_path)
    cleaned = []
    for pi in range(doc.page_count):
        page = doc[pi]
        tables, table_rects = [], []
        try:
            tables = page.find_tables().tables
        except Exception:
            tables = []
        for t in tables:
            tbl_rect = None
            for cell in t.cells:
                if cell is None:
                    continue
                cr = fitz.Rect(cell)
                tbl_rect = cr if tbl_rect is None else (tbl_rect | cr)
            if tbl_rect is not None:
                table_rects.append(tbl_rect)

        events = [(y0, txt) for (y0, y1, txt) in _body_rows(page, table_rects)]
        for t in tables:
            top, block = _table_lines(page, t)
            if block:
                events.append((top, block))
        events.sort(key=lambda e: e[0])
        lines = []
        for y0, payload in events:
            if isinstance(payload, list):
                lines.extend(payload)
            else:
                lines.append(payload)
        cleaned.append({"page": pi, "lines": lines, "text": "\n".join(lines)})
    doc.close()
    os.makedirs(os.path.dirname(clean_cache), exist_ok=True)
    with open(clean_cache, "w", encoding="utf-8") as f:
        json.dump(cleaned, f, ensure_ascii=False, indent=1)
    return cleaned


# ---------- 规则库加载与召回 ----------
_rules_cache = {}
def load_rules(kb_path: str) -> dict:
    """加载 spec_rules.json，返回 {no: rule} 与规则列表。"""
    if kb_path in _rules_cache:
        return _rules_cache[kb_path]
    with open(kb_path, encoding="utf-8") as f:
        kb = json.load(f)
    by_no = {r.get("no"): r for r in kb["rules"] if r.get("no")}
    _rules_cache[kb_path] = {"list": kb["rules"], "by_no": by_no}
    return _rules_cache[kb_path]


def build_keyword_index(rules: list[dict]) -> dict:
    """从规则 keywords 构建 小写关键词 -> 规则no集合 的倒排。"""
    idx = {}
    for r in rules:
        no = r.get("no")
        if not no:
            continue
        for k in r.get("keywords", []):
            k = k.strip().lower()
            if len(k) >= 2:
                idx.setdefault(k, set()).add(no)
    return idx


def recall_by_keywords(keyword_idx: dict, text: str, top_n: int = 8) -> list[str]:
    """给定一段文本，返回命中的规则号(按命中关键词数排序)。"""
    low = text.lower()
    scores = {}
    for kw, nos in keyword_idx.items():
        if kw in low:
            for no in nos:
                scores[no] = scores.get(no, 0) + 1
    return [no for no, _ in sorted(scores.items(), key=lambda x: -x[1])][:top_n]


URI_METHOD_RE = re.compile(
    r"^\s*(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)"
    r"(?:\s+https?://[^\s/]+)?"               # 可选 scheme://host(与 path 紧贴,无空格)
    r"\s*(/[^\s]*)\s*$"                       # path 允许根路径 "/" 与相对/绝对路径
)

def parse_uri_line(line: str) -> tuple[str, str] | None:
    """从一行解析出 (METHOD, path)；失败返回 None。兼容相对 /vX/.. 与绝对 https://host/vX/..。"""
    m = URI_METHOD_RE.match(line)
    if not m:
        return None
    method, path = m.group(1).upper(), m.group(2)
    # 去掉可能的前缀协议/域名
    path = re.sub(r"^https?://[^/]+", "", path)
    return method, path


URI_LABEL_RE = re.compile(r"^URI\s*[:：]?$")
