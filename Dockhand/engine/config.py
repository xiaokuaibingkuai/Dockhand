"""配置模型。

分层而不是一个大对象，是因为改动频率差异很大：
连接（几乎不改）、安全（偶尔调）、适配（换游戏才改）、通知（看心情）。
混在一起的话，用户改个通知设置要面对二十个字段，很容易改错别的。

另外刻意做成"零依赖 dataclass"而不是 pydantic：这个插件要在别人的
环境里加载，能少一个隐式依赖就少一类故障。字段校验自己写，反正也就
十几个字段，而且错误信息能写得比 pydantic 更贴场景。

安全相关字段的默认值全部选"最不容易出事"的那一侧：
    dry_run = True          默认只演练，不真点
    disclaimer_accepted = False  默认没同意免责声明，不能真跑
    pause_hotkey = ""       默认没热键，因此真跑会被拦住
这些默认值会让新用户第一次使用时"跑不起来"，这是**故意**的。
第一次跑不起来 + 一句明确的提示，比第一次就乱点要好得多。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from engine.errors import ConfigError


@dataclass
class ConnectionConfig:
    """与电脑侧 agent 的连接。

    base_url 不用默认值指向 localhost：插件跑在 Docker 里，
    127.0.0.1 是容器自己，一定连不上。给一个明显需要改的示例地址，
    逼用户去想"电脑的 IP 是多少"，比给一个看起来能用的错值好。
    """

    base_url: str = "http://192.168.1.10:8765"
    token: str = ""
    timeout_sec: int = 20

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "ConnectionConfig":
        return cls(
            base_url=str(raw.get("base_url") or cls.base_url).strip(),
            token=str(raw.get("token") or ""),
            timeout_sec=max(3, int(raw.get("timeout_sec") or 20)),
        )


@dataclass
class SafetyConfig:
    """安全配置。改这里等于改"它能做什么"，所以每一项都要能单独解释。

    disclaimer_accepted 是硬门槛的一部分：没勾就不允许实跑。
    这不是形式主义——它决定了出问题时责任边界在哪，也是分发出去
    必须有的东西。
    """

    dry_run: bool = True
    pause_hotkey: str = ""
    disclaimer_accepted: bool = False
    max_actions_per_min: int = 20
    breaker_threshold: int = 1
    breaker_cooldown_sec: int = 300
    session_max_sec: int = 1800
    require_calibration: bool = True

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "SafetyConfig":
        return cls(
            dry_run=bool(raw.get("dry_run", True)),
            pause_hotkey=str(raw.get("pause_hotkey") or "").strip(),
            disclaimer_accepted=bool(raw.get("disclaimer_accepted", False)),
            max_actions_per_min=max(1, int(raw.get("max_actions_per_min") or 20)),
            breaker_threshold=max(1, int(raw.get("breaker_threshold") or 1)),
            breaker_cooldown_sec=max(0, int(raw.get("breaker_cooldown_sec") or 300)),
            session_max_sec=max(60, int(raw.get("session_max_sec") or 1800)),
            require_calibration=bool(raw.get("require_calibration", True)),
        )


@dataclass
class AdapterConfig:
    """要加载哪个适配包。

    pack_id 空字符串表示"自动选第一个非 demo 包"，没有非 demo 包就用 demo。
    这个兜底是为了让第一次装插件的人不用先知道 pack_id 叫什么。
    """

    pack_id: str = ""
    adapter_dir: str = ""

    # 这里曾有一个 allow_demo_live 开关，已删除。
    #
    # 原因：demo 包只能演练是一条硬约束（planner 里强制拦），配置里再留一个
    # 开关，等于告诉用户"勾上就能跑"。但 demo 包没有可执行动作，勾上也跑不动，
    # 只会制造"插件坏了"的误解；更麻烦的是这类开关一旦存在，社区里很快就有人
    # 发"默认关闭但请自行开启"的包，边界就守不住了。约束不该做成可配置项。

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "AdapterConfig":
        return cls(
            pack_id=str(raw.get("pack_id") or "").strip(),
            adapter_dir=str(raw.get("adapter_dir") or "").strip(),
        )


@dataclass
class NotifyConfig:
    """结果通知。

    默认只在失败时通知。原因是这个插件的正常流程是"你说一句，它做完回一句"，
    把中间每一步都播报出来会刷屏；但失败必须主动说，否则用户不知道它停了。
    """

    on_finish: bool = True
    on_error: bool = True
    mention_on_error: bool = False

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "NotifyConfig":
        return cls(
            on_finish=bool(raw.get("on_finish", True)),
            on_error=bool(raw.get("on_error", True)),
            mention_on_error=bool(raw.get("mention_on_error", False)),
        )


@dataclass
class RuntimeConfig:
    """运行时杂项。

    loop 默认关闭，且必须由用户显式打开。自动循环是"无人值守"能力，
    它和"一句话一次操作"的风险等级完全不同，不能默认开。
    """

    loops_enabled: bool = False
    loop_interval_sec: int = 600
    trace_keep_days: int = 7
    log_level: str = "INFO"

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "RuntimeConfig":
        return cls(
            loops_enabled=bool(raw.get("loops_enabled", False)),
            loop_interval_sec=max(60, int(raw.get("loop_interval_sec") or 600)),
            trace_keep_days=7,
            log_level=str(raw.get("log_level") or "INFO").upper(),
        )


@dataclass
class ShipConfig:
    """顶层配置。

    to_dict 用于落盘和脱敏快照；注意它不含任何坐标字段——
    约束三在这里体现为"根本没有这个字段可填"。
    """

    connection: ConnectionConfig = field(default_factory=ConnectionConfig)
    safety: SafetyConfig = field(default_factory=SafetyConfig)
    adapter: AdapterConfig = field(default_factory=AdapterConfig)
    notify: NotifyConfig = field(default_factory=NotifyConfig)
    runtime: RuntimeConfig = field(default_factory=RuntimeConfig)

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> "ShipConfig":
        if raw is None:
            return cls()
        if not isinstance(raw, dict):
            raise ConfigError(
                "配置格式不对：顶层应该是一个对象（section 结构）",
                hint="参考 config.example.toml，把配置分成 connection / safety / adapter / notify / runtime 五节",
            )
        # 约束三的运行时校验：旧版配置里带坐标，必须明确拒绝而不是静默忽略
        from game_adapter.base import validate_config_keys

        validate_config_keys(raw)

        return cls(
            connection=ConnectionConfig.from_dict(raw.get("connection") or {}),
            safety=SafetyConfig.from_dict(raw.get("safety") or {}),
            adapter=AdapterConfig.from_dict(raw.get("adapter") or {}),
            notify=NotifyConfig.from_dict(raw.get("notify") or {}),
            runtime=RuntimeConfig.from_dict(raw.get("runtime") or {}),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def effective_dry_run(self, requested: bool | None = None) -> bool:
        """算出这一次到底是不是演练模式。

        规则：配置说演练就演练，任何调用方都无权把它变成实跑。
        反过来，配置说实跑，单次调用仍然可以要求演练（更保守的方向允许）。
        这个方向性是安全设计的常见失误点——让"更危险"的决定只能来自配置，
        不能来自一次工具调用。
        """
        if self.safety.dry_run:
            return True
        if requested is True:
            return True
        return False
