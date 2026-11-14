"""运行时装配层。

这里是唯一允许把 client / gate / store / pack 拼在一起的地方。
放一起的好处是：任何一次操作，装配关系只有一种可能，不存在
"某条路径没走闸门"的漏洞。这是安全设计里最重要的性质——
不是"记得在每处检查"，而是"结构上只有一处能通过"。

对外只暴露七个方法，对应五个 Tool 和两个运维动作：

    observe()       看一眼
    run_goal()      做一件事
    pause()         停
    resume()        继续
    status()        现在什么情况
    diagnose()      打包给我看
    calibrate()     走校准

刻意不提供"直接调用某个技能"的入口。技能是目标的零件，
让人能单独调零件，就意味着能绕过目标的前置状态检查。
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from game_adapter.base import PackSpec, discover_packs
from .config import ShipConfig
from .errors import ConfigError, EnvError, SafetyError, ShipError, UiError
from .gate import SafetyGate
from .planner import Planner
from .protocol import AgentClient
from .trace import TraceStore, build_diagnostics

# 免责声明必须原样展示给用户。放在代码里而不是 README，是因为
# README 没人看，而这里会在第一次真跑时被打印出来。
DISCLAIMER = (
    "使用前确认：\n"
    "1) 本插件通过模拟点击/按键操作游戏，属于非官方自动化方式，存在账号被处罚的风险；\n"
    "2) 建议只在小号上验证，出问题不影响主号；\n"
    "3) 默认以演练模式运行，只有你明确关闭 dry_run 才会真正操作；\n"
    "4) 你必须自行确认该游戏的服务条款是否允许此类操作；\n"
    "5) 作者不对因使用本插件导致的任何账号、数据或财产损失负责。\n"
    "确认无误后，把配置里的 safety.disclaimer_accepted 改为 true。"
)


class Runtime:
    """插件运行时。生命周期跟随插件：on_load 创建，on_unload 释放。"""

    def __init__(self, config: ShipConfig, data_dir: Path | str, plugin_version: str):
        self.config = config
        self.plugin_version = plugin_version
        self.data_dir = Path(data_dir)

        self.client = AgentClient(
            base_url=config.connection.base_url,
            token=config.connection.token,
            timeout=config.connection.timeout_sec,
        )
        self.gate = SafetyGate(
            max_actions_per_min=config.safety.max_actions_per_min,
            breaker_threshold=config.safety.breaker_threshold,
            breaker_cooldown_sec=config.safety.breaker_cooldown_sec,
            session_max_sec=config.safety.session_max_sec,
        )
        self.gate.set_pause_hotkey(config.safety.pause_hotkey)

        self.store = TraceStore(self.data_dir / "traces")
        # 启动时清一次过期记录。不做定时任务，插件重启频率足够低。
        self.store.cleanup()

        self.packs: dict[str, PackSpec] = {}
        self.pack: PackSpec | None = None
        self._handshake_done = False

        # 免责确认的双来源。
        #
        # 正常路径是改配置里的 safety.disclaimer_accepted。但插件配置的写入
        # 能力在不同 SDK 小版本里不统一（有的有 ctx.config.set，有的没有），
        # 指望"发一句 /ship accept 就帮你改配置"是不现实的。
        #
        # 所以这里加一个本地标记文件：/ship accept 写它，重启后仍然有效。
        # 配置文件和本地标记取"或"——两种方式都能让免责确认生效，
        # 用户在 WebUI 里勾、或者在聊天里发一句，都算数。
        self._acceptance_file = self.data_dir / "disclaimer_accepted.json"
        self._accepted_at = self._read_acceptance()

        self._load_packs()

    # ------------------------------------------------------------------ #
    # 免责确认
    # ------------------------------------------------------------------ #
    def _read_acceptance(self) -> str:
        """读本地确认标记。返回确认时间字符串，没有就返回空串。

        读坏了不报错：一个坏掉的标记文件不该让插件加载失败，
        最坏情况是"需要重新确认一次"，这个代价可以接受。
        """
        try:
            if self._acceptance_file.is_file():
                data = json.loads(self._acceptance_file.read_text(encoding="utf-8"))
                if isinstance(data, dict) and data.get("accepted") is True:
                    return str(data.get("at") or "unknown")
        except Exception:  # noqa: BLE001
            pass
        return ""

    @property
    def disclaimer_accepted(self) -> bool:
        """免责是否已确认。配置与本地标记任一为真即可。"""
        return bool(self.config.safety.disclaimer_accepted or self._accepted_at)

    @property
    def disclaimer_source(self) -> str:
        """确认来源：config / local / none。

        写进状态里，是为了回答"我明明勾了为什么还说没确认"这类问题。
        """
        if self.config.safety.disclaimer_accepted:
            return "config"
        if self._accepted_at:
            return "local"
        return "none"

    def accept_disclaimer(self) -> dict[str, Any]:
        """记录免责确认，落盘到插件数据目录。

        只写这一个事实，不写"我同意了什么"的全文：全文随版本变化，
        存下来的旧文本反而会在将来产生"你当时同意的是旧版"的争议。
        版本号写进去，足够说明用户同意的是哪个版本的声明。
        """
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S")
        payload = {
            "accepted": True,
            "at": stamp,
            "plugin_version": self.plugin_version,
            "disclaimer_version": 1,
        }
        try:
            self._acceptance_file.parent.mkdir(parents=True, exist_ok=True)
            self._acceptance_file.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except OSError as exc:
            raise ConfigError(
                "无法写入确认标记：%s" % exc,
                hint="插件数据目录不可写。可以改为在 WebUI 里勾选 safety.disclaimer_accepted",
            ) from exc

        self._accepted_at = stamp
        # 不写执行轨迹：这不是一次"执行"，写进 traces 会污染"最近跑了什么"的列表。
        # 确认时间已经在标记文件里，需要时读它就行。
        return {
            "ok": True,
            "accepted_at": stamp,
            "source": "local",
            "note": "确认已记录在插件数据目录，重启后仍然有效",
        }

    # ------------------------------------------------------------------ #
    # 加载
    # ------------------------------------------------------------------ #
    def _adapter_root(self) -> Path:
        """适配包根目录。

        默认是插件目录下的 game_adapter/。允许配置覆盖是为了
        让二次开发的人把包放在别处而不必改插件目录。
        """
        configured = self.config.adapter.adapter_dir.strip()
        if configured:
            return Path(configured).expanduser()
        # __file__ = .../engine/runtime.py → 上一级就是插件根
        return Path(__file__).resolve().parent.parent / "game_adapter"

    def _load_packs(self) -> None:
        """扫描并选中一个包。

        选包规则（按顺序）：
          1. 配置指定了 pack_id → 必须存在，不存在直接报错（不要静默回落，
             否则用户改错名字却看到"能用"，实际用的是另一个包）
          2. 没指定 → 优先第一个正式包
          3. 只有 demo 包 → 用 demo（并因此只能演练）
        """
        root = self._adapter_root()
        self.packs = discover_packs(root)

        wanted = self.config.adapter.pack_id
        if wanted:
            if wanted not in self.packs:
                raise ConfigError(
                    "配置里指定的适配包不存在：%s" % wanted,
                    hint="可用包：%s。检查 game_adapter/ 下有没有这个目录" % (
                        ", ".join(sorted(self.packs)) or "（一个都没有）"
                    ),
                    detail={"available": sorted(self.packs), "root": str(root)},
                )
            self.pack = self.packs[wanted]
            return

        live = [p for p in self.packs.values() if not p.demo]
        self.pack = live[0] if live else (next(iter(self.packs.values())) if self.packs else None)

    def reload_packs(self) -> list[str]:
        """重新扫描适配包。

        用户自己写包时会需要这个：改完 JSON 不用重启整个麦麦。
        """
        self._load_packs()
        return sorted(self.packs)

    # ------------------------------------------------------------------ #
    # 握手（延迟执行）
    # ------------------------------------------------------------------ #
    def ensure_handshake(self) -> dict[str, Any]:
        """第一次真正用到时再连 agent。

        不在 on_load 里做，理由很实际：用户很可能先装插件、后开 agent。
        如果在 on_load 就要求连通，那装插件的顺序就被绑死了，
        报错也会出现在"加载插件"这种让人摸不着头脑的时机。

        握手内容：协议版本协商 + 校准状态 + 灌进闸门。
        """
        if self._handshake_done:
            return self.client.capabilities()

        caps = self.client.capabilities(refresh=True)

        cal = caps.get("calibration")
        if isinstance(cal, dict):
            self.gate.set_calibration(
                bool(cal.get("ok")),
                str(cal.get("reason") or ""),
            )
        else:
            # agent 没报校准信息，按未校准处理（保守方向）
            self.gate.set_calibration(
                not self.config.safety.require_calibration,
                "agent 未返回校准状态",
            )

        # agent 自报的暂停状态也要同步过来：用户可能在电脑上按了热键
        if caps.get("paused"):
            self.gate.pause("电脑侧已暂停（可能按了热键）")

        self._handshake_done = True
        return caps

    # ------------------------------------------------------------------ #
    # 只读
    # ------------------------------------------------------------------ #
    def observe(self) -> dict[str, Any]:
        """看一眼当前界面。

        这是唯一不需要通过运行时闸门的操作——它不产生任何副作用，
        而且用户排查问题时恰恰应该在"被熔断拦住"的时候还能看一眼。
        """
        self.ensure_handshake()
        observed = self.client.observe()
        state = str(observed.get("state") or "")
        return {
            "ok": True,
            "state": state,
            "confidence": float(observed.get("confidence") or 0.0),
            "known": state != "unknown",
            "resolution": observed.get("resolution"),
            "elements": observed.get("elements") or {},
            "pack": self.pack.name if self.pack else None,
            "dry_run": self.config.safety.dry_run,
        }

    def status(self) -> dict[str, Any]:
        """完整状态快照。

        这个方法的产出会直接展示给用户，所以要包含"他下一步该做什么"
        所需的一切：连没连上、校准了没、能不能跑、为什么不能跑。
        """
        info: dict[str, Any] = {
            "plugin_version": self.plugin_version,
            "dry_run": self.config.safety.dry_run,
            "pack": None,
            "packs_available": sorted(self.packs),
            "agent": None,
            "gate": self.gate.snapshot(),
            "disclaimer": {
                "accepted": self.disclaimer_accepted,
                "source": self.disclaimer_source,
                "accepted_at": self._accepted_at or None,
            },
            "blocking_problems": [],
        }

        if self.pack is not None:
            info["pack"] = {
                "pack_id": self.pack.pack_id,
                "name": self.pack.name,
                "version": self.pack.version,
                "demo": self.pack.demo,
                "states": self.pack.state_names(),
                "goals": self.pack.goal_names(),
                "skills": sorted(self.pack.skills),
            }
        else:
            info["blocking_problems"].append(
                "没有可用的适配包：game_adapter/ 下没有合法包。发 /ship packs 查看扫描结果"
            )

        # 连不上 agent 不该让 status 抛异常——排查状态下更需要能看状态
        try:
            caps = self.ensure_handshake()
            info["agent"] = {
                "connected": True,
                "protocol": caps.get("protocol"),
                "agent_version": caps.get("agent_version"),
                "resolution": caps.get("resolution"),
                "paused": bool(caps.get("paused")),
                "calibration": caps.get("calibration"),
                "skills": caps.get("skills") or [],
            }
        except ShipError as exc:
            info["agent"] = {"connected": False, "error_class": exc.code, "error": exc.message, "hint": exc.hint}
            info["blocking_problems"].append("连不上电脑上的 agent：%s" % exc.message)

        # 装机闸门问题一次性全列出来，不要一个个报
        info["blocking_problems"].extend(self._installation_blockers(self.config.safety.dry_run))
        info["can_run_live"] = not info["blocking_problems"]
        return info

    # ------------------------------------------------------------------ #
    # 执行
    # ------------------------------------------------------------------ #
    def _installation_blockers(self, effective_dry_run: bool) -> list[str]:
        """真跑前的所有阻塞项，按"用户先解决哪个"排序。

        顺序：免责确认 → 校准 → 热键。前一个是后一个的前提，
        乱序提示会让用户做完一件事又看到还有一件事，体验很差。
        """
        if effective_dry_run:
            return []

        problems: list[str] = []
        if not self.disclaimer_accepted:
            problems.append("未确认免责声明。\n" + DISCLAIMER)
        problems.extend(self.gate.installation_problems(effective_dry_run))
        return problems

    def run_goal(self, goal_name: str, requested_dry_run: bool | None = None) -> dict[str, Any]:
        """执行一个目标。

        `requested_dry_run` 只能往"更保守"的方向修改行为：
        配置是演练时，传 False 也不会真跑。反过来可以。
        这个单向性在 ShipConfig.effective_dry_run 里实现。
        """
        if self.pack is None:
            raise ConfigError(
                "没有可用的适配包，无法执行",
                hint="把适配包放到 game_adapter/ 下，或发 /ship packs 看扫描结果",
            )

        dry_run = self.config.effective_dry_run(requested_dry_run)

        # 装机闸门：一次性把所有阻塞项说完
        blockers = self._installation_blockers(dry_run)
        if blockers:
            raise SafetyError(
                "还不能实际运行，有 %d 项未满足" % len(blockers),
                hint="\n".join("· " + b for b in blockers),
                detail={"blockers": blockers},
            )

        caps = self.ensure_handshake()

        # agent 自报暂停时，不要在它那边还暂停着的情况下继续下发
        if caps.get("paused"):
            raise SafetyError(
                "电脑侧处于暂停状态",
                hint="在电脑上恢复，或发 /ship resume",
            )

        planner = Planner(
            client=self.client,
            gate=self.gate,
            store=self.store,
            pack=self.pack,
            dry_run=dry_run,
        )

        self.gate.begin_session()
        started = time.time()
        try:
            record = planner.run_goal(goal_name)
        finally:
            # 无论成败都要关会话，否则下一次会因为"上一会话超时"被误拦
            self.gate.end_session()

        payload = record.payload()
        return {
            "ok": record.ok,
            "goal": goal_name,
            "pack": self.pack.pack_id,
            "dry_run": dry_run,
            "elapsed_ms": int((time.time() - started) * 1000),
            "steps": payload["steps"],
            "message": record.message,
            "run_id": record.run_id,
        }

    # ------------------------------------------------------------------ #
    # 暂停 / 恢复
    # ------------------------------------------------------------------ #
    def pause(self, reason: str = "用户指令") -> dict[str, Any]:
        """双层暂停：本地闸门 + agent。

        必须两边都停。只停本地的话，agent 那边可能还在跑上一个技能；
        只停 agent 的话，本地下一个请求照样会进闸门。
        先本地后远程：万一远程失败，本地已经拦住，不会漏。
        """
        self.gate.pause(reason)
        remote_error = ""
        try:
            self.client.pause(reason)
        except ShipError as exc:
            remote_error = "%s: %s" % (exc.code, exc.message)
        return {
            "ok": True,
            "paused": True,
            "reason": reason,
            "agent_notified": not remote_error,
            "agent_error": remote_error or None,
        }

    def resume(self) -> dict[str, Any]:
        """恢复。顺序和 pause 相反：先恢复远程，再放开本地闸门。

        这样如果远程恢复失败，本地仍处于暂停，用户不会以为"已经恢复"
        而实际没恢复。
        """
        self.client.resume()
        self.gate.resume()
        self.gate.reset_breaker()
        return {"ok": True, "paused": False}

    # ------------------------------------------------------------------ #
    # 诊断
    # ------------------------------------------------------------------ #
    def diagnose(self, include_screenshots: bool = False) -> dict[str, Any]:
        """打包诊断信息。

        返回包路径，用户直接把这个文件发出来就行。
        """
        agent_snapshot: dict[str, Any] | None = None
        try:
            agent_snapshot = self.client.capabilities(refresh=True)
        except ShipError as exc:
            agent_snapshot = {"reachable": False, "error_class": exc.code, "error": exc.message}

        zip_path = build_diagnostics(
            dest_dir=self.data_dir / "diagnostics",
            plugin_version=self.plugin_version,
            config_snapshot=self.config.to_dict(),
            agent_snapshot=agent_snapshot,
            gate_snapshot=self.gate.snapshot(),
            trace_store=self.store,
            include_screenshots=include_screenshots,
            screenshot_dir=self.data_dir / "shots",
        )
        return {
            "ok": True,
            "path": str(zip_path),
            "includes_screenshots": include_screenshots,
            "note": "配置已脱敏（token 等打码）。如果包含截图，请自行确认画面里没有敏感信息再分享",
        }

    def calibrate(self) -> dict[str, Any]:
        """走校准。

        校准本身在 agent 侧完成（它才能截屏、比对、算缩放）。
        插件这一层的职责是：解释为什么必须校准、触发它、拿回结果。
        """
        caps = self.ensure_handshake()
        cal = caps.get("calibration") or {}
        if cal.get("ok") and not cal.get("stale"):
            return {
                "ok": True,
                "already": True,
                "message": "校准已完成：%s" % (cal.get("reason") or "无需重复校准"),
                "calibration": cal,
            }

        result = self.client.calibrate()
        ok = bool(result.get("ok"))
        self.gate.set_calibration(ok, str(result.get("reason") or ""))
        return {
            "ok": ok,
            "already": False,
            "message": str(result.get("message") or result.get("reason") or ("校准完成" if ok else "校准未通过")),
            "calibration": result,
            "next": None if ok else "按提示调整分辨率/语言后重试；也可发 /ship shot 让作者帮你看哪里不对",
        }

    def screenshot(self, reason: str = "manual") -> dict[str, Any]:
        """让 agent 存一张当前截图，返回路径。

        只存不传：图片不过网络，插件只拿路径。原因同 observe——
        图片走 HTTP 在 Pi 上是浪费，而且传到聊天里可能泄露账号信息。
        """
        result = self.client.screenshot(reason=reason)
        return {
            "ok": bool(result.get("ok", True)),
            "path": result.get("path"),
            "note": "截图保存在电脑上，未上传。需要分享请自行确认画面内容",
        }
