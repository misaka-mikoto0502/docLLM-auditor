"""deprec.py — API 条目"已废弃/历史"确定性分类。

`deprecated_classify(unit)`：基于标题/路径判断该 API 条目是否"已废弃"。
`partition_units(units)`：把一批单元分成 (active, deprecated) 两桶。

判定口径（保守，避免误杀）：
  * 强信号：标题(title_cn/title_en)或路径含 "废弃/弃用/deprecated/不推荐/已下线"。
    已验证 ECS：106/202 单元全部以 `…（废弃）` 落在 title_cn，信号唯一、可复现。
  * 不强判：正文(body)里的"废弃"可能指"废弃某参数/某字段"，不据此自动排除，
    仅当 title/path 命中才排除 —— 宁可少排除、也不把未废弃误伤成"已废弃"。

废弃 API 不参与类 1–6 可整改合规判定（已冻结、无整改动作），但不算静默丢弃：
由 partition_units 显式分到 deprecated 桶，供覆盖率(M8)与报告如实声明。
"""
_STRONG = ("废弃", "弃用", "已下线", "不再维护",
           "deprecated", "Deprecated", "DEPRECATED", "obsolete", "Obsolete")


def deprecated_classify(unit):
    """返回匹配到的废弃标记(str)或 None。"""
    title = " ".join(str(unit.get(k) or "") for k in ("title_cn", "title_en", "title"))
    path = str(unit.get("path") or "")
    hay = title + " " + path
    for m in _STRONG:
        if m in hay:
            return m
    return None


def is_deprecated(unit):
    return deprecated_classify(unit) is not None


def partition_units(units):
    """按废弃与否分桶：返回 (active, deprecated)。顺序分别保持原相对顺序。"""
    active, dep = [], []
    for u in units:
        (dep if is_deprecated(u) else active).append(u)
    return active, dep
