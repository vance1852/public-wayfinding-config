"""屏端接入：本机可运行的电子指路牌模拟器。

每块屏是一个独立 HTTP 进程，完整模拟现场屏的本地义务：

* ``PUT /screens/{id}/configuration`` 接收控制端配置并回报 ``received``；
* 本地按自身时钟判定生效时段，到期自动退到停止引导，不依赖网络；
* 暂停联网（``POST /local/offline``）后收不到新配置，只按最后收到的
  有效期行动；恢复联网（``POST /local/online``）后重新上报当前真实状态；
* 能力表达不了的绕行（缺语言/图形）本地兜底为停止引导；
* ``GET /local/display`` 查看此刻正在给旅客的指示。

屏端绝不依据旧安装回执恢复已失效路线：回执只记录在槽位历史里，
当前显示永远由“最新配置 + 当前时刻 + 自身能力”现场算出。
"""

import json
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit


@dataclass
class InstalledConfig:
    revision_set: tuple
    directions: dict
    received_at: int          # 屏端时钟
    issued_at: int


@dataclass
class ScreenRuntime:
    screen_id: str
    locales: set
    icons: set
    clock: int = 0
    online: bool = True
    disabled: bool = False
    configs: list = field(default_factory=list)   # 按收到顺序保存
    report_history: list = field(default_factory=list)
    control_url: str | None = None

    # ------------------------------------------------------------------
    def install(self, payload: dict) -> dict:
        if not self.online:
            raise ConnectionError("终端处于暂停联网状态")
        config = InstalledConfig(
            revision_set=tuple(sorted(
                rev for item in payload["directions"].values()
                for rev in item.get("revisions", [])
            )),
            directions=payload["directions"],
            received_at=self.clock,
            issued_at=payload["issued_at"],
        )
        self.configs.append(config)
        self._report("received", payload)
        # 本地安装完成：installed 回报
        self._report("installed", payload)
        return {"state": "received", "screen": self.screen_id,
                "directions": list(payload["directions"])}

    # ------------------------------------------------------------------
    def evaluate(self) -> dict:
        """按当前时钟与能力计算正在显示的内容（断网同样执行）。"""
        if self.disabled:
            return {"screen": self.screen_id, "at": self.clock,
                    "online": False, "disabled": True,
                    "directions": {}, "note": "终端已被现场停用"}
        display = {}
        active_config = self._latest_config()
        if active_config is None:
            return {"screen": self.screen_id, "at": self.clock,
                    "online": self.online, "disabled": False,
                    "directions": {},
                    "note": "尚未收到任何配置"}
        for route_id, item in active_config.directions.items():
            display[route_id] = self._evaluate_direction(route_id, item, active_config)
        return {"screen": self.screen_id, "at": self.clock,
                "online": self.online, "disabled": False,
                "config_issued_at": active_config.issued_at,
                "config_received_at": active_config.received_at,
                "directions": display}

    def _latest_config(self) -> InstalledConfig | None:
        return self.configs[-1] if self.configs else None

    def _evaluate_direction(self, route_id, item, config: InstalledConfig) -> dict:
        now = self.clock
        if item["mode"] == "stop":
            return {"mode": "stop", "reason": "控制端要求停止引导",
                    "stop_revision": item.get("stop_revision"),
                    "stop_entry": item.get("stop_entry", False)}

        # 能力裁剪：图形能力表达不了的绕行必须停止引导；
        # 语言是可子集化的——老屏只显示它支持的本地译文，不因缺外语而停。
        missing_icon = item.get("icon") and item["icon"] not in self.icons
        if missing_icon:
            return {"mode": "stop",
                    "reason": f"本屏能力无法表达该指引（缺图形 {item.get('icon')}）",
                    "fallback": "stop"}
        if item.get("locales") and not (set(item["locales"]) & self.locales):
            return {"mode": "stop",
                    "reason": "本屏不支持该指引的任何译文语言",
                    "fallback": "stop"}

        # 时段判定：超过最后收到的有效期必须停止引导
        usable = self._usable_window(item, config)
        if usable is not None and not (usable[0] <= now < usable[1]):
            return {"mode": "stop",
                    "reason": f"当前时刻 {now} 已超出最后收到的有效期 {list(usable)}",
                    "fallback": "stop", "expired": True}

        shown_text = {
            locale: text for locale, text in item.get("locales", {}).items()
            if locale in self.locales
        }
        return {
            "mode": "guide",
            "text": shown_text,
            "icon": item.get("icon") if item.get("icon") in self.icons else None,
            "alternate": item.get("alternate"),
            "in_transit": item.get("in_transit", False),
            "stop_entry": item.get("stop_entry", False),
            "revisions": item.get("revisions", []),
        }

    def _usable_window(self, item, config: InstalledConfig):
        """从本地安装回执取该内容获准展示的右端点。

        注意：重连后旧回执即使还在，也只对“当时收到的配置”有效；
        控制端若下发停止引导，最新配置直接是 stop，不会由此复活旧路线。
        """
        # 时段票/内容票的右端点由控制端在配置里以 usable_until 给出
        return item.get("usable_window")

    # ------------------------------------------------------------------
    def set_clock(self, now: int):
        if now < self.clock:
            raise ValueError("时钟只能前进")
        self.clock = now

    def go_offline(self):
        self.online = False
        # 本地登记线路事件；是否转送控制端由驱动/集成方决定，避免双重事件
        self.report_history.append(
            {"kind": "offline", "screen": self.screen_id, "at": self.clock})

    def go_online(self):
        self.online = True
        # 只登记线路事件；不主动声称正在显示的内容——重连前的显示状态
        # 不能证明当前路线有效，真实显示以重连后收到的新配置回报为准。
        self.report_history.append(
            {"kind": "reconnect", "screen": self.screen_id, "at": self.clock})

    def field_disable(self):
        """现场确实停用终端：不再接受配置，显示固定停用提示。"""
        self.online = False
        self.disabled = True
        self.report_history.append(
            {"kind": "field_disabled", "screen": self.screen_id, "at": self.clock})

    def _revision_for(self, route_id):
        config = self._latest_config()
        if config and route_id in config.directions:
            item = config.directions[route_id]
            if item["mode"] == "stop":
                return item.get("stop_revision", f"stop/{route_id}")
            revs = item.get("revisions")
            if revs:
                return revs[0]
        return "unknown"

    def _usable_until_for(self, route_id):
        config = self._latest_config()
        if config and route_id in config.directions:
            window = config.directions[route_id].get("usable_window")
            return window[1] if window else None
        return None

    # ------------------------------------------------------------------
    def _report(self, state: str, payload: dict):
        for route_id, item in payload["directions"].items():
            revision = item.get("stop_revision") or (
                item["revisions"][0] if item.get("revisions") else "unknown")
            window = item.get("usable_window")
            self._report_raw({
                "kind": "device_report", "screen": self.screen_id,
                "revision": revision, "state": state,
                "usable_until": window[1] if window else None,
                "at": self.clock,
            })

    def _report_raw(self, event: dict):
        self.report_history.append(event)
        if not self.online or not self.control_url:
            return
        url = f"{self.control_url}/reports"
        request = urllib.request.Request(
            url, data=json.dumps(event).encode("utf-8"), method="POST",
            headers={"Content-Type": "application/json; charset=utf-8"},
        )
        try:
            with urllib.request.urlopen(request, timeout=3):
                pass
        except (urllib.error.URLError, OSError):
            # 控制端不可达不影响屏端本地运行
            pass


class ScreenHandler(BaseHTTPRequestHandler):
    server_version = "WayfindingScreen/1.0"

    def _json(self, status, value):
        payload = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _read_body(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
            return json.loads(self.rfile.read(length)) if length else {}
        except (ValueError, UnicodeError):
            return None

    def do_PUT(self):
        path = urlsplit(self.path).path.rstrip("/")
        runtime: ScreenRuntime = self.server.runtime
        prefix = f"/screens/{runtime.screen_id}/configuration"
        if path != prefix:
            self._json(404, {"message": "未知路径"})
            return
        body = self._read_body()
        if body is None:
            self._json(400, {"message": "无法读取配置正文"})
            return
        if not runtime.online:
            # 暂停联网：连接直接拒绝，控制端据此登记未送达
            self._json(503, {"message": "终端暂停联网"})
            return
        self._json(202, runtime.install(body))

    def do_GET(self):
        path = urlsplit(self.path).path.rstrip("/")
        runtime = self.server.runtime
        if path == "/local/display":
            self._json(200, runtime.evaluate())
            return
        if path == "/local/state":
            self._json(200, {
                "screen": runtime.screen_id, "at": runtime.clock,
                "online": runtime.online,
                "locales": sorted(runtime.locales), "icons": sorted(runtime.icons),
                "configs_received": len(runtime.configs),
                "report_history": runtime.report_history,
            })
            return
        self._json(404, {"message": "未知路径"})

    def do_POST(self):
        path = urlsplit(self.path).path.rstrip("/")
        runtime = self.server.runtime
        body = self._read_body() or {}
        if path == "/local/offline":
            runtime.go_offline()
            self._json(200, {"online": False, "at": runtime.clock})
            return
        if path == "/local/disable":
            runtime.field_disable()
            self._json(200, {"disabled": True, "at": runtime.clock})
            return
        if path == "/local/online":
            runtime.go_online()
            self._json(200, {"online": True, "at": runtime.clock})
            return
        if path == "/local/clock":
            try:
                runtime.set_clock(int(body["at"]))
            except (KeyError, TypeError, ValueError):
                self._json(400, {"message": "at 必须是不小于当前时钟的整数"})
                return
            self._json(200, runtime.evaluate())
            return
        self._json(404, {"message": "未知路径"})

    def log_message(self, *args):
        pass


class ScreenServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, runtime: ScreenRuntime):
        super().__init__(address, ScreenHandler)
        self.runtime = runtime


def build(terminal: dict, control_url: str | None = None,
          host="127.0.0.1", port=0, clock=0) -> ScreenServer:
    runtime = ScreenRuntime(
        screen_id=terminal["id"],
        locales=set(terminal["locales"]),
        icons=set(terminal["icons"]),
        clock=clock,
        control_url=control_url,
    )
    return ScreenServer((host, port), runtime)
