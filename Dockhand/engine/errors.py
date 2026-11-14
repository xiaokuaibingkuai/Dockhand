"""错误分类模块。

设计意图（为什么不用裸 RuntimeError）：

这个插件最终要分发给不认识你的人用。当对方发来一句"插件报错了"，
你必须能在一秒内判断：是他环境的问题、还是你代码的问题、还是他界面不认识。

一个字符串错误做不到这件事。所以所有异常都必须带 `code`，代码分五类，
每类对应「用户该做什么」和「作者该做什么」，二者不重叠：

    E_ENV     环境问题    → 用户去修（adb 掉了、防火墙、IP 错、agent 没启动）
    E_PROTO   协议问题    → 版本不匹配，用户升级 agent（作者发布新版）
    E_UI      界面不认识  → 用户截图反馈，作者补模板
    E_STATE   状态不满足  → 时序问题，通常是界面还没加载完，可重试
    E_TIMEOUT 超时        → 可重试，重试仍失败则降级为 E_ENV
    E_SAFETY  安全闸门    → 不是故障，是设计如此，告知用户怎么解锁
    E_CONFIG  配置错误    → 用户改配置

`hint` 字段是给人看的一句话，必须可直接执行（"端口没开就放行 8765"），
不允许写"请检查网络"这种没有动作的话。
"""

from __future__ import annotations

from typing import Any


class ShipError(Exception):
    """所有插件内部异常的基类。

    约定：
    1. 永远不要让 ShipError 之外的异常冒泡到 Tool 层。Tool 层的统一
       except 会把未知异常包成 E_UNKNOWN，那样就丢掉了分类信息。
    2. `detail` 用于放机器可读的上下文（比如 http_status、state_name），
       会被写进 trace 和诊断包，不要往里面塞长文本。
    """

    code: str = "E_UNKNOWN"
    default_hint: str = ""

    def __init__(self, message: str, hint: str = "", detail: dict[str, Any] | None = None):
        super().__init__(message)
        self.message = message
        # hint 为空时回落到类级默认提示，保证 UI 上永远有一句人话
        self.hint = hint or self.default_hint
        self.detail = dict(detail or {})

    def to_dict(self) -> dict[str, Any]:
        """序列化成 Tool 返回值。字段名保持稳定，前端/诊断脚本依赖它。"""
        payload = {"ok": False, "error_class": self.code, "error": self.message}
        if self.hint:
            payload["hint"] = self.hint
        if self.detail:
            payload["detail"] = self.detail
        return payload


class EnvError(ShipError):
    """环境类：连不上 agent、设备离线、权限不足、截图黑屏。

    这类错误的共同点是「插件代码没错，是外面那层不通」，
    所以 hint 里必须给出具体的排查顺序。
    """

    code = "E_ENV"
    default_hint = "检查顺序：1) 电脑上 agent 在跑吗 2) 手机和电脑同一局域网吗 3) 防火墙放行端口了吗 4) adb devices 里设备是 device 还是 offline"


class ProtocolError(ShipError):
    """协议类：版本不匹配、返回体缺字段、JSON 解析失败。

    出现这个错误几乎一定是「插件升级了但 agent 没升级」，
    所以 hint 要直说升级 agent，不要让用户去猜。
    """

    code = "E_PROTO"
    default_hint = "插件与电脑上的 agent 版本不一致，请按 README 更新 agent（两者必须同一大版本）"


class UiError(ShipError):
    """界面类：模板匹配全都没过阈值，或者置信度低于要求。

    这是最需要「停手」的一类。认不出界面还继续点，就是乱点。
    所以这类错误在 planner 里默认不重试，直接终止并上报。
    """

    code = "E_UI"
    default_hint = "当前界面不在适配包的已知状态里。可能是游戏改版、弹窗遮挡、分辨率不支持，或没跑过校准"


class StateError(ShipError):
    """状态类：前置条件不满足、技能在本状态不可用。

    和 E_UI 的区别：E_UI 是「不知道自己在哪」，E_STATE 是「知道在哪，
    但这个状态不能做这件事」。后者常常是时序问题，值得重试。
    """

    code = "E_STATE"
    default_hint = "界面还没准备好（可能在加载），稍后重试；持续出现说明适配包的技能前置条件写错了"


class TimeoutError_(ShipError):
    """超时类。

    单独一类是因为它既可能是环境慢（模拟器卡），也可能是等错了状态。
    重试一次仍超时，traces 里能看出卡在哪个状态，据此判断原因。
    """

    code = "E_TIMEOUT"
    default_hint = "操作超时。模拟器可能卡了，或等待的界面一直没出现；重试一次，仍失败请导出诊断包"


class SafetyError(ShipError):
    """安全闸门拦截。

    这不是故障。用户看到 E_SAFETY 说明设计生效了，所以提示语气要
    是「告诉你怎么解锁」，而不是「出错了」。
    """

    code = "E_SAFETY"
    default_hint = "被安全闸门拦下。请先完成校准、设置暂停热键，或等待熔断冷却"


class ConfigError(ShipError):
    """配置类：适配包缺失、goal 名不存在、字段非法。"""

    code = "E_CONFIG"
    default_hint = "请检查配置和适配包：goal 名必须在 goals.json 里存在，游戏包要在 game_adapter/ 下"


# 把 agent 侧返回的 error_class 还原成对应异常，保证两端的错误分类一致。
# agent 侧只需要返回字符串 code，插件侧查表构造，避免两边各写一套 if。
_ERROR_MAP: dict[str, type[ShipError]] = {
    cls.code: cls
    for cls in (
        ShipError,
        EnvError,
        ProtocolError,
        UiError,
        StateError,
        TimeoutError_,
        SafetyError,
        ConfigError,
    )
}


def error_from_code(code: str, message: str, hint: str = "", detail: dict | None = None) -> ShipError:
    """按 agent 返回的 code 构造异常。

    未知 code 一律当 EnvError 处理：从插件视角看，
    "对面返回了个我不认识的东西"最接近环境/版本问题，提示也最有用。
    """
    cls = _ERROR_MAP.get(code or "", EnvError)
    return cls(message, hint=hint, detail=detail)


def wrap(exc: BaseException) -> ShipError:
    """把任意异常收口成 ShipError，用于 Tool 层统一兜底。

    注意：不要用它包 ShipError 自己，否则会丢掉子类分类。
    """
    if isinstance(exc, ShipError):
        return exc
    return ShipError("%s: %s" % (type(exc).__name__, exc))
