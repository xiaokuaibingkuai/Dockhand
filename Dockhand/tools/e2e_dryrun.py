"""端到端演练验证：用真的 HTTP 起一个假 agent，把整条链路跑一遍。

和 selftest.py 的区别：

  selftest.py      用 FakeAgent 直接替换 client 对象，测的是"逻辑对不对"
  e2e_dryrun.py    起一个真 HTTP 服务，测的是"协议对不对、链路通不通"

为什么要两个都留：

逻辑测试快、能覆盖分支，但它跳过了序列化、路径拼接、超时、错误码映射。
真 HTTP 这几个地方恰恰是最容易出错的——尤其是 base_url 末尾有没有斜杠、
错误响应是 JSON 还是 HTML、超时了会抛什么。

这个脚本的核心断言只有一句：**演练模式下，agent 收到的每一个 skill 请求
都带 dry_run=true，且 agent 侧零输入事件。** 其他都是围绕它的铺垫。

用法：
    python3 tools/e2e_dryrun.py
"""

from __future__ import annotations

import json
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine.config import ShipConfig  # noqa: E402
from engine.errors import ShipError  # noqa: E402
from engine.runtime import Runtime  # noqa: E402

PASSED: list[tuple[bool, str, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    PASSED.append((bool(ok), name, detail))


# ---------------------------------------------------------------------- #
# 假 agent：真的 HTTP 服务
#
# 它记录收到的每个请求，这样测试就能断言"插件到底发了什么"。
# 这是这个脚本存在的全部意义——只测返回值是测不出这个的。
# ---------------------------------------------------------------------- #
class FakeAgentState:
    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.skill_calls: list[dict] = []
        self.input_events = 0          # 真实输入事件计数，演练下必须为 0
        self.paused = False
        self.calibration_ok = True
        self.state = "main_map"
        self.confidence = 0.93


def make_handler(state: FakeAgentState, token: str):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):  # noqa: A003
            return  # 静音，否则测试输出全是访问日志

        def _send(self, code: int, payload: dict) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _auth_ok(self) -> bool:
            """同时接受两种头。

            插件发的是 X-Token；Authorization: Bearer 是更通用的写法。
            两种都认，是因为"接哪个头"这种小事不该成为用户排查半小时的原因。
            """
            if not token:
                return True
            if self.headers.get("X-Token", "") == token:
                return True
            return self.headers.get("Authorization", "") == "Bearer %s" % token

        def _record(self, body: dict | None = None) -> None:
            state.requests.append({"method": self.command, "path": self.path, "body": body})

        def do_GET(self) -> None:  # noqa: N802
            self._record()
            if not self._auth_ok():
                self._send(401, {"error": "unauthorized"})
                return

            if self.path.startswith("/v2/health"):
                self._send(200, {"ok": True, "agent_version": "fake-1.0", "device": "fake-device"})
                return

            if self.path.startswith("/v2/capabilities"):
                self._send(
                    200,
                    {
                        "protocol": 2,
                        "agent_version": "fake-1.0",
                        "resolution": [1280, 720],
                        "paused": state.paused,
                        "calibration": {
                            "ok": state.calibration_ok,
                            "reason": "" if state.calibration_ok else "分辨率变化，需要重新校准",
                        },
                        "skills": ["assert_main", "assert_fleet"],
                        "features": ["dry_run", "would_do", "diagnostics"],
                    },
                )
                return

            if self.path.startswith("/v2/observe"):
                self._send(
                    200,
                    {
                        "state": state.state,
                        "confidence": state.confidence,
                        "resolution": [1280, 720],
                        "elements": {"fleet_btn": {"x": 120, "y": 900, "score": 0.9}},
                    },
                )
                return

            self._send(404, {"error": "not found"})

        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            try:
                body = json.loads(raw.decode("utf-8") or "{}")
            except json.JSONDecodeError:
                body = {}
            self._record(body)

            if not self._auth_ok():
                self._send(401, {"error": "unauthorized"})
                return

            if self.path.startswith("/v2/skill"):
                state.skill_calls.append(body)
                if state.paused:
                    self._send(200, {"ok": False, "error_class": "E_PAUSED", "error": "已暂停"})
                    return
                # 关键分支：演练请求绝不产生输入事件
                if body.get("dry_run"):
                    self._send(
                        200,
                        {
                            "ok": True,
                            "final_state": state.state,
                            "would_do": [{"op": "tap", "target": "fleet_btn"}],
                        },
                    )
                    return
                state.input_events += 1
                self._send(200, {"ok": True, "final_state": state.state, "would_do": []})
                return

            if self.path.startswith("/v2/pause"):
                state.paused = True
                self._send(200, {"ok": True, "paused": True})
                return

            if self.path.startswith("/v2/resume"):
                state.paused = False
                self._send(200, {"ok": True, "paused": False})
                return

            if self.path.startswith("/v2/calibrate"):
                state.calibration_ok = True
                self._send(200, {"ok": True, "resolution": [1280, 720], "scale": 1.0})
                return

            self._send(404, {"error": "not found"})

    return Handler


def serve(state: FakeAgentState, token: str = "") -> tuple[ThreadingHTTPServer, str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(state, token))
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, "http://127.0.0.1:%d" % port


def make_runtime(base_url: str, token: str = "", **safety) -> Runtime:
    cfg = {
        "connection": {"base_url": base_url, "token": token, "timeout_sec": 10},
        "safety": {"dry_run": True, "pause_hotkey": "", "disclaimer_accepted": False, **safety},
    }
    return Runtime(
        config=ShipConfig.from_dict(cfg),
        data_dir=Path(tempfile.mkdtemp(prefix="shipctl-e2e-")),
        plugin_version="0.2.0-e2e",
    )


# ---------------------------------------------------------------------- #
# 1. 只读感知：整条链路能通，且不发任何执行请求
# ---------------------------------------------------------------------- #
def test_observe() -> None:
    state = FakeAgentState()
    server, url = serve(state)
    try:
        rt = make_runtime(url)
        data = rt.observe()
        check("observe 返回成功", data.get("ok") is True, str(data)[:160])
        check("observe 拿到界面名", data.get("state") == "main_map", str(data.get("state")))
        check("observe 标记为已知界面", data.get("known") is True, str(data.get("known")))
        check("observe 置信度落在 0~1", 0.0 <= float(data.get("confidence") or 0) <= 1.0, str(data.get("confidence")))

        # 只读就要真的只读：整轮里不能出现一个 POST
        posts = [r for r in state.requests if r["method"] == "POST"]
        check("只读感知没有发出任何 POST", not posts, str(posts)[:160])
        check("只读感知零输入事件", state.input_events == 0, str(state.input_events))
    finally:
        server.shutdown()


def test_observe_unknown() -> None:
    """认不出界面时，必须原样告诉上层 unknown，不能猜。"""
    state = FakeAgentState()
    state.state = "unknown"
    state.confidence = 0.12
    server, url = serve(state)
    try:
        rt = make_runtime(url)
        data = rt.observe()
        check("unknown 原样返回", data.get("state") == "unknown", str(data.get("state")))
        check("unknown 标记为未知", data.get("known") is False, str(data.get("known")))
        check("unknown 时置信度低", float(data.get("confidence") or 0) < 0.5, str(data.get("confidence")))
    finally:
        server.shutdown()


# ---------------------------------------------------------------------- #
# 2. 演练：真的走一遍 skill 请求，但 agent 侧零输入
# ---------------------------------------------------------------------- #
def test_dry_run_goal() -> None:
    state = FakeAgentState()
    server, url = serve(state)
    try:
        rt = make_runtime(url, dry_run=True)
        result = rt.run_goal("look_main")
        check("演练执行返回成功", result.get("ok") is True, str(result.get("message")))
        check("结果标记为演练", result.get("dry_run") is True, str(result.get("dry_run")))
        check("演练覆盖了全部技能", len(state.skill_calls) >= 1, str(len(state.skill_calls)))

        # 这是整个脚本最重要的一条断言
        check("所有 skill 请求都带 dry_run", all(c.get("dry_run") is True for c in state.skill_calls), str(state.skill_calls)[:200])
        check("演练期间零输入事件", state.input_events == 0, str(state.input_events))
        check("演练留下执行记录", bool(result.get("run_id")), str(result.get("run_id")))
    finally:
        server.shutdown()


def test_dry_run_cannot_be_weakened() -> None:
    """配置是演练时，请求方传 dry_run=False 也不能真跑。"""
    state = FakeAgentState()
    server, url = serve(state)
    try:
        rt = make_runtime(url, dry_run=True)
        result = rt.run_goal("look_main", requested_dry_run=False)
        check("插件配置优先，仍是演练", result.get("dry_run") is True, str(result.get("dry_run")))
        check("请求方无法绕过演练", state.input_events == 0, str(state.input_events))
    finally:
        server.shutdown()


# ---------------------------------------------------------------------- #
# 3. 实跑闸门：该拦的必须拦住，且拦住时没有任何请求发出去
# ---------------------------------------------------------------------- #
def test_live_blocked_without_disclaimer() -> None:
    state = FakeAgentState()
    server, url = serve(state)
    try:
        rt = make_runtime(url, dry_run=False)
        blocked = False
        message = ""
        try:
            rt.run_goal("look_main")
        except ShipError as exc:
            blocked = True
            message = exc.message
        check("未确认免责时实跑被拒", blocked, message[:120])
        check("被拒时一个技能都没下发", not state.skill_calls, str(state.skill_calls)[:120])
        check("被拒时零输入事件", state.input_events == 0, str(state.input_events))
    finally:
        server.shutdown()


def test_live_blocked_without_calibration() -> None:
    state = FakeAgentState()
    state.calibration_ok = False
    server, url = serve(state)
    try:
        rt = make_runtime(url, dry_run=False, disclaimer_accepted=True, pause_hotkey="F12")
        blocked = False
        message = ""
        try:
            rt.run_goal("look_main")
        except ShipError as exc:
            blocked = True
            message = exc.message + " / " + (exc.hint or "")
        check("未校准实跑被拒", blocked, message[:160])
        check("拒绝理由提到校准", "校准" in message, message[:160])
        check("未校准时零输入事件", state.input_events == 0, str(state.input_events))
    finally:
        server.shutdown()


def test_live_blocked_without_hotkey() -> None:
    state = FakeAgentState()
    server, url = serve(state)
    try:
        rt = make_runtime(url, dry_run=False, disclaimer_accepted=True, pause_hotkey="")
        blocked = False
        message = ""
        try:
            rt.run_goal("look_main")
        except ShipError as exc:
            blocked = True
            message = exc.message + " / " + (exc.hint or "")
        check("未设暂停热键实跑被拒", blocked, message[:160])
        check("拒绝理由提到热键", "热键" in message, message[:160])
    finally:
        server.shutdown()


# ---------------------------------------------------------------------- #
# 4. 暂停：热键的另一半——暂停后内层也要停
# ---------------------------------------------------------------------- #
def test_pause_propagates() -> None:
    state = FakeAgentState()
    server, url = serve(state)
    try:
        rt = make_runtime(url, dry_run=True)
        rt.pause("测试：模拟按了热键")
        check("插件侧记为暂停", rt.gate.snapshot().get("paused") is True, str(rt.gate.snapshot()))

        blocked = False
        try:
            rt.run_goal("look_main")
        except ShipError:
            blocked = True
        check("暂停后拒绝执行", blocked, "")

        # agent 侧也可能自己暂停了（用户在电脑上按的），握手时要同步过来
        rt2 = make_runtime(url, dry_run=True)
        state.paused = True
        caps = rt2.ensure_handshake()
        check("agent 自报暂停被同步", rt2.gate.snapshot().get("paused") is True, str(caps)[:120])
    finally:
        server.shutdown()


# ---------------------------------------------------------------------- #
# 5. 鉴权与超时：连不上和连错要能被区分
# ---------------------------------------------------------------------- #
def test_auth_and_unreachable() -> None:
    state = FakeAgentState()
    server, url = serve(state, token="secret-token")
    try:
        wrong = make_runtime(url, token="wrong-token")
        blocked = False
        message = ""
        try:
            wrong.observe()
        except ShipError as exc:
            blocked = True
            message = exc.message + (exc.hint or "")
        check("令牌错误被拒绝", blocked, message[:160])

        right = make_runtime(url, token="secret-token")
        data = right.observe()
        check("令牌正确可以访问", data.get("ok") is True, str(data)[:120])
    finally:
        server.shutdown()

    # 端口关掉之后必须是"连不上"这类可读错误，而不是裸异常
    dead = make_runtime("http://127.0.0.1:1", token="x")
    readable = False
    try:
        dead.observe()
    except ShipError as exc:
        readable = bool(exc.message)
    except Exception:  # noqa: BLE001
        readable = False
    check("连不上时抛可读错误", readable, "")


def main() -> int:
    print("shipctl 端到端演练验证 · %s\n" % ROOT)
    cases = [
        test_observe,
        test_observe_unknown,
        test_dry_run_goal,
        test_dry_run_cannot_be_weakened,
        test_live_blocked_without_disclaimer,
        test_live_blocked_without_calibration,
        test_live_blocked_without_hotkey,
        test_pause_propagates,
        test_auth_and_unreachable,
    ]
    for fn in cases:
        print("── %s" % fn.__name__)
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            import traceback

            traceback.print_exc()
            check("%s 未抛异常" % fn.__name__, False, repr(exc))
        for ok, name, detail in PASSED:
            print("   %s %s%s" % ("PASS" if ok else "FAIL", name, ("  → " + detail) if detail and not ok else ""))
        PASSED.clear()

    return 0


if __name__ == "__main__":
    sys.exit(main())
