"""采集去重归并：按策略版本快照批量归并输入事件，把取舍逐条写进去重明细数据集。

核心约定：
- 归并批次固化策略版本号与时间窗口、排序字段、保留策略快照，归并计算只读快照，
  保证"归并时用到的窗口和排序规则与该批次记录的版本一致"。
- 同一策略版本内，一条事件不能既是保留记录又被归并；写明细前后都会校验。
- 批次确认后该版本冻结：迟到事件不改写已确认版本，只进入后续新批次，
  因此每个已确认版本始终只指向一条明确的保留记录，且迟到归属可解释。
- 血缘追踪从去重明细数据集生成：被归并事件指向保留记录，保留记录自关联，
  使每条输入事件都关联到实际生效的保留记录。
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any

from app.store import store

STRATEGY_TABLE = "dedup"
EVENT_TABLE = "dedup_event"
BATCH_TABLE = "dedup_batch"
DETAIL_TABLE = "dedup_detail"
LINEAGE_TABLE = "lineage"

ROLE_KEEP = "保留"
ROLE_MERGED = "被归并"

EVENT_PENDING = "待归并"
EVENT_MERGED = "已归并"

BATCH_OPEN = "未确认"
BATCH_CONFIRMED = "已确认"

KEEP_EARLIEST = "最早优先"


def _now() -> datetime:
    return datetime.now()


def _fmt(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%S")


def _parse_time(value: Any) -> datetime | None:
    """把入参解析成 datetime；支持 ISO 日期时间、空格分隔与纯日期，解析不了返回 None。"""
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value
    text = str(value).strip()
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _window_delta(window: str) -> timedelta | None:
    """解析时间窗口配置：如 30m、6h、1d；解析不了返回 None（整个范围视作一个窗口）。"""
    match = re.fullmatch(r"(\d+)\s*([smhd])", str(window or "").strip().lower())
    if not match:
        return None
    amount = int(match.group(1))
    unit = match.group(2)
    return {
        "s": timedelta(seconds=amount),
        "m": timedelta(minutes=amount),
        "h": timedelta(hours=amount),
        "d": timedelta(days=amount),
    }[unit]


def _split_windows(start: datetime, end: datetime, delta: timedelta | None) -> list[tuple[datetime, datetime]]:
    """把有效时间范围按窗口大小切成左闭右开的连续窗口。"""
    if delta is None or delta <= timedelta(0):
        return [(start, end)]
    windows: list[tuple[datetime, datetime]] = []
    cursor = start
    while cursor < end:
        nxt = min(cursor + delta, end)
        windows.append((cursor, nxt))
        cursor = nxt
    return windows


def _sort_value(event: dict[str, Any], sort_field: str) -> tuple[int, float, str]:
    """从事件负载里取排序字段值，统一成可比较的三元组；缺值排最后。"""
    payload = event.get("负载") or {}
    value = payload.get(sort_field)
    if value in (None, ""):
        return (1, 0.0, "")
    moment = _parse_time(value)
    if moment is not None:
        return (0, moment.timestamp(), "")
    try:
        return (0, float(value), "")
    except (TypeError, ValueError):
        return (0, 0.0, str(value))


def _next_no(rows: list[dict[str, Any]], prefix: str, field: str) -> str:
    """按前缀生成递增编号，如 EVT-0007。"""
    top = 0
    for row in rows:
        text = str(row.get(field, ""))
        if text.startswith(prefix + "-"):
            try:
                top = max(top, int(text.split("-", 1)[1]))
            except ValueError:
                continue
    return f"{prefix}-{top + 1:04d}"


class DedupMergeService:
    """去重归并与明细追溯的业务规则；路由层只做参数搬运。"""

    # ---------- 基础查询 ----------

    def _find_strategy(self, strategy_no: str) -> dict[str, Any] | None:
        for row in store.rows(STRATEGY_TABLE):
            if str(row.get("策略编号", "")) == strategy_no:
                return row
        return None

    def list_events(self, strategy_no: str | None = None, merge_status: str | None = None) -> list[dict[str, Any]]:
        rows = store.rows(EVENT_TABLE)
        if strategy_no:
            rows = [row for row in rows if row.get("策略编号") == strategy_no]
        if merge_status:
            rows = [row for row in rows if row.get("归并状态") == merge_status]
        return rows

    def list_batches(self, strategy_no: str | None = None) -> list[dict[str, Any]]:
        rows = store.rows(BATCH_TABLE)
        if strategy_no:
            rows = [row for row in rows if row.get("策略编号") == strategy_no]
        return rows

    def list_details(
        self,
        strategy_no: str | None = None,
        batch_no: str | None = None,
        version: int | None = None,
        role: str | None = None,
    ) -> list[dict[str, Any]]:
        rows = store.rows(DETAIL_TABLE)
        if strategy_no:
            rows = [row for row in rows if row.get("策略编号") == strategy_no]
        if batch_no:
            rows = [row for row in rows if row.get("批次编号") == batch_no]
        if version is not None:
            rows = [row for row in rows if int(row.get("策略版本", 0)) == version]
        if role:
            rows = [row for row in rows if row.get("角色") == role]
        return rows

    # ---------- 事件登记 ----------

    def _is_late(self, strategy_no: str, event_time: datetime) -> bool:
        """事件时间落入同策略任一已确认批次的有效范围，即视为迟到。"""
        for batch in store.rows(BATCH_TABLE):
            if batch.get("策略编号") != strategy_no or batch.get("status") != BATCH_CONFIRMED:
                continue
            start = _parse_time(batch.get("范围开始"))
            end = _parse_time(batch.get("范围结束"))
            if start is not None and end is not None and start <= event_time < end:
                return True
        return False

    def register_events(self, strategy_no: str, events: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[str]]:
        """批量登记输入事件；逐条校验，迟到事件打标记但不拒收。"""
        strategy = self._find_strategy(strategy_no)
        if strategy is None:
            return [], [f"去重策略 {strategy_no} 不存在"]
        if strategy.get("status") != "生效中":
            return [], [f"去重策略 {strategy_no} 当前为「{strategy.get('status')}」，未生效不能登记事件"]
        table = store.rows(EVENT_TABLE)
        created: list[dict[str, Any]] = []
        errors: list[str] = []
        for raw in events:
            event_no = str(raw.get("事件编号") or "").strip() or _next_no(table, "EVT", "事件编号")
            if any(str(row.get("事件编号")) == event_no for row in table):
                errors.append(f"事件编号 {event_no} 已存在，跳过")
                continue
            key_value = str(raw.get("去重键值") or "").strip()
            if not key_value:
                errors.append(f"事件 {event_no} 缺少去重键值，跳过")
                continue
            event_time = _parse_time(raw.get("事件时间"))
            if event_time is None:
                errors.append(f"事件 {event_no} 的事件时间无法解析，跳过")
                continue
            arrival = _parse_time(raw.get("到达时间")) or _now()
            late = self._is_late(strategy_no, event_time)
            row = {
                "id": max((int(r.get("id", 0)) for r in table), default=0) + 1,
                "事件编号": event_no,
                "策略编号": strategy_no,
                "去重键值": key_value,
                "事件时间": _fmt(event_time),
                "到达时间": _fmt(arrival),
                "负载": raw.get("负载") if isinstance(raw.get("负载"), dict) else {},
                "归并状态": EVENT_PENDING,
                "归并批次": "",
                "迟到标记": late,
                "status": EVENT_PENDING,
                "pending": True,
                "abnormal": late,
            }
            table.append(row)
            created.append(row)
        return created, errors

    # ---------- 批量归并 ----------

    def _mutex_conflicts(self, strategy_no: str, version: int, extra: list[dict[str, Any]]) -> set[str]:
        """同一有效版本内，既被标记保留又被归并的事件编号集合（应为空）。"""
        keep: set[str] = set()
        merged: set[str] = set()
        for detail in self.list_details(strategy_no=strategy_no, version=version):
            (keep if detail.get("角色") == ROLE_KEEP else merged).add(str(detail.get("事件编号")))
        for detail in extra:
            (keep if detail.get("角色") == ROLE_KEEP else merged).add(str(detail.get("事件编号")))
        return keep & merged

    def run_merge(self, strategy_no: str, range_start: Any, range_end: Any) -> tuple[dict[str, Any] | None, str]:
        """按策略的去重键与有效时间范围批量归并待归并事件，逐条写去重明细。"""
        strategy = self._find_strategy(strategy_no)
        if strategy is None:
            return None, f"去重策略 {strategy_no} 不存在"
        if strategy.get("status") != "生效中":
            return None, f"去重策略 {strategy_no} 当前为「{strategy.get('status')}」，未生效不能归并"
        start = _parse_time(range_start)
        end = _parse_time(range_end)
        if start is None or end is None or start >= end:
            return None, "有效时间范围不合法：需要 范围开始 < 范围结束，且时间格式可解析"

        # 版本快照：归并全程只使用这份快照，保证窗口和排序规则与批次版本一致
        version = int(strategy.get("版本号") or 1)
        sort_field = str(strategy.get("排序字段") or "").strip()
        keep_rule = str(strategy.get("保留策略") or "").strip() or "最新优先"
        window_conf = str(strategy.get("时间窗口") or "").strip()

        events = []
        for event in store.rows(EVENT_TABLE):
            if event.get("策略编号") != strategy_no or event.get("归并状态") != EVENT_PENDING:
                continue
            moment = _parse_time(event.get("事件时间"))
            if moment is not None and start <= moment < end:
                events.append(event)
        if not events:
            return None, "该有效时间范围内没有待归并事件"

        windows = _split_windows(start, end, _window_delta(window_conf))
        groups: dict[tuple[str, int], list[dict[str, Any]]] = {}
        for event in events:
            moment = _parse_time(event["事件时间"])
            window_idx = next(i for i, (ws, we) in enumerate(windows) if ws <= moment < we)
            groups.setdefault((str(event["去重键值"]), window_idx), []).append(event)

        # 已确认版本冻结：当前版本已有已确认批次，或同（去重键, 窗口）已有明细时，
        # 版本自动升代，迟到事件归入新版本——已确认版本始终只指向一条明确的保留记录。
        version_details = self.list_details(strategy_no=strategy_no, version=version)
        version_frozen = any(
            row.get("策略编号") == strategy_no
            and int(row.get("策略版本", 0)) == version
            and row.get("status") == BATCH_CONFIRMED
            for row in store.rows(BATCH_TABLE)
        )
        new_group_keys = {(key, _fmt(windows[idx][0])) for key, idx in groups}
        group_conflict = any(
            (str(d["去重键值"]), str(d["窗口开始"])) in new_group_keys for d in version_details
        )
        if version_frozen or group_conflict:
            version += 1
            strategy["版本号"] = version
            version_bumped = True
        else:
            version_bumped = False

        batch_table = store.rows(BATCH_TABLE)
        batch_no = _next_no(batch_table, "BATCH", "批次编号")
        detail_table = store.rows(DETAIL_TABLE)
        new_details: list[dict[str, Any]] = []
        keep_count = 0
        for (key_value, window_idx), group in sorted(groups.items()):
            win_start, win_end = windows[window_idx]
            if sort_field:
                valued = [e for e in group if _sort_value(e, sort_field)[0] == 0]
                pool = valued or group
                if keep_rule == KEEP_EARLIEST:
                    keeper = min(pool, key=lambda e: _sort_value(e, sort_field))
                else:
                    keeper = max(pool, key=lambda e: _sort_value(e, sort_field))
            else:
                keeper = group[0]
            keep_count += 1
            for event in group:
                is_keeper = event is keeper
                new_details.append({
                    "id": max((int(r.get("id", 0)) for r in detail_table), default=0) + len(new_details) + 1,
                    "明细编号": _next_no(detail_table + new_details, "DTL", "明细编号"),
                    "批次编号": batch_no,
                    "策略编号": strategy_no,
                    "策略版本": version,
                    "去重键值": key_value,
                    "事件编号": event["事件编号"],
                    "角色": ROLE_KEEP if is_keeper else ROLE_MERGED,
                    "归并目标": keeper["事件编号"],
                    "窗口开始": _fmt(win_start),
                    "窗口结束": _fmt(win_end),
                    "时间窗口": window_conf,
                    "排序字段": sort_field,
                    "保留策略": keep_rule,
                    "迟到标记": bool(event.get("迟到标记")),
                    "记录时间": _fmt(_now()),
                    "status": "有效",
                    "pending": False,
                    "abnormal": bool(event.get("迟到标记")),
                })

        # 互斥约束：同一有效版本内，一条事件不能既是保留记录又被归并
        conflicts = self._mutex_conflicts(strategy_no, version, new_details)
        if conflicts:
            return None, f"版本 v{version} 内事件 {'、'.join(sorted(conflicts))} 同时出现保留与被归并，本次归并已取消"

        detail_table.extend(new_details)
        for event in events:
            event["归并状态"] = EVENT_MERGED
            event["归并批次"] = batch_no
            event["status"] = EVENT_MERGED
            event["pending"] = False
        batch = {
            "id": max((int(r.get("id", 0)) for r in batch_table), default=0) + 1,
            "批次编号": batch_no,
            "策略编号": strategy_no,
            "策略版本": version,
            "范围开始": _fmt(start),
            "范围结束": _fmt(end),
            "时间窗口": window_conf,
            "排序字段": sort_field,
            "保留策略": keep_rule,
            "事件总数": len(events),
            "保留条数": keep_count,
            "归并条数": len(events) - keep_count,
            "迟到条数": sum(1 for e in events if e.get("迟到标记")),
            "创建时间": _fmt(_now()),
            "确认时间": "",
            "status": BATCH_OPEN,
            "pending": True,
            "abnormal": False,
        }
        batch_table.append(batch)
        bump_note = "；原版本已确认冻结，自动升代" if version_bumped else ""
        return batch, (
            f"归并完成：批次 {batch_no}（版本 v{version}{bump_note}），"
            f"事件 {len(events)} 条，保留 {keep_count} 条，被归并 {len(events) - keep_count} 条"
        )

    # ---------- 批次确认 ----------

    def confirm_batch(self, batch_id: int) -> tuple[dict[str, Any] | None, str]:
        """确认批次：冻结该版本的取舍结果，确认后迟到事件只能进入后续新批次。"""
        batch = store.find(BATCH_TABLE, batch_id)
        if batch is None:
            return None, f"归并批次 {batch_id} 不存在"
        if batch.get("status") == BATCH_CONFIRMED:
            return batch, f"批次 {batch['批次编号']} 已是已确认状态"
        details = self.list_details(batch_no=batch["批次编号"])
        groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for detail in details:
            groups.setdefault((str(detail["去重键值"]), str(detail["窗口开始"])), []).append(detail)
        for (key_value, win_start), rows in groups.items():
            keepers = [row for row in rows if row.get("角色") == ROLE_KEEP]
            if len(keepers) != 1:
                return None, f"分组（{key_value}，{win_start}）保留记录为 {len(keepers)} 条，不是恰好 1 条，不能确认"
        conflicts = self._mutex_conflicts(batch["策略编号"], int(batch["策略版本"]), [])
        if conflicts:
            return None, f"版本 v{batch['策略版本']} 内事件 {'、'.join(sorted(conflicts))} 同时出现保留与被归并，不能确认"
        batch["status"] = BATCH_CONFIRMED
        batch["确认时间"] = _fmt(_now())
        batch["pending"] = False
        return batch, f"批次 {batch['批次编号']} 已确认，版本 v{batch['策略版本']} 的保留记录已冻结"

    # ---------- 血缘写出 ----------

    def sync_lineage(self, strategy_no: str | None = None) -> tuple[list[dict[str, Any]], str]:
        """根据去重明细数据集写出 被归并事件→保留记录 的血缘关系（幂等）。

        保留记录写一条自关联关系，保证每条输入事件都关联到实际生效的保留记录。
        """
        lineage = store.rows(LINEAGE_TABLE)
        existing = {
            (str(row.get("上游节点")), str(row.get("下游节点")), str(row.get("采集批次")), str(row.get("关系版本")))
            for row in lineage
        }
        created: list[dict[str, Any]] = []
        for detail in self.list_details(strategy_no=strategy_no):
            upstream = str(detail["事件编号"])
            downstream = str(detail["归并目标"])
            key = (upstream, downstream, str(detail["批次编号"]), str(detail["策略版本"]))
            if key in existing:
                continue
            if detail["角色"] == ROLE_KEEP:
                note = (
                    f"保留生效：{upstream} 是去重键 {detail['去重键值']} 在版本 v{detail['策略版本']} "
                    f"实际生效的保留记录"
                )
            else:
                note = (
                    f"归并取舍：{upstream} 在批次 {detail['批次编号']}（版本 v{detail['策略版本']}）"
                    f"被归并，实际生效的保留记录为 {downstream}"
                )
            row = {
                "id": max((int(r.get("id", 0)) for r in lineage), default=0) + 1,
                "关系编号": _next_no(lineage, "LINE", "关系编号"),
                "上游节点": upstream,
                "下游节点": downstream,
                "来源字段": detail["去重键值"],
                "目标字段": detail["排序字段"],
                "转换说明": note,
                "采集批次": detail["批次编号"],
                "关系版本": detail["策略版本"],
                "status": "已记录",
                "pending": True,
                "abnormal": False,
            }
            lineage.append(row)
            created.append(row)
            existing.add(key)
        return created, f"血缘同步完成：新增 {len(created)} 条关系，重复关系已跳过"

    # ---------- 追溯与对账 ----------

    def explain_event(self, event_no: str) -> dict[str, Any] | None:
        """解释一条输入事件的归并去向：归属批次、版本、角色与实际生效的保留记录。"""
        event = next((row for row in store.rows(EVENT_TABLE) if row.get("事件编号") == event_no), None)
        if event is None:
            return None
        details = self.list_details()
        details = [row for row in details if row.get("事件编号") == event_no]
        batches = {row["批次编号"]: row for row in store.rows(BATCH_TABLE)}
        versions = []
        for detail in sorted(details, key=lambda d: (int(d["策略版本"]), str(d["记录时间"]))):
            batch = batches.get(detail["批次编号"], {})
            versions.append({
                "批次编号": detail["批次编号"],
                "策略版本": detail["策略版本"],
                "角色": detail["角色"],
                "归并目标": detail["归并目标"],
                "窗口开始": detail["窗口开始"],
                "窗口结束": detail["窗口结束"],
                "排序字段": detail["排序字段"],
                "保留策略": detail["保留策略"],
                "批次状态": batch.get("status", "未知"),
                "迟到标记": detail["迟到标记"],
            })
        effective = versions[-1]["归并目标"] if versions else None
        if not versions:
            note = "事件尚未参与归并，没有生效的保留记录"
        elif event.get("迟到标记"):
            confirmed = [v for v in versions if v["批次状态"] == BATCH_CONFIRMED]
            note = (
                f"事件在相关窗口确认后到达（迟到），未改写任何已确认版本的保留记录；"
                f"已归入版本 v{versions[-1]['策略版本']} 批次 {versions[-1]['批次编号']}"
                f"（{versions[-1]['批次状态']}），实际生效的保留记录为 {effective}。"
                + (f"其中版本 v{confirmed[0]['策略版本']} 已确认。" if confirmed else "")
            )
        else:
            note = (
                f"事件在版本 v{versions[-1]['策略版本']} 批次 {versions[-1]['批次编号']} 中"
                f"角色为「{versions[-1]['角色']}」，实际生效的保留记录为 {effective}"
            )
        return {
            "事件编号": event_no,
            "策略编号": event.get("策略编号"),
            "去重键值": event.get("去重键值"),
            "事件时间": event.get("事件时间"),
            "迟到标记": bool(event.get("迟到标记")),
            "归并状态": event.get("归并状态"),
            "版本轨迹": versions,
            "实际生效保留记录": effective,
            "说明": note,
        }

    def stats(self, strategy_no: str) -> dict[str, Any] | None:
        """按版本对账：已确认版本（报告引用口径）与当前口径的保留记录差异。"""
        strategy = self._find_strategy(strategy_no)
        if strategy is None:
            return None
        details = self.list_details(strategy_no=strategy_no)
        batches = {row["批次编号"]: row for row in self.list_batches(strategy_no)}
        version_keeps: dict[int, dict[str, str]] = {}
        for detail in details:
            if detail.get("角色") == ROLE_KEEP:
                version_keeps.setdefault(int(detail["策略版本"]), {})[str(detail["去重键值"])] = str(detail["事件编号"])
        confirmed_versions = sorted(
            version for version in version_keeps
            if any(
                b.get("策略版本") == version and b.get("status") == BATCH_CONFIRMED
                for b in batches.values()
            )
        )
        latest_keep: dict[str, dict[str, Any]] = {}
        for detail in sorted(details, key=lambda d: (int(d["策略版本"]), str(d["记录时间"]))):
            if detail.get("角色") == ROLE_KEEP:
                latest_keep[str(detail["去重键值"])] = {
                    "事件编号": str(detail["事件编号"]),
                    "策略版本": int(detail["策略版本"]),
                    "批次编号": str(detail["批次编号"]),
                }
        diffs = []
        for key_value, current in latest_keep.items():
            confirmed_keep = None
            confirmed_version = None
            for version in reversed(confirmed_versions):
                if key_value in version_keeps[version]:
                    confirmed_keep = version_keeps[version][key_value]
                    confirmed_version = version
                    break
            if confirmed_keep is not None and confirmed_keep != current["事件编号"]:
                diffs.append({
                    "去重键值": key_value,
                    "已确认版本": confirmed_version,
                    "已确认保留记录": confirmed_keep,
                    "当前版本": current["策略版本"],
                    "当前保留记录": current["事件编号"],
                    "原因": "窗口确认后到达的迟到事件改变了保留记录；已确认版本不变，本次统计按当前口径",
                })
        return {
            "策略编号": strategy_no,
            "当前策略版本": int(strategy.get("版本号") or 1),
            "版本清单": [
                {
                    "策略版本": version,
                    "保留记录": keeps,
                    "已确认": version in confirmed_versions,
                }
                for version, keeps in sorted(version_keeps.items())
            ],
            "当前口径保留": latest_keep,
            "口径差异": diffs,
        }
