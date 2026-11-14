"""MaiBot 插件入口：shipctl（手游控制台）。

本文件只做三件事：声明配置、声明 4 个 Tool / 1 个 Command、把异常翻译成人话。
真正的逻辑在 engine/ 里，游戏相关的数据在 game_adapter/ 里。

--------------------------------------------------------------------------
Tool 为什么是 4 个而不是十几个

按功能点平铺很自然会写成 8 个：查状态、查校准、跑目标、暂停、恢复、
复位熔断、截图、导诊断。这样写的问题不是代码多，而是**模型会在相近的
工具之间犹豫**——pause 和 stop 选哪个、calibrate 和 setup 有什么区别。
在这个场景下选错工具不是答错一句话，是可能真的去点屏幕。

所以按"意图"收敛成 4 类，每类内部用 action 参数分派：

    ship_state    看      只读，永远安全
    ship_goal     做      会产生动作，走全套闸门
    ship_control  控      暂停 / 恢复 / 复位熔断 / 校准 / 截图
    ship_report   报      状态 / 诊断包

四类的语义边界互不重叠：想做事只可能是 ship_goal，想停下只可能是
ship_control。同一件事不会有两条路，模型就不需要权衡。

另外一条规则：**只读与写入必须分属不同工具**。限流、权限、审计都是按
工具粒度做的，把"看一眼"和"点一下"混进一个工具，就会让二者共享同一套
限流参数——这在实现上省事，在安全上是错的。

--------------------------------------------------------------------------
参数类型为什么全是字符串

ToolParameterInfo 的 param_type 用 STRING 而不是 BOOLEAN，是为了不依赖
某个 SDK 版本是否提供布尔枚举。可选值写在 description 里，模型按描述填，
插件的 `_parse_mode` / action 分支再做一次白名单校验。字符串是唯一
在所有 SDK 版本上都存在的类型，这个稳定性比"类型更精确"更有价值。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------- #
# 让 engine / game_adapter 可以被当作顶层包导入
#
# 背景：MaiBot 的 PluginLoader 加载 plugin.py 的方式并不保证它是一个
# 有父包的子模块（可能是按文件路径直接加载）。这种情况下 `from .engine
# import ...` 会抛 "attempted relative import with no known parent package"。
#
# 处理方式是把插件根目录放到 sys.path 最前面，然后一律用绝对包名导入。
# 相应地，engine 与 game_adapter 内部**不允许**出现 `from ..xxx` 这种
# 跨父包的相对导入（已经统一改成 from game_adapter.base import ...）。
#
# 代价：engine / game_adapter 这两个包名是通用的，若同一 Runner 进程里
# 还有别的插件也插入了同名顶层包，可能互相覆盖。第三方 Runner 通常是
# 一插件一进程，且我们先插入（index 0），风险可接受；若要彻底消除，
# 应改为按插件 id 动态注册父包，那是另一个量级的复杂度，这里不做。
# ---------------------------------------------------------------------- #
_PLUGIN_ROOT = Path(__file__).resolve().parent
if str(_PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_ROOT))

from maibot_sdk import Command, MaiBotPlugin, Tool  # noqa: E402
from maibot_sdk.types import ToolParameterInfo, ToolParamType  # noqa: E402

from engine.config import ShipConfig  # noqa: E402
from engine.errors import ShipError, wrap  # noqa: E402
from engine.runtime import DISCLAIMER, Runtime  # noqa: E402

PLUGIN_VERSION = "0.2.0"


# ---------------------------------------------------------------------- #
# 配置模型
#
# 声明 config_model 的好处：Runner 会据此生成 WebUI 表单和 config.toml，
# 用户不用手写 TOML，改配置也不用记字段名。
#
# 但 PluginConfigBase 的导入路径在不同 SDK 小版本里不保证一致，所以整段
# 用 try 包住：导入成功就声明表单，失败就退化成"从 dict 读配置"。
# 退化后功能完全不变，只是 WebUI 里没有表单项、要手工编辑 config.toml。
# 这一点在 README 里写明了，不让用户面对"配置页是空的"去猜原因。
# ---------------------------------------------------------------------- #
_CONFIG_MODEL_OK = False
try:  # pragma: no cover - 取决于 host 的 SDK 版本
    from maibot_sdk.config import PluginConfigBase  # type: ignore
    from pydantic import Field  # type: ignore

    _CONFIG_MODEL_OK = True
except Exception:  # noqa: BLE001
    PluginConfigBase = object  # type: ignore


if _CONFIG_MODEL_OK:

    class ConnectionSection(PluginConfigBase):  # type: ignore[misc]
        """与电脑上 agent 的连接。"""

        __ui_label__ = "连接"
        __ui_icon__ = "lan"
        __ui_order__ = 0

        base_url: str = Field(
            default="http://192.168.1.10:8765",
            description="agent 地址。插件在 Docker 里跑，127.0.0.1 是容器自己，必须填电脑的局域网 IP",
        )
        token: str = Field(
            default="",
            description="agent 的访问令牌（agent 侧配置里生成的那个）",
        )
        timeout_sec: int = Field(default=20, description="单次请求超时（秒）")

    class SafetySection(PluginConfigBase):  # type: ignore[misc]
        """安全设置。这里每一项都直接决定"它能做什么"。"""

        __ui_label__ = "安全"
        __ui_icon__ = "shield"
        __ui_order__ = 1

        dry_run: bool = Field(
            default=True,
            description="演练模式：只截图识别，不点击。确认识别正确后再关闭",
        )
        pause_hotkey: str = Field(
            default="",
            description="紧急暂停热键名（在 agent 侧设置，例如 F12）。不填则不允许实跑",
        )
        disclaimer_accepted: bool = Field(
            default=False,
            description="是否已阅读并接受免责声明。不勾选则不允许实跑",
        )
        max_actions_per_min: int = Field(default=20, description="每分钟最多动作次数（限流）")
        breaker_threshold: int = Field(
            default=1,
            description="连续失败几次触发熔断。默认 1，即失败一次就停",
        )
        breaker_cooldown_sec: int = Field(default=300, description="熔断冷却时间（秒）")
        session_max_sec: int = Field(default=1800, description="单次会话最长时长（秒）")
        require_calibration: bool = Field(default=True, description="实跑前是否必须完成校准")

    class AdapterSection(PluginConfigBase):  # type: ignore[misc]
        """加载哪个适配包。"""

        __ui_label__ = "适配包"
        __ui_icon__ = "extension"
        __ui_order__ = 2

        pack_id: str = Field(
            default="",
            description="要用的适配包 id（对应 game_adapter/ 下的目录名）。留空则自动选第一个正式包",
        )
        adapter_dir: str = Field(
            default="",
            description="适配包目录的绝对路径。留空则用插件自带的 game_adapter/",
        )
        # 这里刻意不提供"允许 demo 实跑"的开关：demo 只能演练是引擎层强制的
        # 硬约束，不是可配置项。理由见 engine/config.py 里 AdapterConfig 的注释。

    class NotifySection(PluginConfigBase):  # type: ignore[misc]
        """结果通知。"""

        __ui_label__ = "通知"
        __ui_icon__ = "notifications"
        __ui_order__ = 3

        on_finish: bool = Field(default=True, description="执行结束后是否回复一条结果")
        on_error: bool = Field(default=True, description="出错时是否回复一条说明")
        mention_on_error: bool = Field(default=False, description="出错时是否 @ 你")

    class RuntimeSection(PluginConfigBase):  # type: ignore[misc]
        """运行时杂项。"""

        __ui_label__ = "运行"
        __ui_icon__ = "settings"
        __ui_order__ = 4

        loops_enabled: bool = Field(
            default=False,
            description="是否允许循环执行（无人值守）。默认关闭，风险等级与单次执行不同",
        )
        loop_interval_sec: int = Field(default=600, description="循环间隔（秒），仅在开启循环时生效")
        trace_keep_days: int = Field(default=7, description="执行轨迹保留天数，过期自动清理")
        log_level: str = Field(default="INFO", description="日志级别：DEBUG / INFO / WARNING / ERROR")

    class PluginSection(PluginConfigBase):  # type: ignore[misc]
        """插件基础配置节。

        SDK 2.7+ 强制要求 plugin.config_version：Runner 从配置模型的默认值里
        提取这个版本号做配置补齐与迁移判定，缺了会直接拒绝激活（报
        "插件配置文件缺少 [plugin] 配置节，且必须提供 plugin.config_version"）。
        这一节不属于业务配置，只放版本号和总开关。
        """

        __ui_label__ = "基础"
        __ui_icon__ = "tune"
        __ui_order__ = -1

        config_version: str = Field(
            default=PLUGIN_VERSION, description="配置 Schema 版本，由插件维护，请勿手动修改"
        )
        enabled: bool = Field(default=True, description="插件总开关，关闭后停止接收指令")

    class ShipPluginConfig(PluginConfigBase):  # type: ignore[misc]
        """插件配置总表。分节而不是平铺，是因为这几组字段的改动频率差很多。"""

        plugin: PluginSection = Field(default_factory=PluginSection)
        connection: ConnectionSection = Field(default_factory=ConnectionSection)
        safety: SafetySection = Field(default_factory=SafetySection)
        adapter: AdapterSection = Field(default_factory=AdapterSection)
        notify: NotifySection = Field(default_factory=NotifySection)
        runtime: RuntimeSection = Field(default_factory=RuntimeSection)


class ShipControlPlugin(MaiBotPlugin):
    """插件主体。

    刻意不在 __init__ 里做初始化：MaiBotPlugin 的构造由 Host 控制，
    覆写 __init__ 容易和框架注入 ctx 的时机打架。所有初始化放 on_load。
    """

    # ------------------------------------------------------------------ #
    # 生命周期（三个方法必须全部实现，缺一个 Runner 拒绝加载）
    # ------------------------------------------------------------------ #
    async def on_load(self) -> None:
        """加载插件。

        这里**不连 agent**。理由很实际：用户很可能先装插件、后开电脑。
        如果在 on_load 就要求连通，装插件的顺序就被绑定死了，而且报错会
        出现在"加载插件"这种让人摸不着头脑的时机。

        加载失败也不抛异常：一个配置写错的插件完全消失，用户连
        /ship status 都用不了，就失去了自查手段。所以这里捕获后只记日志，
        后续每个 Tool 会返回具体原因。
        """
        self._runtime: Runtime | None = None
        self._config_source = "default"

        try:
            raw = self._collect_config()
            config = ShipConfig.from_dict(raw)
            self._runtime = Runtime(
                config=config,
                data_dir=self._data_dir(),
                plugin_version=PLUGIN_VERSION,
            )
            self.ctx.logger.info(
                "shipctl %s 已加载 | 适配包: %s | 当前: %s | 模式: %s"
                % (
                    PLUGIN_VERSION,
                    ", ".join(sorted(self._runtime.packs)) or "无",
                    self._runtime.pack.pack_id if self._runtime.pack else "无",
                    "演练" if config.safety.dry_run else "实跑",
                )
            )
        except ShipError as exc:
            self._runtime = None
            # 用 warning 而不是 error：插件本身是好的，是配置/适配包有问题。
            # error 级别会让用户以为插件坏了，去重装。
            self.ctx.logger.warning(
                "shipctl 加载未完成：%s | %s（插件仍会加载，可用 /ship status 查看原因）"
                % (exc.message, exc.hint)
            )
        except Exception as exc:  # noqa: BLE001
            self._runtime = None
            self.ctx.logger.error("shipctl 加载出现未预期错误：%r" % (exc,))

    async def on_unload(self) -> None:
        """卸载。

        尽力暂停，避免"插件卸了但动作还在跑"。这里捕获所有异常：
        卸载过程中抛错会掩盖真正的原因，而且此时已经无法再向用户解释。
        """
        runtime = getattr(self, "_runtime", None)
        if runtime is not None:
            try:
                await asyncio.to_thread(runtime.pause, "插件卸载")
            except Exception:  # noqa: BLE001
                pass
        self._runtime = None

    async def on_config_update(self, scope: str, config_data: dict[str, Any], version: str) -> None:
        """配置热更新。

        实现方式是重建 Runtime，而不是逐个字段原地改。原因：配置分散在
        客户端、闸门、包加载器三处，逐字段更新很容易漏一处，漏掉的那处
        会表现为"改了下限流但没生效"这种极难排查的问题。

        重建的副作用是内存里的熔断/暂停状态被清空——但用户改配置通常
        就是想重来一次，这个副作用恰好是期望行为。
        """
        try:
            raw = self._collect_config()
            self._runtime = Runtime(
                config=ShipConfig.from_dict(raw),
                data_dir=self._data_dir(),
                plugin_version=PLUGIN_VERSION,
            )
            self.ctx.logger.info("shipctl 配置已更新（scope=%s, version=%s），运行时已重建" % (scope, version))
        except ShipError as exc:
            self.ctx.logger.warning(
                "shipctl 配置更新失败，已保留旧运行时：%s | %s" % (exc.message, exc.hint)
            )
        except Exception as exc:  # noqa: BLE001
            self.ctx.logger.error("shipctl 配置更新出现未预期错误：%r" % (exc,))

    # ------------------------------------------------------------------ #
    # 配置与路径
    # ------------------------------------------------------------------ #
    def _collect_config(self) -> dict[str, Any]:
        """把配置取成 dict。

        两条路：
          1. 声明了 config_model → self.config 是强类型对象，model_dump 即 dict
          2. 没声明（SDK 不支持 PluginConfigBase）→ get_plugin_config_data()

        注意 self.config 在配置未注入时会抛 RuntimeError，这是 SDK 的设计，
        所以要包住。这里不把它当错误上报——退到第 2 条路即可。
        """
        # 路径 1：强类型配置
        try:
            cfg = getattr(self, "config", None)
        except Exception:  # noqa: BLE001
            cfg = None

        if cfg is not None:
            dump = getattr(cfg, "model_dump", None)
            if callable(dump):
                try:
                    data = dump()
                    if isinstance(data, dict) and data:
                        self._config_source = "config_model"
                        return data
                except Exception:  # noqa: BLE001
                    pass
            if isinstance(cfg, dict) and cfg:
                self._config_source = "config_dict"
                return cfg

        # 路径 2：dict 接口（始终可用）
        getter = getattr(self, "get_plugin_config_data", None)
        if callable(getter):
            try:
                data = getter()
                if isinstance(data, dict) and data:
                    self._config_source = "plugin_config_data"
                    return data
            except Exception:  # noqa: BLE001
                pass

        self._config_source = "default"
        return {}

    def _data_dir(self) -> Path:
        """取持久数据目录。

        SDK 约定的落点是 data/plugins/<plugin_id>/，通过 ctx.paths 暴露。
        这里逐级兜底：拿不到就用插件目录下的 .data/。
        宁可落在不太理想的位置，也不要因为取不到路径导致插件起不来——
        起不来时用户连诊断命令都没有。
        """
        paths = getattr(self.ctx, "paths", None)
        if paths is not None:
            if isinstance(paths, (str, Path)):
                return Path(paths).expanduser()
            for attr in ("data_dir", "plugin_data_dir", "data_path"):
                value = getattr(paths, attr, None)
                if value:
                    return Path(str(value)).expanduser()
        return _PLUGIN_ROOT / ".data"

    def _require_runtime(self) -> Runtime:
        """取运行时；没有就抛一个能指导用户下一步的错误。

        这条消息出现的频率不低（配置写错时每次调用都会看到），
        所以必须写"怎么办"，而不是"runtime is None"。
        """
        runtime = getattr(self, "_runtime", None)
        if runtime is None:
            raise ShipError(
                "插件未就绪：加载阶段配置或适配包有问题",
                hint="发 /ship status 看具体原因；或看日志里以 shipctl 开头的行。"
                     "最常见的原因是 game_adapter/ 下没有合法包，或配置里有旧版的坐标字段",
            )
        return runtime

    async def _reply(self, text: str, stream_id: str | None = None) -> None:
        """往会话里回一条消息。

        Tool 里通常不需要主动发消息（返回值就是给模型的），
        这个仅在 Command 场景下用。失败静默——通知失败不该影响主流程。
        """
        if not stream_id:
            return
        try:
            await self.ctx.send.text(text, stream_id)
        except Exception:  # noqa: BLE001
            pass

    # ================================================================== #
    # Tool 1 / 4：只读 —— 当前界面
    # ================================================================== #
    @Tool(
        "ship_state",
        description=(
            "查看游戏当前所处的界面状态与识别置信度。只读，不产生任何操作。"
            "用于执行前确认目标可做、或排查识别是否准确。"
        ),
        parameters=[],
    )
    async def tool_ship_state(self, **kwargs: Any) -> dict[str, Any]:
        """看一眼。

        返回里 confidence 比 state 更值得看：0.62 和 0.95 都是"认出来了"，
        但前者说明模板匹配很勉强，实跑前应该先处理掉这种勉强——否则
        某天光照/皮肤换一下就认错了，而认错比认不出更危险。
        """
        try:
            runtime = self._require_runtime()
            result = await asyncio.to_thread(runtime.observe)
            conf = float(result.get("confidence") or 0.0)
            if result.get("known") and conf < 0.75:
                result["warning"] = (
                    "置信度偏低（%.2f）。虽然认出来了，但建议补一张更清晰的模板图，"
                    "否则实跑时容易误判" % conf
                )
            return result
        except ShipError as exc:
            return exc.to_dict()
        except Exception as exc:  # noqa: BLE001
            return wrap(exc).to_dict()

    # ================================================================== #
    # Tool 2 / 4：写入 —— 执行目标
    # ================================================================== #
    @Tool(
        "ship_goal",
        description=(
            "执行一个游戏目标（如扫荡、领取、出征）。适配包会把目标展开成技能序列，"
            "在电脑侧闭环执行。mode=auto 时按配置决定演练或实跑；mode=dry 强制只演练；"
            "mode=live 请求实跑（配置里 dry_run=true 时仍然只演练）。"
        ),
        parameters=[
            ToolParameterInfo(
                name="goal",
                param_type=ToolParamType.STRING,
                description="目标名，必须在当前适配包的 goals.json 里存在（例如 look_main）",
                required=True,
            ),
            ToolParameterInfo(
                name="mode",
                param_type=ToolParamType.STRING,
                description="auto（默认，听配置）/ dry（强制演练）/ live（请求实跑）",
                required=False,
                default="auto",
            ),
        ],
    )
    async def tool_ship_goal(self, goal: str, mode: str = "auto", **kwargs: Any) -> dict[str, Any]:
        """执行目标。

        mode 只影响"是否请求演练"，不会改变闸门判断。真正的安全边界在
        SafetyGate 和 config.effective_dry_run 里：配置说演练，这里传 live
        也不会真的动。**让更危险的决定只能来自配置文件，不能来自一次
        工具调用**——这是这个插件最重要的一条设计约束。
        """
        try:
            runtime = self._require_runtime()
            requested = self._parse_mode(mode)
            result = await asyncio.to_thread(runtime.run_goal, goal, requested)
            if result.get("ok") and result.get("dry_run"):
                result["next"] = (
                    "这是演练结果，屏幕没有被操作。确认步骤与识别都正确后，"
                    "把配置里 safety.dry_run 改为 false 才会真正执行"
                )
            return result
        except ShipError as exc:
            return exc.to_dict()
        except Exception as exc:  # noqa: BLE001
            return wrap(exc).to_dict()

    @staticmethod
    def _parse_mode(mode: Any) -> bool | None:
        """把 mode 字符串翻译成 effective_dry_run 的入参。

            auto  → None  听配置的
            dry   → True  强制演练（无论配置）
            live  → False 请求实跑（配置仍可一票否决）

        未知值按 auto 处理而不是报错：模型填了奇怪的值时，回落到
        "听配置的"是更保守的选择，比直接失败更友好。
        """
        value = str(mode or "auto").strip().lower()
        if value == "dry":
            return True
        if value == "live":
            return False
        return None

    # ================================================================== #
    # Tool 3 / 4：控制
    # ================================================================== #
    @Tool(
        "ship_control",
        description=(
            "控制插件状态。action 取值：pause（暂停，立即停手）/ resume（恢复）/ "
            "reset_breaker（解除熔断）/ calibrate（走校准向导）/ screenshot（让电脑截一张图存下来）。"
        ),
        parameters=[
            ToolParameterInfo(
                name="action",
                param_type=ToolParamType.STRING,
                description="pause / resume / reset_breaker / calibrate / screenshot",
                required=True,
            ),
            ToolParameterInfo(
                name="reason",
                param_type=ToolParamType.STRING,
                description="原因，会记进执行轨迹（可选，例如 用户手动停止）",
                required=False,
                default="",
            ),
        ],
    )
    async def tool_ship_control(self, action: str, reason: str = "", **kwargs: Any) -> dict[str, Any]:
        """控制入口。

        合成一个 Tool 的理由见文件头。action 内部再做白名单校验，
        不依赖模型"只填合法值"——模型填错的概率不低，而这里填错的
        后果是执行一个没预期的动作，所以必须在代码里兜住。
        """
        try:
            runtime = self._require_runtime()
            act = str(action or "").strip().lower()

            if act == "pause":
                return await asyncio.to_thread(runtime.pause, reason or "用户指令")

            if act == "resume":
                return await asyncio.to_thread(runtime.resume)

            if act in ("reset_breaker", "reset"):
                runtime.gate.reset_breaker()
                return {"ok": True, "message": "熔断已解除，连续失败计数清零"}

            if act == "calibrate":
                return await asyncio.to_thread(runtime.calibrate)

            if act in ("screenshot", "shot"):
                return await asyncio.to_thread(runtime.screenshot, reason or "manual")

            return {
                "ok": False,
                "error_class": "E_CONFIG",
                "error": "未知的 action：%s" % action,
                "hint": "可用值：pause / resume / reset_breaker / calibrate / screenshot",
            }
        except ShipError as exc:
            return exc.to_dict()
        except Exception as exc:  # noqa: BLE001
            return wrap(exc).to_dict()

    # ================================================================== #
    # Tool 4 / 4：报告
    # ================================================================== #
    @Tool(
        "ship_report",
        description=(
            "出具报告。detail=status 返回整体状态与阻塞项；detail=diag 生成脱敏诊断包"
            "（发给作者排查问题用）；detail=diag_with_shots 同上但包含截图。"
        ),
        parameters=[
            ToolParameterInfo(
                name="detail",
                param_type=ToolParamType.STRING,
                description="status（默认）/ diag / diag_with_shots",
                required=False,
                default="status",
            )
        ],
    )
    async def tool_ship_report(self, detail: str = "status", **kwargs: Any) -> dict[str, Any]:
        """报告。

        status 永远返回内容（哪怕连不上 agent），因为它本身就是排查手段。
        diag 会落一个 zip，把路径给用户，让他直接转发。
        """
        try:
            runtime = self._require_runtime()
            kind = str(detail or "status").strip().lower()

            if kind in ("diag", "diagnose", "pack"):
                return await asyncio.to_thread(runtime.diagnose, False)

            if kind in ("diag_with_shots", "diag_shots", "pack_shots"):
                return await asyncio.to_thread(runtime.diagnose, True)

            if kind in ("status", "state", ""):
                return await asyncio.to_thread(runtime.status)

            return {
                "ok": False,
                "error_class": "E_CONFIG",
                "error": "未知的 detail：%s" % detail,
                "hint": "可用值：status / diag / diag_with_shots",
            }
        except ShipError as exc:
            return exc.to_dict()
        except Exception as exc:  # noqa: BLE001
            return wrap(exc).to_dict()

    # ================================================================== #
    # Command：给人用的运维入口
    # ================================================================== #
    @Command(
        "ship",
        # sub 是子命令，arg 是它的参数（例如 /ship run look_main）。
        # 只用一个可选参数而不是把每个子命令写成独立 Command：
        # 对用户来说 /ship XXX 是一个心智模型，拆成十几条命令只会让 help 更乱。
        pattern=r"^/ship(?:\s+(?P<sub>[A-Za-z_-]+))?(?:\s+(?P<arg>[^\s]+))?\s*$",
        description="shipctl 运维命令",
    )
    async def cmd_ship(self, **kwargs: Any) -> tuple[bool, str, int]:
        """命令行入口，必须返回三元组 (success, response, weight)。

        Tool 是给模型用的、Command 是给人用的，差别不只是措辞：
        命令可以输出长文本（免责声明原文、包清单、逐项体检结果），
        这些东西塞进 Tool 返回值只会白占模型上下文。
        """
        groups = kwargs.get("matched_groups") or {}
        stream_id = kwargs.get("stream_id", "")
        sub = (groups.get("sub", "") or "").strip().lower()
        arg = (groups.get("arg", "") or "").strip()

        try:
            runtime = self._require_runtime()

            if sub in ("", "help"):
                return True, self._help_text(), 2

            if sub == "disclaimer":
                return True, DISCLAIMER, 2

            if sub == "accept":
                # 记录确认。这个动作本身不需要过闸门（它不产生任何操作），
                # 但记完之后要把"还剩几道门"一并说清，否则用户以为确认完就能跑。
                result = await asyncio.to_thread(runtime.accept_disclaimer)
                remain = runtime.gate.installation_problems(False)
                lines = [
                    "已记录免责确认（%s）。" % (result.get("accepted_at") or "刚刚"),
                    "记录在插件数据目录，重启后仍然有效。",
                ]
                if remain:
                    lines.append("\n还差这几项才能实跑：")
                    for item in remain:
                        lines.append("· %s" % item)
                    lines.append("· 以及把 safety.dry_run 改成 false")
                else:
                    lines.append("\n其余条件看起来齐了，把 safety.dry_run 改成 false 就能实跑。")
                return True, "\n".join(lines), 2

            if sub in ("observe", "look"):
                data = await asyncio.to_thread(runtime.observe)
                return True, self._format_observe(data), 2

            if sub == "packs":
                return True, self._packs_text(runtime), 2

            if sub == "run":
                if not arg:
                    goals = runtime.pack.goal_names() if runtime.pack else []
                    return True, "用法：/ship run <目标名>\n当前包的目标：%s" % (", ".join(goals) or "无"), 1
                result = await asyncio.to_thread(runtime.run_goal, arg)
                return True, self._format_run(result), 2

            if sub == "reload":
                names = await asyncio.to_thread(runtime.reload_packs)
                return True, "已重新扫描适配包：%s" % (", ".join(names) or "没有找到合法包"), 2

            if sub in ("status", "doctor"):
                status = await asyncio.to_thread(runtime.status)
                return True, self._format_status(status, verbose=(sub == "doctor")), 2

            if sub in ("pause", "stop"):
                result = await asyncio.to_thread(runtime.pause, "用户命令：/ship pause")
                return True, result.get("message") or "已暂停", 2

            if sub in ("resume", "continue"):
                result = await asyncio.to_thread(runtime.resume)
                note = "（注意：还处于熔断冷却期）" if runtime.gate.snapshot().get("breaker_active") else ""
                return True, (result.get("message") or "已恢复") + note, 2

            if sub in ("reset", "reset-breaker"):
                runtime.gate.reset_breaker()
                return True, "熔断已解除，连续失败计数清零。\n不会自动恢复执行，下一次仍然要你主动发起。", 2

            if sub == "calibrate":
                result = await asyncio.to_thread(runtime.calibrate)
                if result.get("ok"):
                    return True, "校准完成。分辨率 %s，缩放 %s" % (
                        result.get("resolution") or "?",
                        result.get("scale") or "?",
                    ), 2
                return True, "校准未通过：%s" % (result.get("message") or "未知原因"), 1

            if sub in ("diag", "diagnose"):
                include_shots = arg.lower() in ("shots", "screenshot", "with-shots")
                result = await asyncio.to_thread(runtime.diagnose, include_shots)
                return True, "诊断包已生成：\n%s\n%s" % (
                    result.get("path"),
                    result.get("note") or "",
                ), 2

            return True, "未知子命令：%s。发 /ship help 看列表" % sub, 1

        except ShipError as exc:
            # 命令失败也要回话，否则用户以为机器人没收到
            await self._reply(exc.message, stream_id)
            return True, "%s\n%s" % (exc.message, exc.hint or ""), 1
        except Exception as exc:  # noqa: BLE001
            wrapped = wrap(exc)
            return False, "%s\n%s" % (wrapped.message, wrapped.hint or ""), 1

    # ------------------------------------------------------------------ #
    # 文本渲染
    # ------------------------------------------------------------------ #
    @staticmethod
    def _help_text() -> str:
        """帮助。

        刻意把"常用 Tool"也列出来：用户学会用 /ship 之后，
        下一个问题通常是"我在聊天里怎么说它才会动"。
        """
        return (
            "shipctl %s 运维命令：\n"
            "  /ship status        整体状态、阻塞项、为什么不能跑\n"
            "  /ship doctor        一次性体检（连接 / 校准 / 热键 / 适配包 / 闸门）\n"
            "  /ship observe       只读看一眼现在是什么界面\n"
            "  /ship run <目标>    手动执行一个目标（排查用）\n"
            "  /ship pause         暂停（热键之外的第二条路，不替代热键）\n"
            "  /ship resume        恢复\n"
            "  /ship reset         解除熔断，不会自动恢复执行\n"
            "  /ship calibrate     重新校准\n"
            "  /ship diag [shots]  生成脱敏诊断包\n"
            "  /ship packs         列出扫描到的适配包\n"
            "  /ship reload        重新扫描适配包（改完 JSON 不用重启）\n"
            "  /ship disclaimer    显示免责声明原文\n"
            "  /ship accept        记录\"我已阅读并接受\"\n"
            "\n对话里可以直接说：\n"
            "  「现在游戏在什么界面」        → ship_state\n"
            "  「帮我执行 look_main 看看」   → ship_goal\n"
            "  「停下」「继续」「校准一下」  → ship_control\n"
            "  「出个诊断包」                → ship_report"
            % PLUGIN_VERSION
        )

    @staticmethod
    def _packs_text(runtime: Runtime) -> str:
        """包清单。

        用 ▶ 标出当前生效的包：用户放了好几个包时，最常问的就是
        "现在到底用的哪个"。
        """
        lines = ["适配包目录：%s" % runtime._adapter_root()]  # noqa: SLF001
        if not runtime.packs:
            lines.append("  （没有扫到任何合法包）")
            lines.append("\n放新包：把目录放进 game_adapter/ 后发 /ship reload")
            return "\n".join(lines)

        for pack in sorted(runtime.packs.values(), key=lambda p: p.pack_id):
            mark = "▶" if runtime.pack and pack.pack_id == runtime.pack.pack_id else " "
            lines.append(
                "  %s %s v%s%s"
                % (mark, pack.pack_id, pack.version, "（demo，只能演练）" if pack.demo else "")
            )
            lines.append(
                "      状态 %d / 目标 %d / 技能 %d"
                % (len(pack.states), len(pack.goals), len(pack.skills))
            )
        lines.append("\n放新包：把目录放进 game_adapter/ 后发 /ship reload")
        return "\n".join(lines)

    @staticmethod
    def _format_observe(data: dict[str, Any]) -> str:
        """把 observe 的结果说成人话。

        认不出来的情况单独写一段：这是用户最可能遇到的"看起来坏了"，
        而实际上多半只是不在游戏里、或者分辨率变了。
        """
        state = data.get("state") or "unknown"
        conf = float(data.get("confidence") or 0.0)

        if not data.get("known"):
            return (
                "认不出当前画面（state=unknown）。\n"
                "常见原因：\n"
                "1. 不在游戏主界面（还在加载、弹窗、或切到别的应用了）\n"
                "2. 分辨率或缩放改过，需要 /ship calibrate\n"
                "3. 适配包的模板图对不上这个版本的游戏界面\n"
                "\n这不是故障——认不出就停下来，是这套系统的设计。"
            )

        lines = [
            "当前界面：%s（置信度 %.2f）" % (state, conf),
            "分辨率：%s" % (data.get("resolution") or "?"),
            "适配包：%s" % (data.get("pack") or "无"),
            "模式：%s" % ("演练" if data.get("dry_run") else "实跑"),
        ]
        elements = data.get("elements") or {}
        if elements:
            names = ", ".join(sorted(elements))
            lines.append("识别到的元素：%s" % names)
        return "\n".join(lines)

    @staticmethod
    def _format_run(data: dict[str, Any]) -> str:
        """执行结果。

        只列成功的步骤会掩盖问题，所以失败也照原样列出（带分类），
        但把失败原因放在最后一行——用户先看结论，再看细节。
        """
        head = "%s %s（%s，用时 %d ms）" % (
            "完成" if data.get("ok") else "未完成",
            data.get("goal"),
            "演练" if data.get("dry_run") else "实跑",
            int(data.get("elapsed_ms") or 0),
        )

        lines = [head]
        steps = data.get("steps") or []
        if steps:
            lines.append("步骤：")
            for idx, step in enumerate(steps, start=1):
                mark = "✓" if step.get("ok") else "✗"
                detail = step.get("action") or step.get("op") or "?"
                note = ""
                if step.get("would_do"):
                    note = "（演练，未执行）"
                elif step.get("error_class"):
                    note = "（%s）" % step.get("error_class")
                lines.append("  %d. %s %s%s" % (idx, mark, detail, note))
        else:
            lines.append("没有执行任何步骤。")

        if data.get("message"):
            lines.append("\n说明：%s" % data.get("message"))
        if data.get("run_id"):
            # 给 run_id 是为了排查时能对上 traces/*.jsonl 里的那一条
            lines.append("记录：%s" % data.get("run_id"))
        return "\n".join(lines)

    @staticmethod
    def _format_status(status: dict[str, Any], verbose: bool = False) -> str:
        """把状态 dict 排成人能读的几行。

        顺序是固定的，且和"用户接下来该做什么"一致：
        模式 → 包 → 连接 → 校准 → 热键 → 闸门 → 阻塞项。
        反过来先报阻塞项再报原因，用户会先慌。
        """
        lines: list[str] = []
        pack = status.get("pack")
        agent = status.get("agent") or {}
        gate = status.get("gate") or {}

        lines.append("模式：%s" % ("演练（只识别不操作）" if status.get("dry_run") else "实跑"))

        if pack:
            lines.append(
                "适配包：%s v%s%s"
                % (pack.get("pack_id"), pack.get("version"), "（demo）" if pack.get("demo") else "")
            )
        else:
            lines.append("适配包：无")

        if agent.get("connected"):
            lines.append(
                "连接：正常（协议 v%s，agent %s，分辨率 %s）"
                % (
                    agent.get("protocol") or "?",
                    agent.get("agent_version") or "?",
                    agent.get("resolution") or "?",
                )
            )
        else:
            lines.append("连接：失败 — %s" % (agent.get("error") or "未知原因"))

        cal = agent.get("calibration") or {}
        lines.append("校准：%s" % ("通过" if cal.get("ok") else "未通过（%s）" % (cal.get("reason") or "未校准")))
        # "（未验证）"这四个字不能省：插件只能检查你填没填热键，
        # 无法验证它在 agent 上真的注册成功、真的按得停。
        # 假装验过是最坏的做法——用户会因为信任它而在出事时多等几秒。
        hotkey = gate.get("pause_hotkey")
        lines.append("暂停热键：%s" % (("%s（未验证）" % hotkey) if hotkey else "未设置"))

        if gate.get("paused"):
            lines.append("闸门：暂停中 — %s" % gate.get("pause_reason"))
        elif gate.get("breaker_active"):
            lines.append("闸门：熔断中，还需 %s 秒冷却" % gate.get("breaker_remaining_sec"))
        else:
            lines.append("闸门：正常，最近 60 秒动作 %s 次" % gate.get("actions_last_60s"))

        blockers = status.get("blocking_problems") or []
        if blockers:
            lines.append("\n还不能实跑，有 %d 项要解决：" % len(blockers))
            for idx, item in enumerate(blockers, start=1):
                lines.append("%d. %s" % (idx, item))
        elif status.get("dry_run"):
            lines.append("\n当前是演练模式，可以直接试：让助手执行 look_main")
        else:
            lines.append("\n✓ 条件齐备，可以实跑")

        if verbose and pack:
            lines.append("\n状态：%s" % (", ".join(pack.get("states") or []) or "无"))
            lines.append("目标：%s" % (", ".join(pack.get("goals") or []) or "无"))
            lines.append("技能：%s" % (", ".join(pack.get("skills") or []) or "无"))
            lines.append("插件版本：%s" % status.get("plugin_version"))

        return "\n".join(lines)


# 声明配置模型（仅在 SDK 支持时）。放在类外赋值，是为了让不支持的环境里
# 类上根本不存在 config_model 属性，而不是存在一个 None。
if _CONFIG_MODEL_OK:
    ShipControlPlugin.config_model = ShipPluginConfig  # type: ignore[attr-defined]


def create_plugin() -> ShipControlPlugin:
    """SDK 约定的入口：返回实例。"""
    return ShipControlPlugin()
