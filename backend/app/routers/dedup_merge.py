"""采集去重归并接口：事件登记、批量归并、批次确认、去重明细数据集查询、血缘同步与迟到解释。

路径统一挂在 /api/dedup/merge 下（两段以上），避免与 /api/dedup/{entry_id} 的单段路由冲突。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.dedup_merge import DedupMergeService

router = APIRouter(prefix="/api/dedup/merge", tags=["采集去重归并"])

service = DedupMergeService()


@router.post("/events", response_model=ActionResult)
def register_events(payload: EntryPayload) -> ActionResult:
    """批量登记输入事件；迟到事件（事件时间落入已确认批次范围）打标记但不拒收。"""
    strategy_no = str(payload.values.get("策略编号") or "").strip()
    events = payload.values.get("事件列表")
    if not strategy_no:
        return ActionResult(ok=False, message="缺少必填字段：策略编号")
    if not isinstance(events, list) or not events:
        return ActionResult(ok=False, message="缺少必填字段：事件列表（非空数组）")
    created, errors = service.register_events(strategy_no, events)
    if not created and errors:
        return ActionResult(ok=False, message="；".join(errors))
    message = f"已登记 {len(created)} 条事件"
    if errors:
        message += f"，{len(errors)} 条被跳过：{'；'.join(errors)}"
    late = sum(1 for row in created if row.get("迟到标记"))
    if late:
        message += f"；其中 {late} 条为迟到事件，将归入后续新批次"
    return ActionResult(ok=True, message=message, entry={"items": created})


@router.get("/events", response_model=PageResult[dict])
def list_events(
    strategy_no: str | None = Query(default=None, description="按策略编号过滤"),
    merge_status: str | None = Query(default=None, description="待归并、已归并"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """输入事件列表，可按策略与归并状态过滤。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    rows = service.list_events(strategy_no=strategy_no, merge_status=merge_status)
    start = max(page - 1, 0) * size
    return PageResult(items=rows[start:start + size], total=len(rows), page=page, size=size)


@router.post("/run", response_model=ActionResult)
def run_merge(payload: EntryPayload) -> ActionResult:
    """按策略的去重键与有效时间范围批量归并待归并事件，取舍逐条写进去重明细数据集。"""
    strategy_no = str(payload.values.get("策略编号") or "").strip()
    if not strategy_no:
        return ActionResult(ok=False, message="缺少必填字段：策略编号")
    batch, message = service.run_merge(strategy_no, payload.values.get("范围开始"), payload.values.get("范围结束"))
    if batch is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=batch)


@router.get("/batches", response_model=PageResult[dict])
def list_batches(
    strategy_no: str | None = Query(default=None, description="按策略编号过滤"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """归并批次列表，含版本快照与确认状态。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    rows = service.list_batches(strategy_no=strategy_no)
    start = max(page - 1, 0) * size
    return PageResult(items=rows[start:start + size], total=len(rows), page=page, size=size)


@router.post("/batches/{batch_id}/confirm", response_model=ActionResult)
def confirm_batch(batch_id: int) -> ActionResult:
    """确认批次：冻结该版本的取舍结果；确认后迟到事件只能进入后续新批次。"""
    batch, message = service.confirm_batch(batch_id)
    if batch is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=batch)


@router.get("/details", response_model=PageResult[dict])
def list_details(
    strategy_no: str | None = Query(default=None, description="按策略编号过滤"),
    batch_no: str | None = Query(default=None, description="按批次编号过滤"),
    version: int | None = Query(default=None, description="按策略版本过滤"),
    role: str | None = Query(default=None, description="保留、被归并"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """去重明细数据集：整组的保留记录与被归并事件逐条可查，含窗口与排序规则快照。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    rows = service.list_details(strategy_no=strategy_no, batch_no=batch_no, version=version, role=role)
    start = max(page - 1, 0) * size
    return PageResult(items=rows[start:start + size], total=len(rows), page=page, size=size)


@router.post("/lineage", response_model=ActionResult)
def sync_lineage(payload: EntryPayload) -> ActionResult:
    """根据去重明细数据集写出 被归并事件→保留记录 的血缘关系；重复同步自动跳过。"""
    strategy_no = str(payload.values.get("策略编号") or "").strip() or None
    created, message = service.sync_lineage(strategy_no=strategy_no)
    return ActionResult(ok=True, message=message, entry={"items": created})


@router.get("/explain/{event_no}", response_model=dict)
def explain_event(event_no: str) -> dict:
    """解释一条输入事件的归并去向：归属版本、角色、实际生效的保留记录；迟到事件说明归到哪一版。"""
    result = service.explain_event(event_no)
    if result is None:
        raise HTTPException(status_code=404, detail=f"事件 {event_no} 不存在")
    return result


@router.get("/stats", response_model=dict)
def stats(strategy_no: str = Query(description="策略编号")) -> dict:
    """按版本对账：已确认版本（报告引用口径）与当前口径的保留记录差异。"""
    result = service.stats(strategy_no)
    if result is None:
        raise HTTPException(status_code=404, detail=f"去重策略 {strategy_no} 不存在")
    return result
