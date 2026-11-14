"""shipctl 通用执行引擎（包 A）。

这个包是"与具体游戏无关"的全部逻辑，可以单独阅读、单独理解：

    errors    错误分类
    protocol  与电脑侧 agent 的通信协议
    gate      安全闸门与熔断
    trace     执行轨迹与诊断包
    config    配置模型
    planner   目标 → 技能序列的推进策略
    runtime   装配层（唯一出入口）

刻意不包含：
    · 任何图像识别代码（在 agent 侧，见 DECISIONS.md 第 1 条）
    · 任何游戏的界面名、技能名、模板图（在 game_adapter/ 下）
    · 任何坐标（全局禁止，见 game_adapter/base.py）

引擎里出现的"字符串"只有协议字段名和状态名 unknown。
一旦发现自己在引擎里写下某个具体游戏的状态名，说明分层破了。
"""

from .config import ShipConfig
from .errors import (
    ConfigError,
    EnvError,
    ProtocolError,
    SafetyError,
    ShipError,
    StateError,
    TimeoutError_,
    UiError,
    wrap,
)
from .runtime import Runtime

__all__ = [
    "Runtime",
    "ShipConfig",
    "ShipError",
    "EnvError",
    "ProtocolError",
    "UiError",
    "StateError",
    "TimeoutError_",
    "SafetyError",
    "ConfigError",
    "wrap",
]
