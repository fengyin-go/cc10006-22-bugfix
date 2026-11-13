"""数据检索接口：维护检索索引，覆盖提交构建、启用索引、标记失效等动作。

列表与导出共用 ``SearchService.query_entries``，保证两个入口的筛选、排序和分页口径一致。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request

from app.schemas import ActionResult, EntryPayload, SearchPageResult
from app.services.search import SearchService

router = APIRouter(prefix="/api/search", tags=["数据检索"])

service = SearchService()

LIST_FIELDS = ["索引编号", "索引名称", "覆盖表名", "索引字段", "分词策略", "排序权重", "刷新频率", "索引大小"]
STATUSES = ["草稿", "待构建", "已生效", "已失效"]

# 查询参数别名：中文列名、常见英文列名都映射到统一的过滤字段。
FILTER_ALIASES = {
    "索引编号": "索引编号",
    "code": "索引编号",
    "index_code": "索引编号",
    "indexCode": "索引编号",
    "索引名称": "索引名称",
    "name": "索引名称",
    "index_name": "索引名称",
    "indexName": "索引名称",
    "覆盖表名": "覆盖表名",
    "table": "覆盖表名",
    "table_name": "覆盖表名",
    "tableName": "覆盖表名",
    "covered_table": "覆盖表名",
    "索引字段": "索引字段",
    "fields": "索引字段",
    "index_field": "索引字段",
    "indexField": "索引字段",
}
STATUS_ALIASES = {"status": "status", "状态": "status", "索引状态": "status"}
# 分页与协议参数不参与过滤。
META_KEYS = {"page", "size", "keyword", "field", "value", "format"}


def _clean(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _collect_filters(request: Request) -> tuple[dict[str, str], list[str], str | None, str | None, str | None]:
    """把请求里的查询参数统一成 (过滤条件, 非法字段, field, value, status)。

    空白值视为未过滤；无法识别的字段名记为非法，避免它被忽略后显示成全部命中。
    """
    filters: dict[str, str] = {}
    invalid: list[str] = []
    explicit_field: str | None = None
    explicit_value: str | None = None
    status: str | None = None

    for raw_name, raw_value in request.query_params.multi_items():
        name = raw_name.strip()
        value = _clean(raw_value)
        if not value:
            # 空白筛选值以未过滤为准。
            continue
        if name in META_KEYS:
            if name == "field":
                explicit_field = value
            elif name == "value":
                explicit_value = value
            continue
        if name in STATUS_ALIASES:
            status = value
            continue
        canonical = FILTER_ALIASES.get(name)
        if canonical is None:
            invalid.append(name)
            continue
        filters[canonical] = value

    return filters, invalid, explicit_field, explicit_value, status


def _query(request: Request, *, page: int = 1, size: int | None = 20) -> dict[str, Any]:
    filters, invalid, field, value, status = _collect_filters(request)
    keyword = _clean(request.query_params.get("keyword")) or None
    return service.query_entries(
        keyword=keyword,
        status=status,
        filters=filters,
        field=field,
        value=value,
        page=page,
        size=size,
        invalid_fields=invalid,
    )


@router.get("/export")
def export_entries(request: Request) -> dict[str, Any]:
    """导出数据检索清单：沿用列表当前的筛选条件，并显式带上索引状态。

    空白筛选值表示不过滤；非法筛选字段返回空结果并在 message 中说明。
    """
    result = _query(request, page=1, size=None)
    items = []
    for row in result["items"]:
        exported = dict(row)
        exported["索引状态"] = row.get("status")
        items.append(exported)
    return {
        "module": "search",
        "total": result["total"],
        "items": items,
        "filters": result["filters"],
        "status": result["filters"].get("status"),
        "message": result["message"],
    }


@router.get("", response_model=SearchPageResult)
def list_entries(request: Request, page: int = 1, size: int = 20) -> SearchPageResult:
    """按编号、名称、覆盖表名、索引字段与状态过滤；条件之间为“且”，空白值不生效。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    result = _query(request, page=page, size=size)
    return SearchPageResult(
        items=result["items"],
        total=result["total"],
        page=result["page"],
        size=result["size"],
        message=result["message"],
        filters=result["filters"],
    )


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条检索索引明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"检索索引 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条检索索引，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="检索索引已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条检索索引执行提交构建、启用索引、标记失效；不允许的动作会被拦下并说明原因。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
