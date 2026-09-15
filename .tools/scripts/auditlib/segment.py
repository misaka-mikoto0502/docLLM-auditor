"""segment.py — 把清洗后的页文本切成“API 单元”(URI 标签驱动，通用版式)。

关键：这类云厂商 API 参考文档的版式是 —— 每个 API 在其正文中出现一行独立的
“URI”标签，紧随其后的一行才是完整的 "METHOD /vX/{..}/...". 我们**只认这个位置
的 URI 作为 API 边界**，从而避免把权限表/示例里的 “POST /v1/..” 断行片段误当
成新 API（上一版教训）。章节/标题仅用于给单元打上归属标签。

产出每个单元：
  chapter, chapter_title, no, title_cn, api_en,
  method, uri, path, start_page, end_page, text, segments
"""
import re
from . import pdf_lib

CHAP_RE = re.compile(r"^(\d{1,2})\s+(?!\d)([^\s].{1,60})$")
# API 标题形如 "6.1.1 获取Workflow 工作流列表 - ListWorkflows"：多级编号 + 必带 " - EnglishName" 后缀
# （小节标题如 "6.1 Workflow 工作流管理" 无连字符，不会误判为 API 标题）
API_RE  = re.compile(r"^(\d{1,2}(?:\.\d{1,3})+)\s+([一-鿿A-Za-z0-9（）()\- _]+?)\s*-\s*(.+?)\s*$")
# 历史API(第13章 legacy)标题无 "- English" 后缀，如 "13.1.1 查询数据集列表"：
# 三级及更深编号+N自由文本即视为 API 标题（分组标题是二级 N.N，不会被误判）
API_RE_NOEN = re.compile(r"^(\d{1,2}(?:\.\d{1,3}){2,})\s+(.+?)\s*$")
# 兜底标题回溯：2+ 级编号标题(免英文后缀)。仅当某 URI 端点开单元、且上面两个标题正则都
# 未命中时才用——在「端点真正打开」处向后找最近一条 2+ 级编号行当标题，故分组标题
# (它下面还有更深编号或多个端点，不会紧贴单个端点)不会被误判。封面 MaaS 等
# 两段式中文 API 标题(如 "5.1 创建自定义接入点")。
TITLE_BACK_RE = re.compile(r"^(\d{1,2}(?:\.\d{1,3})+)\s+(.+?)\s*$")


def segment_apis(pages: list[dict]) -> list[dict]:
    """按「API 标题即条目起点」切分（2026-08-14 修正, 原为 URI 标签驱动）。

    版式：每 API = 「章节标题 → API 标题(N.N 中文 - English) → 功能介绍/调试/授权信息前导
    → URI 标签 → METHOD path → 契约正文」。本实现：
      * API 标题 → 关闭上一单元、登记本单元归属(no/title_cn/api_en)，并把标题行与其后
        的 功能介绍/调试/授权信息 缓冲进本单元「前导」，直到 URI 出现。
      * URI 标签 + METHOD/path → 前导+标题 与 URI 一起正式开启新单元。
    效果：每个单元严格 = 自己的标题+前导+契约正文；既缺一不丢，也不再串入下一接口前导。
    """
    events = []                      # (page, line)
    for pg in pages:
        for ln in pg["lines"]:
            events.append((pg["page"], ln))

    chapter = chapter_title = ""
    pending = None                   # (no, cn, en)
    pend_page = None                 # 标题所在页(=单元逻辑起点)
    prey = []                        # 标题行 + 其后的前导行缓冲
    recent_head = []                 # 自本章以来所有 (page,line) 缓冲，供兜底标题回溯
    cur = None
    units = []

    def close():
        nonlocal cur
        if cur is not None:
            units.append(cur)
            cur = None

    i, N = 0, len(events)
    while i < N:
        page, ln = events[i]
        s = ln.strip()
        if not s:
            i += 1
            continue

        # 章节标题：重置 pending/前导，避免跨章串标题
        mch = CHAP_RE.match(s)
        if mch and not API_RE.match(s) and not pdf_lib.parse_uri_line(s) and not pdf_lib.URI_LABEL_RE.match(s):
            chapter, chapter_title = mch.group(1), mch.group(2)
            pending = None
            pend_page = None
            prey = []
            recent_head = []
            close()
            cur = None
            i += 1
            continue
        recent_head.append((page, s))
        if len(recent_head) > 600:
            recent_head = recent_head[-600:]

        # API 标题：条目起点——关闭上一单元，缓冲本单元前导(起于标题行)
        # 优先带英文名(API_RE)，兜底历史API无英文名(API_RE_NOEN，三级及更深编号)
        mah = API_RE.match(s)
        is_title = False
        if mah:
            pending = (mah.group(1), mah.group(2).strip(), mah.group(3).strip())
            is_title = True
        else:
            mh = API_RE_NOEN.match(s)
            if mh:
                pending = (mh.group(1), mh.group(2).strip(), "")
                is_title = True
        if is_title:
            close()
            pend_page = page
            prey = [s]
            recent_head = []
            i += 1
            continue

        # “URI”标签：后续行若为完整 METHOD+path，则前导+URI 开启新单元
        if pdf_lib.URI_LABEL_RE.match(s):
            j = i + 1
            found = None
            while j < N and j - i <= 8:
                pj, lj = events[j]
                up = pdf_lib.parse_uri_line(lj)
                if up:
                    found = up
                    break
                # 遇到明显不是 URI 的正文(非空且非标签)，放弃
                if lj.strip() and not pdf_lib.URI_LABEL_RE.match(lj.strip()) and "表" not in lj:
                    break
                j += 1
            if found:
                close()
                method, path = found
                tail_lines = []     # 被并入 path 的折行续段行(同时保留进单元文本)
                # 折行 URI 续段合并(2026-08-19)：长 URI 在 PDF 里折成两行时，
                # parse_uri_line 只取到方法行前半段。若紧随其后的非空行为"纯 ASCII 路径续片"
                # (非方法、非表格行、非 http 前缀、无空格、含 / 或 ? 或以 { 开头)，视为续段并入 path。
                # 仅当方法行 path 以 '-' 或 '/' 结尾才直接拼接，避免吞掉"请求参数"等正文。
                kk = j + 1
                while kk < N:
                    tks = events[kk][1].strip()
                    if not tks:
                        kk += 1
                        continue
                    # 仅当方法行 path 以 '-' 或 '/' 结尾(明显折行未完)才尝试合并续段；
                    # 续段须为纯 ASCII、无空格、非方法、非表格、非 http、含字母数字
                    # (排除裸 '{'/'}' 等 JSON 起始符误吞)。
                    if (path.endswith(("-", "/"))
                            and pdf_lib.parse_uri_line(tks) is None
                            and not tks.startswith("|")
                            and not tks.lower().startswith("http")
                            and re.fullmatch(r"[A-Za-z0-9_{}./?=&~\-]+", tks)
                            and re.search(r"[A-Za-z0-9]", tks)
                            and tks != "{" and tks != "}"):
                        path += tks          # 折行首段以 - 或 / 结尾,直接拼接续段
                        tail_lines.append(tks)
                        kk += 1
                        continue
                    break
                # 兜底：URI 端点真正打开但上面未识别出 API 标题(如 MaaS 两段式中文标题)。
                # 在本章缓冲里向后找最近一条 2+ 级编号行当标题+前导；找不到则维持空标题。
                if not pending:
                    tbk = None
                    for k in range(len(recent_head) - 1, -1, -1):
                        _pp, _ls = recent_head[k]
                        if pdf_lib.parse_uri_line(_ls) or pdf_lib.URI_LABEL_RE.match(_ls):
                            continue
                        _tm = TITLE_BACK_RE.match(_ls)
                        if _tm:
                            tbk = (k, _pp, _tm.group(1), _tm.group(2).strip())
                            break
                    if tbk:
                        pending = (tbk[2], tbk[3], "")
                        pend_page = tbk[1]
                        prey = [_ls for (_pp, _ls) in recent_head[tbk[0]+1:]]
                cur = {
                    "chapter": chapter, "chapter_title": chapter_title,
                    "no": pending[0] if pending else "", "title_cn": pending[1] if pending else "",
                    "api_en": pending[2] if pending else "",
                    "method": method, "uri": f"{method} {path}", "path": path,
                    "start_page": pend_page if pend_page is not None else page,
                    "end_page": page, "text": "", "segments": [],
                }
                head = "\n".join(prey)
                method_line = events[j][1].strip()
                cur["text"] = ((head + "\n") if head else "") + s + "\n" + method_line \
                    + (("\n" + "\n".join(tail_lines)) if tail_lines else "") + "\n"
                cur["segments"] = (list(prey) if prey else []) + [s, method_line] + tail_lines
                prey = []
                recent_head = []
                pending = None        # 消费式：开单元后清空，避免跨端点串用上一标题
                pend_page = None
                i = kk              # 跳过已并入 path 的折行续段行
                continue

        if cur is not None:
            cur["text"] += s + "\n"
            cur["segments"].append(s)
            cur["end_page"] = page
        elif pending is not None:
            # 无单元打开且已见标题：缓冲为前导(功能介绍/调试/授权信息等)
            prey.append(s)
        i += 1

    close()
    return units
