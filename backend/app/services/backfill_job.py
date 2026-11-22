"""数据回填任务领域服务：管理历史数据回填、补数和断点续做。

支持回填检查点（checkpoint）：中断后可从上次位置继续，不会重复处理已完成的记录。
上游接收 Schema 演进的不兼容变更事件，下游联动治理报告记录回填结果。

字段补数缺口（backfill_gap）：
    检查点只能给出已处理条数，无法回答“某一列在哪些日期还缺数据”。
    每次 Schema 演进对同一字段产生一个元数据版本，版本带允许写入的列值；
    版本在生效日期范围内按天生成缺口。缺口可按湖表、字段版本、日期范围检索，
    每条缺口都能追到元数据版本（schema_evolution 记录）和回填任务。

补数记录（backfill_fill）：
    针对某版本某日期的第一次成功写入会保留为补数记录；重复提交已经补完的
    日期时只保留第一次完成的数据记录，后续提交被幂等拦截。
"""
from __future__ import annotations

from typing import Any

from app.services.base import BaseModuleService
from app.services.date_utils import daterange, merge_ranges
from app.store import store

# 缺口与补数记录存放的内存表名
GAP_TABLE = "backfill_gap"
FILL_TABLE = "backfill_fill"

# 缺口生命周期：待补 → 已补齐；写入值不被新版本接受时进入待复核
GAP_PENDING = "待补"
GAP_FILLED = "已补齐"
GAP_REVIEW = "待复核"


class BackfillJobService(BaseModuleService):
    MODULE = "backfill_job"
    ENTITY = "回填任务"
    OPERATION_TERM = "调度"
    LIST_FIELDS = ["任务名称", "目标湖表", "回填时间范围", "处理记录数", "执行状态"]
    REQUIRED_FIELDS = ["任务名称", "目标湖表", "回填时间范围"]
    STATUS_ORDER = ["待调度", "回填中", "已完成", "回填失败"]
    ACTION_RULES = {"启动回填": "回填中", "取消回填": "回填失败"}
    NEGATIVE_ACTIONS = []

    # 回填检查点：任务 id → 已处理记录偏移
    _checkpoints: dict[int, int] = {}

    # ------------------------------------------------------------------
    # 字段补数缺口登记
    # ------------------------------------------------------------------

    def register_gaps(
        self,
        *,
        table_name: str,
        column_name: str,
        version_id: int,
        version_label: str,
        allowed_values: list[str],
        start_date: str,
        end_date: str,
    ) -> tuple[list[dict[str, Any]] | None, str]:
        """按元数据版本在日期范围内逐日登记字段补数缺口。

        同一版本同一天只保留一条缺口（重复登记幂等）；不同版本即使同一天
        也各自保留一条，用于表达同列两版变更在同一日并存的缺口。
        """
        table_name = (table_name or "").strip()
        column_name = (column_name or "").strip()
        if not table_name or not column_name:
            return None, "登记缺口需要提供湖表名和变更字段"
        if not allowed_values:
            return None, "登记缺口需要提供该版本允许写入的列值"
        try:
            dates = daterange(start_date, end_date)
        except ValueError as exc:
            return None, str(exc)

        rows = store.rows(GAP_TABLE)
        registered: list[dict[str, Any]] = []
        skipped = 0
        for day in dates:
            existed = self._find_gap(version_id, day)
            if existed is not None:
                skipped += 1
                registered.append(existed)
                continue
            gap = {
                "id": self._next_id(GAP_TABLE),
                "湖表名": table_name,
                "字段名": column_name,
                "元数据版本ID": version_id,
                "字段版本": version_label,
                "业务日期": day,
                "允许列值": list(allowed_values),
                "缺口状态": GAP_PENDING,
                "回填任务ID": None,
                "已写入列值": None,
                "复核说明": None,
            }
            rows.append(gap)
            registered.append(gap)

        store.log_event("backfill_gap_registered", "schema_evolution", "backfill_job", {
            "table_name": table_name,
            "column_name": column_name,
            "version_id": version_id,
            "version_label": version_label,
            "date_count": len(dates),
            "new_count": len(dates) - skipped,
            "skipped_duplicate": skipped,
        })
        return registered, (
            f"版本 {version_label} 登记缺口 {len(dates)} 天，"
            f"新增 {len(dates) - skipped} 条，重复跳过 {skipped} 条"
        )

    def query_gaps(
        self,
        *,
        table_name: str | None = None,
        column_name: str | None = None,
        version_id: int | None = None,
        version_label: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        """字段补数缺口查询：按湖表、字段版本、日期范围筛选待补部分。"""
        gaps = list(store.rows(GAP_TABLE))
        if table_name:
            gaps = [g for g in gaps if g["湖表名"] == table_name.strip()]
        if column_name:
            gaps = [g for g in gaps if g["字段名"] == column_name.strip()]
        if version_id is not None:
            gaps = [g for g in gaps if g["元数据版本ID"] == version_id]
        if version_label:
            gaps = [g for g in gaps if g["字段版本"] == version_label.strip()]
        if start_date:
            gaps = [g for g in gaps if g["业务日期"] >= start_date.strip()]
        if end_date:
            gaps = [g for g in gaps if g["业务日期"] <= end_date.strip()]
        if status:
            gaps = [g for g in gaps if g["缺口状态"] == status.strip()]
        return sorted(gaps, key=lambda g: (g["元数据版本ID"], g["业务日期"], g["id"]))

    def plan_gap_backfill(
        self,
        *,
        table_name: str,
        version_id: int,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> tuple[dict[str, Any] | None, str]:
        """Schema 演进核算下一次字段变更关联的回填输入。

        基于缺口查询结果，在选定日期范围内只挑仍为「待补」的缺口，
        返回可继续执行的日期范围与允许写入列值，供实际启动任务使用。
        """
        version = store.find("schema_evolution", version_id)
        if version is None:
            return None, f"元数据版本 {version_id} 不存在"
        if table_name and version.get("湖表名") != table_name.strip():
            return None, (
                f"元数据版本 {version_id} 属于湖表 {version.get('湖表名')}，"
                f"与筛选湖表 {table_name} 不一致"
            )

        gaps = self.query_gaps(
            table_name=version.get("湖表名", ""),
            version_id=version_id,
            start_date=start_date,
            end_date=end_date,
        )
        pending = [g for g in gaps if g["缺口状态"] == GAP_PENDING]
        pending_dates = sorted(g["业务日期"] for g in pending)
        allowed_values = list(version.get("允许列值", []) or [])
        already_filled = len(gaps) - len(pending)

        plan = {
            "湖表名": version.get("湖表名"),
            "字段名": version.get("变更字段"),
            "元数据版本ID": version_id,
            "字段版本": version.get("字段版本"),
            "查询起始": start_date,
            "查询结束": end_date,
            "允许列值": allowed_values,
            "缺口总数": len(gaps),
            "待补缺口数": len(pending),
            "已补齐缺口数": already_filled,
            "待补日期": pending_dates,
            "可执行范围": merge_ranges(pending_dates),
        }
        store.log_event("backfill_gap_planned", "schema_evolution", "backfill_job", {
            "version_id": version_id,
            "pending_dates": len(pending_dates),
            "start_date": start_date,
            "end_date": end_date,
        })
        return plan, (
            f"版本 {plan['字段版本']} 在所选范围内有 {len(pending)} 天可继续补数，"
            f"已补齐 {already_filled} 天"
        )

    def start_gap_backfill(
        self,
        *,
        job_id: int,
        version_id: int,
        start_date: str,
        end_date: str,
    ) -> tuple[dict[str, Any] | None, str]:
        """实际启动任务：使用查询选中的日期范围及该版本允许写入的列值。

        任务进入回填中，并把选中范围、版本和允许列值登记到任务上，
        同时在待补缺口上预关联回填任务，保证每条缺口可追到任务。
        """
        entry = store.find(self.MODULE, job_id)
        if entry is None:
            return None, "回填任务不存在"
        if entry.get("status") != "待调度":
            return None, f"回填任务状态为{entry.get('status')}，不可启动"
        version = store.find("schema_evolution", version_id)
        if version is None:
            return None, f"元数据版本 {version_id} 不存在"

        table_name = version.get("湖表名", "")
        if entry.get("目标湖表") and entry.get("目标湖表") != table_name:
            return None, (
                f"回填任务目标湖表 {entry.get('目标湖表')} 与版本所属湖表 "
                f"{table_name} 不一致"
            )

        gaps = self.query_gaps(
            table_name=table_name,
            version_id=version_id,
            start_date=start_date,
            end_date=end_date,
            status=GAP_PENDING,
        )
        if not gaps:
            return None, "选中日期范围内没有待补缺口，无需启动回填"

        allowed_values = list(version.get("允许列值", []) or [])
        entry["status"] = "回填中"
        entry["pending"] = True
        entry["abnormal"] = False
        entry["关联版本ID"] = version_id
        entry["字段版本"] = version.get("字段版本")
        entry["允许列值"] = allowed_values
        entry["选中起始"] = start_date
        entry["选中结束"] = end_date
        entry["处理记录数"] = "0"
        self._checkpoints[job_id] = 0

        for gap in gaps:
            gap["回填任务ID"] = job_id

        store.log_event("backfill_gap_started", "backfill_job", "backfill_job", {
            "task_id": job_id,
            "version_id": version_id,
            "table_name": table_name,
            "start_date": start_date,
            "end_date": end_date,
            "gap_count": len(gaps),
            "allowed_values": allowed_values,
        })
        return entry, (
            f"回填任务 {entry.get('任务名称')} 已启动：版本 {version.get('字段版本')}，"
            f"范围 {start_date}~{end_date}，覆盖 {len(gaps)} 个待补缺口，"
            f"允许列值={ '、'.join(allowed_values) }"
        )

    def fill_gap(
        self,
        *,
        job_id: int,
        version_id: int,
        business_date: str,
        column_value: str,
    ) -> tuple[dict[str, Any] | None, str]:
        """提交某版本某日的补数写入。

        - 已补齐的日期重复提交：幂等拦截，只保留第一次完成的数据记录；
        - 列值不在该版本允许列值内：缺口进入待复核，值仍留痕等待人工复核；
        - 正常写入：缺口标记已补齐并生成补数记录。
        """
        entry = store.find(self.MODULE, job_id)
        if entry is None:
            return None, "回填任务不存在"
        if entry.get("status") != "回填中":
            return None, f"回填任务状态为{entry.get('status')}，不可写入补数"
        gap = self._find_gap(version_id, business_date)
        if gap is None:
            return None, (
                f"版本 {version_id} 在 {business_date} 没有登记缺口，"
                "无法提交补数"
            )
        if entry.get("目标湖表") and entry.get("目标湖表") != gap["湖表名"]:
            return None, "回填任务与缺口所属湖表不一致"

        # 幂等：该版本该日期已补齐，只保留第一次完成的数据记录
        existing = self._find_fill(version_id, business_date)
        if existing is not None and gap["缺口状态"] == GAP_FILLED:
            store.log_event("backfill_fill_deduplicated", "backfill_job", "backfill_job", {
                "task_id": job_id,
                "version_id": version_id,
                "business_date": business_date,
                "kept_fill_id": existing["id"],
            })
            return None, (
                f"{business_date} 的缺口已在补数记录 #{existing['id']} 完成，"
                "重复提交已忽略，仅保留第一次数据"
            )

        allowed_values = list(gap.get("允许列值", []) or [])
        fill_record = {
            "id": self._next_id(FILL_TABLE),
            "湖表名": gap["湖表名"],
            "字段名": gap["字段名"],
            "元数据版本ID": version_id,
            "字段版本": gap["字段版本"],
            "业务日期": business_date,
            "回填任务ID": job_id,
            "写入列值": column_value,
            "是否采用": True,
            "记录说明": None,
        }

        if column_value not in allowed_values:
            # 新版本允许列值收窄：旧值不再被接受，留痕并转待复核
            gap["缺口状态"] = GAP_REVIEW
            gap["回填任务ID"] = job_id
            gap["已写入列值"] = column_value
            gap["复核说明"] = (
                f"写入值「{column_value}」不在版本 {gap['字段版本']} 允许列值"
                f"（{'、'.join(allowed_values)}）内，待人工复核"
            )
            fill_record["是否采用"] = False
            fill_record["记录说明"] = "列值超出版本允许范围，待复核"
            store.rows(FILL_TABLE).append(fill_record)
            store.log_event("backfill_gap_review", "backfill_job", "quality_monitor", {
                "task_id": job_id,
                "gap_id": gap["id"],
                "version_id": version_id,
                "business_date": business_date,
                "column_value": column_value,
            })
            return gap, (
                f"{business_date} 写入值「{column_value}」不被版本 {gap['字段版本']} "
                f"允许，缺口转待复核（补数记录 #{fill_record['id']} 已留痕）"
            )

        gap["缺口状态"] = GAP_FILLED
        gap["回填任务ID"] = job_id
        gap["已写入列值"] = column_value
        gap["复核说明"] = None
        store.rows(FILL_TABLE).append(fill_record)

        checkpoint = self._checkpoints.get(job_id, 0) + 1
        self._checkpoints[job_id] = checkpoint
        entry["处理记录数"] = str(checkpoint)

        store.log_event("backfill_gap_filled", "backfill_job", "backfill_job", {
            "task_id": job_id,
            "gap_id": gap["id"],
            "fill_id": fill_record["id"],
            "version_id": version_id,
            "business_date": business_date,
            "column_value": column_value,
        })
        return gap, (
            f"{business_date} 补数完成（补数记录 #{fill_record['id']}），"
            f"写入值「{column_value}」"
        )

    def review_overlapping_gaps(
        self,
        *,
        table_name: str,
        column_name: str,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> dict[str, Any]:
        """同列多版变更且同一天都有缺口时，检查重叠日期的写入情况。

        返回每个重叠日期下各版本缺口的状态、已写入字段数据和仍待复核的值，
        用于安排补数后核对哪些字段数据已写入、哪些值仍待复核。
        """
        gaps = self.query_gaps(
            table_name=table_name,
            column_name=column_name,
            start_date=start_date,
            end_date=end_date,
        )
        by_date: dict[str, list[dict[str, Any]]] = {}
        for gap in gaps:
            by_date.setdefault(gap["业务日期"], []).append(gap)

        overlap_days: list[dict[str, Any]] = []
        for day in sorted(day for day, items in by_date.items() if len(items) > 1):
            version_gaps = []
            pending_review_values: list[str] = []
            written_values: list[str] = []
            for gap in sorted(by_date[day], key=lambda g: g["元数据版本ID"]):
                version_gaps.append({
                    "缺口ID": gap["id"],
                    "元数据版本ID": gap["元数据版本ID"],
                    "字段版本": gap["字段版本"],
                    "允许列值": gap["允许列值"],
                    "缺口状态": gap["缺口状态"],
                    "回填任务ID": gap["回填任务ID"],
                    "已写入列值": gap["已写入列值"],
                    "复核说明": gap["复核说明"],
                })
                if gap["缺口状态"] == GAP_REVIEW:
                    pending_review_values.append(
                        f"{gap['字段版本']}:{gap['已写入列值']}"
                    )
                if gap["已写入列值"] is not None:
                    written_values.append(
                        f"{gap['字段版本']}={gap['已写入列值']}"
                    )
            overlap_days.append({
                "业务日期": day,
                "版本缺口": version_gaps,
                "已写入字段数据": written_values,
                "待复核值": pending_review_values,
            })

        fills = [
            f for f in store.rows(FILL_TABLE)
            if f["湖表名"] == table_name and f["字段名"] == column_name
            and (start_date is None or f["业务日期"] >= start_date)
            and (end_date is None or f["业务日期"] <= end_date)
        ]
        return {
            "湖表名": table_name,
            "字段名": column_name,
            "重叠日期数": len(overlap_days),
            "重叠日期明细": overlap_days,
            "补数记录数": len(fills),
            "待复核值总数": sum(1 for f in fills if not f["是否采用"]),
        }

    # ------------------------------------------------------------------
    # 回填任务检查点流程
    # ------------------------------------------------------------------

    def start_backfill(self, entry_id: int) -> tuple[dict[str, Any] | None, str]:
        """启动回填任务：检查 Schema 变更状态后启动。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, "回填任务不存在"
        if entry.get("status") != "待调度":
            return None, f"回填任务状态为{entry.get('status')}，不可启动"

        table_name = entry.get("目标湖表", "")
        time_range = entry.get("回填时间范围", "")

        # 检查同湖表是否有执行中的回填
        active_backfills = [
            bj for bj in store.rows(self.MODULE)
            if bj.get("目标湖表") == table_name
            and bj.get("status") == "回填中"
            and bj.get("id") != entry_id
        ]
        if active_backfills:
            return None, f"湖表 {table_name} 已有回填任务在执行中"

        # 初始化检查点
        checkpoint = self._checkpoints.get(entry_id, 0)

        entry["status"] = "回填中"
        entry["pending"] = True
        entry["abnormal"] = False
        entry["处理记录数"] = str(checkpoint)

        store.log_event("backfill_started", "backfill_job", "backfill_job", {
            "task_id": entry_id,
            "table_name": table_name,
            "time_range": time_range,
            "checkpoint": checkpoint,
        })

        return entry, f"回填任务 {entry.get('任务名称')} 已启动，时间范围={time_range}，检查点从 {checkpoint} 开始"

    def checkpoint_backfill(self, entry_id: int, processed_count: int) -> tuple[dict[str, Any] | None, str]:
        """更新回填检查点，支持断点续做。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, "回填任务不存在"
        if entry.get("status") != "回填中":
            return None, f"回填任务状态为{entry.get('status')}，不可保存检查点"

        self._checkpoints[entry_id] = processed_count
        entry["处理记录数"] = str(processed_count)

        store.log_event("backfill_checkpoint", "backfill_job", "backfill_job", {
            "task_id": entry_id,
            "checkpoint": processed_count,
        })

        return entry, f"回填任务检查点已更新：已处理 {processed_count} 条记录"

    def complete_backfill(self, entry_id: int, total_records: int) -> tuple[dict[str, Any] | None, str]:
        """完成回填任务，通知治理报告。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, "回填任务不存在"
        if entry.get("status") != "回填中":
            return None, f"回填任务状态为{entry.get('status')}，不可完成"

        table_name = entry.get("目标湖表", "")

        entry["status"] = "已完成"
        entry["pending"] = False
        entry["abnormal"] = False
        entry["处理记录数"] = str(total_records)

        # 清理检查点
        self._checkpoints.pop(entry_id, None)

        # 通知治理报告
        store.log_event("backfill_completed_to_govern", "backfill_job", "govern_report", {
            "task_id": entry_id,
            "table_name": table_name,
            "total_records": total_records,
        })

        return entry, f"回填任务 {entry.get('任务名称')} 已完成，总计 {total_records} 条记录"

    def fail_backfill(self, entry_id: int) -> tuple[dict[str, Any] | None, str]:
        """回填失败：保留检查点以便恢复。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, "回填任务不存在"
        if entry.get("status") != "回填中":
            return None, f"回填任务状态为{entry.get('status')}，不可标记失败"

        checkpoint = self._checkpoints.get(entry_id, 0)

        entry["status"] = "回填失败"
        entry["pending"] = False
        entry["abnormal"] = True

        store.log_event("backfill_failed", "backfill_job", "retention_mgr", {
            "task_id": entry_id,
            "failed_at_checkpoint": checkpoint,
        })

        return entry, f"回填任务失败，检查点保留在 {checkpoint} 条，可从该位置恢复"

    def get_checkpoint(self, entry_id: int) -> int | None:
        """获取回填检查点。"""
        return self._checkpoints.get(entry_id)

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        if action == "启动回填":
            return self.start_backfill(entry_id)
        return super().run_action(entry_id, action)

    # ------------------------------------------------------------------
    # 内部辅助
    # ------------------------------------------------------------------

    def _find_gap(self, version_id: int, business_date: str) -> dict[str, Any] | None:
        for gap in store.rows(GAP_TABLE):
            if gap["元数据版本ID"] == version_id and gap["业务日期"] == business_date:
                return gap
        return None

    def _find_fill(self, version_id: int, business_date: str) -> dict[str, Any] | None:
        for fill in store.rows(FILL_TABLE):
            if fill["元数据版本ID"] == version_id and fill["业务日期"] == business_date:
                return fill
        return None

    @staticmethod
    def _next_id(table: str) -> int:
        rows = store.rows(table)
        return max((int(row.get("id", 0)) for row in rows), default=0) + 1
