"""目标执行策略。

这个模块只做一件事：把一个"目标名"变成一串技能调用，并决定失败时怎么办。
它不知道坐标、不知道模板、不知道图像——这是刻意的分层，也是这套设计
能长期维护的原因。

执行前必须先看一眼（observe），理由是 requires_state：

    "collect" 这个目标的前提是"我在主页面"。如果现在其实在战斗结算弹窗上，
    直接点下去就是错位操作。所以先 observe，状态不符就拒绝执行，
    而不是"点一下看看会发生什么"。后者是自动化脚本翻车的主要原因。

失败策略只有三档，故意做得很少：

    abort       失败即停（默认）
    retry_once  整个目标重试一次

为什么不做"单步重试三次"这种：单步重试在"确认/消耗/购买"类操作上是
灾难（可能重复扣资源），而分不清哪些步骤能重试的是适配包作者，
不是引擎。所以引擎只做整体重试，且技能自己可以标 retryable=False
把整体重试也禁掉。
"""

from __future__ import annotations

import time
from typing import Any

from .errors import ConfigError, ShipError, StateError, UiError, wrap
from .gate import SafetyGate
from .protocol import AgentClient
from .trace import RunRecorder, StepRecord, TraceStore


class Planner:
    """把 goal 推进成 skill 序列。

    依赖注入而不是自己 new 客户端，是为了可测：单测里塞一个假 client
    就能跑完整流程，不需要真的开一个 HTTP 服务。
    """

    def __init__(
        self,
        client: AgentClient,
        gate: SafetyGate,
        store: TraceStore,
        pack: Any,
        dry_run: bool,
    ):
        self.client = client
        self.gate = gate
        self.store = store
        self.pack = pack
        self.dry_run = bool(dry_run)

    # ------------------------------------------------------------------ #
    # 对外入口
    # ------------------------------------------------------------------ #
    def run_goal(self, goal_name: str, args: dict[str, Any] | None = None) -> RunRecorder:
        """执行一个目标，返回记录器。

        注意返回值而不是抛异常：调用方需要 trace 内容来渲染结果卡片，
        异常会把它冲掉。真正的失败信息在 recorder.ok / error_class 里，
        由 runtime 层决定要不要转成异常。
        """
        if self.pack is None:
            raise ConfigError(
                "没有加载任何适配包",
                hint="在 game_adapter/ 下放一个包，或在配置里指定 pack_id",
            )
        goal = self.pack.goals.get(goal_name)
        if goal is None:
            raise ConfigError(
                "没有这个目标：%s" % goal_name,
                hint="可用的目标：%s" % (", ".join(self.pack.goal_names()) or "无"),
                detail={"available": self.pack.goal_names()},
            )

        # demo 包不允许实跑。这条在引擎层拦，而不是靠文档提醒。
        if self.pack.demo and not self.dry_run:
            from .errors import SafetyError

            raise SafetyError(
                "当前是 demo 适配包，只能演练",
                hint="demo 包故意只含界面识别、不含可执行动作。要真跑请换正式包",
                detail={"pack_id": self.pack.pack_id},
            )

        record = RunRecorder(self.store, goal_name, self.dry_run)
        with record:
            try:
                self._execute(record, goal, dict(args or {}))
            except ShipError as exc:
                # 已分类的失败：记下来并把异常继续抛给调用方做 UI 决策
                record.finish(ok=False, error_class=exc.code, message=exc.message)
                self.gate.record_failure(exc.code)
                raise
            except Exception as exc:  # noqa: BLE001
                wrapped = wrap(exc)
                record.finish(ok=False, error_class=wrapped.code, message=wrapped.message)
                self.gate.record_failure(wrapped.code)
                raise wrapped
        return record

    # ------------------------------------------------------------------ #
    # 内部流程
    # ------------------------------------------------------------------ #
    def _execute(self, record: RunRecorder, goal: Any, args: dict[str, Any]) -> None:
        """走完一个目标。任何一步失败都直接抛出，由 run_goal 收口。"""
        attempts = 0
        while True:
            attempts += 1
            try:
                self._attempt(record, goal, args)
                record.finish(ok=True, message="完成")
                self.gate.record_success()
                return
            except ShipError as exc:
                can_retry = goal.on_fail == "retry_once" and attempts < goal.max_attempts
                # 非幂等技能不允许整体重试：重试可能重复消耗资源
                if can_retry and not self._has_non_retryable(goal):
                    record.step(
                        StepRecord(
                            ts=time.time(),
                            kind="retry",
                            ok=False,
                            error_class=exc.code,
                            message="第 %d 次尝试失败，按策略重试：%s" % (attempts, exc.message),
                        )
                    )
                    continue
                raise

    def _attempt(self, record: RunRecorder, goal: Any, args: dict[str, Any]) -> None:
        """一次完整尝试：先看现场，再逐步执行。"""
        # --- 步骤一：看现场，校验前置状态 ---
        observed = self._observe(record)
        state = str(observed.get("state") or "")
        confidence = float(observed.get("confidence") or 0.0)

        # 约束二在此处的体现：unknown 一律终止，不论 goal 要求什么
        if state == "unknown":
            record.step(
                StepRecord(
                    ts=time.time(),
                    kind="gate_denied",
                    state=state,
                    confidence=confidence,
                    ok=False,
                    error_class="E_UI",
                    message="当前界面无法识别，拒绝执行任何操作",
                )
            )
            raise UiError(
                "当前界面无法识别（unknown），已终止执行",
                hint="可能原因：游戏改版、弹窗遮挡、分辨率/语言不支持，或从未跑过校准。"
                     "发 /ship shot 看一眼当前画面，或先运行校准",
                detail={"confidence": confidence},
            )

        requires = goal.requires_state
        if requires not in ("any", state):
            record.step(
                StepRecord(
                    ts=time.time(),
                    kind="gate_denied",
                    state=state,
                    confidence=confidence,
                    ok=False,
                    error_class="E_STATE",
                    message="前置状态不符：需要在 %s，实际在 %s" % (requires, state),
                )
            )
            raise StateError(
                "目标 %s 需要在「%s」界面，但当前在「%s」" % (goal.name, requires, state),
                hint="先回到目标要求的界面再执行；如果已经在该界面却被判错，说明模板需要更新",
                detail={"required": requires, "actual": state},
            )

        # --- 步骤二：逐技能执行 ---
        for idx, step in enumerate(goal.steps, start=1):
            skill_name = str(step.get("skill"))
            skill = self.pack.skills.get(skill_name)
            if skill is None:
                # load_pack 已经查过引用，走到这里说明 pack 在运行时被改了
                raise ConfigError("目标引用了不存在的技能：%s" % skill_name)

            self._run_skill(record, goal, skill, step, idx)

    def _run_skill(self, record: RunRecorder, goal: Any, skill: Any, step: dict, idx: int) -> None:
        """执行单个技能。

        注意这里**不做任何坐标计算**。坐标完全在 agent 侧由模板匹配现场得出。
        插件这一层的输入只有技能名和参数，输出只有成功/失败和最终状态。
        """
        started = time.time()

        # 实跑前过闸门。演练模式不产生输入事件，不受限流/熔断约束，
        # 但依然记录——用户需要看清"如果真跑会怎样"。
        if not self.dry_run:
            try:
                self.gate.check_can_act()
            except ShipError as exc:
                record.step(
                    StepRecord(
                        ts=time.time(),
                        kind="gate_denied",
                        skill=skill.name,
                        ok=False,
                        error_class=exc.code,
                        message=exc.message,
                    )
                )
                raise

        try:
            result = self.client.run_skill(
                skill=skill.name,
                args=dict(step.get("args") or {}),
                dry_run=self.dry_run,
                deadline_ms=skill.timeout_ms,
            )
        except ShipError as exc:
            record.step(
                StepRecord(
                    ts=time.time(),
                    kind="skill",
                    skill=skill.name,
                    ok=False,
                    error_class=exc.code,
                    message=exc.message,
                    elapsed_ms=int((time.time() - started) * 1000),
                )
            )
            raise

        final_state = str(result.get("final_state") or "")
        ok = bool(result.get("ok", True))

        record.step(
            StepRecord(
                ts=time.time(),
                kind="skill",
                state=final_state,
                confidence=float(result.get("confidence") or 0.0),
                skill=skill.name,
                ok=ok,
                error_class=str(result.get("error_class") or ""),
                message=str(result.get("error") or ""),
                would_do=list(result.get("would_do") or []),
                elapsed_ms=int((time.time() - started) * 1000),
                extra={"index": idx, "goal": goal.name},
            )
        )

        if not ok:
            code = str(result.get("error_class") or "E_UNKNOWN")
            from .errors import error_from_code

            raise error_from_code(
                code,
                "技能 %s 执行失败：%s" % (skill.name, result.get("error") or "未给出原因"),
                hint="看 trace 里这一步的 state 和 confidence，通常能判断是识别错还是界面没到位",
                detail={"skill": skill.name, "final_state": final_state},
            )

        # 技能执行完的实际状态必须和声明的 expect_state 一致。
        # 不一致不一定是错误（比如多了一步动画），但必须记录，否则
        # 后面会莫名其妙地在错的界面上继续操作。
        if self.dry_run:
            return

        if final_state == "unknown":
            record.step(
                StepRecord(
                    ts=time.time(),
                    kind="gate_denied",
                    state=final_state,
                    skill=skill.name,
                    ok=False,
                    error_class="E_UI",
                    message="技能执行后界面变为 unknown，终止",
                )
            )
            raise UiError(
                "技能 %s 执行后界面无法识别，已终止" % skill.name,
                hint="可能是技能执行后弹出了未适配的窗口。发 /ship shot 看一下当前画面",
            )

        if skill.expect_state not in ("", "any", final_state):
            record.step(
                StepRecord(
                    ts=time.time(),
                    kind="state_mismatch",
                    state=final_state,
                    skill=skill.name,
                    ok=False,
                    message="期望 %s，实际 %s" % (skill.expect_state, final_state),
                )
            )
            raise StateError(
                "技能 %s 执行后应在「%s」，实际在「%s」"
                % (skill.name, skill.expect_state, final_state),
                hint="适配包里的 expect_state 可能写错了，或界面加载慢。重试一次看是否偶发",
                detail={"expected": skill.expect_state, "actual": final_state},
            )

    def _observe(self, record: RunRecorder) -> dict[str, Any]:
        """看一眼前方界面，并记录。

        演练模式下这一步就是全部价值：用户能看到"它以为现在在哪、有多确定"。
        """
        try:
            observed = self.client.observe()
        except ShipError as exc:
            record.step(
                StepRecord(
                    ts=time.time(),
                    kind="observe",
                    ok=False,
                    error_class=exc.code,
                    message=exc.message,
                )
            )
            raise
        record.step(
            StepRecord(
                ts=time.time(),
                kind="observe",
                state=str(observed.get("state") or ""),
                confidence=float(observed.get("confidence") or 0.0),
                ok=True,
            )
        )
        return observed

    @staticmethod
    def _has_non_retryable(goal: Any) -> bool:
        """目标里有没有"绝不能重复执行"的技能。

        只要有一个，整个目标就不允许整体重试——因为你无法保证重试时
        不会把那个技能再跑一遍。
        """
        for step in goal.steps:
            if step.get("retryable") is False:
                return True
        return False
