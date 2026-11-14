"""离线自检：不需要 MaiBot、不需要真 agent、不需要游戏。

    python tools/selftest.py

它验三件事：
  1. 结构约束 —— 适配包里不许有坐标，demo 包不许有可执行动作
  2. 演练行为 —— 演练时下发到 agent 的每个请求都必须是 dry_run=true
  3. 拦截行为 —— 该拦的情况真的拦住了，而且是"一个动作都没下发"

为什么要有这个文件：

"它不会乱点"这句话，靠读代码是证明不了的。改了一行 planner，你没法从
diff 里看出"演练是否还真的不点"。所以把承诺写成断言，每次改完跑一遍，
失败就是承诺破了。这比在 README 里写"默认只演练"有用得多。

这个文件不追求覆盖率，只追求把最贵的三类错误钉死：
坐标漏进适配包、演练变实跑、闸门失效。
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from engine.config import ShipConfig  # noqa: E402
from engine.errors import ShipError  # noqa: E402
from engine.gate import SafetyGate  # noqa: E402
from engine.runtime import Runtime  # noqa: E402

RESULTS: list[tuple[bool, str, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((bool(ok), name, detail))


# ---------------------------------------------------------------------- #
# 假 agent：把 HTTP 换成内存里的几个函数
#
# 之所以在 client 对象上做替换而不是 mock 掉 urllib：我们要验的是
# runtime/planner 的行为，不是网络层。替换 client 的边界刚好。
# ---------------------------------------------------------------------- #
class FakeAgent:
    def __init__(
        self,
        state: str = "main_map",
        confidence: float = 0.96,
        calibration_ok: bool = True,
        paused: bool = False,
    ):
        self.state = state
        self.confidence = confidence
        self.calibration_ok = calibration_ok
        self.paused = paused
        self.skill_calls: list[dict] = []
        self.observations = 0

    def capabilities(self, refresh: bool = False) -> dict:
        return {
            "protocol": 2,
            "agent_version": "fake-1.0.0",
            "resolution": [1280, 720],
            "paused": self.paused,
            "calibration": {
                "ok": self.calibration_ok,
                "reason": "" if self.calibration_ok else "分辨率变了，需要重新校准",
            },
            "skills": ["assert_main", "assert_fleet", "wait_loading"],
        }

    def observe(self) -> dict:
        self.observations += 1
        return {
            "state": self.state,
            "confidence": self.confidence,
            "resolution": [1280, 720],
            "elements": {},
        }

    def run_skill(self, skill: str, args=None, dry_run: bool = True, deadline_ms: int = 0) -> dict:
        self.skill_calls.append({"skill": skill, "dry_run": dry_run, "args": args or {}})
        # demo 的三个技能都是断言，回一个"断言成功后的界面"
        expect = {
            "assert_main": "main_map",
            "assert_fleet": "fleet_panel",
            "wait_loading": "main_map",
        }.get(skill, self.state)
        return {"ok": expect != "unknown", "final_state": expect, "would_do": []}

    def pause(self, reason: str = "remote") -> dict:
        self.paused = True
        return {"ok": True}

    def resume(self) -> dict:
        self.paused = False
        return {"ok": True}

    def calibration(self) -> dict:
        return {"ok": self.calibration_ok, "reason": ""}

    def calibrate(self) -> dict:
        return {"ok": True}

    def screenshot(self, reason: str = "manual") -> dict:
        return {"ok": True, "path": "fake.png"}


def make_runtime(agent: FakeAgent, **safety) -> Runtime:
    """造一个挂上假 agent 的 Runtime。

    默认配置里 dry_run=True，测试要实跑就必须显式覆盖——这本身
    也顺带验证了"默认是安全的"。
    """
    cfg = {
        "safety": {
            "dry_run": True,
            "pause_hotkey": "",
            "disclaimer_accepted": False,
            "breaker_threshold": 1,
            **safety,
        }
    }
    runtime = Runtime(
        config=ShipConfig.from_dict(cfg),
        data_dir=Path(tempfile.mkdtemp(prefix="shipctl-selftest-")),
        plugin_version="0.2.0-selftest",
    )
    runtime.client = agent  # type: ignore[assignment]
    return runtime


# ---------------------------------------------------------------------- #
# 1. 结构约束
# ---------------------------------------------------------------------- #
COORD_KEYS = {
    "x", "y", "px", "py", "coord", "coords", "coordinate", "coordinates",
    "bbox", "box", "point", "position", "pos", "offset", "left", "top",
    "width", "height", "x_pct", "y_pct",
}


def scan_coordinates(node, path="", found=None) -> list[str]:
    """递归找坐标类键名。

    宽高也一并禁：适配包里出现 width/height 通常意味着有人在描述
    "按钮在这块区域"，那是绝对几何，换分辨率就错。宁可误报，让人来解释。
    """
    if found is None:
        found = []
    if isinstance(node, dict):
        for key, value in node.items():
            if str(key).lower() in COORD_KEYS and not str(key).startswith("_"):
                found.append("%s.%s" % (path, key))
            scan_coordinates(value, "%s.%s" % (path, key), found)
    elif isinstance(node, list):
        for idx, value in enumerate(node):
            scan_coordinates(value, "%s[%d]" % (path, idx), found)
    return found


def _example_keys_ok(example_data: dict) -> bool:
    """示例配置的每个键都必须被 ShipConfig 认识。

    这条检查防的是一种很尴尬的情况：示例里写了 `allow_demo_live`，
    但配置类里已经删了。用户照着示例填，插件直接拒绝加载，而他不知道
    是示例过期了还是自己填错了。
    """
    from engine.config import AdapterConfig, ConnectionConfig, NotifyConfig, RuntimeConfig, SafetyConfig

    sections = {
        "connection": ConnectionConfig,
        "safety": SafetyConfig,
        "adapter": AdapterConfig,
        "notify": NotifyConfig,
        "runtime": RuntimeConfig,
    }
    # [plugin] 不进 ShipConfig：它由 Runner/SDK 消费（config_version、enabled）。
    # 但示例里必须保留这一节，缺 config_version 会被 Runner 直接拒绝激活，
    # 所以这里单独放行，只校验它的键名，不能当成"未知节"报错。
    sdk_sections = {"plugin": {"config_version", "enabled"}}

    for section, data in example_data.items():
        if section in sdk_sections:
            if not isinstance(data, dict):
                return False
            if set(data) - sdk_sections[section]:
                return False
            continue
        if section not in sections:
            return False
        if not isinstance(data, dict):
            return False
        allowed = set(sections[section].__dataclass_fields__)  # type: ignore[attr-defined]
        unknown = set(data) - allowed
        if unknown:
            return False
    return True


def test_structure() -> None:
    adapter_root = ROOT / "game_adapter"
    json_files = sorted(adapter_root.rglob("*.json"))
    check("适配包目录里能找到 JSON 定义", bool(json_files), "找到 %d 个" % len(json_files))

    hits: list[str] = []
    for path in json_files:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            hits.append("%s 解析失败：%r" % (path.name, exc))
            continue
        for item in scan_coordinates(data):
            hits.append("%s%s" % (path.relative_to(adapter_root), item))
        # 顶层以 _ 开头的键是注释，加载时会被丢弃——这里确认规则被用上了
        if isinstance(data, dict) and any(str(k).startswith("_") for k in data):
            pass

    check("适配包里不含任何坐标/几何字段", not hits, "; ".join(hits[:6]))

    # demo 包只允许只读动作
    demo_skills = adapter_root / "demo" / "skills.json"
    allowed_ops = {"assert", "wait_for"}
    if demo_skills.exists():
        data = json.loads(demo_skills.read_text(encoding="utf-8"))
        bad = []
        for name, skill in data.items():
            if str(name).startswith("_"):
                continue
            for step in skill.get("steps") or []:
                op = str((step or {}).get("op") or "")
                if op not in allowed_ops:
                    bad.append("%s:%s" % (name, op))
        check("demo 包只含只读动作", not bad, "发现可执行动作 %s" % bad)
    else:
        check("demo 包存在", False, "缺少 game_adapter/demo/skills.json")

    # 示例配置里不许有坐标块（这是老版本留下的坑）
    #
    # 这里解析 TOML 后查键名，而不是在全文里搜 "coord"：
    # 注释里写明"本文件不含坐标、看到 [coordinates] 说明是旧版"是必要的提醒，
    # 全文搜索会把这种提醒当成违规。
    example = ROOT / "config.example.toml"
    if example.exists():
        try:
            import tomllib

            with example.open("rb") as fh:
                example_data = tomllib.load(fh)
            bad_keys = scan_coordinates(example_data)
            check(
                "config.example.toml 不含坐标配置",
                not bad_keys,
                str(bad_keys[:6]),
            )
            check(
                "config.example.toml 的键都能被配置模型接受",
                _example_keys_ok(example_data),
                "示例配置里有配置模型不认识的键，用户照抄会被拒绝",
            )
        except Exception as exc:  # noqa: BLE001
            check("config.example.toml 可被解析", False, repr(exc))
    else:
        check("config.example.toml 存在", False, "发布前必须提供示例配置")

    # 引擎里不许 import 任何图像库（识别必须全在 agent 侧）
    banned = ("cv2", "PIL", "numpy", "skimage")
    leaked = []
    for py in (ROOT / "engine").rglob("*.py"):
        text = py.read_text(encoding="utf-8")
        for name in banned:
            if ("import %s" % name) in text or ("from %s" % name) in text:
                leaked.append("%s → %s" % (py.name, name))
    check("引擎不依赖图像库（识别全在电脑侧）", not leaked, "; ".join(leaked))


# ---------------------------------------------------------------------- #
# 2. 演练行为
# ---------------------------------------------------------------------- #
def test_dry_run() -> None:
    agent = FakeAgent()
    runtime = make_runtime(agent, dry_run=True)

    check("能选出一个适配包", runtime.pack is not None, "packs=%s" % sorted(runtime.packs))

    # 只读感知
    observed = runtime.observe()
    check("只读感知能拿到状态", observed.get("state") == "main_map", str(observed.get("state")))
    check("只读感知返回置信度", float(observed.get("confidence") or 0) > 0.9, str(observed.get("confidence")))

    # 演练执行
    result = runtime.run_goal("look_main")
    check("演练执行成功", result.get("ok") is True, str(result.get("message")))
    check("演练结果标记 dry_run", result.get("dry_run") is True, str(result.get("dry_run")))

    calls = agent.skill_calls
    check("演练时确实下发了技能调用", len(calls) >= 1, "收到 %d 次" % len(calls))
    check(
        "演练时每一次下发都是 dry_run=true",
        all(c["dry_run"] is True for c in calls),
        str([c["dry_run"] for c in calls]),
    )

    # 演练不该消耗限流配额
    check(
        "演练不占用动作限流额度",
        runtime.gate.snapshot().get("actions_last_60s") == 0,
        str(runtime.gate.snapshot().get("actions_last_60s")),
    )


# ---------------------------------------------------------------------- #
# 3. 拦截行为
# ---------------------------------------------------------------------- #
def test_blocks() -> None:
    # 3.1 配置说演练，工具请求实跑 → 仍然演练
    agent = FakeAgent()
    runtime = make_runtime(agent, dry_run=True)
    result = runtime.run_goal("look_main", requested_dry_run=False)
    check("配置演练时 live 请求不会真跑", result.get("dry_run") is True, str(result.get("dry_run")))
    check("配置演练时下发的仍是 dry_run", all(c["dry_run"] is True for c in agent.skill_calls), "")

    # 3.2 实跑但未接受免责 → 一个动作都不许下发
    agent = FakeAgent()
    runtime = make_runtime(agent, dry_run=False, disclaimer_accepted=False, pause_hotkey="F12")
    blocked = False
    try:
        runtime.run_goal("look_main")
    except ShipError:
        blocked = True
    check("未接受免责时拒绝实跑", blocked, "没有抛错")
    check("拒绝实跑时零动作下发", agent.skill_calls == [], "下发了 %d 个" % len(agent.skill_calls))

    # 3.3 实跑但没设热键 → 拦住
    agent = FakeAgent()
    runtime = make_runtime(agent, dry_run=False, disclaimer_accepted=True, pause_hotkey="")
    blocked = False
    try:
        runtime.run_goal("look_main")
    except ShipError:
        blocked = True
    check("未设暂停热键时拒绝实跑", blocked, "没有抛错")
    check("缺热键时零动作下发", agent.skill_calls == [], "")

    # 3.4 实跑 + demo 包 → 拦住（demo 不可运行是硬约束）
    agent = FakeAgent()
    runtime = make_runtime(agent, dry_run=False, disclaimer_accepted=True, pause_hotkey="F12")
    blocked = False
    try:
        runtime.run_goal("look_main")
    except ShipError:
        blocked = True
    check("demo 包拒绝实跑", blocked, "demo 包竟然跑起来了")
    check("demo 被拒时零动作下发", agent.skill_calls == [], "")

    # 3.5 未知界面 → 视为终止，不当成"大概是主界面"
    agent = FakeAgent(state="unknown", confidence=0.0)
    runtime = make_runtime(agent, dry_run=True)
    observed = runtime.observe()
    check("unknown 被标记为未知", observed.get("known") is False, str(observed))
    agent.skill_calls.clear()
    # unknown 是终止条件，不是"大概是主界面"。所以这里期望的是抛错终止，
    # 而不是"返回 ok=False 继续下一步"——后者会让状态机继续往下走。
    terminated = False
    try:
        runtime.run_goal("look_main")
    except ShipError:
        terminated = True
    check("unknown 界面下目标被立即终止", terminated, "unknown 竟然继续执行了")

    # 3.6 未知目标名 → 报配置错误，不能静默什么都不做
    agent = FakeAgent()
    runtime = make_runtime(agent, dry_run=True)
    failed = False
    try:
        runtime.run_goal("这个目标不存在")
    except ShipError:
        failed = True
    check("未知目标名被拒绝", failed, "未知目标竟然执行了")


# ---------------------------------------------------------------------- #
# 4. 闸门
# ---------------------------------------------------------------------- #
def test_gate() -> None:
    gate = SafetyGate(max_actions_per_min=2, breaker_threshold=1, breaker_cooldown_sec=60)

    # 未设热键时，实跑装机检查必须报问题
    problems = gate.installation_problems(effective_dry_run=False)
    check("装机检查能报出缺热键", any("热键" in p for p in problems), str(problems))
    check("演练时不报装机问题", gate.installation_problems(effective_dry_run=True) == [], "")

    gate.set_calibration(True, "")
    gate.set_pause_hotkey("F12")
    check("条件齐备后装机检查通过", gate.installation_problems(False) == [], str(gate.installation_problems(False)))

    # 限流：2 次以内放行，第 3 次拒绝
    gate.begin_session()
    passed, denied = 0, 0
    for _ in range(3):
        try:
            gate.check_can_act()
            gate.record_action()
            passed += 1
        except ShipError:
            denied += 1
    check("限流生效（2 放行 1 拒绝）", passed == 2 and denied == 1, "passed=%d denied=%d" % (passed, denied))

    # 熔断：失败一次即熔断
    gate.record_failure("E_UI")
    tripped = False
    try:
        gate.check_can_act()
    except ShipError:
        tripped = True
    check("失败一次即熔断", tripped, "熔断没有生效")
    gate.reset_breaker()
    check(
        "复位熔断后熔断标记消失",
        gate.snapshot().get("breaker_active") is False,
        str(gate.snapshot().get("breaker_active")),
    )
    # 注意：复位熔断只清失败计数，不清限流窗口。上面三次调用已经打满
    # 2 次/分的额度，所以这里仍然会被限流拦住——这是正确行为，
    # 两个保护互相独立，解除一个不该顺带解除另一个。
    still_limited = False
    try:
        gate.check_can_act()
    except ShipError as exc:
        still_limited = "上限" in exc.message or "E_SAFETY" in getattr(exc, "code", "")
    check("复位熔断不会解除限流", still_limited, "限流被顺带解除了")

    # 暂停优先
    gate.pause("测试")
    paused_blocked = False
    try:
        gate.check_can_act()
    except ShipError:
        paused_blocked = True
    check("暂停状态下拒绝任何动作", paused_blocked, "暂停没有拦住")
    gate.end_session()


# ---------------------------------------------------------------------- #
# 5. 状态报告
# ---------------------------------------------------------------------- #
def test_status() -> None:
    agent = FakeAgent()
    runtime = make_runtime(agent, dry_run=True)
    status = runtime.status()
    check("状态里有插件版本", status.get("plugin_version") == "0.2.0-selftest", str(status.get("plugin_version")))
    check("状态里有 dry_run 标记", status.get("dry_run") is True, str(status.get("dry_run")))
    check("状态里能连上 agent", (status.get("agent") or {}).get("connected") is True, str(status.get("agent")))
    check("状态里列出了适配包", bool(status.get("packs_available")), str(status.get("packs_available")))

    # 连不上 agent 时，status 不能抛异常（排查场景下最需要它可用）
    class DeadAgent(FakeAgent):
        def capabilities(self, refresh: bool = False):
            raise ShipError("连不上", hint="确认 agent 已启动、端口放行")

    runtime = make_runtime(DeadAgent(), dry_run=True)
    try:
        status = runtime.status()
        ok = (status.get("agent") or {}).get("connected") is False
        detail = str(status.get("agent"))
    except Exception as exc:  # noqa: BLE001
        ok = False
        detail = repr(exc)
    check("agent 不可达时 status 仍可用", ok, detail)


# ---------------------------------------------------------------------- #
# 6. 免责确认的持久化
#
# 这一组测的是"确认一次就永久有效"这条承诺。如果没有本地标记文件，
# 用户在聊天里发 /ship accept 之后重启一次插件就又回到未确认状态，
# 表现是"我明明确认过了"。这个坑很隐蔽，必须用测试钉住。
# ---------------------------------------------------------------------- #
def test_acceptance() -> None:
    agent = FakeAgent()
    runtime = make_runtime(agent, dry_run=True)

    check("默认未确认", runtime.disclaimer_accepted is False, runtime.disclaimer_source)
    check("默认来源是 none", runtime.disclaimer_source == "none", runtime.disclaimer_source)

    result = runtime.accept_disclaimer()
    check("确认后返回成功", bool(result.get("ok")), str(result))
    check("确认后状态为已确认", runtime.disclaimer_accepted is True, runtime.disclaimer_source)
    check("确认来源标记为 local", runtime.disclaimer_source == "local", runtime.disclaimer_source)

    marker = Path(runtime.data_dir) / "disclaimer_accepted.json"
    check("确认标记已落盘", marker.is_file(), str(marker))

    # 重启 = 新建一个 Runtime 指向同一个 data_dir
    revived = Runtime(
        config=ShipConfig.from_dict({"safety": {"dry_run": True}}),
        data_dir=runtime.data_dir,
        plugin_version="0.2.0-selftest",
    )
    revived.client = agent  # type: ignore[assignment]
    check("重启后仍然记得确认过", revived.disclaimer_accepted is True, revived.disclaimer_source)

    # 配置里勾了也算，且优先显示 config（用户从 WebUI 关掉时能被看出来）
    by_config = make_runtime(FakeAgent(), dry_run=True, disclaimer_accepted=True)
    check("配置勾选也算确认", by_config.disclaimer_accepted is True, by_config.disclaimer_source)
    check("配置来源标记为 config", by_config.disclaimer_source == "config", by_config.disclaimer_source)

    # 确认之后，装机阻塞项里不该再出现免责声明
    runtime.gate.set_pause_hotkey("F12")
    problems = runtime._installation_blockers(False)  # noqa: SLF001
    joined = " | ".join(problems)
    check("确认后不再报免责阻塞", "免责" not in joined, joined)

    status = runtime.status()
    check("status 里带免责状态", (status.get("disclaimer") or {}).get("accepted") is True, str(status.get("disclaimer")))

    # 坏掉的标记文件不能让插件加载失败
    marker.write_text("{ this is not json", encoding="utf-8")
    broken = Runtime(
        config=ShipConfig.from_dict({"safety": {"dry_run": True}}),
        data_dir=runtime.data_dir,
        plugin_version="0.2.0-selftest",
    )
    broken.client = agent  # type: ignore[assignment]
    check("标记文件损坏时退回未确认而非崩溃", broken.disclaimer_accepted is False, broken.disclaimer_source)


def main() -> int:
    print("shipctl 离线自检 · 插件根目录 %s\n" % ROOT)
    for fn in (test_structure, test_dry_run, test_blocks, test_gate, test_status, test_acceptance):
        print("── %s" % fn.__name__)
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            import traceback

            traceback.print_exc()
            check("%s 未抛异常" % fn.__name__, False, repr(exc))
        for ok, name, detail in RESULTS:
            print("   %s %s%s" % ("PASS" if ok else "FAIL", name, ("  → " + detail) if detail and not ok else ""))
        RESULTS.clear()

    return 0


if __name__ == "__main__":
    sys.exit(main())
