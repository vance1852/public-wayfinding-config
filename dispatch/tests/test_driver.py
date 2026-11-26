"""联调驱动与合作方资料端的真实 HTTP 对接测试。"""

import json
import threading
import unittest
from pathlib import Path

from partner import PartnerServer, load_scenario
from dispatch.driver import FixturePartner, ReplayPartnerStore, replay

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"


class DriverPartnerTest(unittest.TestCase):
    def _start_partner(self, name):
        scenario = load_scenario(FIXTURES / name)
        server = PartnerServer(("127.0.0.1", 0), scenario)
        thread = threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.02})
        thread.start()
        self.addCleanup(server.shutdown)
        self.addCleanup(thread.join, 3)
        self.addCleanup(server.server_close)
        return server, scenario

    def test_replay_against_recorded_exchanges(self):
        for name in ("two-directions-and-closure.json",
                     "pass-through-after-entry-ends.json"):
            with self.subTest(case=name):
                server, scenario = self._start_partner(name)
                partner = FixturePartner(
                    f"http://127.0.0.1:{server.server_port}")
                facts = partner.facts()
                events = partner.events()
                store = ReplayPartnerStore(facts, partner)
                summary = replay(facts, events, store)

                # 首个 PUT 命中记录交换并拿到 202
                results = {item["result"] for item in summary["overview"]["delivery_log"]}
                self.assertIn("accepted", results)
                # 资料端 journal 记录了全部请求，含未匹配（404）请求
                journal = partner.journal()
                self.assertTrue(any(item["matched"] for item in journal))
                # 事件全部进入决策历史
                self.assertEqual(
                    len(summary["overview"]["decisions"]), len(events))

    def test_closure_direction_and_signs(self):
        server, scenario = self._start_partner("two-directions-and-closure.json")
        partner = FixturePartner(f"http://127.0.0.1:{server.server_port}")
        store = ReplayPartnerStore(partner.facts(), partner)
        replay(partner.facts(), partner.events(), store)
        east = store.engine.direction_status("east")
        west = store.engine.direction_status("west")
        self.assertFalse(east["can_guide_now"])     # 封闭期
        self.assertTrue(west["can_guide_now"])      # 反方向继续服务
        self.assertIn("museum", store.engine.pending_signs)


if __name__ == "__main__":
    unittest.main()
