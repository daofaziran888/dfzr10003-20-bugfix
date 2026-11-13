"""快照管理业务规则。

状态机（只能按恢复校验阶段推进，不允许跳变）：
    草稿 ──开始校验──▶ 运行中 ──校验通过──▶ 已完成 ──清理快照──▶ 已清理
     ▲                    │
     └──── 校验未通过 ─────┘   （未通过就退回草稿，并保留失败证据）

创建快照只登记元数据并做存储校验：尚未开始恢复校验前一律留在「草稿」，
草稿快照永远不能冒充可用快照被重跑引用。重跑只允许引用「已完成且恢复校验
通过」的快照；清理前必须确认没有运行中的重跑引用。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from app.store import store

MODULE = "snapshot"
BACKFILL_MODULE = "backfill"

REQUIRED_FIELDS = ["快照编号", "表名称", "快照时间"]
# 登记时允许一并写入的业务字段；其余字段一律由状态机维护，前端无法绕过。
PERSIST_FIELDS = ["快照编号", "表名称", "快照时间", "快照大小", "保留天数", "创建批次"]

STATUS_DRAFT = "草稿"
STATUS_RUNNING = "运行中"
STATUS_DONE = "已完成"
STATUS_CLEANED = "已清理"
STATUS_ORDER = [STATUS_DRAFT, STATUS_RUNNING, STATUS_DONE, STATUS_CLEANED]

# 恢复校验结论
CHECK_PENDING = "待校验"
CHECK_RUNNING = "校验中"
CHECK_PASSED = "通过"
CHECK_FAILED = "未通过"

# 存储校验结果
STORAGE_PENDING = "待校验"
STORAGE_PASSED = "通过"
STORAGE_FAILED = "失败"

# 各状态允许执行的动作；动作合法性全部收敛在这里，接口层不做状态判断。
ACTION_RULES: dict[str, set[str]] = {
    STATUS_DRAFT: {"开始校验", "重试存储校验"},
    STATUS_RUNNING: {"校验通过", "校验未通过"},
    STATUS_DONE: {"清理快照"},
    STATUS_CLEANED: {"归档记录"},
}


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class SnapshotService:
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
            rows = [row for row in rows if keyword in str(row.get("快照编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return [self._decorate(dict(row)) for row in rows[start:start + size]], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        row = store.find(MODULE, entry_id)
        return self._decorate(dict(row)) if row is not None else None

    def find_by_code(self, snapshot_code: str) -> dict[str, Any] | None:
        """按业务主键「快照编号」找持久化记录（返回可变原对象，供状态机内部使用）。"""
        code = str(snapshot_code or "").strip()
        if not code:
            return None
        for row in store.rows(MODULE):
            if str(row.get("快照编号", "")).strip() == code:
                return row
        return None

    # ------------------------------------------------------------ 幂等创建
    def create_entry(
        self, values: dict[str, Any]
    ) -> tuple[dict[str, Any] | None, list[str], bool]:
        """登记快照。返回（记录, 缺失字段, 是否新建）。

        同一快照编号重复提交是幂等的：已存在就返回同一份完整记录，元数据不会
        被二次提交；若上一次停在存储校验失败，则只重试存储校验这一步。
        """
        missing = [
            field
            for field in REQUIRED_FIELDS
            if not str(values.get(field) or "").strip()
        ]
        if missing:
            return None, missing, False

        existing = self.find_by_code(str(values["快照编号"]).strip())
        if existing is not None:
            if existing.get("存储校验") == STORAGE_FAILED:
                self._run_storage_check(existing, values)
            return self._decorate(dict(existing)), [], False

        rows = store.rows(MODULE)
        entry: dict[str, Any] = {
            "id": max((int(row.get("id", 0)) for row in rows), default=0) + 1,
            "status": STATUS_DRAFT,
            "pending": True,
            "abnormal": False,
            "快照大小": "",
            "保留天数": "",
            "创建批次": "",
            "恢复校验": CHECK_PENDING,
            # 「快照状态」是展示字段，始终与状态机的 status 保持同源，不允许各页面各写各的。
            "快照状态": STATUS_DRAFT,
            "存储校验": STORAGE_PENDING,
            "校验阶段": "尚未开始校验",
            "校验结论": "",
            "校验编号": "",
            "失败证据": "",
            "最近校验时间": "",
            "清理时间": "",
            "归档时间": "",
            "引用批次": "",
            "校验历史": [],
        }
        for field in PERSIST_FIELDS:
            entry[field] = str(values.get(field) or "").strip()
        rows.append(entry)
        # 元数据先落库，再走存储校验；校验失败也保留记录，下一次按同一快照编号续跑。
        self._run_storage_check(entry, values)
        return self._decorate(dict(entry)), [], True

    def _run_storage_check(self, entry: dict[str, Any], values: dict[str, Any]) -> None:
        """模拟存储层可用性校验。测试/前端可通过「模拟存储校验结果=失败」注入故障。"""
        failed = str(values.get("模拟存储校验结果") or "").strip() == "失败"
        if failed:
            entry["存储校验"] = STORAGE_FAILED
            entry["失败证据"] = f"[{_now()}] 存储校验失败：存储桶不可访问，快照数据未完成落盘"
            entry["abnormal"] = True
            # 存储校验不通过时状态保持草稿，绝不能进入后续校验阶段。
            entry["status"] = STATUS_DRAFT
            entry["快照状态"] = STATUS_DRAFT
            entry["pending"] = True
        else:
            entry["存储校验"] = STORAGE_PASSED
            if entry.get("校验阶段") == "尚未开始校验":
                entry["失败证据"] = ""
                entry["abnormal"] = False

    # ------------------------------------------------------------ 动作状态机
    def run_action(
        self, entry_id: int, action: str, values: dict[str, Any] | None = None
    ) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"快照记录 {entry_id} 不存在或已归档"
        values = values or {}
        current = str(entry.get("status"))
        if action not in ACTION_RULES.get(current, set()):
            allowed = "、".join(sorted(ACTION_RULES.get(current, set()))) or "无可用动作"
            return None, f"快照 {entry.get('快照编号')} 当前为「{current}」，只允许：{allowed}"

        if action == "重试存储校验":
            if entry.get("存储校验") != STORAGE_FAILED:
                return None, "存储校验并未失败，无需重试"
            self._run_storage_check(entry, values)
            if entry.get("存储校验") == STORAGE_FAILED:
                return self._decorate(dict(entry)), "存储校验重试后仍失败，快照保持草稿并保留失败证据"
            return self._decorate(dict(entry)), "存储校验已重试通过，快照仍停留在草稿，等待开始恢复校验"

        if action == "开始校验":
            if entry.get("存储校验") != STORAGE_PASSED:
                return None, "存储校验未通过，不能开始恢复校验"
            entry["status"] = STATUS_RUNNING
            entry["快照状态"] = STATUS_RUNNING
            entry["pending"] = True
            entry["校验阶段"] = "校验中"
            entry["恢复校验"] = CHECK_RUNNING
            return self._decorate(dict(entry)), "恢复校验已开始，快照进入运行中"

        if action == "校验通过":
            conclusion = str(values.get("校验结论") or "").strip() or "恢复抽样比对一致，快照可用于回滚"
            evidence_no = self._next_evidence_no(str(entry["快照编号"]))
            record = {
                "校验编号": evidence_no,
                "结果": CHECK_PASSED,
                "结论": conclusion,
                "时间": _now(),
            }
            entry.setdefault("校验历史", []).append(record)
            entry["status"] = STATUS_DONE
            entry["快照状态"] = STATUS_DONE
            entry["pending"] = False
            entry["abnormal"] = False
            entry["校验阶段"] = "校验通过"
            entry["恢复校验"] = CHECK_PASSED
            entry["校验结论"] = conclusion
            entry["校验编号"] = evidence_no
            entry["失败证据"] = ""
            entry["最近校验时间"] = record["时间"]
            return self._decorate(dict(entry)), f"恢复校验通过（{evidence_no}），快照已完成，可被重跑引用"

        if action == "校验未通过":
            reason = str(values.get("失败证据") or values.get("校验结论") or "").strip()
            reason = reason or "恢复抽样比对存在差异，快照不可用于回滚"
            evidence_no = self._next_evidence_no(str(entry["快照编号"]))
            record = {
                "校验编号": evidence_no,
                "结果": CHECK_FAILED,
                "结论": reason,
                "时间": _now(),
            }
            entry.setdefault("校验历史", []).append(record)
            # 校验未通过：不允许进入已完成，退回草稿并保留失败证据。
            entry["status"] = STATUS_DRAFT
            entry["快照状态"] = STATUS_DRAFT
            entry["pending"] = True
            entry["abnormal"] = True
            entry["校验阶段"] = "校验未通过"
            entry["恢复校验"] = CHECK_FAILED
            entry["失败证据"] = f"[{record['时间']}] {evidence_no} {reason}"
            entry["最近校验时间"] = record["时间"]
            return self._decorate(dict(entry)), f"恢复校验未通过（{evidence_no}），快照保持草稿并保留失败证据"

        if action == "清理快照":
            running = self.running_reference_codes(str(entry.get("快照编号", "")))
            if running:
                # 引用与清理冲突时，以仍被运行中重跑引用的一侧为准：拒绝清理且原状态不动。
                return (
                    None,
                    f"快照仍被运行中的重跑批次引用（{'、'.join(running)}），确认无运行中引用后才能清理",
                )
            entry["status"] = STATUS_CLEANED
            entry["快照状态"] = STATUS_CLEANED
            entry["pending"] = False
            entry["清理时间"] = _now()
            return self._decorate(dict(entry)), "快照已清理归档，历史重跑不能再把它当作可回滚快照"

        # 归档记录：已清理快照的归档登记，状态不再变化。
        entry["归档时间"] = _now()
        return self._decorate(dict(entry)), "清理记录已归档"

    def _next_evidence_no(self, snapshot_code: str) -> str:
        entry = self.find_by_code(snapshot_code)
        seq = len(entry.get("校验历史", [])) + 1 if entry is not None else 1
        return f"VRF-{snapshot_code}-{seq:03d}"

    # ------------------------------------------------------------ 引用与回滚
    def running_reference_codes(self, snapshot_code: str) -> list[str]:
        """返回仍在运行中、且引用该快照的重跑批次编号。"""
        return [
            str(row.get("批次编号", ""))
            for row in store.rows(BACKFILL_MODULE)
            if str(row.get("引用快照编号", "")).strip() == snapshot_code
            and row.get("status") == STATUS_RUNNING
        ]

    def all_reference_codes(self, snapshot_code: str) -> list[str]:
        return [
            str(row.get("批次编号", ""))
            for row in store.rows(BACKFILL_MODULE)
            if str(row.get("引用快照编号", "")).strip() == snapshot_code
        ]

    def validate_reference(self, snapshot_code: str) -> tuple[dict[str, Any] | None, str]:
        """重跑引用快照前的统一校验口径，快照页与重跑页共用这一份判断。"""
        entry = self.find_by_code(snapshot_code)
        if entry is None:
            return None, f"快照 {snapshot_code} 不存在"
        if entry.get("status") != STATUS_DONE:
            return None, f"快照 {snapshot_code} 当前为「{entry.get('status')}」，只有已完成快照才能被重跑引用"
        if entry.get("恢复校验") != CHECK_PASSED or not entry.get("校验编号"):
            return None, f"快照 {snapshot_code} 恢复校验未通过，不能作为重跑回滚依据"
        return entry, ""

    def is_rollbackable(self, entry: dict[str, Any]) -> bool:
        """可回滚只看实时状态：已完成 + 恢复校验通过 + 仍有校验证据。已清理一律不可回滚。"""
        return (
            entry.get("status") == STATUS_DONE
            and entry.get("恢复校验") == CHECK_PASSED
            and bool(entry.get("校验编号"))
        )

    def recovery_evidence(self, keyword: str | None = None) -> list[dict[str, Any]]:
        """恢复校验结果清单：每次校验（含失败）都留痕，回滚依据取最近一次通过结论。"""
        result: list[dict[str, Any]] = []
        for row in store.rows(MODULE):
            if keyword and keyword not in str(row.get("快照编号", "")):
                continue
            for record in row.get("校验历史", []):
                result.append({
                    "校验编号": record["校验编号"],
                    "快照编号": row.get("快照编号"),
                    "表名称": row.get("表名称"),
                    "保留天数": row.get("保留天数"),
                    "快照状态": row.get("status"),
                    "校验结果": record["结果"],
                    "校验结论": record["结论"],
                    "校验时间": record["时间"],
                    "引用批次": row.get("引用批次") or "",
                    "可回滚依据": self.is_rollbackable(row) and record["校验编号"] == row.get("校验编号"),
                })
        return result

    # ------------------------------------------------------------ 视图组装
    def _decorate(self, entry: dict[str, Any]) -> dict[str, Any]:
        """给持久化记录补实时派生字段；派生值不落库，避免旧页面把已清理快照再标成可回滚。"""
        code = str(entry.get("快照编号", ""))
        entry["快照状态"] = entry.get("status")
        entry["运行中引用"] = self.running_reference_codes(code)
        entry["可回滚"] = self.is_rollbackable(entry)
        entry["回滚依据校验编号"] = entry.get("校验编号") if entry["可回滚"] else ""
        return entry


# 回滚依据口径（写进代码注释，方便评审）：
# 同一快照可能经历多次恢复校验，统一以「最近一次校验通过」生成的校验编号作为
# 重跑回滚依据；校验未通过的结论只作失败证据，永不作为回滚依据。快照一旦清理，
# 即使历史证据还在，可回滚标记也实时变为 false。
snapshot_service = SnapshotService()
