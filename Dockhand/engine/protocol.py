"""与设备端 agent 的通信协议（v2）。

为什么协议要带版本号并且做能力协商：

插件在 Pi 的 Docker 里，agent 在用户 Windows 电脑上，两者天然会不同步。
如果没有版本协商，一次 agent 没更新就会表现成"点了没反应""字段读不到"
这种无法归因的怪现象。带上大版本号 + 能力清单，就能一句话说清
"你的 agent 太旧，请更新"，而不是让用户去猜。

协议端点（全部在 agent 侧实现）：

    GET  /v2/health         存活 + 设备数
    GET  /v2/capabilities   协议版本、agent 版本、分辨率、支持技能、暂停状态、校准状态
    GET  /v2/observe        看一眼：返回当前界面状态（结构化，无图片）
    POST /v2/skill          执行一个技能（agent 侧本地闭环，跑到结束才返回）
    POST /v2/pause          暂停（幂等）
    POST /v2/resume         恢复（幂等）

设计要点一：为什么 observe 不返回图片。

插件在 ARM 小主机上，图片一过网络就是几百毫秒加几十 MB 内存。
agent 在 Windows 上，有 numpy/OpenCV，识别放那边是零成本的。
所以插件永远只收 `{state, confidence, elements}` 这种几百字节的结构。

设计要点二：为什么 /skill 是「跑到结束才返回」，而不是「一步一步下发」。

这是整个方案能用的前提。LLM 单次调用 3~5 秒，如果每一步都要过 LLM，
实时操作完全不可用。所以目标级指令下发后，agent 自己跑一个 2~5 Hz 的
本地状态机闭环，插件只在「完成 / 失败 / 超时」时介入。

设计要点三：非幂等请求绝不自动重试。

`/skill` 会真的点击。网络抖动导致超时的时候，你无法区分
"没发出去"和"发出去了但回包丢了"。自动重试的结果可能是点两次。
所以在协议层就把 /skill 定义为不可自动重试，宁可报 E_TIMEOUT 让人确认。
只有 GET 和 pause/resume 这类幂等操作才允许重试一次。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import urljoin

from .errors import EnvError, ProtocolError, ShipError, TimeoutError_, error_from_code

# 协议大版本。改动返回体结构时必须 +1，agent 侧会据此拒绝不匹配的连接。
# 小改动（新增可选字段）不加版本，靠"可选字段缺失也不报错"来兼容。
PROTOCOL_VERSION = 2

# 单次请求的默认超时（秒）。
# observe 很短，skill 可能跑两分钟，所以分成两个默认值，不要在调用处硬编码。
DEFAULT_TIMEOUT = 20
SKILL_TIMEOUT = 180


class AgentClient:
    """agent 的 HTTP 客户端。

    刻意用 urllib 而不是 requests/httpx：
    插件要在别人的机器上加载，少一个第三方依赖就少一类"装不上"的故障。
    urllib 够用，而且不需要处理连接池——我们的请求频率本来就低。

    所有方法都是同步的。异步封装在 runtime 层用 asyncio.to_thread 做，
    这样"协议层"和"并发模型"解耦，单元测试可以直接同步调用。
    """

    def __init__(self, base_url: str, token: str = "", timeout: int = DEFAULT_TIMEOUT):
        self.base = (base_url or "").strip().rstrip("/")
        self.token = (token or "").strip()
        self.timeout = max(3, int(timeout or DEFAULT_TIMEOUT))
        # 协商结果缓存，避免每次调用都多打一次 capabilities
        self._caps: dict[str, Any] | None = None

    # ------------------------------------------------------------------ #
    # 底层请求
    # ------------------------------------------------------------------ #
    def _url(self, path: str) -> str:
        """拼 URL。

        用 urljoin 而不是字符串相加，是为了容忍用户在配置里
        多写或少写结尾斜杠——这是最常见的配置错误之一。
        """
        base = self.base if self.base.endswith("/") else self.base + "/"
        return urljoin(base, path.lstrip("/"))

    def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        timeout: int | None = None,
        idempotent: bool = True,
    ) -> Any:
        """发一个请求并解析 JSON。

        参数 `idempotent` 决定失败时是否重试一次：
          - True（GET / pause / resume）：网络层错误重试 1 次
          - False（/skill）：绝不重试，见模块 docstring 第三条

        重试只针对"连接层面没成功"的错误（URLError），
        对 HTTP 4xx 不重试——那是有明确语义的业务拒绝，重试没用。
        """
        if not self.base:
            raise EnvError(
                "未配置 agent 地址",
                hint="在插件配置里填上电脑的局域网地址，例如 http://192.168.1.10:8765",
            )

        url = self._url(path)
        headers = {"Accept": "application/json"}
        if self.token:
            headers["X-Token"] = self.token

        data = None
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"

        attempts = 2 if idempotent else 1
        last: Exception | None = None

        for attempt in range(1, attempts + 1):
            req = urllib.request.Request(url, data=data, headers=headers, method=method)
            try:
                with urllib.request.urlopen(req, timeout=timeout or self.timeout) as resp:
                    raw = resp.read()
                return self._parse(raw, path)
            except urllib.error.HTTPError as exc:
                # agent 主动返回的业务错误。约定返回体里带 error_class / error。
                raw = b""
                try:
                    raw = exc.read()
                except Exception:  # noqa: BLE001
                    pass
                body = self._try_json(raw)
                code = str(body.get("error_class") or "")
                message = str(body.get("error") or ("agent 返回 HTTP %s" % exc.code))
                hint = str(body.get("hint") or "")
                detail = body.get("detail") if isinstance(body.get("detail"), dict) else None
                if code:
                    raise error_from_code(code, message, hint, detail)
                # 没有 error_class 的 HTTP 错误，按状态码粗分：
                # 5xx 大概率是对面崩了（环境），4xx 大概率是参数/鉴权（配置或环境）
                if exc.code in (401, 403):
                    raise EnvError(
                        "agent 拒绝了请求（HTTP %s）：token 不一致" % exc.code,
                        hint="检查插件配置里的 token 是否和 agent 启动参数 --token 一致",
                    )
                if exc.code >= 500:
                    raise EnvError(
                        "agent 内部错误（HTTP %s）：%s" % (exc.code, message[:200]),
                        hint="查看电脑上 agent 的窗口输出，通常那里有完整栈",
                        detail={"http_status": exc.code},
                    )
                raise ProtocolError(
                    "agent 返回 HTTP %s：%s" % (exc.code, message[:200]),
                    detail={"http_status": exc.code, "path": path},
                )
            except urllib.error.URLError as exc:
                # 连接都没建立。这是幂等请求才值得重试的情况。
                last = exc
                if attempt < attempts:
                    continue
                raise EnvError(
                    "连不上 agent（%s）" % getattr(exc, "reason", exc),
                    hint="1) 确认电脑上 agent 正在运行 2) 确认填的是电脑局域网 IP 不是 127.0.0.1 "
                         "3) 确认两端在同一网络 4) Windows 防火墙放行该端口",
                    detail={"url": url},
                )
            except TimeoutError as exc:  # socket 超时（注意不是自定义的 TimeoutError_）
                last = exc
                raise TimeoutError_(
                    "请求 %s 超时（%s 秒）" % (path, timeout or self.timeout),
                    detail={"path": path},
                )
            except ShipError:
                raise
            except Exception as exc:  # noqa: BLE001
                raise EnvError("请求失败：%s: %s" % (type(exc).__name__, exc), detail={"url": url})

        raise EnvError("请求失败：%s" % last)

    @staticmethod
    def _try_json(raw: bytes) -> dict[str, Any]:
        """尽力解析 JSON，失败返回空 dict。

        错误响应体不一定是合法 JSON（比如反向代理返回的 HTML 错误页），
        这里不能让解析失败再盖掉原本的错误信息。
        """
        if not raw:
            return {}
        try:
            parsed = json.loads(raw.decode("utf-8", "replace"))
            return parsed if isinstance(parsed, dict) else {}
        except Exception:  # noqa: BLE001
            return {}

    def _parse(self, raw: bytes, path: str) -> Any:
        """解析成功响应。

        空响应体返回 None 而不是报错——pause/resume 这类端点
        可能只回一个 204，不该因此判定失败。
        """
        if not raw:
            return None
        try:
            return json.loads(raw.decode("utf-8", "replace"))
        except Exception as exc:  # noqa: BLE001
            raise ProtocolError(
                "agent 返回的不是合法 JSON（%s）：%s" % (path, str(exc)[:120]),
                hint="多半是版本不匹配，或该端口被别的服务占用了",
                detail={"snippet": raw[:200].decode("utf-8", "replace")},
            )

    # ------------------------------------------------------------------ #
    # 端点
    # ------------------------------------------------------------------ #
    def health(self) -> dict[str, Any]:
        """存活检查。返回 agent 自报的设备信息。"""
        return self._request("GET", "/v2/health") or {}

    def capabilities(self, refresh: bool = False) -> dict[str, Any]:
        """取能力清单，并做协议大版本协商。

        协商放在这里而不是 on_load，是因为 on_load 时 agent 可能还没起，
        插件不应该因为"对方暂时不在"就加载失败——那样用户必须先开 agent
        再重启麦麦，顺序错了就报错，体验很差。

        所以策略是：加载永远成功，第一次真正用的时候才协商。
        """
        if self._caps is not None and not refresh:
            return self._caps

        caps = self._request("GET", "/v2/capabilities") or {}
        remote = caps.get("protocol")
        if remote is None:
            raise ProtocolError(
                "agent 没有返回协议版本，可能是不支持 v2 的旧版 agent",
                hint="请按 README 把电脑上的 agent 更新到与插件同版本",
            )
        if int(remote) != PROTOCOL_VERSION:
            raise ProtocolError(
                "协议版本不匹配：插件要 v%s，agent 是 v%s" % (PROTOCOL_VERSION, remote),
                hint="插件和 agent 必须同一大版本，请把两边都更新到最新（README 有步骤）",
                detail={"plugin_protocol": PROTOCOL_VERSION, "agent_protocol": remote},
            )
        self._caps = caps
        return caps

    def observe(self) -> dict[str, Any]:
        """看一眼前当前界面，拿结构化状态。

        返回形如：
            {"state": "main_map", "confidence": 0.93,
             "resolution": [1920, 1080], "ui_hash": "a1b2c3",
             "elements": {"fleet_btn": {"x": 120, "y": 900, "score": 0.93}}}

        注意 state 可能是 "unknown"。调用方必须把 unknown 当终止条件，
        不能当"大概是主界面"继续往下走。
        """
        data = self._request("GET", "/v2/observe") or {}
        if "state" not in data:
            raise ProtocolError("observe 返回体缺少 state 字段", detail={"keys": list(data)[:10]})
        return data

    def run_skill(
        self,
        skill: str,
        args: dict[str, Any] | None = None,
        dry_run: bool = True,
        deadline_ms: int = 120_000,
    ) -> dict[str, Any]:
        """执行一个技能，等它跑完。

        dry_run=True 时 agent 只做识别和计划、不产生输入事件，
        返回体里的 would_do 会列出原本要做的动作。

        这个调用**不重试**，原因见模块 docstring。
        """
        payload = {
            "skill": str(skill),
            "args": dict(args or {}),
            "dry_run": bool(dry_run),
            "deadline_ms": int(max(1000, deadline_ms)),
        }
        return self._request("POST", "/v2/skill", payload, timeout=SKILL_TIMEOUT, idempotent=False) or {}

    def pause(self, reason: str = "remote") -> dict[str, Any]:
        """暂停。agent 收到后必须释放所有按住的键并把 paused 置真。"""
        return self._request("POST", "/v2/pause", {"reason": str(reason)}) or {}

    def resume(self) -> dict[str, Any]:
        """恢复。"""
        return self._request("POST", "/v2/resume", {}) or {}

    def calibration(self) -> dict[str, Any]:
        """取校准状态。

        装机闸门要用它判断"这台机器能不能实跑"。
        """
        data = self._request("GET", "/v2/capabilities") or {}
        cal = data.get("calibration")
        return cal if isinstance(cal, dict) else {"ok": False, "reason": "agent 未返回校准信息"}

    def calibrate(self) -> dict[str, Any]:
        """触发校准。

        校准的主体在 agent 侧（它才能截屏、算缩放、存基准图），
        插件只负责触发和解释。这也意味着校准向导的 UI 在 Windows 上，
        不在聊天里——聊天里没法让用户"在游戏里点一下这个按钮"。

        幂等（重复触发只是重新校准），所以允许重试。超时给得比较长，
        因为向导可能要等用户操作。
        """
        return self._request("POST", "/v2/calibrate", {}, timeout=300) or {}

    def screenshot(self, reason: str = "manual") -> dict[str, Any]:
        """让 agent 存一张截图到它自己的磁盘，只回路径。

        `reason` 只是给人看的标记，会写进 agent 的文件名或日志，
        方便用户事后按"我给作者看的那张"找到。
        """
        return self._request("POST", "/v2/screenshot", {"reason": str(reason)[:64]}) or {}
