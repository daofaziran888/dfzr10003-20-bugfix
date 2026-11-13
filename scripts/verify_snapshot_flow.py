"""端到端校验脚本：直接驱动服务层，覆盖快照创建→恢复校验→重跑引用→清理保护全链路。

运行（在仓库根目录）：
    backend/.venv/bin/python -I scripts/verify_snapshot_flow.py
"""
from __future__ import annotations

import sys

sys.path.insert(0, "/workspace/backend")

# -I 模式下手动挂路径，避免依赖当前工作目录里的同名模块
from app.seed import SEED_ROWS  # noqa: E402
from app.services import backfill as backfill_module  # noqa: E402
from app.services import snapshot as snapshot_module  # noqa: E402
from app.store import Store  # noqa: E402

PASS = 0
FAIL = 0


def check(name: str, condition: bool, detail: str = "") -> None:
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name} {detail}")


def fresh_services() -> tuple[snapshot_module.SnapshotService, backfill_module.BackfillService, Store]:
    """每个场景用独立内存仓库，避免互相污染。"""
    store = Store()
    snapshot_module.store = store
    backfill_module.store = store
    return snapshot_module.SnapshotService(), backfill_module.BackfillService(), store


def main() -> int:
    # ------------------------------------------------ 场景 1：种子数据一致性
    print("场景1 种子数据 SNAP-0002 / BACKFILL-0002 三方同源")
    snap_svc, bf_svc, store = fresh_services()
    snap2 = snap_svc.find_by_code("SNAP-0002")
    check("SNAP-0002 已完成", snap2["status"] == "已完成")
    check("SNAP-0002 恢复校验通过", snap2["恢复校验"] == "通过")
    check("SNAP-0002 保留天数 30", snap2["保留天数"] == "30")
    check("SNAP-0002 可回滚", snap_svc.is_rollbackable(snap2))
    snap2_view = snap_svc.get_entry(snap2["id"])
    check("列表/详情标记可回滚并带校验编号", snap2_view["可回滚"] and snap2_view["回滚依据校验编号"] == "VRF-SNAP-0002-001")
    bf2 = next(row for row in store.rows("backfill") if row["批次编号"] == "BACKFILL-0002")
    bf2_view = bf_svc.get_entry(bf2["id"])
    check("BACKFILL-0002 引用 SNAP-0002", bf2_view["引用快照编号"] == "SNAP-0002")
    check("重跑页快照状态=已完成（同步）", bf2_view["引用快照状态"] == "已完成")
    check("重跑页保留期与快照页一致(30)", bf2_view["引用保留天数"] == "30")
    check("重跑页校验结论与快照页一致(通过)", bf2_view["引用恢复校验"] == "通过")
    check("重跑页可回滚且回滚依据相同", bf2_view["可回滚"] and bf2_view["回滚依据校验编号"] == "VRF-SNAP-0002-001")
    evidence = snap_svc.recovery_evidence()
    ev2 = next(item for item in evidence if item["快照编号"] == "SNAP-0002")
    check("恢复校验结果保留期一致", ev2["保留天数"] == "30")
    check("恢复校验结果引用批次一致", ev2["引用批次"] == "BACKFILL-0002")
    check("恢复校验结果标记为回滚依据", ev2["可回滚依据"] is True)
    snap3 = snap_svc.find_by_code("SNAP-0003")
    check("已清理 SNAP-0003 不可回滚", not snap_svc.is_rollbackable(snap3))
    bf3 = next(row for row in store.rows("backfill") if row["批次编号"] == "BACKFILL-0003")
    check("旧页面不会把已清理快照显示为可回滚", not bf_svc.get_entry(bf3["id"])["可回滚"])

    # ------------------------------------------------ 场景 2：创建后停在草稿、字段完整
    print("场景2 创建快照：字段全保存，未校验前保持草稿")
    snap_svc, bf_svc, _ = fresh_services()
    payload = {
        "快照编号": "SNAP-0100", "表名称": "fact_order", "快照时间": "2026-11-13 01:00:00",
        "快照大小": "5.5GB", "保留天数": "45", "创建批次": "BATCH-X1",
    }
    entry, missing, created = snap_svc.create_entry(payload)
    check("创建成功且为新建", created and not missing)
    check("保留天数已保存", entry["保留天数"] == "45")
    check("创建批次已保存", entry["创建批次"] == "BATCH-X1")
    check("快照大小已保存", entry["快照大小"] == "5.5GB")
    check("尚未开始校验时留在草稿", entry["status"] == "草稿" and entry["快照状态"] == "草稿")
    check("恢复校验为待校验", entry["恢复校验"] == "待校验")
    check("草稿不能被重跑引用", snap_svc.validate_reference("SNAP-0100")[1] != "")
    check("草稿不可回滚", entry["可回滚"] is False)
    # 必填缺失
    _, missing, created = snap_svc.create_entry({"快照编号": "SNAP-0101"})
    check("缺必填字段被拦截", bool(missing) and not created)

    # ------------------------------------------------ 场景 3：幂等创建
    print("场景3 同批次重复创建幂等，返回同一份完整记录")
    again, missing, created = snap_svc.create_entry({**payload, "保留天数": "99"})
    check("重复提交不再新建", created is False and not missing)
    check("已写元数据不被覆盖（保留天数仍45）", again["保留天数"] == "45")
    check("返回的是同一条记录（id 不变）", again["id"] == entry["id"])
    check("记录字段完整", all(again.get(k) for k in ["快照编号", "表名称", "快照时间", "快照大小", "保留天数", "创建批次"]))

    # ------------------------------------------------ 场景 4：存储校验失败续跑
    print("场景4 存储校验失败：保持草稿，下次从同编号续跑且不重复提交元数据")
    snap_svc, bf_svc, _ = fresh_services()
    bad_payload = {"快照编号": "SNAP-0200", "表名称": "t1", "快照时间": "2026-11-13 02:00:00",
                   "保留天数": "7", "模拟存储校验结果": "失败"}
    bad, _, created = snap_svc.create_entry(bad_payload)
    check("失败也建了记录（只建一条）", created and snap_svc.find_by_code("SNAP-0200") is not None)
    check("存储校验失败留痕", bad["存储校验"] == "失败" and "存储校验失败" in bad["失败证据"])
    check("失败后状态仍是草稿", bad["status"] == "草稿")
    check("失败快照不能开始恢复校验", snap_svc.run_action(bad["id"], "开始校验", {})[0] is None)
    retry_payload = {"快照编号": "SNAP-0200", "表名称": "t1", "快照时间": "2026-11-13 02:00:00", "保留天数": "7"}
    retried, _, created = snap_svc.create_entry(retry_payload)
    check("续跑不新建记录", created is False)
    check("续跑后存储校验通过", retried["存储校验"] == "通过")
    check("失败证据已清除", retried["失败证据"] == "")
    check("仍是同编号同一条元数据", retried["id"] == bad["id"] and retried["保留天数"] == "7")

    # ------------------------------------------------ 场景 5：状态机按校验阶段推进
    print("场景5 状态机：开始校验→运行中→通过→已完成；失败留证据回草稿")
    snap_svc, bf_svc, _ = fresh_services()
    e, _, _ = snap_svc.create_entry({"快照编号": "SNAP-0300", "表名称": "t", "快照时间": "2026-11-13 03:00:00", "保留天数": "21"})
    check("草稿不能直接清理", snap_svc.run_action(e["id"], "清理快照", {})[0] is None)
    running, msg = snap_svc.run_action(e["id"], "开始校验", {})
    check("开始校验后进入运行中", running is not None and running["status"] == "运行中" and "运行中" in msg)
    check("运行中不能直接清理", snap_svc.run_action(e["id"], "清理快照", {})[0] is None)
    check("运行中不能重复开始校验", snap_svc.run_action(e["id"], "开始校验", {})[0] is None)
    failed, msg = snap_svc.run_action(e["id"], "校验未通过", {"失败证据": "抽样3行不一致"})
    check("校验未通过退回草稿", failed is not None and failed["status"] == "草稿")
    check("失败证据保留", "抽样3行不一致" in failed["失败证据"] and failed["abnormal"] is True)
    check("恢复校验字段显示未通过", failed["恢复校验"] == "未通过")
    check("失败快照仍不能被引用", snap_svc.validate_reference("SNAP-0300")[1] != "")
    snap_svc.run_action(e["id"], "开始校验", {})
    done, msg = snap_svc.run_action(e["id"], "校验通过", {"校验结论": "抽样一致"})
    check("校验通过才转已完成", done is not None and done["status"] == "已完成")
    check("通过证据可作为回滚依据", done["可回滚"] and done["回滚依据校验编号"].startswith("VRF-SNAP-0300"))
    check("校验历史含失败+通过两条", len(done["校验历史"]) == 2)
    check("回滚依据是最近一次通过结论", done["校验编号"] == done["校验历史"][-1]["校验编号"])
    check("校验结果页失败记录不作为回滚依据",
          next(x for x in snap_svc.recovery_evidence() if x["校验结果"] == "未通过" and x["快照编号"] == "SNAP-0300")["可回滚依据"] is False)

    # ------------------------------------------------ 场景 6：重跑引用保护
    print("场景6 重跑只能引用有效快照，且状态/证据写入重跑待办")
    snap_svc, bf_svc, _ = fresh_services()
    bf, _ = bf_svc.create_entry({"批次编号": "BACKFILL-0900", "目标表名": "t", "开始日期": "2026-11-13"})
    # 新建一个草稿快照
    draft, _, _ = snap_svc.create_entry({"快照编号": "SNAP-0400", "表名称": "t", "快照时间": "2026-11-13 04:00:00"})
    _, err = bf_svc.run_action(bf["id"], "引用快照", {"快照编号": "SNAP-0400"})
    check("引用草稿被拒绝", "已完成" in err)
    _, err = bf_svc.run_action(bf["id"], "引用快照", {"快照编号": "SNAP-9999"})
    check("引用不存在快照被拒绝", "不存在" in err)
    _, err = bf_svc.run_action(bf["id"], "引用快照", {"快照编号": "SNAP-0003"})
    check("引用已清理快照被拒绝", "已完成" in err or "已清理" in err)
    ok, msg = bf_svc.run_action(bf["id"], "引用快照", {"快照编号": "SNAP-0002"})
    check("引用已完成+校验通过快照成功", ok is not None and ok["引用快照编号"] == "SNAP-0002")
    check("快照状态写入重跑待办", ok["引用快照状态"] == "已完成")
    check("保留期写入(30)", ok["引用保留天数"] == "30")
    check("回滚依据校验编号写入", ok["回滚依据校验编号"] == "VRF-SNAP-0002-001")
    check("重跑待办标记可回滚", ok["可回滚"] is True)
    snap2 = snap_svc.find_by_code("SNAP-0002")
    check("快照侧回写引用批次", "BACKFILL-0900" in snap2["引用批次"])
    # 启动重跑后不能再改引用
    bf_svc.run_action(bf["id"], "启动重跑", {})
    _, err = bf_svc.run_action(bf["id"], "引用快照", {"快照编号": "SNAP-0002"})
    check("运行中批次不允许更换引用", err != "")

    # ------------------------------------------------ 场景 7：清理与运行中引用冲突
    print("场景7 清理保护：运行中引用优先，失败保持原状态并留证据")
    snap_svc, bf_svc, store = fresh_services()
    bf2 = next(r for r in store.rows("backfill") if r["批次编号"] == "BACKFILL-0002")
    check("BACKFILL-0002 种子为运行中", bf2["status"] == "运行中")
    snap2 = snap_svc.find_by_code("SNAP-0002")
    before_status = snap2["status"]
    _, err = snap_svc.run_action(snap2["id"], "清理快照", {})
    check("清理被运行中引用拦下", "运行中的重跑" in err)
    check("清理失败后原状态保持已完成", snap_svc.find_by_code("SNAP-0002")["status"] == before_status == "已完成")
    # 暂停后仍算引用（非运行中即可清理），按规则只有“运行中引用”阻断
    bf_svc.run_action(bf2["id"], "暂停重跑", {})
    cleaned, msg = snap_svc.run_action(snap2["id"], "清理快照", {})
    check("引用暂停后允许清理", cleaned is not None and cleaned["status"] == "已清理")
    check("清理时间已记录", bool(cleaned["清理时间"]))
    check("已清理快照 validate_reference 拒绝", snap_svc.validate_reference("SNAP-0002")[1] != "")
    bf2_view = bf_svc.get_entry(bf2["id"])
    check("重跑页实时同步为已清理且不可回滚", bf2_view["引用快照状态"] == "已清理" and bf2_view["可回滚"] is False)
    check("回滚依据编号在清理后清空", bf2_view["回滚依据校验编号"] == "")
    check("历史证据仍保留在校验历史", len(cleaned["校验历史"]) >= 1 and cleaned["校验编号"] == "VRF-SNAP-0002-001")
    # 已清理不能再被任何批次引用
    bf_new, _ = bf_svc.create_entry({"批次编号": "BACKFILL-0901", "目标表名": "t", "开始日期": "2026-11-13"})
    _, err = bf_svc.run_action(bf_new["id"], "引用快照", {"快照编号": "SNAP-0002"})
    check("已清理快照不能被新批次引用", err != "")
    # 归档
    archived, _ = snap_svc.run_action(snap2["id"], "归档记录", {})
    check("归档只登记时间，状态保持已清理", archived["status"] == "已清理" and bool(archived["归档时间"]))

    # ------------------------------------------------ 场景 8：重跑状态机
    print("场景8 重跑状态机受控，非法跳变被拦截")
    snap_svc, bf_svc, _ = fresh_services()
    bf, _ = bf_svc.create_entry({"批次编号": "BACKFILL-0902", "目标表名": "t", "开始日期": "2026-11-13"})
    check("草稿不能直接完成", bf_svc.run_action(bf["id"], "完成重跑", {})[0] is None)
    bf_svc.run_action(bf["id"], "启动重跑", {})
    bf_svc.run_action(bf["id"], "暂停重跑", {})
    paused = bf_svc.get_entry(bf["id"])
    check("暂停后状态正确", paused["status"] == "已暂停" and paused["重跑状态"] == "已暂停")
    bf_svc.run_action(bf["id"], "恢复重跑", {})
    done, _ = bf_svc.run_action(bf["id"], "完成重跑", {})
    check("完成重跑进入已完成", done["status"] == "已完成" and done["pending"] is False)
    check("已完成不能再启动", bf_svc.run_action(bf["id"], "启动重跑", {})[0] is None)
    archived, _ = bf_svc.run_action(bf["id"], "归档批次", {})
    check("归档保留已完成状态并登记时间", archived["status"] == "已完成" and bool(archived["归档时间"]))
    # 批次编号幂等
    same, missing = bf_svc.create_entry({"批次编号": "BACKFILL-0902", "目标表名": "t", "开始日期": "2026-11-13"})
    check("重跑批次重复登记幂等", not missing and same["id"] == bf["id"])

    # ------------------------------------------------ 场景 9：HTTP 层冒烟
    print("场景9 FastAPI 路由冒烟（TestClient 不可用，用路由函数直调）")
    from fastapi import HTTPException
    from app.routers import backfill as router_bf
    from app.routers import snapshot as router_snap
    fresh_services()
    # 路由模块持有的是单例服务，重新指向新 store 后重建单例
    router_snap.snapshot_service = snapshot_module.SnapshotService()
    router_bf.backfill_service = backfill_module.BackfillService()
    created_result = router_snap.create_entry(type("P", (), {"values": {
        "快照编号": "SNAP-0500", "表名称": "t", "快照时间": "2026-11-13 05:00:00", "保留天数": "60"}})())
    check("POST 创建接口 ok 且保留天数回传", created_result.ok and created_result.entry["保留天数"] == "60")
    dup_result = router_snap.create_entry(type("P", (), {"values": {
        "快照编号": "SNAP-0500", "表名称": "t", "快照时间": "2026-11-13 05:00:00"}})())
    check("POST 重复创建返回幂等提示", dup_result.ok and "幂等" in dup_result.message)
    page = router_snap.list_entries(keyword=None, status=None)
    check("GET 列表含可回滚派生字段", all("可回滚" in item for item in page.items))
    rec = router_snap.recovery_evidence(keyword=None)
    check("GET /recovery 返回校验清单", rec["total"] >= 0 and "items" in rec)
    try:
        router_snap.get_entry(99999)
        check("不存在详情返回404", False)
    except HTTPException as exc:
        check("不存在详情返回404", exc.status_code == 404)
    bad_action = router_snap.run_action(1, type("P", (), {"values": {"action": "清理快照"}})())
    check("非法动作返回 ok=false 且带原因", not bad_action.ok and bad_action.message)

    print(f"\n结果：通过 {PASS} 条，失败 {FAIL} 条")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
