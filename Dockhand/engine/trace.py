"""执行轨迹（trace）与诊断包。

这不是调试工具，是产品的核心能力。理由：

这个插件最终装在别人的机器上，你看不到对方的屏幕。所有支持成本都来自
"用户说它不对，而你不知道当时发生了什么"。没有 trace 的时候，你只能
问用户"你截个图给我看看"——三轮对话才能定位一个本可以自动记录的问题。

所以这里的设计原则是：**每一次可能产生副作用的行为，都必须留下一条
可回放的记录**，包含：当时的界面状态、识别置信度、做了什么决策、
结果属于哪一类错误。有了这些，用户发一个 zip 过来，你就能复现。

存在两个硬限制（公开版必须有，自用版可以不管）：

1. 体积。trace 会持续增长，必须按天分文件 + 只保留最近 N 天，
   否则会在用户的 Pi 上吃掉几个 G。这是分发出去最容易翻车的地方——
   内存和磁盘不是你的，是用户的。
2. 隐私。截图和日志里可能有玩家昵称、联盟名、UID。导出诊断包时
   必须脱敏，且默认不包含截图（只包含结构化记录），要附截图得用户
   自己勾。这一条不做，你迟早会收到别人的账号信息。
"""

from __future__ import annotations

import json
import os
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# 保留天数与单日行数上限。取这两个值的原因：
# - 7 天：足够覆盖"用户昨天用了今天来报错"的绝大多数场景
# - 5000 行/天：按每次操作 10 条 trace 估，等于 500 次操作，
#   正常玩家一天远远用不到，异常循环时能截住
KEEP_DAYS = 7
MAX_LINES_PER_DAY = 5000

# 诊断包里这些字段一律打码。键名匹配是大小写不敏感的。
_SECRET_KEYS = ("token", "password", "secret", "key", "cookie", "session")


def _mask(value: Any) -> str:
    """把敏感值变成 `ab****yz` 形式。

    保留首尾各 2 个字符是为了让用户自己能核对"填的是不是这个"，
    全打码会让人无法判断是配置错了还是别的问题。
    """
    text = str(value or "")
    if not text:
        return ""
    if len(text) <= 4:
        return "*" * len(text)
    return text[:2] + "*" * (len(text) - 4) + text[-2:]


def scrub(obj: Any) -> Any:
    """递归脱敏。

    对 dict 按键名匹配，对 list 逐项处理，对 str 不做内容替换
    （不做"看起来像 token 的字符串"这种猜测，误伤比漏打更烦）。
    """
    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        for key, value in obj.items():
            if any(secret in str(key).lower() for secret in _SECRET_KEYS):
                out[key] = _mask(value)
            else:
                out[key] = scrub(value)
        return out
    if isinstance(obj, (list, tuple)):
        return [scrub(item) for item in obj]
    return obj


@dataclass
class StepRecord:
    """一步操作的记录。

    字段刻意少而固定，因为下游（诊断脚本、你自己看）依赖字段名稳定。
    新增字段可以，改名不行。
    """

    ts: float
    kind: str                     # observe / skill / gate_denied / pause ...
    state: str = ""               # 执行时的界面状态
    confidence: float = 0.0       # 状态识别置信度
    skill: str = ""               # 技能名（kind=skill 时有值）
    ok: bool = True
    error_class: str = ""         # 失败时的错误分类
    message: str = ""             # 一句话说明
    would_do: list[Any] = field(default_factory=list)  # 演练模式下"本来会做什么"
    elapsed_ms: int = 0
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data = {
            "ts": round(self.ts, 3),
            "kind": self.kind,
            "state": self.state,
            "confidence": round(float(self.confidence or 0), 3),
            "skill": self.skill,
            "ok": bool(self.ok),
        }
        # 空字段不写入，控制体积。读的时候用 .get() 即可。
        if self.error_class:
            data["error_class"] = self.error_class
        if self.message:
            data["message"] = self.message[:500]
        if self.would_do:
            data["would_do"] = self.would_do[:20]
        if self.elapsed_ms:
            data["elapsed_ms"] = int(self.elapsed_ms)
        if self.extra:
            data["extra"] = scrub(self.extra)
        return data


class RunRecorder:
    """一次运行（一个 goal）的记录器。

    用法是上下文管理器，保证异常路径也会落盘：

        with recorder.run("collect", dry_run=True) as run:
            run.step(StepRecord(...))
            run.finish(ok=True)

    为什么用上下文管理器而不是手动 start/end：异常路径最容易漏写 end，
    而恰恰是异常路径的记录最有价值。
    """

    def __init__(self, store: "TraceStore", goal: str, dry_run: bool):
        self.store = store
        self.goal = goal
        self.dry_run = dry_run
        self.run_id = "%s-%d" % (time.strftime("%H%M%S"), int(time.time() * 1000) % 1000)
        self.started = time.time()
        self.steps: list[StepRecord] = []
        self.ok = False
        self.error_class = ""
        self.message = ""
        self._finished = False

    def __enter__(self) -> "RunRecorder":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        if exc is not None and not self._finished:
            # 未捕获异常也要留下记录，否则最严重的故障恰好没有 trace
            self.finish(ok=False, error_class="E_UNKNOWN", message="%s: %s" % (type(exc).__name__, exc))
        elif not self._finished:
            self.finish(ok=False, message="未正常收尾")
        return False  # 不吞异常，异常照常向上传播

    def step(self, record: StepRecord) -> None:
        self.steps.append(record)

    def finish(self, ok: bool, error_class: str = "", message: str = "") -> None:
        if self._finished:
            return
        self._finished = True
        self.ok = bool(ok)
        self.error_class = error_class
        self.message = message
        self.store.write(self)

    def payload(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "goal": self.goal,
            "dry_run": self.dry_run,
            "started_at": round(self.started, 3),
            "ended_at": round(time.time(), 3),
            "elapsed_ms": int((time.time() - self.started) * 1000),
            "ok": self.ok,
            "error_class": self.error_class,
            "message": self.message[:500],
            "steps": [s.to_dict() for s in self.steps],
        }


class TraceStore:
    """按天分文件的 JSONL 存储。

    选 JSONL 而不是 SQLite：诊断时用户只需要 `grep`，不需要装任何东西，
    也不需要理解表结构。这是为"用户能自助"服务的。
    """

    def __init__(self, root: Path | str):
        self.root = Path(root)

    def _file_for(self, ts: float | None = None) -> Path:
        day = time.strftime("%Y%m%d", time.localtime(ts or time.time()))
        return self.root / ("trace-%s.jsonl" % day)

    def write(self, run: RunRecorder) -> None:
        """落盘一次运行。

        写失败不能抛异常——trace 写不进去不应该导致业务失败。
        这是有意的降级：宁可丢记录，也不能因为磁盘满让插件崩掉。
        """
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            path = self._file_for(run.started)
            line = json.dumps(run.payload(), ensure_ascii=False)
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
            self._enforce_limits(path)
        except Exception:  # noqa: BLE001
            pass

    def _enforce_limits(self, path: Path) -> None:
        """控制单文件体积：超出行数就砍掉最老的行。

        实现上整读整写，看起来笨，但这文件最多几 MB，比任何增量方案都可靠。
        在用户磁盘上做"聪明"的滚动，出错代价太高。
        """
        try:
            with open(path, "r", encoding="utf-8") as fh:
                lines = fh.readlines()
            if len(lines) <= MAX_LINES_PER_DAY:
                return
            with open(path, "w", encoding="utf-8") as fh:
                fh.writelines(lines[-MAX_LINES_PER_DAY:])
        except Exception:  # noqa: BLE001
            pass

    def recent_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        """取最近若干次运行（跨天，从新到旧）。"""
        runs: list[dict[str, Any]] = []
        for path in sorted(self.root.glob("trace-*.jsonl"), reverse=True):
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    for line in fh:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            runs.append(json.loads(line))
                        except json.JSONDecodeError:
                            continue
            except OSError:
                continue
            if len(runs) >= limit:
                break
        runs.sort(key=lambda r: r.get("started_at", 0), reverse=True)
        return runs[:limit]

    def cleanup(self) -> int:
        """删除超过保留天数的文件，返回删除个数。

        在 on_load 里调一次即可。不需要定时任务——插件启动频率足够低。
        """
        cutoff = time.time() - KEEP_DAYS * 86400
        removed = 0
        for path in self.root.glob("trace-*.jsonl"):
            try:
                stamp = path.stem.replace("trace-", "")
                file_ts = time.mktime(time.strptime(stamp, "%Y%m%d"))
            except Exception:  # noqa: BLE001
                continue
            if file_ts < cutoff:
                try:
                    os.remove(path)
                    removed += 1
                except OSError:
                    pass
        return removed


def build_diagnostics(
    dest_dir: Path | str,
    *,
    plugin_version: str,
    config_snapshot: dict[str, Any],
    agent_snapshot: dict[str, Any] | None = None,
    gate_snapshot: dict[str, Any] | None = None,
    trace_store: TraceStore | None = None,
    include_screenshots: bool = False,
    screenshot_dir: Path | str | None = None,
) -> Path:
    """打一个诊断包。

    只包含结构化数据，默认不含截图。截图要用户显式要求才放进去，
    因为截图里可能有昵称、联盟、UID——这是别人的隐私，不是你的调试素材。

    返回 zip 的绝对路径，调用方直接把这个路径发给用户。
    """
    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    zip_path = dest / ("shipctl-diag-%s.zip" % stamp)

    manifest = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "plugin_version": plugin_version,
        "os": {"platform": os.name, "path_sep": os.sep},
        "includes_screenshots": bool(include_screenshots),
        "note": "本包含脱敏后的配置与执行记录；如含截图，请自行确认无敏感信息后再分享",
    }

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        zf.writestr("config.json", json.dumps(scrub(config_snapshot), ensure_ascii=False, indent=2))
        if agent_snapshot is not None:
            zf.writestr("agent.json", json.dumps(scrub(agent_snapshot), ensure_ascii=False, indent=2))
        if gate_snapshot is not None:
            zf.writestr("gate.json", json.dumps(scrub(gate_snapshot), ensure_ascii=False, indent=2))

        if trace_store is not None:
            runs = trace_store.recent_runs(limit=50)
            zf.writestr("recent_runs.json", json.dumps(scrub(runs), ensure_ascii=False, indent=2))

        if include_screenshots and screenshot_dir:
            shot_dir = Path(screenshot_dir)
            if shot_dir.is_dir():
                # 只取最近 5 张，避免包太大发不出去
                shots = sorted(shot_dir.glob("*.png"), key=lambda p: p.stat().st_mtime, reverse=True)[:5]
                for shot in shots:
                    try:
                        zf.write(shot, arcname="screenshots/" + shot.name)
                    except OSError:
                        continue

    return zip_path
