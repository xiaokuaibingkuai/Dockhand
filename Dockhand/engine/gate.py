"""安全闸门与熔断。

把散落在各处的 if 判断收成一扇门，原因有两个：

1. 可解释。用户问"为什么不让我跑"，答案必须是一句具体的话，
   而不是"某处逻辑返回了 False"。
2. 不可绕过。只要有第二处地方能发动作（哪怕是个调试用的小函数），
   所有闸门就都白装了。所以本模块的 `check_can_run()` 是唯一入口，
   且 runtime 层强制所有动作走它。

三层闸门，按"装 — 跑 — 会"的顺序：

    装机闸门（InstallationGate）—— 一次性前置条件
        · 校准通过（否则不知道屏幕长什么样）
        · 暂停热键已配置（否则用户按不住它）
        · 有一点必须说清：热键是 agent 侧实现的（它才能抢全局键盘），
          插件只能校验"配置里声明了热键"，不能校验"热键真的生效"。
          这是诚实标注的局限，不要假装能验。

    运行闸门（RuntimeGate）—— 每次动作前检查
        · 是否被暂停（人按了热键 / 上次熔断未冷却）
        · 连续失败次数是否超限（熔断）
        · 单位时间动作数是否超限（限流）

    会话闸门（SessionGate）—— 一次连续操作的生命周期
        · 必须显式开始（begin），不会因为"调了个工具"就自动开跑
        · 有总时长上限，到点自动结束，防止忘了关
        · 结束必须释放（end），异常路径也要走到

为什么熔断阈值在公开版要比自用版低：

自用的时候，识别失败 3 次再停是可以接受的，你会自己看日志。
分发出去以后，"失败了还在继续试"是用户投诉的主要来源——
他们会在群里说"这插件乱点我屏幕"。所以公开版默认连续 1 次
识别失败就熔断，把"停"当作默认行为，"继续"才需要理由。
"""

from __future__ import annotations

import time
from collections import deque
from typing import Any

from .errors import SafetyError


class SafetyGate:
    """三层闸门的统一实现。

    所有状态都放在实例里，不放全局——插件可能被重载，
    重载后闸门应该回到干净状态（用户重载插件通常就是为了"重来一次"）。
    """

    def __init__(
        self,
        max_actions_per_min: int = 20,
        breaker_threshold: int = 1,
        breaker_cooldown_sec: int = 300,
        session_max_sec: int = 1800,
    ):
        # 限流：滑动窗口，只保留最近 60 秒的动作时间戳
        self.max_actions_per_min = max(1, int(max_actions_per_min))
        self._action_times: deque[float] = deque()

        # 熔断
        self.breaker_threshold = max(1, int(breaker_threshold))
        self.breaker_cooldown_sec = max(0, int(breaker_cooldown_sec))
        self._consecutive_failures = 0
        self._breaker_until = 0.0
        self._last_failure_class = ""

        # 会话
        self.session_max_sec = max(60, int(session_max_sec))
        self._session_started: float | None = None

        # 人工暂停（插件侧记录；agent 侧也有自己的 paused 标志，两边都要认）
        self._paused_reason = ""

        # 装机闸门所需的外部事实，由 runtime 在启动时灌进来
        self._calibrated = False
        self._calibration_reason = "尚未检查校准状态"
        self._pause_hotkey = ""

    # ------------------------------------------------------------------ #
    # 外部状态注入
    # ------------------------------------------------------------------ #
    def set_calibration(self, ok: bool, reason: str = "") -> None:
        """由 runtime 在协商成功后调用，记录校准结论。

        不在这里主动去查，是为了让 gate 保持纯逻辑、可单测，
        不需要 mock 一个 HTTP 客户端。
        """
        self._calibrated = bool(ok)
        self._calibration_reason = reason or ("校准通过" if ok else "校准未通过")

    def set_pause_hotkey(self, hotkey: str) -> None:
        """记录用户声明的暂停热键（仅用于装机闸门校验"有没有声明"）。"""
        self._pause_hotkey = (hotkey or "").strip()

    # ------------------------------------------------------------------ #
    # 装机闸门
    # ------------------------------------------------------------------ #
    def installation_problems(self, effective_dry_run: bool) -> list[str]:
        """返回装机阶段缺什么。

        注意 `effective_dry_run`：演练模式不需要校准也不需要热键，
        因为它本来就不产生输入事件。要求演练模式也校准会把
        "想先看看识别对不对"的用户挡在门外，那是错的顺序。
        """
        if effective_dry_run:
            return []

        problems: list[str] = []
        if not self._calibrated:
            problems.append("未完成校准：%s。先运行 /ship calibrate 走校准向导" % self._calibration_reason)
        if not self._pause_hotkey:
            problems.append(
                "未设置暂停热键：必须在 agent 配置里设一个紧急停止热键（例如 F12），"
                "否则出问题时你按不住它。设好后在插件配置里填同一个键名"
            )
        return problems

    # ------------------------------------------------------------------ #
    # 运行闸门
    # ------------------------------------------------------------------ #
    def _breaker_active(self) -> bool:
        return time.time() < self._breaker_until

    def check_can_act(self) -> None:
        """每次要产生输入事件前调用。不通过就抛 SafetyError。

        为什么抛异常而不是返回 bool：
        返回 bool 太容易被调用方忽略（`gate.check() or continue` 这种写法
        迟早会出现），异常会强制每个调用点处理"不能做"的情况。
        """
        now = time.time()

        # 1) 人工暂停优先于一切，包括熔断
        if self._paused_reason:
            raise SafetyError(
                "已暂停：%s" % self._paused_reason,
                hint="发送 /ship resume 或按下暂停热键恢复",
            )

        # 2) 熔断
        if self._breaker_active():
            remain = int(self._breaker_until - now)
            raise SafetyError(
                "熔断中：连续 %d 次失败（%s），还需等待 %d 秒"
                % (self._consecutive_failures, self._last_failure_class or "未知", remain),
                hint="熔断是为了避免在异常界面上反复乱点。排查后可用 /ship reset 手动解除",
                detail={"remaining_sec": remain},
            )

        # 3) 会话
        if self._session_started is None:
            raise SafetyError(
                "没有进行中的操作会话",
                hint="每次实跑前需要显式开始一次会话（调用 run_goal 会自动开始）",
            )
        elapsed = now - self._session_started
        if elapsed > self.session_max_sec:
            self.end_session("会话超时")
            raise SafetyError(
                "会话已超时（%.0f 秒上限）自动结束" % self.session_max_sec,
                hint="这是防止忘记关闭的兜底。需要继续就重新发起一次目标",
            )

        # 4) 限流
        self._trim_action_window(now)
        if len(self._action_times) >= self.max_actions_per_min:
            raise SafetyError(
                "已达每分钟动作上限（%d 次）" % self.max_actions_per_min,
                hint="这是防止异常循环的保护。等一分钟，或调高配置里的上限（不建议）",
            )

    def record_action(self) -> None:
        """记录一次已经发生的动作。只统计真正发出去的，演练模式不计。"""
        self._action_times.append(time.time())

    def _trim_action_window(self, now: float) -> None:
        while self._action_times and now - self._action_times[0] >= 60.0:
            self._action_times.popleft()

    # ------------------------------------------------------------------ #
    # 结果记账（熔断依据）
    # ------------------------------------------------------------------ #
    def record_success(self) -> None:
        """成功一次就把连续失败清零。

        这里不做"失败率"统计，是刻意的：滑动失败率在低频操作场景下
        噪声太大，容易在正常波动时误熔断。连续失败更适合"界面变了"
        这种真实故障模式——真出问题时是连续失败，不是偶发。
        """
        self._consecutive_failures = 0
        self._last_failure_class = ""

    def record_failure(self, error_class: str) -> None:
        """失败记账，达到阈值就开熔断。"""
        self._consecutive_failures += 1
        self._last_failure_class = error_class
        if self._consecutive_failures >= self.breaker_threshold:
            self._breaker_until = time.time() + self.breaker_cooldown_sec

    # ------------------------------------------------------------------ #
    # 会话控制
    # ------------------------------------------------------------------ #
    def begin_session(self) -> None:
        self._session_started = time.time()

    def end_session(self, reason: str = "") -> None:
        self._session_started = None
        if reason:
            self._paused_reason = ""

    @property
    def in_session(self) -> bool:
        return self._session_started is not None

    def session_elapsed(self) -> float:
        return 0.0 if self._session_started is None else time.time() - self._session_started

    # ------------------------------------------------------------------ #
    # 暂停 / 复位
    # ------------------------------------------------------------------ #
    def pause(self, reason: str) -> None:
        self._paused_reason = reason or "人工暂停"

    def resume(self) -> None:
        self._paused_reason = ""

    def reset_breaker(self) -> None:
        """手动解除熔断。

        为什么允许手动解除：如果用户明确知道失败原因（比如只是游戏在维护），
        强制等 5 分钟没有意义。但解除不应该清掉失败计数之外的东西。
        """
        self._breaker_until = 0.0
        self._consecutive_failures = 0
        self._last_failure_class = ""

    @property
    def paused(self) -> bool:
        return bool(self._paused_reason)

    def snapshot(self) -> dict[str, Any]:
        """给 /ship status 和诊断包用的状态快照。字段名保持稳定。"""
        self._trim_action_window(time.time())
        return {
            "paused": self.paused,
            "pause_reason": self._paused_reason,
            "breaker_active": self._breaker_active(),
            "breaker_remaining_sec": max(0, int(self._breaker_until - time.time())),
            "consecutive_failures": self._consecutive_failures,
            "last_failure_class": self._last_failure_class,
            "actions_last_60s": len(self._action_times),
            "max_actions_per_min": self.max_actions_per_min,
            "in_session": self.in_session,
            "session_elapsed_sec": round(self.session_elapsed(), 1),
            "calibrated": self._calibrated,
            "pause_hotkey": self._pause_hotkey or None,
        }
