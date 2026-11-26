"""本地配置分发控制端。

控制端把 :mod:`dispatch.engine` 的判定通过 HTTP 暴露给现场人员与屏端：

* ``POST /events``（及 ``/publish``、``/withdraw``、``/closures``）录入分区改动；
* ``PUT /screens/{id}/configuration`` 由转发器发往屏端，回报走
  ``POST /reports``（received/installed/displaying、offline/reconnect/
  field_disabled）；
* ``GET /directions/{route}`` 回答何时可开始引导、欠哪些终端确认；
* ``GET /screens`` 区分收到、可用、正在显示与失联后最后可证实状态；
* ``GET /signs`` 给出封闭时无法证明撤下、必须人工补挂标牌的入口。

只用标准库，默认监听回环地址。
"""

import json
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from .engine import DispatchEngine


class ConfigurationStore:
    """线程安全的引擎包装，负责下发与状态查询。"""

    def __init__(self, facts: dict, screen_base_url: str | None = None,
                 partner_base_url: str | None = None):
        self.lock = threading.RLock()
        self.engine = DispatchEngine(facts)
        # 屏端接入地址（screen.py 模拟器或真实屏端网关）
        self.screen_base_url = screen_base_url
        # 合作方资料端地址，用于本地联调回放
        self.partner_base_url = partner_base_url
        self.delivery_log: list[dict] = []

    # ------------------------------------------------------------------
    def ingest(self, event: dict) -> dict:
        with self.lock:
            decision = self.engine.ingest(event)
            plan = self.engine.delivery_plan()
            if event["kind"] == "reconnect":
                for screen in self.engine.screens.values():
                    if screen.connectivity.value == "online":
                        screen.last_sent_signature = None
                plan += self.engine.flush_pending()
                plan += self.engine.delivery_plan()
        deliveries = [self._deliver(item) for item in plan]
        return {"decision": self._decision_dict(decision), "deliveries": deliveries}

    def advance(self, now: int) -> dict:
        with self.lock:
            plan = self.engine.advance(now)
        deliveries = [self._deliver(item) for item in plan]
        return {"at": now, "deliveries": deliveries}

    def _deliver(self, item: dict) -> dict:
        """把一套配置发往屏端；离线/停用只登记滞留，不伪造送达。"""
        record = {"screen": item["screen"], "deferred": item["deferred"],
                  "payload": item["payload"]}
        if item["deferred"] or not self.screen_base_url:
            record["result"] = "deferred"
            self.delivery_log.append(record)
            return record
        url = f"{self.screen_base_url}/screens/{item['screen']}/configuration"
        request = urllib.request.Request(
            url, data=json.dumps(item["payload"]).encode("utf-8"),
            method="PUT",
            headers={"Content-Type": "application/json; charset=utf-8"},
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                body = json.loads(response.read().decode("utf-8"))
            record["result"] = "accepted"
            record["response"] = body
        except urllib.error.HTTPError as exc:
            record["result"] = "rejected"
            record["status"] = exc.code
            record["response"] = exc.read().decode("utf-8", "replace")
        except (urllib.error.URLError, OSError) as exc:
            # 屏端进程暂停联网：登记为未送达，等待重连补发
            record["result"] = "unreachable"
            record["error"] = str(exc)
        self.delivery_log.append(record)
        return record

    # ------------------------------------------------------------------
    def report(self, event: dict) -> dict:
        """屏端回报入口。业务发布不是设备安装回执，回报只更新槽位状态。"""
        with self.lock:
            decision = self.engine.ingest(event)
            plan = self.engine.delivery_plan()
            if event.get("kind") == "reconnect":
                # 重连必须按当前时刻重算并下发：旧安装回执不能恢复失效路线。
                # 强制让各屏期望配置重新进入下发流程。
                for screen in self.engine.screens.values():
                    if screen.connectivity.value == "online":
                        screen.last_sent_signature = None
                plan += self.engine.flush_pending()
                plan += self.engine.delivery_plan()
        deliveries = [self._deliver(item) for item in plan]
        return {"decision": self._decision_dict(decision), "deliveries": deliveries}

    # ------------------------------------------------------------------
    def direction(self, route_id: str) -> dict:
        with self.lock:
            status = self.engine.direction_status(route_id)
            status["in_transit_notices"] = self.engine.in_transit_notices(route_id)
            return status

    def screens(self) -> dict:
        with self.lock:
            view = self.engine.overview()
        return {"at": view["at"], "screens": view["screens"]}

    def signs(self) -> dict:
        with self.lock:
            signs = list(self.engine.pending_signs.values())
            # 入口恢复联网且控制端已重新下发停止引导配置，即视为可证实撤下；
            # 停用的终端无法远程证实，标牌保持到现场人工确认。
            resolved = []
            for sign in signs:
                screen = self.engine.screens[sign["screen"]]
                if screen.connectivity.value == "online" \
                        and screen.id not in self.engine.pending_delivery \
                        and screen.last_sent_at is not None \
                        and screen.last_sent_at >= sign["since"]:
                    rendered = self.engine.render_screen(screen).get(sign["route"])
                    if rendered is not None and rendered.mode.value == "stop":
                        resolved.append(sign["screen"])
            active = [s for s in signs if s["screen"] not in resolved]
            return {"at": self.engine.clock, "signs": active, "resolved": resolved}

    def overview(self) -> dict:
        with self.lock:
            view = self.engine.overview()
            view["delivery_log"] = list(self.delivery_log)
            view["signs"] = self.signs()["signs"]
            return view

    @staticmethod
    def _decision_dict(decision) -> dict:
        return {
            "at": decision.at, "kind": decision.kind,
            "revision": decision.revision, "status": decision.status,
            "reasons": decision.reasons,
            "affected_screens": decision.affected_screens,
            "detail": decision.detail,
        }


class ControlHandler(BaseHTTPRequestHandler):
    server_version = "DispatchControl/1.0"

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
            if not 0 <= length <= 1_048_576:
                raise ValueError
            body = json.loads(self.rfile.read(length)) if length else {}
            if not isinstance(body, dict):
                raise ValueError
            return body, None
        except (ValueError, UnicodeError):
            return None, "无法读取 JSON 请求正文"

    def do_POST(self):
        path = urlsplit(self.path).path.rstrip("/")
        body, error = self._read_body()
        if error:
            self._json(400, {"message": error})
            return
        store = self.server.store
        if path == "/events":
            events = body.get("events")
            if not isinstance(events, list) or not events:
                self._json(400, {"message": "events 必须是非空数组"})
                return
            results = [store.ingest(event) for event in events]
            self._json(200, {"results": results})
            return
        if path in ("/publish", "/withdraw", "/closures", "/reports"):
            kind = {
                "/publish": "publish",
                "/withdraw": "withdraw",
                "/closures": "closure",
                "/reports": None,
            }[path]
            event = dict(body)
            if kind is None:
                # 回报：device_report/offline/reconnect/field_disabled 原样送入
                result = store.report(event)
            else:
                event.setdefault("kind", kind)
                result = store.ingest(event)
            self._json(200, result)
            return
        if path == "/clock":
            now = body.get("at")
            if not isinstance(now, int):
                self._json(400, {"message": "at 必须是分钟整数"})
                return
            self._json(200, store.advance(now))
            return
        self._json(404, {"message": "未知路径"})

    def do_GET(self):
        path = urlsplit(self.path).path.rstrip("/")
        store = self.server.store
        if path == "/overview":
            self._json(200, store.overview())
            return
        if path == "/screens":
            self._json(200, store.screens())
            return
        if path == "/signs":
            self._json(200, store.signs())
            return
        if path.startswith("/directions/"):
            route_id = path.rsplit("/", 1)[-1]
            with store.lock:
                if route_id not in store.engine.routes:
                    self._json(404, {"message": f"未知方向 {route_id}"})
                    return
            self._json(200, store.direction(route_id))
            return
        self._json(404, {"message": "未知路径"})

    def log_message(self, *args):
        pass


class ControlServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, store: ConfigurationStore):
        super().__init__(address, ControlHandler)
        self.store = store


def run(facts: dict, host="127.0.0.1", port=8080,
        screen_base_url=None, partner_base_url=None) -> ControlServer:
    store = ConfigurationStore(facts, screen_base_url, partner_base_url)
    server = ControlServer((host, port), store)
    return server
