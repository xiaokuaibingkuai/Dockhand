"""适配包（pack）的接口契约。

这是整个项目唯一需要"守住"的东西。引擎可以改、Tool 可以改，
但只要这里的契约稳定，别人写的适配包就不会因为你升级而全废。

三条硬约束不是文档里的口号，是这个文件里会真正拒绝加载的规则：

    约束一：适配包里不允许出现坐标。
        任何 JSON 里出现 x / y / coord / position 这类键，直接拒绝加载。
        原因：坐标绑定具体分辨率、模拟器缩放、DPI、游戏 UI 缩放、语言。
        你机器上 (960, 540) 是"出击"按钮，别人机器上可能是一片海。
        适配包只能写"状态名 + 技能名"，坐标由 agent 用模板匹配在现场算。

    约束二：unknown 是终止条件，不是"大概"。
        states.json 里必须声明 unknown，且 goals.json 里不允许出现
        以 unknown 为前置或目标状态的步骤。引擎在 planner 层会再拦一次。
        原因：认不出界面还继续点，就是盲点。这是账号安全底线。

    约束三：config.toml 不再有坐标块。
        配置 schema 里根本没有坐标项，validate_config() 见到坐标类键就报错。
        原因：用户配置文件里一旦能写坐标，就会有人写死坐标然后来报"不准"。

为什么用"加载时拒绝"而不是"加载时警告"：

警告没人看。你写 `logger.warning("检测到坐标，可能不兼容")`，
对方照样跑，跑坏了还是找你。硬失败会立刻逼出正确做法，
而且错误信息里能写清楚"改哪、为什么"，这比事后排查便宜得多。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from engine.errors import ConfigError

# 一旦在任何适配包文件里看到这些键，就判定为"写了坐标"，拒绝加载。
# 用精确匹配而不是模糊匹配，避免误伤 max_retries / box_size 这类正常键。
_COORD_KEYS = {
    "x", "y", "xy", "coord", "coords", "coordinate", "coordinates",
    "position", "pos", "point", "tap_at", "click_at", "swipe_from", "swipe_to",
    "offset", "offset_x", "offset_y", "screen_x", "screen_y",
}

# 配置里这些键也算坐标，单独一张表，因为配置的键名风格不同（带前缀）
_CONFIG_COORD_PATTERN = re.compile(
    r"(^|_)(x|y|coord|coords|coordinate|position|tap_point|click_point|swipe_point)($|_)",
    re.IGNORECASE,
)

# 状态名与技能名的合法字符。限制成下划线风格是为了让 goals.json 里的
# 引用可被静态检查——带空格的键在 JSON 里合法但在配置/命令行里很麻烦。
_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,47}$")

UNKNOWN_STATE = "unknown"


@dataclass
class SkillSpec:
    """一个技能的定义（纯声明，不含任何坐标、不含任何可执行代码）。

    steps 里允许的 step 类型由 agent 解释，插件不解释——这是关键的分层：
    插件只负责"什么时候调哪个技能"，不负责"技能内部怎么点"。
    这样加游戏不用改插件，改游戏也不用改插件。

    允许的 step 形态（agent 侧实现，插件只做结构校验）：
        {"op": "tap",     "target": "<element_key>"}     按元素名点，不按坐标
        {"op": "wait",    "ms": 800}
        {"op": "wait_for","state": "<state_name>", "timeout_ms": 5000}
        {"op": "swipe",   "target_from": "<key>", "target_to": "<key>", "duration_ms": 300}
        {"op": "key",     "name": "back"}
        {"op": "assert",  "state": "<state_name>"}
    """

    name: str
    description: str
    pre_states: list[str]
    steps: list[dict[str, Any]]
    expect_state: str
    timeout_ms: int = 30_000
    # 是否允许在 dry_run 之外安全重试。涉及"确认/消耗/购买"的技能必须为 False，
    # 因为重试可能导致重复消耗。
    retryable: bool = True

    @classmethod
    def from_dict(cls, name: str, raw: dict[str, Any]) -> "SkillSpec":
        if not isinstance(raw, dict):
            raise ConfigError("技能 %s 的定义必须是对象" % name, hint="检查 skills.json 的结构")
        steps = raw.get("steps") or []
        if not isinstance(steps, list) or not steps:
            raise ConfigError(
                "技能 %s 没有 steps" % name,
                hint="每个技能至少要有一步动作，否则它没有意义",
            )
        _reject_coords(steps, "skills.json 中技能 %s 的 steps" % name)

        pre = raw.get("pre_states") or ["any"]
        expect = str(raw.get("expect_state") or "")
        if not expect:
            raise ConfigError(
                "技能 %s 没有声明 expect_state" % name,
                hint="每个技能必须说明'执行完之后应该在什么界面'，否则引擎无法判断成败",
            )
        if expect == UNKNOWN_STATE:
            raise ConfigError(
                "技能 %s 的 expect_state 不能是 unknown" % name,
                hint="unknown 是终止条件：认不出界面时必须停下来，不能作为期望结果",
            )

        return cls(
            name=name,
            description=str(raw.get("description") or ""),
            pre_states=[str(s) for s in pre],
            steps=steps,
            expect_state=expect,
            timeout_ms=int(raw.get("timeout_ms") or 30_000),
            retryable=bool(raw.get("retryable", True)),
        )


@dataclass
class GoalSpec:
    """一个目标：把自然语言目标映射成技能序列。

    requires_state 是"我必须在哪个界面开始"，on_fail 是失败策略。
    不允许在 goals 里写坐标或写步骤内部细节——那是技能的职责。
    """

    name: str
    description: str
    requires_state: str
    steps: list[dict[str, Any]]
    on_fail: str = "abort"     # abort / retry_once
    max_attempts: int = 1

    @classmethod
    def from_dict(cls, name: str, raw: dict[str, Any]) -> "GoalSpec":
        if not isinstance(raw, dict):
            raise ConfigError("目标 %s 的定义必须是对象" % name)
        steps = raw.get("steps") or []
        if not isinstance(steps, list) or not steps:
            raise ConfigError("目标 %s 没有 steps" % name, hint="目标至少要引用一个技能")
        _reject_coords(steps, "goals.json 中目标 %s 的 steps" % name)

        requires = str(raw.get("requires_state") or "any")
        if requires == UNKNOWN_STATE:
            raise ConfigError(
                "目标 %s 的 requires_state 不能是 unknown" % name,
                hint="unknown 表示认不出界面，此时不能开始任何操作",
            )

        return cls(
            name=name,
            description=str(raw.get("description") or ""),
            requires_state=requires,
            steps=steps,
            on_fail=str(raw.get("on_fail") or "abort"),
            max_attempts=max(1, int(raw.get("max_attempts") or 1)),
        )


@dataclass
class PackSpec:
    """一个完整适配包。

    demo 标志很重要：demo 包只用于"让你看看识别长什么样"，
    里面的状态和技能故意不可运行（技能 steps 为只读断言或缺失）。
    引入这个标志是为了让"我发了个 demo"和"我发了个能用的挂"
    在数据层面就分开，而不是靠 README 里的一句话。
    """

    pack_id: str
    name: str
    version: str
    author: str = ""
    demo: bool = False
    entry_states: list[str] = field(default_factory=list)
    skills: dict[str, SkillSpec] = field(default_factory=dict)
    goals: dict[str, GoalSpec] = field(default_factory=dict)
    states: dict[str, dict[str, Any]] = field(default_factory=dict)
    root: Path | None = None

    def goal_names(self) -> list[str]:
        return sorted(self.goals)

    def state_names(self) -> list[str]:
        return sorted(self.states)


def _reject_coords(node: Any, where: str) -> None:
    """递归扫描，发现坐标类键立即报错。

    报错信息要包含"哪个文件、哪个键、为什么不允许"——这是用户唯一
    需要的三件事。只写"检测到坐标"会让人不知道从哪改。
    """
    if isinstance(node, dict):
        for key, value in node.items():
            if str(key).lower() in _COORD_KEYS:
                raise ConfigError(
                    "适配包被拒绝：%s 出现坐标字段 `%s`" % (where, key),
                    hint="适配包不允许写死坐标（换机器必然失效）。请改成声明元素名，由 agent 用模板匹配定位",
                    detail={"where": where, "field": str(key)},
                )
            _reject_coords(value, where)
    elif isinstance(node, list):
        for item in node:
            _reject_coords(item, where)


def validate_config_keys(config: dict[str, Any]) -> None:
    """检查用户配置里有没有坐标类键。

    约束三的落地。用户可能从旧版配置复制过来，里面带坐标块，
    这时候不能沉默接受——一沉默，他就以为坐标准了。
    """
    def walk(node: Any, path: str) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                full = "%s.%s" % (path, key) if path else str(key)
                if _CONFIG_COORD_PATTERN.search(str(key)):
                    raise ConfigError(
                        "配置里不允许出现坐标字段：%s" % full,
                        hint="v0.2 起坐标不再由配置提供，全部由 agent 的校准+模板匹配现场计算。"
                             "请删除该字段，并运行一次校准",
                        detail={"field": full},
                    )
                walk(value, full)
        elif isinstance(node, list):
            for idx, item in enumerate(node):
                walk(item, "%s[%d]" % (path, idx))

    walk(config, "")


def _load_json(path: Path, label: str) -> dict[str, Any]:
    """读 JSON 并给出人话错误。"""
    if not path.is_file():
        raise ConfigError(
            "适配包缺少 %s" % label,
            hint="一个合法的适配包至少要有 manifest.json / states.json / skills.json / goals.json",
            detail={"expected": str(path)},
        )
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except json.JSONDecodeError as exc:
        raise ConfigError(
            "适配包 %s 不是合法 JSON：%s" % (label, exc),
            hint="常见原因：多了一个逗号、用了单引号、写了注释（JSON 不支持注释）",
            detail={"file": str(path)},
        )
    if not isinstance(data, dict):
        raise ConfigError("适配包 %s 的顶层必须是对象" % label, detail={"file": str(path)})

    # 以 "_" 开头的顶层键视为注释，加载时丢弃。
    #
    # JSON 本身不支持注释，但适配包是给人手写的，不许写注释会逼着人
    # 在 README 和 JSON 之间来回看。约定 "_comment" 这类键被忽略，
    # 是一种不需要解析器改动的折中。
    # 注意：只过滤顶层，且只过滤下划线开头——正常的状态名/技能名
    # 必须以小写字母开头（见 _NAME_PATTERN），所以不会误吞真实定义。
    return {str(k): v for k, v in data.items() if not str(k).startswith("_")}


def load_pack(root: Path | str) -> PackSpec:
    """从目录加载并校验一个适配包。

    校验顺序按"便宜的先做"排列：先看文件在不在，再看 JSON 合不合法，
    再看名字合不合法，最后看交叉引用。这样错误信息总是最具体的那一条。
    """
    base = Path(root)
    if not base.is_dir():
        raise ConfigError(
            "适配包目录不存在：%s" % base,
            hint="检查配置里的游戏包名是否对应 game_adapter/ 下的一个子目录",
            detail={"path": str(base)},
        )

    manifest = _load_json(base / "manifest.json", "manifest.json")
    states = _load_json(base / "states.json", "states.json")
    skills_raw = _load_json(base / "skills.json", "skills.json")
    goals_raw = _load_json(base / "goals.json", "goals.json")

    _reject_coords(manifest, "manifest.json")
    _reject_coords(states, "states.json")

    # --- 名字合法性 ---
    for name in states:
        if not _NAME_PATTERN.match(str(name)):
            raise ConfigError(
                "states.json 里的状态名 `%s` 不合法" % name,
                hint="状态名只能用小写字母、数字、下划线，且以字母开头（例如 main_map）",
            )
    for name in skills_raw:
        if not _NAME_PATTERN.match(str(name)):
            raise ConfigError("skills.json 里的技能名 `%s` 不合法" % name)
    for name in goals_raw:
        if not _NAME_PATTERN.match(str(name)):
            raise ConfigError("goals.json 里的目标名 `%s` 不合法" % name)

    # --- 约束二的静态部分：unknown 必须存在 ---
    if UNKNOWN_STATE not in states:
        raise ConfigError(
            "适配包没有声明 unknown 状态",
            hint="必须显式声明 unknown（表示'这个界面我不认识'），否则引擎无法在异常界面停手",
        )

    # --- 构建技能 ---
    skills = {name: SkillSpec.from_dict(name, raw) for name, raw in skills_raw.items()}

    # --- 构建目标，并做交叉引用检查 ---
    goals: dict[str, GoalSpec] = {}
    for name, raw in goals_raw.items():
        goal = GoalSpec.from_dict(name, raw)
        for idx, step in enumerate(goal.steps):
            skill_name = step.get("skill")
            if not skill_name:
                raise ConfigError(
                    "目标 %s 的第 %d 步没有指定 skill" % (name, idx + 1),
                    hint='正确写法：{"skill": "open_map"}',
                )
            if skill_name not in skills:
                raise ConfigError(
                    "目标 %s 引用了不存在的技能 `%s`" % (name, skill_name),
                    hint="检查 skills.json 里有没有这个技能（注意大小写和下划线）",
                    detail={"available": sorted(skills)},
                )
            _reject_coords(step, "goals.json 中目标 %s 的第 %d 步" % (name, idx + 1))
        goals[name] = goal

    # --- 技能内部步骤的结构检查 ---
    for skill in skills.values():
        for idx, step in enumerate(skill.steps):
            op = step.get("op")
            if not op:
                raise ConfigError(
                    "技能 %s 的第 %d 步没有 op" % (skill.name, idx + 1),
                    hint='每步必须声明操作类型，例如 {"op": "tap", "target": "fleet_btn"}',
                )
            # wait_for / assert 引用状态时，状态必须存在
            ref = step.get("state")
            if ref is not None and str(ref) not in states:
                raise ConfigError(
                    "技能 %s 的第 %d 步引用了未定义的状态 `%s`" % (skill.name, idx + 1, ref),
                    detail={"available": sorted(states)},
                )
            # 约束二的动态部分：技能不允许"等待 unknown 出现"或"断言处于 unknown"
            if str(ref or "") == UNKNOWN_STATE:
                raise ConfigError(
                    "技能 %s 不允许以 unknown 作为目标状态" % skill.name,
                    hint="遇到 unknown 只能停，不能把它当成一个可预期的界面",
                )
        # 前置状态必须存在
        for pre in skill.pre_states:
            if pre != "any" and pre not in states:
                raise ConfigError(
                    "技能 %s 的前置状态 `%s` 未在 states.json 中定义" % (skill.name, pre),
                    detail={"available": sorted(states)},
                )

    pack = PackSpec(
        pack_id=str(manifest.get("pack_id") or base.name),
        name=str(manifest.get("name") or base.name),
        version=str(manifest.get("version") or "0.0.0"),
        author=str(manifest.get("author") or ""),
        demo=bool(manifest.get("demo", False)),
        entry_states=[str(s) for s in (manifest.get("entry_states") or [])],
        skills=skills,
        goals=goals,
        states=states,
        root=base,
    )
    return pack


def discover_packs(adapter_root: Path | str) -> dict[str, PackSpec]:
    """扫描 game_adapter/ 下所有包，坏包跳过而不是整体失败。

    为什么要跳过坏包：用户可能自己往里放了一个写错的包，
    如果因此整个插件加载失败，他就连"帮我看看这包哪错了"都问不了。
    好包能用、坏包在状态里标出来，才是可运维的行为。
    """
    root = Path(adapter_root)
    packs: dict[str, PackSpec] = {}
    if not root.is_dir():
        return packs
    for child in sorted(root.iterdir()):
        if not child.is_dir() or child.name.startswith((".", "_")):
            continue
        try:
            pack = load_pack(child)
            packs[pack.pack_id] = pack
        except ConfigError:
            continue
    return packs
