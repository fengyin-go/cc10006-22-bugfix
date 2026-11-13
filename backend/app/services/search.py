"""数据检索业务规则：状态流转、字段校验与筛选口径都收在这里。

检索索引的列表查询与导出共用同一套过滤逻辑（:meth:`SearchService.query_entries`），
避免出现“列表一个口径、导出另一个口径”的情况。
"""
from __future__ import annotations

import threading
import time
from typing import Any

from app.store import store

MODULE = "search"
REQUIRED_FIELDS = ["索引编号", "索引名称", "覆盖表名"]
STATUS_ORDER = ["草稿", "待构建", "已生效", "已失效"]
ACTION_RULES = {"提交构建": "待构建", "启用索引": "已生效", "标记失效": "已失效"}
# 同一索引在极短时间内重复提交的构建视为同一次构建（并发请求、双击），只生效一次。
BUILD_COALESCE_SECONDS = 0.25
# “构建”相关的同义动作都归一到同一套状态流转，避免不同入口构建出两份生效索引。
ACTION_ALIASES = {
    "构建索引": "提交构建",
    "重新构建": "提交构建",
    "重建索引": "提交构建",
    "启用": "启用索引",
    "失效": "标记失效",
}
NEGATIVE_ACTIONS: list[str] = []

# 参与过滤的索引字段。编号、名称、覆盖表名、索引字段是检索页明确承诺的四个口径。
FILTER_FIELDS = ["索引编号", "索引名称", "覆盖表名", "索引字段"]
# 其余列表字段也允许作为筛选条件，避免出现“传了字段却被当成全部命中”的误解。
SUPPORTED_FILTER_FIELDS = FILTER_FIELDS + ["分词策略", "排序权重", "刷新频率", "索引大小"]
STATUS_FILTER_KEYS = {"status", "状态"}

# 索引状态变化后，同步到数据编目（目录）与元数据中心的状态。
CATALOG_FIELDS = ["编目编号", "数据集名称", "存储路径"]
META_FIELDS = ["条目编号", "对象名称", "属性值"]
SYNC_STATUS = {
    "草稿": {"catalog": "草稿", "meta": "草稿"},
    "待构建": {"catalog": "待审核", "meta": "待审核"},
    "已生效": {"catalog": "已发布", "meta": "已生效"},
    "已失效": {"catalog": "已废弃", "meta": "已作废"},
}
SYNC_OPEN_STATUS = {"已发布", "已生效"}
SYNC_VISIBLE_STATUS = {"catalog": SYNC_OPEN_STATUS, "meta": SYNC_OPEN_STATUS}


def _text(value: Any) -> str:
    """把任意字段值转成可比较的文本，None 统一视为空串。"""
    if value is None:
        return ""
    return str(value).strip()


class SearchService:
    def __init__(self) -> None:
        # 内存仓库被 FastAPI 的线程池并发访问；构建、启用、失效必须是串行动作。
        # 构建只推进原记录版本、不新增记录，所以并发构建最终留下的仍是一个生效索引。
        self._lock = threading.RLock()
        # 每个索引最近一次构建生效的时间，用于把并发/重复的构建合并成一次。
        self._build_at: dict[int, float] = {}

    @property
    def filterable_fields(self) -> list[str]:
        return list(SUPPORTED_FILTER_FIELDS)

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    def query_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        filters: dict[str, Any] | None = None,
        field: str | None = None,
        value: str | None = None,
        page: int = 1,
        size: int | None = 20,
        invalid_fields: list[str] | None = None,
    ) -> dict[str, Any]:
        """按统一口径过滤检索索引。

        - 编号、名称、覆盖表名、索引字段都支持模糊匹配；
        - 多个条件之间是“且”的关系；
        - 空白筛选值视为未过滤；
        - ``field``/``value`` 可显式指定单一字段，非法字段返回空结果而不是全部命中；
        - 返回结果按 id 升序排列，保证翻页稳定。
        """
        with self._lock:
            applied: dict[str, str] = {}
            invalid = [item for item in (invalid_fields or []) if _text(item)]

            keyword_text = _text(keyword)
            status_text = _text(status)
            if status_text:
                applied["status"] = status_text

            for name, raw in (filters or {}).items():
                field_value = _text(raw)
                if not field_value:
                    # 空白筛选值以未过滤为准。
                    continue
                if name in STATUS_FILTER_KEYS:
                    applied["status"] = field_value
                elif name in SUPPORTED_FILTER_FIELDS:
                    applied[name] = field_value
                else:
                    invalid.append(str(name))

            field_name = _text(field)
            if field_name:
                field_value = _text(value)
                if not field_value and keyword_text:
                    # field 指定字段时，keyword 作为该字段的取值。
                    field_value = keyword_text
                    keyword_text = ""
                if field_value:
                    if field_name in STATUS_FILTER_KEYS:
                        applied["status"] = field_value
                    elif field_name in SUPPORTED_FILTER_FIELDS:
                        applied[field_name] = field_value
                    else:
                        invalid.append(field_name)

            rows = list(store.rows(MODULE))
            if keyword_text:
                rows = [
                    row
                    for row in rows
                    if any(self._matches(row, name, keyword_text) for name in FILTER_FIELDS)
                ]

            for name, expected in applied.items():
                if name == "status":
                    rows = [
                        row
                        for row in rows
                        if _text(row.get("status")) == expected
                    ]
                else:
                    rows = [row for row in rows if self._matches(row, name, expected)]

            # 稳定排序，保证同样的筛选条件下翻到第二页结果不会漂移。
            rows.sort(key=lambda row: int(row.get("id", 0) or 0))

            if invalid:
                # 非法字段不能被忽略后显示成“全部命中”。
                rows = []

            total = len(rows)
            safe_page = max(int(page or 1), 1)
            if size is None:
                safe_size = total
                items = rows
                safe_page = 1
            else:
                safe_size = max(int(size or 1), 1)
                start = (safe_page - 1) * safe_size
                items = rows[start:start + safe_size]

            return {
                "items": items,
                "total": total,
                "page": safe_page,
                "size": safe_size,
                "filters": applied,
                "message": self._build_message(
                    invalid=invalid,
                    applied=applied,
                    keyword=keyword_text,
                    total=total,
                ),
            }

    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        filters: dict[str, Any] | None = None,
        field: str | None = None,
        value: str | None = None,
        page: int = 1,
        size: int = 20,
        invalid_fields: list[str] | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        """兼容原有调用方：只返回当前页数据与总数。"""
        result = self.query_entries(
            keyword=keyword,
            status=status,
            filters=filters,
            field=field,
            value=value,
            page=page,
            size=size,
            invalid_fields=invalid_fields,
        )
        return result["items"], result["total"]

    def _matches(self, row: dict[str, Any], name: str, expected: str) -> bool:
        return expected.casefold() in _text(row.get(name)).casefold()

    def _build_message(
        self,
        *,
        invalid: list[str],
        applied: dict[str, str],
        keyword: str,
        total: int,
    ) -> str | None:
        if invalid:
            names = "、".join(dict.fromkeys(invalid))
            return f"筛选字段「{names}」不在检索索引可筛选范围内，未命中任何记录"
        if total:
            return None
        conditions = []
        if keyword:
            conditions.append(f"关键字={keyword}")
        conditions.extend(f"{name}={value}" for name, value in applied.items())
        if conditions:
            return "未命中条件：" + "、".join(conditions)
        return "暂无检索索引数据"

    # ------------------------------------------------------------------
    # 明细与登记
    # ------------------------------------------------------------------
    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not _text(values.get(field))]
        if missing:
            return None, missing
        with self._lock:
            rows = store.rows(MODULE)
            entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
            for name in SUPPORTED_FILTER_FIELDS:
                if name in values:
                    entry[name] = _text(values.get(name))
            for name in REQUIRED_FIELDS:
                entry[name] = _text(values.get(name))
            entry["status"] = STATUS_ORDER[0]
            entry["pending"] = True
            entry["abnormal"] = False
            self._write_version(entry, 1)
            rows.append(entry)
            return entry, []

    # ------------------------------------------------------------------
    # 动作与跨模块同步
    # ------------------------------------------------------------------
    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        action = _text(action)
        action = ACTION_ALIASES.get(action, action)
        with self._lock:
            entry = store.find(MODULE, entry_id)
            if entry is None:
                return None, f"检索索引 {entry_id} 不存在或已归档"
            if action not in ACTION_RULES:
                return None, f"动作「{action}」不属于数据检索可执行范围"
            if entry.get("status") not in STATUS_ORDER:
                return None, f"检索索引当前状态「{entry.get('status')}」不在允许的状态序列里"

            if action == "提交构建":
                # 并发到达的多次构建合并成一次；已生效索引原地重建，不新增记录。
                now = time.monotonic()
                last_build = self._build_at.get(int(entry_id), 0.0)
                if now - last_build < BUILD_COALESCE_SECONDS:
                    target = _text(entry.get("status")) or ACTION_RULES[action]
                    message = "检索索引构建请求已合并，本次未重复生效"
                else:
                    self._build_at[int(entry_id)] = now
                    version = int(entry.get("version") or 1) + 1
                    self._write_version(entry, version)
                    if entry.get("status") == "已生效":
                        target = "已生效"
                        message = "检索索引已重新构建，仅更新原记录版本"
                    else:
                        target = "待构建"
                        message = "检索索引已提交构建"
                if target not in STATUS_ORDER:
                    target = ACTION_RULES[action]
            else:
                target = ACTION_RULES[action]
                message = f"检索索引已{action}"

            if target not in STATUS_ORDER:
                return None, f"目标状态「{target}」不在允许的状态序列里"

            entry["status"] = target
            entry["pending"] = target in ("草稿", "待构建")
            entry["abnormal"] = action in NEGATIVE_ACTIONS
            if target == "已生效":
                # 同名且覆盖表相同的索引只允许保留一个生效版本。
                self._deactivate_duplicates(entry)
            self._sync_related(entry)
            return entry, message

    def _write_version(self, entry: dict[str, Any], version: int) -> None:
        entry["version"] = version
        entry["版本"] = version
        entry["索引版本"] = version

    def _deactivate_duplicates(self, entry: dict[str, Any]) -> None:
        name = _text(entry.get("索引名称"))
        table = _text(entry.get("覆盖表名"))
        if not name or not table:
            return
        for row in store.rows(MODULE):
            if row is entry or row.get("status") != "已生效":
                continue
            if (
                _text(row.get("索引名称")).casefold() == name.casefold()
                and _text(row.get("覆盖表名")).casefold() == table.casefold()
            ):
                row["status"] = "已失效"
                row["pending"] = False
                self._sync_related(row)

    def _sync_related(self, entry: dict[str, Any]) -> None:
        """把索引状态同步到数据编目与元数据中心。

        优先按覆盖表名匹配，其次按索引编号、索引名称匹配，避免同名不同表的索引
        互相改写目录状态。
        """
        target_status = _text(entry.get("status"))
        mapping = SYNC_STATUS.get(target_status)
        if mapping is None:
            return

        for module, keys in (("catalog", CATALOG_FIELDS), ("meta", META_FIELDS)):
            rows = store.rows(module)
            matched = self._match_related_rows(entry, rows, keys)
            for row in matched:
                row["status"] = mapping[module]
                row["pending"] = mapping[module] not in SYNC_VISIBLE_STATUS[module]

    def _match_related_rows(
        self,
        entry: dict[str, Any],
        rows: list[dict[str, Any]],
        keys: list[str],
    ) -> list[dict[str, Any]]:
        tiers = [
            _text(entry.get("覆盖表名")),
            _text(entry.get("索引编号")),
            _text(entry.get("索引名称")),
        ]
        matched: list[dict[str, Any]] = []
        seen: set[int] = set()
        for tier in tiers:
            if not tier:
                continue
            tier_hit = False
            for row in rows:
                if id(row) in seen:
                    continue
                row_values = {_text(row.get(key)).casefold() for key in keys if _text(row.get(key))}
                if tier.casefold() in row_values:
                    matched.append(row)
                    seen.add(id(row))
                    tier_hit = True
            if tier_hit:
                # 一旦在更精确的层级命中，就不再按更宽泛的名称扩散。
                break
        return matched
