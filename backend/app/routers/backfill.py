"""字段补数缺口端点：缺口查询、回填核算、任务启动、补数写入与重叠复核。

这些端点围绕 backfill_job 领域服务的字段补数缺口能力展开，
路径前缀 /api/backfill 固定，不会被 /api/{module} 资源路由抢占。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.services.backfill_job import BackfillJobService
from app.services.schema_evolution import SchemaEvolutionService

router = APIRouter(prefix="/api/backfill", tags=["字段补数缺口"])


class GapQueryPayload(BaseModel):
    湖表名: str | None = None
    字段名: str | None = None
    元数据版本ID: int | None = None
    字段版本: str | None = None
    起始日期: str | None = None
    结束日期: str | None = None
    缺口状态: str | None = None


class PlanPayload(BaseModel):
    湖表名: str
    元数据版本ID: int
    起始日期: str | None = None
    结束日期: str | None = None


class StartGapBackfillPayload(BaseModel):
    回填任务ID: int
    元数据版本ID: int
    起始日期: str
    结束日期: str


class FillGapPayload(BaseModel):
    回填任务ID: int
    元数据版本ID: int
    业务日期: str
    写入列值: str


class OverlapPayload(BaseModel):
    湖表名: str
    字段名: str
    起始日期: str | None = None
    结束日期: str | None = None


class FieldChangePayload(BaseModel):
    湖表名: str
    变更字段: str
    变更类型: str = "修改字段类型"
    允许列值: list[str] = Field(default_factory=list)
    回填起始日期: str
    回填结束日期: str
    变更前快照: str = ""


def _result(ok: bool, message: str, **extra: Any) -> dict[str, Any]:
    payload = {"ok": ok, "message": message}
    payload.update(extra)
    return payload


@router.post("/gaps/query")
def query_gaps(payload: GapQueryPayload) -> dict[str, Any]:
    """字段补数缺口查询结果：按湖表、字段版本和日期范围筛选待补部分。"""
    svc = BackfillJobService()
    gaps = svc.query_gaps(
        table_name=payload.湖表名,
        column_name=payload.字段名,
        version_id=payload.元数据版本ID,
        version_label=payload.字段版本,
        start_date=payload.起始日期,
        end_date=payload.结束日期,
        status=payload.缺口状态,
    )
    pending = sum(1 for gap in gaps if gap["缺口状态"] == "待补")
    review = sum(1 for gap in gaps if gap["缺口状态"] == "待复核")
    return _result(
        True,
        f"查询到 {len(gaps)} 条缺口，其中待补 {pending} 条、待复核 {review} 条",
        items=gaps,
        total=len(gaps),
        pending_total=pending,
        review_total=review,
    )


@router.post("/gaps/plan")
def plan_gap_backfill(payload: PlanPayload) -> dict[str, Any]:
    """Schema 演进用缺口查询结果核算下一次字段变更关联的回填输入。"""
    svc = BackfillJobService()
    plan, message = svc.plan_gap_backfill(
        table_name=payload.湖表名,
        version_id=payload.元数据版本ID,
        start_date=payload.起始日期,
        end_date=payload.结束日期,
    )
    if plan is None:
        return _result(False, message)
    return _result(True, message, plan=plan)


@router.post("/gaps/start")
def start_gap_backfill(payload: StartGapBackfillPayload) -> dict[str, Any]:
    """实际启动任务：使用查询选中的日期范围及允许写入的列值。"""
    svc = BackfillJobService()
    entry, message = svc.start_gap_backfill(
        job_id=payload.回填任务ID,
        version_id=payload.元数据版本ID,
        start_date=payload.起始日期,
        end_date=payload.结束日期,
    )
    return _result(entry is not None, message, entry=entry)


@router.post("/gaps/fill")
def fill_gap(payload: FillGapPayload) -> dict[str, Any]:
    """提交单日补数；重复提交已补完日期会被幂等拦截。"""
    svc = BackfillJobService()
    gap, message = svc.fill_gap(
        job_id=payload.回填任务ID,
        version_id=payload.元数据版本ID,
        business_date=payload.业务日期,
        column_value=payload.写入列值,
    )
    return _result(gap is not None, message, gap=gap)


@router.post("/gaps/overlap-review")
def overlap_review(payload: OverlapPayload) -> dict[str, Any]:
    """同列多版变更重叠日期：核对写入了哪些字段数据、哪些值仍待复核。"""
    svc = BackfillJobService()
    report = svc.review_overlapping_gaps(
        table_name=payload.湖表名,
        column_name=payload.字段名,
        start_date=payload.起始日期,
        end_date=payload.结束日期,
    )
    return _result(
        True,
        f"重叠日期 {report['重叠日期数']} 天，待复核值 {report['待复核值总数']} 个",
        report=report,
    )


@router.get("/fills")
def list_fills(
    湖表名: str | None = None,
    字段名: str | None = None,
    元数据版本ID: int | None = None,
) -> dict[str, Any]:
    """补数记录查询：每条缺口可追溯到元数据版本和回填任务。"""
    from app.store import store

    rows = list(store.rows("backfill_fill"))
    if 湖表名:
        rows = [r for r in rows if r["湖表名"] == 湖表名]
    if 字段名:
        rows = [r for r in rows if r["字段名"] == 字段名]
    if 元数据版本ID is not None:
        rows = [r for r in rows if r["元数据版本ID"] == 元数据版本ID]
    rows.sort(key=lambda r: (r["元数据版本ID"], r["业务日期"], r["id"]))
    return _result(True, f"补数记录 {len(rows)} 条", items=rows, total=len(rows))


@router.get("/field-versions")
def list_field_versions(湖表名: str, 字段名: str) -> dict[str, Any]:
    """同表同列的字段版本列表，供缺口查询筛选。"""
    svc = SchemaEvolutionService()
    versions = svc.list_field_versions(湖表名, 字段名)
    return _result(True, f"字段版本 {len(versions)} 个", items=versions, total=len(versions))


@router.post("/field-changes")
def create_field_change(payload: FieldChangePayload) -> dict[str, Any]:
    """登记一次带补数范围和允许列值的不兼容字段变更（草稿）。"""
    svc = SchemaEvolutionService()
    entry, message = svc.create_field_change(
        table_name=payload.湖表名,
        column_name=payload.变更字段,
        change_type=payload.变更类型,
        allowed_values=payload.允许列值,
        backfill_start=payload.回填起始日期,
        backfill_end=payload.回填结束日期,
        snapshot_ref=payload.变更前快照,
    )
    return _result(entry is not None, message, entry=entry)
