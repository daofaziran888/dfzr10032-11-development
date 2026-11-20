"""采集去重业务规则：状态流转、字段校验与筛选口径都收在这里。"""
from __future__ import annotations

from typing import Any

from app.store import store

MODULE = "dedup"
REQUIRED_FIELDS = ["策略编号", "输入来源", "去重键"]
CONFIG_FIELDS = ["去重键", "排序字段", "保留策略", "时间窗口", "重复阈值"]
OPTIONAL_FIELDS = ["排序字段", "保留策略", "时间窗口", "重复阈值", "策略状态"]
STATUS_ORDER = ["草稿", "已配置", "生效中", "已停用"]
ACTION_RULES = {"提交配置": "已配置", "启用策略": "生效中", "停用策略": "已停用"}
NEGATIVE_ACTIONS = ["停用策略"]


class DedupService:
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
        extra: dict[str, str] | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("策略编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        if extra:
            for field, val in extra.items():
                rows = [row for row in rows if val in str(row.get(field, ""))]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        for field in OPTIONAL_FIELDS:
            entry[field] = values.get(field) or ""
        entry["版本号"] = 1
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str, values: dict[str, Any] | None = None) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"去重策略 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于采集去重可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        version_note = ""
        if action == "提交配置":
            changed = False
            for field in CONFIG_FIELDS:
                new_val = (values or {}).get(field)
                if new_val is not None and str(new_val).strip() and str(new_val) != str(entry.get(field, "")):
                    entry[field] = new_val
                    changed = True
            # 提交配置即固化一版：之后归并批次按此版本快照窗口与排序规则
            entry["版本号"] = int(entry.get("版本号") or 0) + 1
            version_note = f"，配置已固化为版本 v{entry['版本号']}" if changed else f"，当前版本 v{entry['版本号']}"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"去重策略已{action}{version_note}"
