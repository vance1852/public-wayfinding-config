"""本地联调回放驱动。

从合作方资料端（``partner.py``）拉取起始事实与原始事件，按到达时刻送入
配置分发控制端；需要下发给终端的配置通过资料端记录的
``PUT /screens/{id}/configuration`` 交换发送，202 即视为终端收件。

用法::

    python -m dispatch.driver fixtures/two-directions-and-closure.json \\
        --partner http://127.0.0.1:8090

也可用 ``--serve`` 在回放同时起控制端，供现场用 HTTP 查状态。
"""

import argparse
import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

from .engine import DispatchEngine
from .service import ControlServer, ConfigurationStore


def http_json(url: str, method="GET", body=None, timeout=5):
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    request = urllib.request.Request(
        url, data=data, method=method,
        headers={"Content-Type": "application/json; charset=utf-8"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        payload = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(payload)
        except json.JSONDecodeError:
            return exc.code, {"raw": payload}


class FixturePartner:
    """合作方资料端客户端：拉资料、回放设备交换、查 journal。"""

    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")

    def facts(self):
        status, value = http_json(f"{self.base_url}/fixture/facts")
        if status != 200:
            raise RuntimeError(f"资料端返回 {status}")
        return value

    def events(self):
        status, value = http_json(f"{self.base_url}/fixture/events")
        if status != 200:
            raise RuntimeError(f"资料端返回 {status}")
        return value

    def journal(self):
        status, value = http_json(f"{self.base_url}/fixture/journal")
        return value if status == 200 else []

    def push_configuration(self, screen_id: str, payload: dict):
        return http_json(
            f"{self.base_url}/screens/{screen_id}/configuration",
            method="PUT", body=payload,
        )


class ReplayPartnerStore(ConfigurationStore):
    """把配置下发动作转到合作方资料端的记录交换。"""

    def __init__(self, facts, partner: FixturePartner):
        super().__init__(facts)
        self.partner = partner

    def _deliver(self, item: dict) -> dict:
        record = {"screen": item["screen"], "deferred": item["deferred"],
                  "payload": item["payload"]}
        if item["deferred"]:
            record["result"] = "deferred"
            self.delivery_log.append(record)
            return record
        status, body = self.partner.push_configuration(item["screen"], item["payload"])
        record["status"] = status
        record["response"] = body
        # 资料端只有一条记录交换：首个匹配 202，其后未匹配为 404。
        # 404 不代表终端拒收，只代表本次联调没有更多录好的回执。
        record["result"] = "accepted" if status == 202 else "no_recorded_receipt"
        self.delivery_log.append(record)
        return record


def replay(facts: dict, events: list, store: ConfigurationStore,
           speed: float = 0.0, realtime: bool = False) -> dict:
    """按事件到达时刻回放；同一时刻按记录顺序处理。"""
    trace = []
    for event in events:
        at = event["at"]
        store.advance(at) if isinstance(store, ConfigurationStore) else None
        if isinstance(store, ReplayPartnerStore):
            result = store.ingest(event)
        else:
            result = store.ingest(event)
        trace.append({"at": at, "kind": event["kind"], "result": result})
    return {
        "at": store.engine.clock,
        "trace": trace,
        "overview": store.overview(),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("scenario", type=Path, help="合作方资料文件（用于提示）")
    parser.add_argument("--partner", default="http://127.0.0.1:8090",
                        help="合作方资料端地址")
    parser.add_argument("--serve", type=int, default=None,
                        help="同时在该端口启动控制端 HTTP 服务")
    parser.add_argument("--output", type=Path, default=None,
                        help="把回放汇总写入 JSON 文件")
    args = parser.parse_args()

    partner = FixturePartner(args.partner)
    facts = partner.facts()
    events = partner.events()
    store = ReplayPartnerStore(facts, partner)

    server = None
    if args.serve:
        server = ControlServer(("127.0.0.1", args.serve), store)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        print(f"控制端 http://127.0.0.1:{args.serve}", flush=True)

    summary = replay(facts, events, store)
    summary["partner_journal"] = partner.journal()

    text = json.dumps(summary, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(text, encoding="utf-8")
        print(f"回放汇总已写入 {args.output}")
    else:
        print(text)

    if server:
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            server.shutdown()


if __name__ == "__main__":
    main()
