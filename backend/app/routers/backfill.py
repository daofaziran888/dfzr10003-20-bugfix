"""历史重跑接口：维护重跑批次，覆盖启动、暂停、完成、归档与快照引用保护。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.backfill import STATUS_ORDER, backfill_service

router = APIRouter(prefix="/api/backfill", tags=["历史重跑"])

LIST_FIELDS = [
    "批次编号", "目标表名", "开始日期", "结束日期", "并行数量", "覆盖模式", "重跑状态",
    "异常区间", "引用快照编号", "引用快照状态", "引用保留天数", "引用恢复校验",
    "回滚依据校验编号", "可回滚",
]
STATUSES = STATUS_ORDER


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按批次编号检索"),
    status: str | None = Query(default=None, description="草稿、运行中、已暂停、已完成"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按批次编号与状态过滤历史重跑列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = backfill_service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出历史重跑清单：返回当前过滤条件下的全量数据。"""
    items, total = backfill_service.list_entries(page=1, size=10000)
    return {"module": "backfill", "total": total, "items": items}


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条重跑批次明细；不存在时给出可读的错误说明。"""
    entry = backfill_service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"重跑批次 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条重跑批次，缺字段时说明原因而不是静默丢弃；重复批次编号按幂等返回。"""
    entry, missing = backfill_service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="重跑批次已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条批次执行状态机动作或引用快照；违反状态机/引用保护时拦下并说明原因。"""
    action = str(payload.values.get("action") or "").strip()
    if not action:
        return ActionResult(ok=False, message="缺少 action 参数")
    entry, message = backfill_service.run_action(entry_id, action, payload.values)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
