"""引擎安全规则测试：覆盖两个合作方场景与题目全部关键约束。"""

import copy
import json
import unittest
from pathlib import Path

from dispatch.engine import (
    Connectivity,
    DispatchEngine,
    PublishStatus,
    ScreenMode,
    TicketType,
)

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"


def load(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def replay(engine, events, advance_clock=True):
    decisions = []
    for event in events:
        if advance_clock and event["at"] > engine.clock:
            engine.advance(event["at"])
        decisions.append(engine.ingest(event))
        engine.delivery_plan()
    return decisions


class TwoDirectionsClosureTest(unittest.TestCase):
    def setUp(self):
        self.data = load("two-directions-and-closure.json")
        self.engine = DispatchEngine(copy.deepcopy(self.data["facts"]))
        self.decisions = replay(self.engine, self.data["events"])

    def test_publish_partial_when_some_screens_cannot_render(self):
        publish = self.decisions[0]
        self.assertEqual(publish.status, PublishStatus.PARTIAL.value)
        detail = publish.detail
        # fork-v1 只有 walk 图形，无法表达 lift 绕行；station 不在该路段目标内
        missing_screens = {item["screen"] for item in detail["missing"]}
        self.assertIn("fork-v1", missing_screens)
        self.assertNotIn("station", missing_screens)
        self.assertIn("museum", detail["renderable"])
        self.assertIn("fork-v2", detail["renderable"])

    def test_translation_requires_rail_operator(self):
        # rail 是 f-r 的译文维护方；quarter 发译文应被拒
        engine = DispatchEngine(copy.deepcopy(self.data["facts"]))
        blocked = engine.ingest({
            "at": 580, "kind": "publish", "operator": "quarter",
            "revision": "quarter/x", "hours": [600, 900],
            "route": "east", "link": "f-r",
            "locales": {"zh": "乱改译文"},
        })
        self.assertEqual(blocked.status, PublishStatus.BLOCKED.value)
        self.assertTrue(any("无权" in r for r in blocked.reasons))

    def test_withdraw_only_own_and_only_before_effective(self):
        # rail/12 在 583 发布、604 撤回，其生效起点 606，撤回合法
        withdraw = next(d for d in self.decisions if d.kind == "withdraw")
        self.assertEqual(withdraw.status, "withdrawn")
        ticket = next(t for t in self.engine.tickets if t.revision == "rail/12")
        self.assertTrue(ticket.withdrawn)

        # 撤回别人的票被拒
        engine = DispatchEngine(copy.deepcopy(self.data["facts"]))
        engine.ingest(self.data["events"][0])  # quarter/32
        bad = engine.ingest({
            "at": 585, "kind": "withdraw", "operator": "rail",
            "revision": "quarter/32",
        })
        self.assertEqual(bad.status, PublishStatus.BLOCKED.value)

        # 已生效的票不能撤回
        engine.advance(650)
        late = engine.ingest({
            "at": 650, "kind": "withdraw", "operator": "quarter",
            "revision": "quarter/32",
        })
        self.assertEqual(late.status, PublishStatus.BLOCKED.value)

    def test_closure_pushes_stop_and_flags_unconfirmable_entry(self):
        # 599 封闭时 museum 已离线（591），必须列入人工标牌
        signs = self.engine.pending_signs
        self.assertIn("museum", signs)
        self.assertEqual(signs["museum"]["route"], "east")

        # 在线的 fork-v2 在封闭段上：译文已撤回，无法拼合绕行 -> 停止引导
        content = self.engine.render_screen(self.engine.screens["fork-v2"])["east"]
        self.assertEqual(content.mode, ScreenMode.STOP)
        self.assertTrue(content.in_transit)

    def test_reverse_direction_kept_serving(self):
        # 封闭只影响 east 的 f-r；west 方向全程不受影响
        west = self.engine.direction_status("west")
        self.assertTrue(west["can_guide_now"])
        rendered = self.engine.render_screen(self.engine.screens["station"])["west"]
        self.assertEqual(rendered.mode, ScreenMode.GUIDE)

    def test_offline_screen_uses_last_commitment_then_must_stop(self):
        # 用独立引擎停在 museum 离线后、封闭发生时
        engine = DispatchEngine(copy.deepcopy(self.data["facts"]))
        for event in self.data["events"][:5]:  # 到 591 offline
            if event["at"] > engine.clock:
                engine.advance(event["at"])
            engine.ingest(event)
        museum = engine.screens["museum"]
        self.assertEqual(museum.connectivity, Connectivity.OFFLINE)
        engine.advance(600)
        admittance = engine.entry_admittance("east", 600)
        # 封闭尚未发生（599 事件未送入），museum 的离线承诺在 624 前仍有效
        self.assertTrue(self.engine._offline_commitment_covers(
            museum,
            engine.render_link("east", "m-f", 600), 600))
        # 超过 624 后离线承诺失效
        engine.advance(625)
        self.assertFalse(engine._offline_commitment_covers(
            museum,
            engine.render_link("east", "m-f", 625), 625))

    def test_stale_install_receipt_cannot_revive_invalid_route(self):
        # 611 的 installed 回执是 museum 离线期间产生、615 重连后才送达；
        # 重连后 east 仍封闭且译文已撤，入口绝不能因该回执恢复引导
        admittance = self.engine.entry_admittance("east")
        self.assertFalse(admittance["admit"])
        slot = self.engine.screens["museum"].slots.get("quarter/32")
        # 回执如实登记为重连纪元前的冻结历史，不再作为当前路线证据
        self.assertIsNotNone(slot)
        self.assertTrue(slot.installed is not None or slot.frozen is not None)
        rendered = self.engine.render_screen(self.engine.screens["museum"])["east"]
        self.assertEqual(rendered.mode, ScreenMode.STOP)

    def test_old_screen_keeps_old_guidance_while_path_open(self):
        # fork-v1 不能表达 lift 绕行；在绕行票生效但路段本身未封闭时，
        # 它应保留适用的旧基线指引（基线 624 前有效）
        engine = DispatchEngine(copy.deepcopy(self.data["facts"]))
        engine.ingest(self.data["events"][0])  # quarter/32
        engine.advance(610)
        rendered = engine.render_screen(engine.screens["fork-v1"])["east"]
        # 绕行内容由能力更强的 fork-v2 承担；fork-v1 退回基线
        self.assertEqual(rendered.mode, ScreenMode.GUIDE)
        self.assertEqual(rendered.revisions, ["street/31"])

    def test_old_screen_stops_when_it_cannot_show_detour_during_closure(self):
        engine = DispatchEngine(copy.deepcopy(self.data["facts"]))
        for event in self.data["events"][:6]:  # 到 599 封闭
            if event["at"] > engine.clock:
                engine.advance(event["at"])
            engine.ingest(event)
        engine.advance(605)
        rendered = engine.render_screen(engine.screens["fork-v1"])["east"]
        self.assertEqual(rendered.mode, ScreenMode.STOP)

    def test_field_disabled_is_distinct_state(self):
        self.assertEqual(self.engine.screens["museum"].connectivity,
                         Connectivity.DISABLED)

    def test_four_statuses_distinguished_in_slots(self):
        slot = self.engine.screens["fork-v2"].slots["quarter/32"]
        self.assertIsNotNone(slot.installed)
        self.assertIsNone(slot.displaying)  # 只到 installed，未证实显示
        museum_slot = self.engine.screens["museum"].slots["street/31"]
        # 重连纪元后实时状态已冻结，但失联前最后可证实的 displaying 仍可查
        self.assertTrue(
            museum_slot.displaying is not None
            or (museum_slot.frozen and museum_slot.frozen["state"] == "displaying")
        )

    def test_closure_overrides_scheduled_hours_and_staged_content(self):
        # 临时封闭 599-710 越过日常时段；预发的 rail/12（606 生效）在封闭期内
        # 无法启用，且 604 已撤回；解封后 east 仍无有效译文拼合
        self.engine.advance(650)
        self.assertTrue(self.engine.link_closed("f-r", 650))
        self.engine.advance(720)
        self.assertFalse(self.engine.link_closed("f-r", 720))
        rendered = self.engine.render_screen(self.engine.screens["fork-v2"])["east"]
        self.assertEqual(rendered.mode, ScreenMode.STOP)

    def test_baseline_expiry_forces_stop(self):
        engine = DispatchEngine(copy.deepcopy(self.data["facts"]))
        engine.advance(700)  # 无票券、无封闭，但基线 usable_until=624
        rendered = engine.render_screen(engine.screens["fork-v2"])["east"]
        self.assertEqual(rendered.mode, ScreenMode.STOP)


class PassThroughAfterEntryEndsTest(unittest.TestCase):
    def setUp(self):
        self.data = load("pass-through-after-entry-ends.json")
        self.engine = DispatchEngine(copy.deepcopy(self.data["facts"]))
        self.decisions = replay(self.engine, self.data["events"])

    def test_route_level_ticket_authorized_by_entry_operator(self):
        publish = self.decisions[0]
        self.assertNotEqual(publish.status, PublishStatus.BLOCKED.value)
        ticket = self.engine.tickets[0]
        self.assertEqual(ticket.ticket_type, TicketType.HOURS)

    def test_entry_guides_before_stop_time_blocks_after(self):
        engine = DispatchEngine(copy.deepcopy(self.data["facts"]))
        engine.ingest(self.data["events"][0])  # rail/8 发布于 880
        engine.advance(905)
        self.assertTrue(engine.entry_admittance("to-gate")["admit"])
        engine.advance(911)
        admittance = engine.entry_admittance("to-gate")
        self.assertFalse(admittance["admit"])
        self.assertTrue(any("停引导" in b["reason"] for b in admittance["blockers"]))

    def test_in_transit_notice_survives_entry_stop(self):
        # 912 的查询：入口 910 已停，但中间点 mid 仍要给在途旅客走完提示
        self.engine.advance(912)
        notices = self.engine.in_transit_notices("to-gate")
        self.assertEqual(len(notices), 1)
        self.assertEqual(notices[0]["site"], "middle")
        self.assertEqual(notices[0]["mode"], "guide")
        # 入口屏是停止新进入者，中间点仍是 guide
        entry = self.engine.render_screen(
            self.engine.screens["east-entry"])["to-gate"]
        self.assertEqual(entry.mode, ScreenMode.STOP)
        self.assertTrue(entry.stop_entry)
        mid = self.engine.render_screen(self.engine.screens["mid"])["to-gate"]
        self.assertEqual(mid.mode, ScreenMode.GUIDE)

    def test_everything_stops_after_route_hours_end(self):
        self.engine.advance(940)
        mid = self.engine.render_screen(self.engine.screens["mid"])["to-gate"]
        self.assertEqual(mid.mode, ScreenMode.STOP)
        self.assertIsNone(self.engine.direction_status("to-gate")["next_guidance_at"])

    def test_query_event_answered(self):
        decision = self.decisions[-1]
        self.assertEqual(decision.kind, "query")
        self.assertIn("direction_status", decision.detail)


class DeliveryPlanTest(unittest.TestCase):
    def test_only_changes_trigger_delivery_and_deferred_when_offline(self):
        data = load("pass-through-after-entry-ends.json")
        engine = DispatchEngine(copy.deepcopy(data["facts"]))
        first = engine.advance(880)
        self.assertTrue(any(not item["deferred"] for item in first))
        # 无变化时不再下发
        self.assertEqual(engine.delivery_plan(), [])

        engine.ingest({"at": 881, "kind": "offline", "screen": "mid"})
        # 推进到基线到期后：期望内容变为停止引导，离线屏只能登记滞留
        engine.advance(941)
        self.assertIn("mid", engine.pending_delivery)
        deferred_payload = engine.pending_delivery["mid"][1]
        self.assertEqual(deferred_payload["directions"]["to-gate"]["mode"], "stop")
        # 重连补发，且内容按当前时刻重算（不依据旧回执恢复路线）
        engine.ingest({"at": 942, "kind": "reconnect", "screen": "mid"})
        flushed = engine.flush_pending()
        self.assertEqual([item["screen"] for item in flushed], ["mid"])
        self.assertFalse(flushed[0]["deferred"])

    def test_payload_carries_usable_window_for_local_expiry(self):
        data = load("two-directions-and-closure.json")
        engine = DispatchEngine(copy.deepcopy(data["facts"]))
        plan = engine.advance(581)
        museum = next(item for item in plan if item["screen"] == "museum")
        east = museum["payload"]["directions"]["east"]
        self.assertIn("usable_window", east)
        window = east["usable_window"]
        # 此时 museum 入口显示基线（票券 610 才生效），窗口受基线 624 约束
        self.assertEqual(window[0], 480)
        self.assertEqual(window[1], 624)


if __name__ == "__main__":
    unittest.main()
