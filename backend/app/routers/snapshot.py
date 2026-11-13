"""快照管理接口：维护快照记录，覆盖创建、恢复校验、清理、归档全流程。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.snapshot import STATUS_ORDER, snapshot_service

router = APIRouter(prefix="/api/snapshot", tags=["快照管理"])

LIST_FIELDS = ["快照编号", "表名称", "快照时间", "快照大小", "保留天数", "恢复校验", "快照状态", "创建批次"]
STATUSES = STATUS_ORDER


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按快照编号检索"),
    status: str | None = Query(default=None, description="草稿、运行中、已完成、已清理"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按快照编号与状态过滤快照管理列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = snapshot_service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/recovery")
def recovery_evidence(
    keyword: str | None = Query(default=None, description="按快照编号检索校验记录"),
) -> dict[str, Any]:
    """恢复校验结果：每次校验都留痕，标记当前可作为回滚依据的那条通过结论。"""
    items = snapshot_service.recovery_evidence(keyword=keyword)
    return {"module": "snapshot", "total": len(items), "items": items}


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出快照管理清单：返回当前过滤条件下的全量数据。"""
    items, total = snapshot_service.list_entries(page=1, size=10000)
    return {"module": "snapshot", "total": total, "items": items}


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条快照记录明细；不存在时给出可读的错误说明。"""
    entry = snapshot_service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"快照记录 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条快照记录。

    同一快照编号重复提交按幂等处理：返回同一份完整记录，已写入的元数据不会
    重复提交；若上次停在存储校验失败，则只续跑存储校验。
    """
    entry, missing, created = snapshot_service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    message = "快照记录已登记，存储校验通过，等待开始恢复校验" if created else "快照编号已存在，按幂等返回原有完整记录"
    if entry is not None and entry.get("存储校验") == "失败":
        message = "存储校验失败，状态保持草稿；请修复存储后用同一快照编号重新提交以续跑"
    return ActionResult(ok=True, message=message, entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条快照执行状态机动作；不允许的动作或违反引用保护时返回失败原因。"""
    action = str(payload.values.get("action") or "").strip()
    if not action:
        return ActionResult(ok=False, message="缺少 action 参数")
    entry, message = snapshot_service.run_action(entry_id, action, payload.values)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
