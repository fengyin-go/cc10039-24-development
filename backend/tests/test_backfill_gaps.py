"""字段补数缺口端到端测试。

覆盖题述场景：
1. 缺口可按湖表、字段版本、日期范围查询，每条缺口可追溯元数据版本和回填任务；
2. Schema 演进用缺口查询结果核算回填输入，启动任务使用选中日期范围和允许列值；
3. 同列两版变更后同一天都有缺口，新版本允许列值收窄；
4. 重复提交已补完日期只保留第一次完成的数据记录；
5. 重叠日期复核能看出写入了哪些字段数据、哪些值仍待复核。
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.store import store
from app.services.backfill_job import (
    BackfillJobService,
    FILL_TABLE,
    GAP_FILLED,
    GAP_PENDING,
    GAP_REVIEW,
    GAP_TABLE,
)
from app.services.schema_evolution import SchemaEvolutionService

TABLE = "订单湖表"
COLUMN = "会员等级"
START = "2026-10-01"
END = "2026-10-05"
# v1 允许四个等级；v2 收窄为三个等级，「钻石」不再被新版本接受
V1_VALUES = ["普通", "银卡", "金卡", "钻石"]
V2_VALUES = ["普通", "银卡", "金卡"]


@pytest.fixture()
def two_versions():
    """准备同列两版不兼容变更并执行，生成同日期范围的两版缺口。"""
    # store 是全局单例，先清理本用例使用的表/字段残留，保证版本从 v1 开始
    store.rows(GAP_TABLE).clear()
    store.rows(FILL_TABLE).clear()
    schema_rows = store.rows("schema_evolution")
    schema_rows[:] = [
        row for row in schema_rows
        if not (row.get("湖表名") == TABLE and row.get("变更字段") == COLUMN)
    ]

    schema_svc = SchemaEvolutionService()
    backfill_svc = BackfillJobService()

    v1, msg1 = schema_svc.create_field_change(
        table_name=TABLE,
        column_name=COLUMN,
        change_type="修改字段类型",
        allowed_values=V1_VALUES,
        backfill_start=START,
        backfill_end=END,
    )
    assert v1 is not None, msg1
    schema_svc.submit_approval(v1["id"])
    executed1, msg1 = schema_svc.execute_change(v1["id"])
    assert executed1 is not None, msg1
    assert executed1["字段版本"] == "v1"

    v2, msg2 = schema_svc.create_field_change(
        table_name=TABLE,
        column_name=COLUMN,
        change_type="修改字段类型",
        allowed_values=V2_VALUES,
        backfill_start=START,
        backfill_end=END,
    )
    assert v2 is not None, msg2
    schema_svc.submit_approval(v2["id"])
    executed2, msg2 = schema_svc.execute_change(v2["id"])
    assert executed2 is not None, msg2
    assert executed2["字段版本"] == "v2"

    yield {
        "v1_id": v1["id"],
        "v2_id": v2["id"],
        "backfill_svc": backfill_svc,
        "schema_svc": schema_svc,
    }


class TestGapRegistrationAndQuery:
    def test_two_versions_create_same_day_gaps(self, two_versions):
        svc = two_versions["backfill_svc"]
        gaps = svc.query_gaps(table_name=TABLE, column_name=COLUMN)
        # 两个版本各 5 天，共 10 条缺口
        assert len(gaps) == 10
        day_one = [g for g in gaps if g["业务日期"] == START]
        assert len(day_one) == 2
        labels = {g["字段版本"] for g in day_one}
        assert labels == {"v1", "v2"}
        # 新版本允许列值更少
        v2_gap = next(g for g in day_one if g["字段版本"] == "v2")
        assert v2_gap["允许列值"] == V2_VALUES
        v1_gap = next(g for g in day_one if g["字段版本"] == "v1")
        assert set(v1_gap["允许列值"]) == set(V1_VALUES)

    def test_query_filters_by_version_and_date_range(self, two_versions):
        svc = two_versions["backfill_svc"]
        v2_id = two_versions["v2_id"]
        gaps = svc.query_gaps(
            table_name=TABLE,
            version_id=v2_id,
            start_date="2026-10-03",
            end_date="2026-10-04",
            status=GAP_PENDING,
        )
        assert [g["业务日期"] for g in gaps] == ["2026-10-03", "2026-10-04"]
        assert all(g["元数据版本ID"] == v2_id for g in gaps)

    def test_gap_traces_metadata_version(self, two_versions):
        svc = two_versions["backfill_svc"]
        gaps = svc.query_gaps(table_name=TABLE, version_id=two_versions["v1_id"])
        assert gaps, "v1 缺口应存在"
        for gap in gaps:
            assert gap["元数据版本ID"] == two_versions["v1_id"]
            assert store.find("schema_evolution", gap["元数据版本ID"]) is not None

    def test_re_register_same_version_is_idempotent(self, two_versions):
        svc = two_versions["backfill_svc"]
        before = len(store.rows(GAP_TABLE))
        svc.register_gaps(
            table_name=TABLE,
            column_name=COLUMN,
            version_id=two_versions["v1_id"],
            version_label="v1",
            allowed_values=V1_VALUES,
            start_date=START,
            end_date=END,
        )
        after = len(store.rows(GAP_TABLE))
        assert before == after


class TestBackfillPlanAndStart:
    def _create_job(self, name: str) -> int:
        svc = BackfillJobService()
        entry, missing = svc.create_entry({
            "任务名称": name,
            "目标湖表": TABLE,
            "回填时间范围": f"{START}~{END}",
        })
        assert entry is not None, missing
        return entry["id"]

    def test_plan_counts_executable_ranges(self, two_versions):
        svc = two_versions["backfill_svc"]
        plan, msg = svc.plan_gap_backfill(
            table_name=TABLE,
            version_id=two_versions["v2_id"],
            start_date=START,
            end_date=END,
        )
        assert plan is not None, msg
        assert plan["待补缺口数"] == 5
        assert plan["允许列值"] == V2_VALUES
        assert plan["可执行范围"] == [{"start": START, "end": END}]

    def test_start_uses_selected_range_and_allowed_values(self, two_versions):
        svc = two_versions["backfill_svc"]
        job_id = self._create_job("两版缺口补数任务-v2")
        entry, msg = svc.start_gap_backfill(
            job_id=job_id,
            version_id=two_versions["v2_id"],
            start_date="2026-10-02",
            end_date="2026-10-04",
        )
        assert entry is not None, msg
        assert entry["status"] == "回填中"
        assert entry["允许列值"] == V2_VALUES
        assert entry["选中起始"] == "2026-10-02"
        # 启动后缺口可追溯到回填任务
        gaps = svc.query_gaps(
            table_name=TABLE,
            version_id=two_versions["v2_id"],
            start_date="2026-10-02",
            end_date="2026-10-04",
        )
        assert all(g["回填任务ID"] == job_id for g in gaps)
        # 范围外的缺口不关联任务
        outside = svc.query_gaps(
            table_name=TABLE,
            version_id=two_versions["v2_id"],
            start_date=START,
            end_date="2026-10-01",
        )
        assert all(g["回填任务ID"] is None for g in outside)

    def test_start_without_pending_gap_rejected(self, two_versions):
        svc = two_versions["backfill_svc"]
        job_id = self._create_job("空范围任务")
        entry, msg = svc.start_gap_backfill(
            job_id=job_id,
            version_id=two_versions["v2_id"],
            start_date="2026-09-01",
            end_date="2026-09-02",
        )
        assert entry is None
        assert "没有待补缺口" in msg


class TestFillIdempotencyAndReview:
    def test_duplicate_fill_keeps_first_record_only(self, two_versions):
        svc = two_versions["backfill_svc"]
        job_entry, _ = svc.create_entry({
            "任务名称": "幂等补数任务",
            "目标湖表": TABLE,
            "回填时间范围": f"{START}~{END}",
        })
        job_id = job_entry["id"]
        version_id = two_versions["v1_id"]
        svc.start_gap_backfill(
            job_id=job_id, version_id=version_id,
            start_date=START, end_date=START,
        )

        gap1, msg1 = svc.fill_gap(
            job_id=job_id, version_id=version_id,
            business_date=START, column_value="金卡",
        )
        assert gap1 is not None and gap1["缺口状态"] == GAP_FILLED
        first_fill = svc._find_fill(version_id, START)
        assert first_fill is not None and first_fill["写入列值"] == "金卡"

        # 重复提交不同值：必须被拦截，且不新增补数记录
        gap2, msg2 = svc.fill_gap(
            job_id=job_id, version_id=version_id,
            business_date=START, column_value="银卡",
        )
        assert gap2 is None
        assert "重复提交" in msg2
        fills = [
            f for f in store.rows(FILL_TABLE)
            if f["元数据版本ID"] == version_id and f["业务日期"] == START
        ]
        assert len(fills) == 1
        assert fills[0]["写入列值"] == "金卡"

    def test_value_outside_allowed_set_goes_to_review(self, two_versions):
        svc = two_versions["backfill_svc"]
        job_entry, _ = svc.create_entry({
            "任务名称": "收窄列值复核任务",
            "目标湖表": TABLE,
            "回填时间范围": f"{START}~{END}",
        })
        job_id = job_entry["id"]
        v2_id = two_versions["v2_id"]
        svc.start_gap_backfill(
            job_id=job_id, version_id=v2_id,
            start_date=START, end_date=START,
        )
        gap, msg = svc.fill_gap(
            job_id=job_id, version_id=v2_id,
            business_date=START, column_value="钻石",
        )
        assert gap is not None
        assert gap["缺口状态"] == GAP_REVIEW
        assert "待人工复核" in (gap["复核说明"] or "")
        # 留痕补数记录标记为未采用
        fill = svc._find_fill(v2_id, START)
        assert fill is not None and fill["是否采用"] is False

    def test_overlap_review_shows_written_and_pending_values(self, two_versions):
        svc = two_versions["backfill_svc"]
        v1_id = two_versions["v1_id"]
        v2_id = two_versions["v2_id"]

        job1, _ = svc.create_entry({
            "任务名称": "v1补数任务", "目标湖表": TABLE,
            "回填时间范围": f"{START}~{END}",
        })
        job2, _ = svc.create_entry({
            "任务名称": "v2补数任务", "目标湖表": TABLE,
            "回填时间范围": f"{START}~{END}",
        })
        svc.start_gap_backfill(job_id=job1["id"], version_id=v1_id,
                               start_date=START, end_date=END)
        svc.start_gap_backfill(job_id=job2["id"], version_id=v2_id,
                               start_date=START, end_date=END)

        # v1 接受「钻石」；v2 不再接受「钻石」，进入待复核
        svc.fill_gap(job_id=job1["id"], version_id=v1_id,
                     business_date=START, column_value="钻石")
        svc.fill_gap(job_id=job2["id"], version_id=v2_id,
                     business_date=START, column_value="钻石")
        # v2 次日写入合法值
        svc.fill_gap(job_id=job2["id"], version_id=v2_id,
                     business_date="2026-10-02", column_value="银卡")

        report = svc.review_overlapping_gaps(
            table_name=TABLE, column_name=COLUMN,
            start_date=START, end_date=END,
        )
        # 5 天每天都有两版缺口 → 5 个重叠日期
        assert report["重叠日期数"] == 5
        first_day = next(d for d in report["重叠日期明细"] if d["业务日期"] == START)
        assert "v1=钻石" in first_day["已写入字段数据"]
        assert "v2=钻石" in first_day["已写入字段数据"]
        assert "v2:钻石" in first_day["待复核值"]
        # v1 缺口已补齐，v2 缺口待复核
        statuses = {item["字段版本"]: item["缺口状态"] for item in first_day["版本缺口"]}
        assert statuses == {"v1": GAP_FILLED, "v2": GAP_REVIEW}
        # 每条版本缺口都带任务追溯
        assert all(item["回填任务ID"] is not None for item in first_day["版本缺口"])
        assert report["待复核值总数"] >= 1

    def test_fill_records_trace_version_and_job(self, two_versions):
        svc = two_versions["backfill_svc"]
        job_entry, _ = svc.create_entry({
            "任务名称": "追溯补数任务", "目标湖表": TABLE,
            "回填时间范围": f"{START}~{END}",
        })
        job_id = job_entry["id"]
        version_id = two_versions["v1_id"]
        svc.start_gap_backfill(job_id=job_id, version_id=version_id,
                               start_date=START, end_date=START)
        svc.fill_gap(job_id=job_id, version_id=version_id,
                     business_date=START, column_value="普通")
        fill = svc._find_fill(version_id, START)
        assert fill is not None
        assert fill["回填任务ID"] == job_id
        assert fill["元数据版本ID"] == version_id
        assert store.find("backfill_job", job_id) is not None
