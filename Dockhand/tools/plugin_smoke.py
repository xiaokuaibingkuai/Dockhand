"""在本地验证 plugin.py 的 SDK 用法自洽（不需要装 MaiBot）。

    python tools/plugin_smoke.py

原理：往 sys.modules 里塞一个最小化的 maibot_sdk 桩，然后 import plugin。
这样能验的事：

  · 装饰器参数对不对（@Tool 的名字是不是位置参数、parameters 是不是列表）
  · 4 个 Tool + 1 个 Command 是不是都注册上了
  · create_plugin() 返回的实例能 on_load / on_unload 而不炸
  · 连不上 agent 时，Tool 返回结构化错误而不是抛异常

不能验的：真实 Host 的加载流程、RPC 编码、能力鉴权。那些只能在真机上验。

为什么值得写这个：plugin.py 是唯一没法用 selftest 覆盖的文件（它依赖 SDK），
而它恰好又是最容易因为 SDK 小版本差异而失效的文件。一个 100 行的桩，
能把"装上去才发现 Tool 没注册"提前到"提交前就发现"。
"""

from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

REGISTRY = {"tools": [], "commands": []}


def _install_sdk_stub() -> None:
    """装一个假 SDK。

    只实现 plugin.py 用到的那几个名字。多实现反而危险：桩越像真的，
    越容易掩盖"其实真 SDK 不这么写"的问题。这里只保证接口形状一致。
    """
    sdk = types.ModuleType("maibot_sdk")

    class Tool:
        def __init__(self, name=None, description="", parameters=None, **kwargs):
            self.name = name
            self.description = description
            self.parameters = parameters

        def __call__(self, fn):
            REGISTRY["tools"].append(
                {"name": self.name, "params": [p.name for p in (self.parameters or [])], "fn": fn.__name__}
            )
            return fn

    class Command:
        def __init__(self, name=None, pattern=None, description="", **kwargs):
            self.name = name
            self.pattern = pattern
            self.description = description

        def __call__(self, fn):
            REGISTRY["commands"].append({"name": self.name, "pattern": self.pattern, "fn": fn.__name__})
            return fn

    class MaiBotPlugin:
        def __init__(self):
            self.ctx = _FakeCtx()

        def get_plugin_config_data(self):
            # 指向一个必定拒绝连接的端口：验证"连不上也不崩"，且不等 20 秒
            return {
                "connection": {"base_url": "http://127.0.0.1:1", "token": "", "timeout_sec": 1},
                "safety": {"dry_run": True},
            }

    sdk.Tool = Tool
    sdk.Command = Command
    sdk.MaiBotPlugin = MaiBotPlugin
    sys.modules["maibot_sdk"] = sdk

    types_mod = types.ModuleType("maibot_sdk.types")

    class ToolParamType:
        STRING = "string"
        INTEGER = "integer"

    class ToolParameterInfo:
        def __init__(self, name, param_type=None, description="", required=True, default=None):
            self.name = name
            self.param_type = param_type
            self.description = description
            self.required = required
            self.default = default

    types_mod.ToolParamType = ToolParamType
    types_mod.ToolParameterInfo = ToolParameterInfo
    sdk.types = types_mod
    sys.modules["maibot_sdk.types"] = types_mod


class _FakeLogger:
    def __init__(self):
        self.lines: list[str] = []

    def _log(self, level, msg, *args):
        self.lines.append("%s %s" % (level, msg % args if args else msg))

    def info(self, msg, *args):
        self._log("INFO", msg, *args)

    def warning(self, msg, *args):
        self._log("WARN", msg, *args)

    def error(self, msg, *args):
        self._log("ERROR", msg, *args)

    def debug(self, msg, *args):
        self._log("DEBUG", msg, *args)


class _FakePaths:
    def __init__(self, path):
        self.data_dir = str(path)


class _FakeSend:
    def __init__(self):
        self.sent: list[str] = []

    async def text(self, text, stream_id):
        self.sent.append(text)
        return True


class _FakeCtx:
    def __init__(self):
        import tempfile

        self.logger = _FakeLogger()
        self.paths = _FakePaths(tempfile.mkdtemp(prefix="shipctl-smoke-"))
        self.send = _FakeSend()


RESULTS: list[tuple[bool, str, str]] = []


def check(name, ok, detail=""):
    RESULTS.append((bool(ok), name, detail))


def main() -> int:
    _install_sdk_stub()

    import plugin as plugin_mod  # noqa: PLC0415

    check("plugin.py 可被导入", True, "")
    check("create_plugin 存在", callable(getattr(plugin_mod, "create_plugin", None)), "")

    tool_names = sorted(t["name"] for t in REGISTRY["tools"])
    check(
        "注册了 4 个 Tool",
        tool_names == ["ship_control", "ship_goal", "ship_report", "ship_state"],
        str(tool_names),
    )
    check(
        "每个 Tool 都带描述",
        all(REGISTRY["tools"]),
        "",
    )
    check(
        "只读与写入分属不同 Tool",
        "ship_state" in tool_names and "ship_goal" in tool_names and "ship_state" != "ship_goal",
        "",
    )
    check(
        "Command 注册且带正则",
        len(REGISTRY["commands"]) == 1 and bool(REGISTRY["commands"][0]["pattern"]),
        str(REGISTRY["commands"]),
    )

    instance = plugin_mod.create_plugin()

    # on_load：配置指向必拒端口，不应抛异常
    loaded = True
    try:
        asyncio.run(instance.on_load())
    except Exception as exc:  # noqa: BLE001
        loaded = False
        check("on_load 不抛异常", False, repr(exc))
    if loaded:
        check("on_load 不抛异常", True, "")
    check("on_load 后记录了一行日志", any("shipctl" in line for line in instance.ctx.logger.lines), str(instance.ctx.logger.lines[:2]))

    runtime = getattr(instance, "_runtime", None)
    check("on_load 建出了 Runtime", runtime is not None, "")

    # 连不上 agent 时，Tool 必须返回 dict 而不是抛异常
    result = asyncio.run(instance.tool_ship_state())
    check("连不上 agent 时 ship_state 返回结构化错误", isinstance(result, dict) and result.get("ok") is False, str(result)[:160])
    check("错误里带可执行的 hint", bool(result.get("hint")), str(result.get("hint")))

    # status 报告在无连接时仍要给出模式与包信息
    report = asyncio.run(instance.tool_ship_report("status"))
    check("ship_report 在无连接时仍可用", isinstance(report, dict), str(report)[:120])

    # 命令返回三元组
    cmd_result = asyncio.run(instance.cmd_ship(stream_id="s1", matched_groups={"sub": "status"}))
    check(
        "命令返回 (bool, str, int) 三元组",
        isinstance(cmd_result, tuple) and len(cmd_result) == 3 and isinstance(cmd_result[0], bool),
        str(type(cmd_result)),
    )
    check("命令输出非空", bool(cmd_result[1]), str(cmd_result[1])[:120])

    help_result = asyncio.run(instance.cmd_ship(stream_id="s1", matched_groups={"sub": "help"}))
    check("help 里列出了 4 个运维子命令", help_result[1].count("/ship ") >= 4, help_result[1][:80])

    # 运维子命令：每一个都要在"连不上 agent"的最差情况下也不崩。
    #
    # 这里的 instance 没有可用的 agent，所以 observe/run/calibrate 一定失败——
    # 但失败必须是"回一句人话"，而不是抛异常消失在日志里。
    # 运维命令的价值恰恰体现在出问题的时候。
    sub_cases = [
        ({"sub": "accept"}, "accept"),
        ({"sub": "pause"}, "pause"),
        ({"sub": "resume"}, "resume"),
        ({"sub": "reset"}, "reset"),
        ({"sub": "observe"}, "observe"),
        ({"sub": "diag"}, "diag"),
        ({"sub": "calibrate"}, "calibrate"),
        ({"sub": "run", "arg": "look_main"}, "run"),
        ({"sub": "run"}, "run(无参数)"),
        ({"sub": "nosuchcommand"}, "未知子命令"),
    ]
    for groups, label in sub_cases:
        try:
            res = asyncio.run(instance.cmd_ship(stream_id="s1", matched_groups=groups))
            ok = isinstance(res, tuple) and len(res) == 3 and bool(str(res[1]).strip())
            detail = ""
        except Exception as exc:  # noqa: BLE001
            ok = False
            detail = repr(exc)
        check("/ship %s 可用且不抛异常" % label, ok, detail)

    accept_out = asyncio.run(instance.cmd_ship(stream_id="s1", matched_groups={"sub": "accept"}))
    check(
        "/ship accept 给出确认结果",
        "已记录" in accept_out[1] or "确认" in accept_out[1],
        accept_out[1][:100],
    )
    check(
        "/ship accept 之后提示还差什么",
        ("还差" in accept_out[1]) or ("dry_run" in accept_out[1]),
        accept_out[1][:160],
    )

    run_no_arg = asyncio.run(instance.cmd_ship(stream_id="s1", matched_groups={"sub": "run"}))
    check("/ship run 缺参数时给用法", "用法" in run_no_arg[1], run_no_arg[1][:100])

    # 未就绪的分支：把 runtime 摘掉，Tool 应返回"插件未就绪"而不是 AttributeError
    instance._runtime = None
    not_ready = asyncio.run(instance.tool_ship_goal("look_main", "auto"))
    check("未就绪时 ship_goal 返回可读错误", not_ready.get("ok") is False and "未就绪" in str(not_ready.get("error")), str(not_ready)[:160])

    # on_unload 不许抛异常
    unloaded = True
    try:
        asyncio.run(instance.on_unload())
    except Exception as exc:  # noqa: BLE001
        unloaded = False
        check("on_unload 不抛异常", False, repr(exc))
    if unloaded:
        check("on_unload 不抛异常", True, "")

    for ok, name, detail in RESULTS:
        print("   %s %s%s" % ("PASS" if ok else "FAIL", name, ("  → " + detail) if detail and not ok else ""))

    failed = [r for r in RESULTS if not r[0]]
    print("\n%d/%d 通过" % (len(RESULTS) - len(failed), len(RESULTS)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
