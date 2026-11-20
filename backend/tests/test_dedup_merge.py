"""采集去重归并能力的端到端验证：版本快照、互斥约束、迟到事件、血缘与对账。

运行：cd backend && python3 tests/test_dedup_merge.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

PASSED = 0


def check(label: str, condition: bool, detail: str = "") -> None:
    global PASSED
    assert condition, f"断言失败：{label} {detail}"
    PASSED += 1
    print(f"  ✓ {label}")


def must_ok(resp, label: str) -> dict:
    body = resp.json()
    check(label, resp.status_code == 200 and body.get("ok"), f"resp={resp.status_code} body={body}")
    return body


print("== 1. 登记策略并固化版本 ==")
resp = client.post("/api/dedup", json={"values": {
    "策略编号": "TST-0001", "输入来源": "测试通道", "去重键": "device_id",
    "排序字段": "ts", "保留策略": "最新优先", "时间窗口": "1h",
}})
body = must_ok(resp, "登记去重策略")
strategy_id = body["entry"]["id"]
check("初始版本号为 1", body["entry"]["版本号"] == 1)

resp = client.post(f"/api/dedup/{strategy_id}/actions", json={"values": {"action": "提交配置"}})
body = must_ok(resp, "提交配置")
check("提交配置后版本号 +1", body["entry"]["版本号"] == 2)

resp = client.post(f"/api/dedup/{strategy_id}/actions", json={"values": {"action": "启用策略"}})
must_ok(resp, "启用策略")

print("== 2. 批量登记输入事件 ==")
events = [
    {"事件编号": "TEVT-01", "去重键值": "dev-A", "事件时间": "2026-11-01T10:05:00", "负载": {"ts": "2026-11-01T10:05:00"}},
    {"事件编号": "TEVT-02", "去重键值": "dev-A", "事件时间": "2026-11-01T10:10:00", "负载": {"ts": "2026-11-01T10:10:00"}},
    {"事件编号": "TEVT-03", "去重键值": "dev-A", "事件时间": "2026-11-01T10:20:00", "负载": {"ts": "2026-11-01T10:20:00"}},
    {"事件编号": "TEVT-04", "去重键值": "dev-B", "事件时间": "2026-11-01T10:06:00", "负载": {"ts": "2026-11-01T10:06:00"}},
    {"事件编号": "TEVT-05", "去重键值": "dev-B", "事件时间": "2026-11-01T10:07:00", "负载": {"ts": "2026-11-01T10:07:00"}},
]
resp = client.post("/api/dedup/merge/events", json={"values": {"策略编号": "TST-0001", "事件列表": events}})
body = must_ok(resp, "批量登记 5 条事件")
check("登记成功 5 条", len(body["entry"]["items"]) == 5)

print("== 3. 按有效时间范围批量归并 ==")
resp = client.post("/api/dedup/merge/run", json={"values": {
    "策略编号": "TST-0001", "范围开始": "2026-11-01T00:00:00", "范围结束": "2026-11-02T00:00:00",
}})
body = must_ok(resp, "执行批量归并")
batch1 = body["entry"]
check("保留 2 条", batch1["保留条数"] == 2)
check("被归并 3 条", batch1["归并条数"] == 3)
check("批次快照版本 v2", batch1["策略版本"] == 2)
check("批次快照排序字段 ts", batch1["排序字段"] == "ts")
check("批次快照时间窗口 1h", batch1["时间窗口"] == "1h")

resp = client.get("/api/dedup/merge/details", params={"strategy_no": "TST-0001", "size": 50})
details = resp.json()["items"]
check("明细共 5 条", len(details) == 5)
roles = {d["事件编号"]: d["角色"] for d in details}
check("dev-A 最新 TEVT-03 保留", roles["TEVT-03"] == "保留")
check("dev-B 最新 TEVT-05 保留", roles["TEVT-05"] == "保留")
check("TEVT-01/02/04 被归并", all(roles[e] == "被归并" for e in ("TEVT-01", "TEVT-02", "TEVT-04")))
targets = {d["事件编号"]: d["归并目标"] for d in details}
check("被归并事件指向保留记录", targets["TEVT-01"] == "TEVT-03" and targets["TEVT-02"] == "TEVT-03" and targets["TEVT-04"] == "TEVT-05")
check("保留记录归并目标为自身", targets["TEVT-03"] == "TEVT-03" and targets["TEVT-05"] == "TEVT-05")
check("明细快照与批次版本一致", all(d["策略版本"] == 2 and d["排序字段"] == "ts" and d["时间窗口"] == "1h" for d in details))
check("明细窗口按 1h 切分", all(d["窗口开始"] == "2026-11-01T10:00:00" and d["窗口结束"] == "2026-11-01T11:00:00" for d in details))

# 互斥约束：同一有效版本内没有事件既是保留又被归并
keep_set = {d["事件编号"] for d in details if d["角色"] == "保留"}
merged_set = {d["事件编号"] for d in details if d["角色"] == "被归并"}
check("同版本内保留与被归并互斥", not (keep_set & merged_set))

print("== 4. 确认批次，冻结版本 ==")
resp = client.post(f"/api/dedup/merge/batches/{batch1['id']}/confirm")
body = must_ok(resp, "确认批次")
check("批次状态已确认", body["entry"]["status"] == "已确认")
check("确认时间已记录", bool(body["entry"]["确认时间"]))

print("== 5. 窗口确认后收到迟到事件 ==")
late_event = {"事件编号": "TEVT-06", "去重键值": "dev-A", "事件时间": "2026-11-01T10:25:00", "负载": {"ts": "2026-11-01T10:25:00"}}
resp = client.post("/api/dedup/merge/events", json={"values": {"策略编号": "TST-0001", "事件列表": [late_event]}})
body = must_ok(resp, "登记迟到事件")
check("迟到标记为 True", body["entry"]["items"][0]["迟到标记"] is True)

resp = client.post("/api/dedup/merge/run", json={"values": {
    "策略编号": "TST-0001", "范围开始": "2026-11-01T00:00:00", "范围结束": "2026-11-02T00:00:00",
}})
body = must_ok(resp, "迟到事件再次归并")
batch2 = body["entry"]
check("迟到事件进入新批次", batch2["批次编号"] != batch1["批次编号"])
check("已确认版本冻结，自动升代为 v3", batch2["策略版本"] == 3)
check("新批次迟到条数为 1", batch2["迟到条数"] == 1)

resp = client.get("/api/dedup/merge/details", params={"batch_no": batch1["批次编号"], "size": 50})
old_details = resp.json()["items"]
old_roles = {d["事件编号"]: d["角色"] for d in old_details}
check("已确认版本的保留记录不变", old_roles.get("TEVT-03") == "保留" and old_roles.get("TEVT-06") is None)

resp = client.get("/api/dedup/merge/details", params={"batch_no": batch2["批次编号"], "size": 50})
new_details = resp.json()["items"]
check("迟到事件在新批次中为保留", new_details[0]["事件编号"] == "TEVT-06" and new_details[0]["角色"] == "保留")
check("新批次明细带迟到标记", new_details[0]["迟到标记"] is True)

print("== 6. 迟到事件归属可解释 ==")
resp = client.get("/api/dedup/merge/explain/TEVT-06")
expl = resp.json()
check("迟到事件有版本轨迹", len(expl["版本轨迹"]) == 1)
check("迟到事件归属新批次", expl["版本轨迹"][0]["批次编号"] == batch2["批次编号"])
check("迟到事件归属新版本 v3", expl["版本轨迹"][0]["策略版本"] == 3)
check("说明含迟到归因", "迟到" in expl["说明"] and "未改写" in expl["说明"])
check("迟到事件实际生效保留为自身", expl["实际生效保留记录"] == "TEVT-06")

resp = client.get("/api/dedup/merge/explain/TEVT-01")
expl = resp.json()
check("被归并事件指向保留记录", expl["实际生效保留记录"] == "TEVT-03" and expl["版本轨迹"][0]["角色"] == "被归并")

print("== 7. 血缘同步：被归并事件 → 保留记录 ==")
resp = client.post("/api/dedup/merge/lineage", json={"values": {"策略编号": "TST-0001"}})
body = must_ok(resp, "血缘同步")
created = body["entry"]["items"]
check("血缘关系 6 条（5+1 事件各一条）", len(created) == 6)
edges = {(r["上游节点"], r["下游节点"]) for r in created}
check("被归并事件指向保留记录", ("TEVT-01", "TEVT-03") in edges and ("TEVT-02", "TEVT-03") in edges and ("TEVT-04", "TEVT-05") in edges)
check("保留记录自关联", ("TEVT-03", "TEVT-03") in edges and ("TEVT-05", "TEVT-05") in edges and ("TEVT-06", "TEVT-06") in edges)
check("血缘关系落在 lineage 数据集", any(r["上游节点"] == "TEVT-01" and r["下游节点"] == "TEVT-03" for r in client.get("/api/lineage", params={"size": 200}).json()["items"]))

resp = client.post("/api/dedup/merge/lineage", json={"values": {"策略编号": "TST-0001"}})
body = must_ok(resp, "重复血缘同步")
check("重复同步幂等，新增 0 条", len(body["entry"]["items"]) == 0)

# 每条输入事件都关联到实际生效的保留记录
resp = client.get("/api/dedup/merge/events", params={"strategy_no": "TST-0001", "size": 50})
all_events = [e["事件编号"] for e in resp.json()["items"]]
lineage_items = client.get("/api/lineage", params={"size": 200}).json()["items"]
linked = {r["上游节点"] for r in lineage_items if r["上游节点"] in all_events}
check("每条输入事件都有血缘关联", set(all_events) <= linked)

print("== 8. 版本对账：报告口径 vs 当前口径 ==")
resp = client.get("/api/dedup/merge/stats", params={"strategy_no": "TST-0001"})
stats = resp.json()
check("dev-A 当前保留为迟到事件", stats["当前口径保留"]["dev-A"]["事件编号"] == "TEVT-06")
check("dev-B 当前保留不变", stats["当前口径保留"]["dev-B"]["事件编号"] == "TEVT-05")
check("存在口径差异", len(stats["口径差异"]) == 1)
diff = stats["口径差异"][0]
check("差异：已确认保留 TEVT-03 vs 当前 TEVT-06", diff["已确认保留记录"] == "TEVT-03" and diff["当前保留记录"] == "TEVT-06")
check("差异版本：已确认 v2 vs 当前 v3", diff["已确认版本"] == 2 and diff["当前版本"] == 3)
v2_keeps = next(v for v in stats["版本清单"] if v["策略版本"] == 2)
check("已确认版本 v2 仍唯一指向 TEVT-03", v2_keeps["保留记录"]["dev-A"] == "TEVT-03" and v2_keeps["已确认"])
check("版本 v3 未确认，保留 TEVT-06", next(v for v in stats["版本清单"] if v["策略版本"] == 3)["保留记录"]["dev-A"] == "TEVT-06")

print("== 9. 配置变更产生新版本，旧批次快照不变 ==")
resp = client.post(f"/api/dedup/{strategy_id}/actions", json={"values": {"action": "提交配置", "排序字段": "ver"}})
body = must_ok(resp, "变更排序字段并提交配置")
check("版本号升为 v4", body["entry"]["版本号"] == 4)
resp = client.post(f"/api/dedup/{strategy_id}/actions", json={"values": {"action": "启用策略"}})
must_ok(resp, "重新启用策略")

new_events = [
    {"事件编号": "TEVT-07", "去重键值": "dev-C", "事件时间": "2026-11-02T09:00:00", "负载": {"ver": 1}},
    {"事件编号": "TEVT-08", "去重键值": "dev-C", "事件时间": "2026-11-02T09:01:00", "负载": {"ver": 2}},
]
resp = client.post("/api/dedup/merge/events", json={"values": {"策略编号": "TST-0001", "事件列表": new_events}})
must_ok(resp, "登记新版本事件")
resp = client.post("/api/dedup/merge/run", json={"values": {
    "策略编号": "TST-0001", "范围开始": "2026-11-02T00:00:00", "范围结束": "2026-11-03T00:00:00",
}})
body = must_ok(resp, "新版本归并")
batch3 = body["entry"]
check("新批次版本 v4", batch3["策略版本"] == 4)
check("新批次快照排序字段 ver", batch3["排序字段"] == "ver")

resp = client.get("/api/dedup/merge/details", params={"batch_no": batch3["批次编号"], "size": 50})
v4_details = resp.json()["items"]
v4_roles = {d["事件编号"]: d["角色"] for d in v4_details}
check("v4 按 ver 排序保留 TEVT-08", v4_roles["TEVT-08"] == "保留" and v4_roles["TEVT-07"] == "被归并")
check("v4 明细快照与 v4 版本一致", all(d["策略版本"] == 4 and d["排序字段"] == "ver" for d in v4_details))

resp = client.get("/api/dedup/merge/details", params={"batch_no": batch1["批次编号"], "size": 50})
check("旧批次明细快照仍是 ts / v2", all(d["策略版本"] == 2 and d["排序字段"] == "ts" for d in resp.json()["items"]))

print("== 10. 异常路径 ==")
resp = client.post("/api/dedup/merge/run", json={"values": {"策略编号": "TST-0001", "范围开始": "2026-11-02T00:00:00", "范围结束": "2026-11-03T00:00:00"}})
body = resp.json()
check("无待归并事件时给出说明", not body["ok"] and "没有待归并事件" in body["message"])
resp = client.post("/api/dedup/merge/run", json={"values": {"策略编号": "TST-0001", "范围开始": "2026-11-03T00:00:00", "范围结束": "2026-11-02T00:00:00"}})
check("时间范围倒置被拒绝", not resp.json()["ok"])
resp = client.post("/api/dedup/merge/events", json={"values": {"策略编号": "TST-0001", "事件列表": [{"事件编号": "TEVT-06", "去重键值": "dev-A", "事件时间": "2026-11-01T10:25:00"}]}})
check("重复事件编号被跳过", not resp.json()["ok"] or "已存在" in resp.json()["message"])
resp = client.get("/api/dedup/merge/explain/NOPE-99")
check("不存在的事件返回 404", resp.status_code == 404)

print(f"\n全部通过：{PASSED} 项断言")
