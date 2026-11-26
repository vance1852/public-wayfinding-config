"""端到端现场演示：控制端 + 全部屏端在本机联演。

启动后会：

1. 为资料里的每块终端起一个屏端进程内模拟器（独立端口）；
2. 启动配置分发控制端，配置直接下发到屏端并接收回报；
3. 按场景事件推进虚拟时钟，在关键时点打印每块屏正在给旅客的指示；
4. 重点演示暂停联网、恢复联网、内容到期三种现场检查。

用法::

    python -m dispatch.demo fixtures/two-directions-and-closure.json
    python -m dispatch.demo fixtures/pass-through-after-entry-ends.json
"""

import argparse
import json
import threading
import urllib.request
from pathlib import Path

from . import screen as screen_mod
from .service import ControlServer, ConfigurationStore


def http_json(url: str, method="GET", body=None, timeout=5):
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    request = urllib.request.Request(
        url, data=data, method=method,
        headers={"Content-Type": "application/json; charset=utf-8"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read()
        return response.status, json.loads(raw.decode("utf-8")) if raw else None


class LocalScreenFabric:
    """管理一组本机屏端模拟器。"""

    def __init__(self, facts, control_url):
        self.servers = {}
        self.urls = {}
        for terminal in facts["terminals"]:
            server = screen_mod.build(terminal, control_url=control_url,
                                      host="127.0.0.1", port=0)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            self.servers[terminal["id"]] = server
            self.urls[terminal["id"]] = f"http://127.0.0.1:{server.server_port}"

    def call(self, screen_id: str, path: str, method="GET", body=None):
        return http_json(self.urls[screen_id] + path, method, body)

    def shutdown(self):
        for server in self.servers.values():
            server.shutdown()
            server.server_close()


def print_screens(fabric, facts, title):
    print(f"\n=== {title} ===")
    for terminal in facts["terminals"]:
        _, state = fabric.call(terminal["id"], "/local/display")
        online = "停用" if state.get("disabled") else ("在线" if state["online"] else "断网")
        print(f"[屏 {terminal['id']} @ {terminal['site']}｜{online}｜时钟 {state['at']}]")
        if state.get("disabled"):
            print("  （终端已被现场停用，等待人工处置）")
            continue
        if not state["directions"]:
            print("  （尚无配置）")
        for route_id, item in state["directions"].items():
            if item["mode"] == "guide":
                text = "；".join(item["text"].values())
                tag = "在途" if item.get("in_transit") else "入口"
                print(f"  → {route_id}（{tag}）：{text}"
                      f"  [图形 {item.get('icon')}, 绕行 {item.get('alternate')}]")
            else:
                print(f"  → {route_id}：停止引导（{item['reason']}）")


def run_demo(facts: dict, events: list, control_port: int = 8080) -> ControlServer:
    store = ConfigurationStore(facts)
    server = ControlServer(("127.0.0.1", control_port), store)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    control_url = f"http://127.0.0.1:{control_port}"
    fabric = LocalScreenFabric(facts, control_url)
    store.screen_base_url = "http://127.0.0.1:{port}"  # 占位，实际按屏路由
    # 控制端需要按屏寻址：替换下发函数
    def deliver_via_fabric(item):
        record = {"screen": item["screen"], "deferred": item["deferred"],
                  "payload": item["payload"]}
        if item["deferred"]:
            record["result"] = "deferred"
            store.delivery_log.append(record)
            return record
        try:
            status, body = fabric.call(
                item["screen"],
                f"/screens/{item['screen']}/configuration",
                method="PUT", body=item["payload"])
            record["result"] = "accepted" if status == 202 else "rejected"
            record["response"] = body
        except OSError as exc:
            record["result"] = "unreachable"
            record["error"] = str(exc)
        store.delivery_log.append(record)
        return record

    store._deliver = deliver_via_fabric

    print(f"控制端：{control_url}")
    for sid, url in sorted(fabric.urls.items()):
        print(f"屏端 {sid}：{url}")

    last_clock = 0
    checkpoints = set()
    for event in events:
        at = event["at"]
        if at > last_clock:
            store.advance(at)
            for terminal in facts["terminals"]:
                try:
                    fabric.call(terminal["id"], "/local/clock",
                                method="POST", body={"at": at})
                except OSError:
                    pass
            last_clock = at
        kind = event["kind"]
        if kind == "offline":
            fabric.call(event["screen"], "/local/offline", method="POST", body={})
        elif kind == "reconnect":
            fabric.call(event["screen"], "/local/online", method="POST", body={})
        elif kind == "field_disabled":
            fabric.call(event["screen"], "/local/disable", method="POST", body={})
        store.ingest(event)
        checkpoints.add(at)

    print_screens(fabric, facts, f"事件回放结束（时钟 {store.engine.clock}）")
    _, overview_now = http_json(control_url + "/overview")
    print("\n--- 回放结束时各方向状态 ---")
    for route in overview_now["routes"]:
        print(f"方向 {route['route']}：可引导={route['can_guide_now']}，"
              f"下一可引导时刻={route['next_guidance_at']}，"
              f"待确认终端={len(route['pending_confirmations'])}")
    _, signs_now = http_json(control_url + "/signs")
    print("待人工补挂标牌：", [s["screen"] for s in signs_now["signs"]] or "无")

    # 现场检查一：暂停联网期间，屏只按最后有效期行动
    target = facts["terminals"][0]["id"]
    if store.engine.screens[target].connectivity.value != "disabled":
        fabric.call(target, "/local/offline", method="POST", body={})
        store.ingest({"kind": "offline", "screen": target,
                      "at": store.engine.clock + 1})
        store.advance(store.engine.clock + 1)
        print_screens(fabric, facts, f"检查点 A：{target} 暂停联网")
    else:
        print(f"\n检查点 A 跳过：{target} 已被现场停用，改用 fork-v2 演示断网")
        target = "fork-v2"
        fabric.call(target, "/local/offline", method="POST", body={})
        store.ingest({"kind": "offline", "screen": target,
                      "at": store.engine.clock + 1})
        store.advance(store.engine.clock + 1)
        print_screens(fabric, facts, f"检查点 A：{target} 暂停联网")

    # 现场检查二：到期后即使仍断网也必须停止引导
    expired_at = store.engine.clock + 60
    store.advance(expired_at)
    for terminal in facts["terminals"]:
        try:
            fabric.call(terminal["id"], "/local/clock",
                        method="POST", body={"at": expired_at})
        except OSError:
            pass
    print_screens(fabric, facts, f"检查点 B：时钟同步推进到 {expired_at}（到期）")

    # 现场检查三：恢复联网，旧回执不复活失效路线，按当前配置显示
    fabric.call(target, "/local/online", method="POST", body={})
    store.ingest({"kind": "reconnect", "screen": target, "at": expired_at})
    print_screens(fabric, facts, f"检查点 C：{target} 恢复联网")

    _, overview = http_json(control_url + "/overview")
    _, signs = http_json(control_url + "/signs")
    print("\n=== 控制端视角 ===")
    print(json.dumps({
        "directions": overview["routes"],
        "pending_signs": signs["signs"],
        "resolved_signs": signs["resolved"],
        "decisions": overview["decisions"],
    }, ensure_ascii=False, indent=2))
    return server, fabric


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("scenario", type=Path)
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    scenario = json.loads(args.scenario.read_text(encoding="utf-8"))
    server, fabric = run_demo(scenario["facts"], scenario["events"], args.port)
    print("\n演示结束（进程退出）。")


if __name__ == "__main__":
    main()
