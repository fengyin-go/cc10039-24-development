"""Schema 演进领域服务：管理湖表 Schema 的可控演进、兼容性检查和回滚。

上游依赖快照版本模块提供变更前快照，下游联动回填任务进行存量数据迁移。
兼容性检查是核心：变更类型与兼容级别必须匹配，不兼容变更需要回填。

字段版本化：
    同一湖表同一字段每次成功执行的不兼容变更都会分配一个递增字段版本号，
    版本携带「允许写入的列值」和需要补数的日期范围。执行变更时把这些信息
    交给回填服务登记字段补数缺口，作为下一次字段变更关联的回填输入。
"""
from __future__ import annotations

from typing import Any

from app.services.base import BaseModuleService
from app.services.backfill_job import BackfillJobService
from app.services.date_utils import daterange
from app.store import store


# 合法兼容级别及其允许的变更类型
COMPATIBILITY_RULES: dict[str, set[str]] = {
    "向后兼容": {"添加字段", "修改默认值", "添加索引"},
    "向前兼容": {"删除字段", "放宽约束"},
    "不兼容": {"修改字段类型", "重命名字段", "删除字段"},
}


class SchemaEvolutionService(BaseModuleService):
    MODULE = "schema_evolution"
    ENTITY = "Schema变更"
    OPERATION_TERM = "校验"
    LIST_FIELDS = ["湖表名", "变更类型", "变更字段", "兼容级别", "变更前快照"]
    REQUIRED_FIELDS = ["湖表名", "变更类型", "变更字段"]
    STATUS_ORDER = ["草稿", "待审批", "已执行", "已回滚"]
    ACTION_RULES = {"提交审批": "待审批", "执行变更": "已执行"}
    NEGATIVE_ACTIONS = []

    def create_field_change(
        self,
        *,
        table_name: str,
        column_name: str,
        change_type: str,
        allowed_values: list[str],
        backfill_start: str,
        backfill_end: str,
        snapshot_ref: str = "",
    ) -> tuple[dict[str, Any] | None, str]:
        """登记一次带字段版本补数信息的不兼容 Schema 变更。"""
        table_name = (table_name or "").strip()
        column_name = (column_name or "").strip()
        if not table_name or not column_name:
            return None, "湖表名和变更字段不能为空"
        if not allowed_values:
            return None, "不兼容变更必须声明新版本允许写入的列值"
        try:
            daterange(backfill_start, backfill_end)
        except ValueError as exc:
            return None, str(exc)

        entry, missing = self.create_entry({
            "湖表名": table_name,
            "变更类型": change_type or "修改字段类型",
            "变更字段": column_name,
            "兼容级别": "不兼容",
            "变更前快照": snapshot_ref,
        })
        if entry is None:
            return None, f"缺少必填字段：{'、'.join(missing)}"
        entry["允许列值"] = list(allowed_values)
        entry["回填起始日期"] = backfill_start
        entry["回填结束日期"] = backfill_end
        entry["字段版本"] = None
        return entry, (
            f"Schema 变更草稿已登记：{table_name}.{column_name}，"
            f"补数范围 {backfill_start}~{backfill_end}"
        )

    def submit_approval(self, entry_id: int) -> tuple[dict[str, Any] | None, str]:
        """提交审批：执行兼容性检查。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, "Schema 变更不存在"
        if entry.get("status") != "草稿":
            return None, f"Schema 变更状态为{entry.get('status')}，不可提交审批"

        change_type = entry.get("变更类型", "")
        compat_level = entry.get("兼容级别", "")

        # 兼容性检查
        allowed = COMPATIBILITY_RULES.get(compat_level, set())
        if change_type not in allowed:
            return (
                None,
                f"兼容性检查失败：{compat_level} 不允许 {change_type}，"
                f"允许的操作：{'、'.join(allowed) if allowed else '无'}",
            )

        # 检查变更前快照是否可用
        snapshot_ref = entry.get("变更前快照", "")
        if snapshot_ref:
            snapshots = store.find_by_field("snapshot_version", "版本号", snapshot_ref)
            available = [s for s in snapshots if s.get("status") == "可用"]
            if not available:
                return None, f"变更前快照 {snapshot_ref} 不可用"

        entry["status"] = "待审批"
        entry["pending"] = True
        entry["abnormal"] = False

        store.log_event("schema_submitted", "schema_evolution", "snapshot_version", {
            "entry_id": entry_id,
            "table_name": entry.get("湖表名", ""),
            "change_type": change_type,
            "compat_level": compat_level,
        })

        return entry, f"Schema 变更 {entry.get('湖表名')} - {change_type} 已提交审批，兼容级别={compat_level}"

    def execute_change(self, entry_id: int) -> tuple[dict[str, Any] | None, str]:
        """执行 Schema 变更：联动回填任务进行存量数据迁移。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, "Schema 变更不存在"
        if entry.get("status") != "待审批":
            return None, f"Schema 变更状态为{entry.get('status')}，不可执行"

        table_name = entry.get("湖表名", "")
        change_type = entry.get("变更类型", "")
        compat_level = entry.get("兼容级别", "")
        column_name = entry.get("变更字段", "")

        # 自身状态流转
        entry["status"] = "已执行"
        entry["pending"] = False
        entry["abnormal"] = False

        gap_message = ""
        # 如果是不兼容变更，触发回填任务
        if compat_level == "不兼容":
            backfill_jobs = store.find_by_field("backfill_job", "目标湖表", table_name)
            if backfill_jobs:
                for bj in backfill_jobs:
                    if bj.get("status") == "待调度":
                        bj["pending"] = True

            # 带补数范围的不兼容变更：分配字段版本并登记字段补数缺口
            start_date = entry.get("回填起始日期")
            end_date = entry.get("回填结束日期")
            allowed_values = list(entry.get("允许列值", []) or [])
            if start_date and end_date and allowed_values:
                version_label = self._next_field_version(table_name, column_name)
                entry["字段版本"] = version_label
                backfill_svc = BackfillJobService()
                _, gap_message = backfill_svc.register_gaps(
                    table_name=table_name,
                    column_name=column_name,
                    version_id=entry_id,
                    version_label=version_label,
                    allowed_values=allowed_values,
                    start_date=start_date,
                    end_date=end_date,
                )

            store.log_event("schema_to_backfill", "schema_evolution", "backfill_job", {
                "entry_id": entry_id,
                "table_name": table_name,
                "change_type": change_type,
                "compat_level": compat_level,
                "field_version": entry.get("字段版本"),
                "affected_jobs": len(backfill_jobs),
            })

        store.log_event("schema_executed", "schema_evolution", "lake_table", {
            "entry_id": entry_id,
            "table_name": table_name,
            "change_type": change_type,
        })

        message = f"Schema 变更 {table_name} - {change_type} 已执行"
        if gap_message:
            message = f"{message}；{gap_message}"
        return entry, message

    def rollback_change(self, entry_id: int) -> tuple[dict[str, Any] | None, str]:
        """回滚 Schema 变更：恢复到变更前快照。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, "Schema 变更不存在"
        if entry.get("status") != "已执行":
            return None, f"Schema 变更状态为{entry.get('status')}，不可回滚"

        snapshot_ref = entry.get("变更前快照", "")
        table_name = entry.get("湖表名", "")

        # 尝试恢复快照
        if snapshot_ref:
            # 查找该类快照
            for key in store.snapshot_keys():
                if snapshot_ref in key and table_name in key:
                    entry["status"] = "已回滚"
                    entry["pending"] = False
                    entry["abnormal"] = False
                    store.log_event("schema_rolled_back", "schema_evolution", "snapshot_version", {
                        "entry_id": entry_id,
                        "table_name": table_name,
                        "snapshot_ref": snapshot_ref,
                    })
                    return entry, f"Schema 变更回滚到快照 {snapshot_ref}"

        entry["status"] = "已回滚"
        entry["pending"] = False
        entry["abnormal"] = True
        return entry, f"Schema 变更已回滚（快照 {snapshot_ref} 无法精确恢复）"

    def list_field_versions(self, table_name: str, column_name: str) -> list[dict[str, Any]]:
        """查询同表同列的全部字段版本，按版本序号排序。"""
        versions = [
            row for row in store.rows(self.MODULE)
            if row.get("湖表名") == table_name
            and row.get("变更字段") == column_name
            and row.get("字段版本")
        ]
        return sorted(versions, key=lambda row: row["id"])

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        if action == "提交审批":
            return self.submit_approval(entry_id)
        if action == "执行变更":
            return self.execute_change(entry_id)
        return super().run_action(entry_id, action)

    def _next_field_version(self, table_name: str, column_name: str) -> str:
        """同表同列每执行一次不兼容变更，字段版本号递增（v1、v2……）。"""
        existing = self.list_field_versions(table_name, column_name)
        return f"v{len(existing) + 1}"
