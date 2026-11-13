"""历史重跑业务规则。

重跑批次自身的状态机：
    草稿 ──启动重跑──▶ 运行中 ──暂停重跑──▶ 已暂停 ──恢复重跑──▶ 运行中
                        └── 完成重跑 ──▶ 已完成 ──归档批次（登记归档时间）

重跑引用快照的保护规则（与快照服务共用同一套判断，不允许重跑页另立口径）：
- 只有「已完成且恢复校验通过」的快照才能被引用，草稿/运行中/已清理一律拒绝；
- 引用时把快照状态、保留天数、校验结论和回滚依据校验编号写入重跑待办；
- 每次读取都按快照实时状态重新计算「可回滚」，已清理快照不会在旧页面上复活。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from app.services.snapshot import snapshot_service
from app.store import store

MODULE = "backfill"
REQUIRED_FIELDS = ["批次编号", "目标表名", "开始日期"]
PERSIST_FIELDS = [
    "批次编号", "目标表名", "开始日期", "结束日期", "并行数量", "覆盖模式", "异常区间",
]

STATUS_DRAFT = "草稿"
STATUS_RUNNING = "运行中"
STATUS_PAUSED = "已暂停"
STATUS_DONE = "已完成"
STATUS_ORDER = [STATUS_DRAFT, STATUS_RUNNING, STATUS_PAUSED, STATUS_DONE]

# 各状态允许执行的动作，全部前向受控，不能再把任意动作映射成固定状态。
ACTION_RULES: dict[str, set[str]] = {
    STATUS_DRAFT: {"启动重跑", "引用快照"},
    STATUS_RUNNING: {"暂停重跑", "完成重跑"},
    STATUS_PAUSED: {"恢复重跑", "引用快照"},
    STATUS_DONE: {"归档批次"},
}

# 引用快照时随批次固化的证据字段（快照服务是唯一数据源，这里只做快照备份）。
REF_FIELDS = ["引用快照编号", "引用快照状态", "引用保留天数", "引用恢复校验", "回滚依据校验编号"]


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class BackfillService:
    # ------------------------------------------------------------------ 查询
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("批次编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return [self._decorate(dict(row)) for row in rows[start:start + size]], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        row = store.find(MODULE, entry_id)
        return self._decorate(dict(row)) if row is not None else None

    # ------------------------------------------------------------ 登记批次
    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [
            field
            for field in REQUIRED_FIELDS
            if not str(values.get(field) or "").strip()
        ]
        if missing:
            return None, missing
        # 批次编号同样做幂等：重复登记直接返回原记录，不产生第二条。
        existed = next(
            (row for row in store.rows(MODULE)
             if str(row.get("批次编号", "")).strip() == str(values["批次编号"]).strip()),
            None,
        )
        if existed is not None:
            return self._decorate(dict(existed)), []

        rows = store.rows(MODULE)
        entry: dict[str, Any] = {
            "id": max((int(row.get("id", 0)) for row in rows), default=0) + 1,
            "status": STATUS_DRAFT,
            "pending": True,
            "abnormal": False,
            "重跑状态": STATUS_DRAFT,
            "结束日期": "",
            "并行数量": "",
            "覆盖模式": "",
            "异常区间": "",
            "归档时间": "",
        }
        for field in REF_FIELDS:
            entry[field] = ""
        entry["可回滚"] = False
        for field in PERSIST_FIELDS:
            entry[field] = str(values.get(field) or "").strip()
        rows.append(entry)
        return self._decorate(dict(entry)), []

    # ------------------------------------------------------------ 动作状态机
    def run_action(
        self, entry_id: int, action: str, values: dict[str, Any] | None = None
    ) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"重跑批次 {entry_id} 不存在或已归档"
        values = values or {}
        current = str(entry.get("status"))
        if action not in ACTION_RULES.get(current, set()):
            allowed = "、".join(sorted(ACTION_RULES.get(current, set()))) or "无可用动作"
            return None, f"批次 {entry.get('批次编号')} 当前为「{current}」，只允许：{allowed}"

        if action == "引用快照":
            return self._attach_snapshot(entry, values)

        transitions = {
            "启动重跑": STATUS_RUNNING,
            "暂停重跑": STATUS_PAUSED,
            "恢复重跑": STATUS_RUNNING,
            "完成重跑": STATUS_DONE,
        }
        if action in transitions:
            target = transitions[action]
            entry["status"] = target
            entry["重跑状态"] = target
            entry["pending"] = target != STATUS_DONE
            return self._decorate(dict(entry)), f"重跑批次已{action}，当前状态：{target}"

        # 归档批次：只做归档登记，状态保留已完成。
        entry["归档时间"] = _now()
        return self._decorate(dict(entry)), "重跑批次已归档登记"

    def _attach_snapshot(
        self, entry: dict[str, Any], values: dict[str, Any]
    ) -> tuple[dict[str, Any] | None, str]:
        snapshot_code = str(values.get("快照编号") or "").strip()
        if not snapshot_code:
            return None, "引用快照时必须填写快照编号"
        snapshot, error = snapshot_service.validate_reference(snapshot_code)
        if snapshot is None:
            return None, error

        # 引用成立：快照状态与恢复证据写入重跑待办，并在快照侧回写引用批次。
        entry["引用快照编号"] = snapshot_code
        entry["引用快照状态"] = snapshot["status"]
        entry["引用保留天数"] = snapshot.get("保留天数", "")
        entry["引用恢复校验"] = snapshot.get("恢复校验", "")
        entry["回滚依据校验编号"] = snapshot.get("校验编号", "")
        referenced = set(snapshot_service.all_reference_codes(snapshot_code))
        referenced.add(str(entry["批次编号"]))
        snapshot["引用批次"] = "、".join(sorted(referenced))
        return self._decorate(dict(entry)), f"已引用有效快照 {snapshot_code}，回滚依据：{entry['回滚依据校验编号']}"

    # ------------------------------------------------------------ 视图组装
    def _decorate(self, entry: dict[str, Any]) -> dict[str, Any]:
        """重跑页的快照状态、保留期、校验结论和可回滚标记都以快照实时数据为准。"""
        entry["重跑状态"] = entry.get("status")
        snapshot_code = str(entry.get("引用快照编号", "")).strip()
        if not snapshot_code:
            entry["可回滚"] = False
            entry["回滚依据校验编号"] = ""
            return entry

        live = snapshot_service.find_by_code(snapshot_code)
        if live is None:
            entry["可回滚"] = False
            return entry
        # 快照页与重跑页必须显示同一份状态与证据，所以这里直接用快照实时值覆盖展示字段。
        entry["引用快照状态"] = live.get("status")
        entry["引用保留天数"] = live.get("保留天数", "")
        entry["引用恢复校验"] = live.get("恢复校验", "")
        entry["可回滚"] = snapshot_service.is_rollbackable(live)
        entry["回滚依据校验编号"] = live.get("校验编号", "") if entry["可回滚"] else ""
        return entry


backfill_service = BackfillService()
