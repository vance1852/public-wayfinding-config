"""分区配置分发的纯判定引擎。

引擎不做任何网络 IO：控制端把合作方资料和事件按序送入，引擎维护全部
路线票券、封闭段和终端状态，并给出每个终端当前应当显示的配置（期望配置）。
时间使用活动日的分钟数，所有时段均为左闭右开 ``[start, end)``。

安全规则集中在本模块：

* 入口引导要求该方向后续每个指路点都存在可给出相容指引的终端；
* 终端离线时，离线前已承诺的有效时段仍计入相容性；
* 封闭段上的在途指引与普通路线分别处理，撤回入口票券不会清掉在途提示；
* 老屏可保留其能力可表达且现场仍可通行的旧指引，表达不了有效绕行时
  退到停止引导（``stop``）；
* 安装回执只证明送达/显示，绝不能让已失效路线恢复。
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class TicketType(str, Enum):
    """票券用途。"""

    CHANGE = "change"          # 通行/绕行改动（图形+无障碍）
    TRANSLATION = "translation"  # 本地译文
    HOURS = "hours"            # 路线时段/停引导时点改动


class PublishStatus(str, Enum):
    """一次发布的处置结果。"""

    BLOCKED = "blocked"    # 权限或资料校验未过，完全拒收
    STAGED = "staged"      # 已入库预发，尚无终端能显示
    PARTIAL = "partial"    # 仅部分目标终端可显示
    EFFECTIVE = "effective"  # 全部目标终端都能给出相容指引


class ScreenMode(str, Enum):
    """屏幕对某一方向的指示模式。"""

    GUIDE = "guide"      # 给出路线/绕行指引
    STOP = "stop"        # 停止引导
    NONE = "none"        # 该屏不负责此方向（如反向或终点）


class Connectivity(str, Enum):
    ONLINE = "online"
    OFFLINE = "offline"
    DISABLED = "disabled"  # 现场确实停用


@dataclass
class Ticket:
    """发布人提交的、带生效时段的路线修改票券。"""

    revision: str
    operator: str
    route_id: str
    at: int
    hours: tuple[int, int]
    link: Optional[str] = None
    ticket_type: TicketType = TicketType.CHANGE
    alternate: Optional[str] = None
    icon: Optional[str] = None
    locales: Optional[dict] = None
    accessible: Optional[bool] = None
    stop_entry_at: Optional[int] = None
    withdrawn: bool = False
    withdrawn_at: Optional[int] = None

    @property
    def is_link_ticket(self) -> bool:
        return self.link is not None

    def active_at(self, now: int, closures) -> bool:
        """票券在 now 是否处于自身生效时段，且未被撤回。"""
        start, end = self.hours
        if not (start <= now < end) or self.withdrawn:
            return False
        if self.is_link_ticket and any(
            c.link == self.link and c.covers(now) for c in closures
        ):
            # 封闭段上的普通通行改动被封闭压住；绕行类票券的生效性在
            # 渲染时按 alternate 另行判定，这里只负责时段过滤。
            if self.ticket_type is TicketType.CHANGE and not self.alternate:
                return False
        return True


@dataclass
class Closure:
    """临时封闭，可越过日常上线时间并压住预发内容。"""

    link: str
    from_at: int
    until: int
    operator: str

    def covers(self, now: int) -> bool:
        return self.from_at <= now < self.until


@dataclass
class ScreenSlot:
    """某终端对某一具体内容版本的四态跟踪。"""

    revision: str
    # 控制端明确区分的四种状态（失联后只冻结在最后可证实状态）
    received: Optional[int] = None
    installed: Optional[int] = None
    displaying: Optional[int] = None
    usable_until: Optional[int] = None
    # 该回报产生时终端所处的重连纪元；重连后旧纪元回报不能再证明当前路线
    epoch: int = 0
    # 失联前最后可证实状态的冻结快照（仅供总览展示，不参与相容性判定）
    frozen: Optional[dict] = None

    def provable_state(self) -> str:
        if self.displaying is not None:
            return "displaying"
        if self.installed is not None:
            return "installed"
        if self.received is not None:
            return "received"
        if self.frozen is not None:
            return self.frozen["state"]
        return "unknown"


@dataclass
class ScreenState:
    """单个终端的运行时状态。"""

    terminal: dict
    site: str
    slots: dict = field(default_factory=dict)  # revision -> ScreenSlot
    connectivity: Connectivity = Connectivity.ONLINE
    offline_since: Optional[int] = None
    last_seen: Optional[int] = None
    # 重连纪元：每次 reconnect 递增，用于作废重连前的旧显示承诺
    epoch: int = 0
    # 最近一次实际下发给该屏的配置签名，用于只在内容变化时下发
    last_sent_signature: Optional[str] = None
    last_sent_at: Optional[int] = None
    # 最近一次下发所处的纪元；跨纪元的下发记录不能作为当前相容性证据
    last_sent_epoch: int = 0

    @property
    def id(self) -> str:
        return self.terminal["id"]

    @property
    def locales(self) -> set:
        return set(self.terminal["locales"])

    @property
    def icons(self) -> set:
        return set(self.terminal["icons"])

    def slot(self, revision: str) -> ScreenSlot:
        return self.slots.setdefault(revision, ScreenSlot(revision=revision))

    def last_provable(self) -> Optional[ScreenSlot]:
        """失联后的最后可证实状态：取回报时间最大的槽位（含冻结快照）。"""
        candidates = [s for s in self.slots.values() if s.provable_state() != "unknown"]
        if not candidates:
            return None

        def stamp(slot: ScreenSlot) -> int:
            for value in (slot.displaying, slot.installed, slot.received):
                if value is not None:
                    return value
            if slot.frozen and slot.frozen.get("at") is not None:
                return slot.frozen["at"]
            return -1

        return max(candidates, key=stamp)


@dataclass
class Decision:
    """发布/撤回/封闭事件的处置记录。"""

    at: int
    kind: str
    revision: Optional[str]
    status: str
    reasons: list = field(default_factory=list)
    affected_screens: list = field(default_factory=list)
    detail: dict = field(default_factory=dict)


@dataclass
class DesiredContent:
    """某终端在某时刻对某一方向应当显示的内容。"""

    mode: ScreenMode
    route_id: str
    revisions: list            # 该指引依赖的全部内容版本
    locales: Optional[dict] = None
    icon: Optional[str] = None
    alternate: Optional[str] = None
    in_transit: bool = False   # 在途提示（走完/退出），入口回撤不清此内容
    stop_entry: bool = False   # 停止新进入者，但在途继续

    def signature(self) -> str:
        return "|".join([
            self.mode.value,
            self.route_id,
            ",".join(self.revisions),
            self.icon or "",
            self.alternate or "",
            "T" if self.in_transit else "F",
            "T" if self.stop_entry else "F",
        ])


STOP_REVISION_PREFIX = "stop/"


class DispatchEngine:
    """根据资料与事件计算各终端期望配置，并解释每次发布的成败。"""

    def __init__(self, facts: dict, clock: int = 0):
        self.facts = facts
        self.clock = clock
        self.terminals = {t["id"]: t for t in facts["terminals"]}
        self.routes = {r["id"]: r for r in facts["routes"]}
        self.tickets: list[Ticket] = []
        self.closures: list[Closure] = []
        self.screens: dict[str, ScreenState] = {
            t["id"]: ScreenState(terminal=t, site=t["site"]) for t in facts["terminals"]
        }
        self.decisions: list[Decision] = []
        # 封闭发生时无法证明已撤下的入口：entry_screen_id -> 原因
        self.pending_signs: dict[str, dict] = {}
        # 已经下发但当时离线/停用、待重连补发的意图：screen_id -> (at, content)
        self.pending_delivery: dict[str, tuple] = {}
        self.ops = facts.get("operators", {})

    # ------------------------------------------------------------------
    # 基础几何
    # ------------------------------------------------------------------
    def route_links(self, route_id: str) -> list[str]:
        return list(self.routes[route_id]["links"])

    def site_after_link(self, route_id: str, link: str) -> str:
        route = self.routes[route_id]
        index = route["links"].index(link)
        return route["sites"][index + 1]

    def site_before_link(self, route_id: str, link: str) -> str:
        route = self.routes[route_id]
        index = route["links"].index(link)
        return route["sites"][index]

    def link_closed(self, link: str, now: Optional[int] = None) -> bool:
        now = self.clock if now is None else now
        return any(c.link == link and c.covers(now) for c in self.closures)

    def screens_for_route(self, route_id: str) -> list[ScreenState]:
        """共址设备可分管不同方向；这里返回位于该路线各站点的全部终端，
        具体是否负责该方向在渲染时按站点位置判定。"""
        route_sites = self.routes[route_id]["sites"]
        return [s for s in self.screens.values() if s.site in route_sites]

    def entry_screens(self, route_id: str) -> list[ScreenState]:
        entry_site = self.routes[route_id]["sites"][0]
        return [s for s in self.screens.values() if s.site == entry_site]

    # ------------------------------------------------------------------
    # 发布
    # ------------------------------------------------------------------
    def publish(self, event: dict) -> Decision:
        """核对一次路线修改涉及的路线及终端，入库并给出处置结论。"""
        now = event["at"]
        revision = event["revision"]
        operator = event["operator"]
        route_id = event["route"]
        hours = tuple(event["hours"])

        reasons = []
        if route_id not in self.routes:
            reasons.append(f"路线 {route_id} 不存在")
        if any(t.revision == revision and not t.withdrawn for t in self.tickets):
            reasons.append(f"版本 {revision} 已存在且未撤回")
        start, end = hours
        if not (0 <= start < end):
            reasons.append(f"生效时段 {list(hours)} 非法（要求左闭右开）")

        link = event.get("link")
        ticket_type = self._classify_ticket(event)
        if reasons:
            decision = Decision(now, "publish", revision, PublishStatus.BLOCKED.value, reasons)
            return decision

        if link is not None and link not in self.routes[route_id]["links"]:
            reasons.append(f"路段 {link} 不属于路线 {route_id}")
        if not self._operator_may_publish(operator, route_id, link, ticket_type):
            reasons.append(
                f"机构 {operator} 无权发布该内容（改动/译文/时段分属不同维护方）"
            )
        if reasons:
            decision = Decision(now, "publish", revision, PublishStatus.BLOCKED.value, reasons)
            return decision

        ticket = Ticket(
            revision=revision,
            operator=operator,
            route_id=route_id,
            at=now,
            hours=hours,
            link=link,
            ticket_type=ticket_type,
            alternate=event.get("alternate"),
            icon=event.get("icon"),
            locales=event.get("locales"),
            accessible=event.get("accessible"),
            stop_entry_at=event.get("stop_entry_at"),
        )
        self.tickets.append(ticket)

        targets = self._target_screens(ticket)
        renderable = [s for s in targets if self._can_render_ticket(s, ticket)]
        missing = [
            {"screen": s.id, "reason": self._render_blocker(s, ticket)}
            for s in targets if s not in renderable
        ]
        active = start <= now < end

        if not renderable:
            status = PublishStatus.STAGED.value
        elif missing:
            status = PublishStatus.PARTIAL.value
        elif active:
            status = PublishStatus.EFFECTIVE.value
        else:
            status = PublishStatus.STAGED.value

        decision = Decision(
            now, "publish", revision, status,
            reasons=[m["reason"] for m in missing],
            affected_screens=[s.id for s in targets],
            detail={
                "ticket_type": ticket_type.value,
                "renderable": [s.id for s in renderable],
                "missing": missing,
                "active_window": list(hours),
                "link": link,
            },
        )
        return decision

    @staticmethod
    def _classify_ticket(event: dict) -> TicketType:
        if event.get("stop_entry_at") is not None or (
            "link" not in event and "locales" not in event and "icon" not in event
            and "alternate" not in event
        ):
            return TicketType.HOURS
        if "locales" in event and "icon" not in event and "alternate" not in event:
            return TicketType.TRANSLATION
        return TicketType.CHANGE

    def _operator_may_publish(self, operator, route_id, link, ticket_type) -> bool:
        changes = self.ops.get("changes", {})
        translations = self.ops.get("translations", {})
        entry_map = self.ops.get("entry", {})
        if ticket_type is TicketType.HOURS:
            entry_site = self.routes[route_id]["sites"][0]
            return entry_map.get(entry_site) == operator
        owner = changes.get(link) if ticket_type is TicketType.CHANGE else translations.get(link)
        return owner == operator

    def _target_screens(self, ticket: Ticket) -> list[ScreenState]:
        """票券面向该方向上从入口到该路段（含）的全部终端。"""
        route = self.routes[ticket.route_id]
        if ticket.link is None:
            # 路线级时段票：入口及各中间指路点都需要知道停引导时点
            sites = route["sites"][:-1]
        else:
            index = route["links"].index(ticket.link)
            sites = route["sites"][: index + 1]
        return [s for s in self.screens.values() if s.site in sites]

    def _can_render_ticket(self, screen: ScreenState, ticket: Ticket) -> bool:
        return self._render_blocker(screen, ticket) is None

    def _render_blocker(self, screen: ScreenState, ticket: Ticket) -> Optional[str]:
        if ticket.ticket_type is TicketType.CHANGE and ticket.icon:
            if ticket.icon not in screen.icons:
                return f"{screen.id} 不支持图形 {ticket.icon}"
        if ticket.locales:
            missing = sorted(set(ticket.locales) - screen.locales)
            if missing:
                return f"{screen.id} 缺少译文语言 {','.join(missing)}"
        if ticket.ticket_type is TicketType.TRANSLATION:
            # 译文票要与通行改动拼合后才有完整指引；拼合失败在渲染期体现，
            # 这里仅核对语言能力。
            pass
        return None

    # ------------------------------------------------------------------
    # 撤回：一方只能撤回自己尚未生效的改动
    # ------------------------------------------------------------------
    def withdraw(self, event: dict) -> Decision:
        now = event["at"]
        revision = event["revision"]
        operator = event["operator"]
        ticket = next((t for t in self.tickets if t.revision == revision), None)
        if ticket is None:
            return self._block(now, "withdraw", revision, f"版本 {revision} 不存在")
        if ticket.operator != operator:
            return self._block(
                now, "withdraw", revision,
                f"机构 {operator} 只能撤回自己的改动（{revision} 属于 {ticket.operator}）",
            )
        if ticket.withdrawn:
            return self._block(now, "withdraw", revision, f"版本 {revision} 已撤回")
        start, end = ticket.hours
        if now >= start:
            return self._block(
                now, "withdraw", revision,
                f"版本 {revision} 已到生效时点 {start}，不能再撤回（在途提示另行保留）",
            )
        ticket.withdrawn = True
        ticket.withdrawn_at = now
        targets = self._target_screens(ticket)
        return Decision(
            now, "withdraw", revision, "withdrawn",
            affected_screens=[s.id for s in targets],
            detail={"hours": list(ticket.hours)},
        )

    def _block(self, now, kind, revision, reason) -> Decision:
        decision = Decision(now, kind, revision, PublishStatus.BLOCKED.value, [reason])
        return decision

    # ------------------------------------------------------------------
    # 封闭
    # ------------------------------------------------------------------
    def close(self, event: dict) -> Decision:
        now = event["at"]
        link = event["link"]
        if link not in {l for r in self.routes.values() for l in r["links"]}:
            return self._block(now, "closure", None, f"路段 {link} 不存在")
        closure = Closure(
            link=link, from_at=event["from_at"], until=event["until"],
            operator=event["operator"],
        )
        self.closures.append(closure)

        # 仍在线的终端：由期望配置重算立即撤下经过封闭段的引导；
        # 入口若此刻离线，无法证明它已撤下，必须留给现场补挂标牌。
        for route_id, route in self.routes.items():
            if link not in route["links"]:
                continue
            for screen in self.entry_screens(route_id):
                if screen.connectivity is not Connectivity.ONLINE:
                    self.pending_signs[screen.id] = {
                        "screen": screen.id,
                        "site": screen.site,
                        "route": route_id,
                        "link": link,
                        "reason": "封闭发生时终端离线/停用，无法证明入口已停止引导",
                        "since": now,
                        "operator": self.ops.get("field", {}).get(screen.site),
                    }
        return Decision(
            now, "closure", None, "closed",
            affected_screens=[s.id for s in self.screens.values()],
            detail={"link": link, "from_at": closure.from_at, "until": closure.until},
        )

    # ------------------------------------------------------------------
    # 终端回报：received / installed / displaying 与线路事件
    # ------------------------------------------------------------------
    def device_report(self, event: dict) -> Decision:
        now = event["at"]
        screen_id = event["screen"]
        if screen_id not in self.screens:
            return self._block(now, "device_report", None, f"未知终端 {screen_id}")
        screen = self.screens[screen_id]
        state = event["state"]
        revision = event["revision"]
        slot = screen.slot(revision)
        slot.usable_until = event.get("usable_until")
        slot.epoch = screen.epoch
        if state == "received":
            slot.received = now
        elif state == "installed":
            slot.installed = now
        elif state == "displaying":
            slot.displaying = now
        else:
            return self._block(now, "device_report", revision, f"未知回报状态 {state}")
        screen.last_seen = now
        return Decision(
            now, "device_report", revision, state,
            affected_screens=[screen_id],
            detail={"usable_until": slot.usable_until},
        )

    def mark_offline(self, event: dict) -> Decision:
        now = event["at"]
        screen = self.screens[event["screen"]]
        screen.connectivity = Connectivity.OFFLINE
        screen.offline_since = now
        return Decision(now, "offline", None, "offline", affected_screens=[screen.id])

    def mark_reconnect(self, event: dict) -> Decision:
        now = event["at"]
        screen = self.screens[event["screen"]]
        was_offline = screen.connectivity is Connectivity.OFFLINE
        screen.connectivity = Connectivity.ONLINE
        screen.offline_since = None
        screen.last_seen = now
        if was_offline:
            # 进入新的连接纪元：重连前的安装/显示回执一律不能再证明
            # 当前路线有效，路线恢复只能依据重连后重新下发的配置与回报。
            screen.epoch += 1
            # 旧下发签名失效，使期望配置在重连后重新进入下发流程
            screen.last_sent_signature = None
            for slot in screen.slots.values():
                if slot.epoch < screen.epoch and slot.provable_state() != "unknown":
                    slot.frozen = {
                        "state": slot.provable_state(),
                        "at": slot.displaying or slot.installed or slot.received,
                        "usable_until": slot.usable_until,
                        "epoch": slot.epoch,
                    }
                    slot.received = slot.installed = slot.displaying = None
        return Decision(now, "reconnect", None, "online", affected_screens=[screen.id])

    def field_disable(self, event: dict) -> Decision:
        now = event["at"]
        screen = self.screens[event["screen"]]
        screen.connectivity = Connectivity.DISABLED
        screen.last_seen = now
        return Decision(now, "field_disabled", None, "disabled",
                        affected_screens=[screen.id], detail={"operator": event.get("operator")})

    # ------------------------------------------------------------------
    # 期望配置渲染
    # ------------------------------------------------------------------
    def _baseline_usable(self, now: int) -> bool:
        baseline = self.facts["baseline"]
        return now < baseline["usable_until"]

    def _active_tickets(self, route_id: str, link: str, now: int) -> list[Ticket]:
        return [
            t for t in self.tickets
            if not t.withdrawn and t.route_id == route_id and t.link == link
            and t.hours[0] <= now < t.hours[1]
        ]

    def render_link(self, route_id: str, link: str, now: Optional[int] = None) -> DesiredContent:
        """渲染某路段在当前时刻的指引（供该路段起点的屏幕显示）。"""
        now = self.clock if now is None else now
        route = self.routes[route_id]
        start, end = route["hours"]
        if not (start <= now < end):
            return DesiredContent(ScreenMode.STOP, route_id, [],
                                  stop_entry=False)

        if self.link_closed(link, now):
            # 封闭段：只有有效绕行可继续引导，否则停止引导。
            detour = self._detour_content(route_id, link, now)
            if detour is not None:
                detour.in_transit = True
                return detour
            return DesiredContent(ScreenMode.STOP, route_id, [], in_transit=True)

        active = self._active_tickets(route_id, link, now)
        changes = [t for t in active if t.ticket_type is TicketType.CHANGE]
        if changes:
            # 同一路段同时刻至多一条通行改动；绕行内容需要译文拼合。
            change = changes[0]
            content = self._assemble(route_id, link, change, now)
            if content is not None:
                return content
            # 改动在但译文拼合不完整：若现场仍可按旧路通行，退回基线；
            # 基线也失效时才停止引导。封闭期已在上面分支处理为 STOP。
        if self._baseline_usable(now):
            baseline = self.facts["baseline"]
            return DesiredContent(
                ScreenMode.GUIDE, route_id, [baseline["revision"]],
                locales=dict(baseline["locales"]),
            )
        return DesiredContent(ScreenMode.STOP, route_id, [])

    def render_for_screen(self, screen: ScreenState, route_id: str, link: str,
                          now: Optional[int] = None) -> DesiredContent:
        """在路段通用指引之上按该屏能力做裁剪。

        老屏可保留适用于其能力且现场仍能通行的旧指引（基线）；
        若它表达不了当前唯一有效的绕行（封闭期），则退到停止引导。
        """
        now = self.clock if now is None else now
        desired = self.render_link(route_id, link, now)
        if desired.mode is not ScreenMode.GUIDE or self._screen_supports(screen, desired):
            return desired
        # 能力表达不了的情形：封闭期禁止保留旧指引；非封闭期回退基线
        if self.link_closed(link, now):
            return DesiredContent(ScreenMode.STOP, route_id, desired.revisions,
                                  in_transit=True)
        if self._baseline_usable(now):
            baseline = self.facts["baseline"]
            return DesiredContent(
                ScreenMode.GUIDE, route_id, [baseline["revision"]],
                locales=dict(baseline["locales"]),
            )
        return DesiredContent(ScreenMode.STOP, route_id, desired.revisions)

    def _detour_content(self, route_id: str, link: str, now: int) -> Optional[DesiredContent]:
        """封闭段上的有效绕行：通行改动 + 译文必须同时齐备且在时段内。"""
        active = self._active_tickets(route_id, link, now)
        change = next((t for t in active if t.ticket_type is TicketType.CHANGE and t.alternate),
                      None)
        if change is None:
            return None
        return self._assemble(route_id, link, change, now, detour=True)

    def _assemble(self, route_id, link, change: Ticket, now: int,
                  detour: bool = False) -> Optional[DesiredContent]:
        """把通行改动与本地译文拼成完整指引；缺译文则失败。"""
        revisions = [change.revision]
        locales = dict(change.locales or {})
        translation = next(
            (t for t in self._active_tickets(route_id, link, now)
             if t.ticket_type is TicketType.TRANSLATION),
            None,
        )
        if translation is not None:
            locales.update(translation.locales or {})
            revisions.append(translation.revision)
        if not locales:
            return None
        return DesiredContent(
            ScreenMode.GUIDE, route_id, revisions,
            locales=locales, icon=change.icon, alternate=change.alternate,
            in_transit=detour,
        )

    def render_screen(self, screen: ScreenState, now: Optional[int] = None) -> dict:
        """计算一个终端对其负责的全部方向的期望内容。"""
        now = self.clock if now is None else now
        result = {}
        for route_id, route in self.routes.items():
            if screen.site not in route["sites"]:
                continue
            index = route["sites"].index(screen.site)
            if index == len(route["sites"]) - 1:
                # 终点屏不引导该方向
                result[route_id] = DesiredContent(ScreenMode.NONE, route_id, [])
                continue
            link = route["links"][index]
            content = self.render_for_screen(screen, route_id, link, now)
            if index == 0:
                # 入口只有在该方向连续后续指路点都能给出相容指引时才可引导入内
                admittance = self.entry_admittance(route_id, now)
                if not admittance["admit"] and content.mode is ScreenMode.GUIDE:
                    content = DesiredContent(
                        ScreenMode.STOP, route_id, content.revisions,
                        in_transit=content.in_transit,
                    )
                # 入口还要执行路线级停引导时点
                hours_ticket = self._active_hours_ticket(route_id, now)
                if hours_ticket and hours_ticket.stop_entry_at is not None \
                        and now >= hours_ticket.stop_entry_at:
                    content = DesiredContent(
                        ScreenMode.STOP, route_id,
                        content.revisions + [hours_ticket.revision],
                        stop_entry=True,
                    )
            result[route_id] = content
        return result

    def _active_hours_ticket(self, route_id: str, now: int) -> Optional[Ticket]:
        active = [
            t for t in self.tickets
            if not t.withdrawn and t.route_id == route_id and t.link is None
            and t.hours[0] <= now < t.hours[1]
        ]
        return active[-1] if active else None

    # ------------------------------------------------------------------
    # 入口相容性：连续后续指路点都能给出相容指引才可引导入内
    # ------------------------------------------------------------------
    def entry_admittance(self, route_id: str, now: Optional[int] = None) -> dict:
        """入口相容性判定（独立计算，不依赖 render_screen，避免递归重入）。"""
        now = self.clock if now is None else now
        route = self.routes[route_id]
        blockers = []
        chain = []
        # 入口自身 + 每个后续指路点（不含终点）
        for index, site in enumerate(route["sites"][:-1]):
            link = route["links"][index]
            generic = self.render_link(route_id, link, now)
            if index == 0:
                hours_ticket = self._active_hours_ticket(route_id, now)
                if hours_ticket and hours_ticket.stop_entry_at is not None \
                        and now >= hours_ticket.stop_entry_at:
                    blockers.append({
                        "site": site, "reason": "已到停引导新进入者时点",
                        "revision": hours_ticket.revision,
                    })
                    continue
            # 各屏按自身能力渲染；至少一块屏能引导，该指路点即相容
            site_screens = [s for s in self.screens.values() if s.site == site]
            per_screen = {s.id: self.render_for_screen(s, route_id, link, now)
                          for s in site_screens}
            capable = self._capable_screens(site, route_id, generic, now,
                                            entry=(index == 0),
                                            per_screen=per_screen)
            chain.append({"site": site, "link": link,
                          "generic_mode": generic.mode.value,
                          "capable": [s.id for s in capable]})
            if not capable:
                blockers.append({
                    "site": site,
                    "reason": "没有终端能给出相容指引"
                    if generic.mode is ScreenMode.GUIDE else "该路段只能停止引导",
                })
        return {
            "route": route_id,
            "at": now,
            "admit": not blockers,
            "blockers": blockers,
            "chain": chain,
        }

    def _capable_screens(self, site, route_id, desired: DesiredContent, now: int,
                         entry: bool, per_screen: dict | None = None) -> list[ScreenState]:
        screens = [s for s in self.screens.values() if s.site == site]
        capable = []
        for screen in screens:
            if screen.connectivity is Connectivity.DISABLED:
                continue
            own = (per_screen or {}).get(screen.id, desired)
            if own.mode is ScreenMode.STOP:
                # 停止引导是任何屏都能表达的兜底；离线屏须看其最后承诺
                if self._offline_commitment_covers(screen, own, now):
                    capable.append(screen)
                elif screen.connectivity is Connectivity.ONLINE:
                    capable.append(screen)
                continue
            if not self._screen_supports(screen, own):
                continue
            if screen.connectivity is Connectivity.ONLINE:
                # 从未失联过的在线屏可立即接收当前配置，视为相容；
                # 经历过离线重连的屏，必须在当前纪元拿到新证据，
                # 旧安装回执不能证明当前路线仍然有效。
                if screen.epoch == 0 or self._has_current_epoch_evidence(screen, own):
                    capable.append(screen)
            else:
                # 离线：离线前已承诺的有效时段仍计入
                if self._offline_commitment_covers(screen, own, now):
                    capable.append(screen)
        return capable

    def _has_current_epoch_evidence(self, screen: ScreenState,
                                    desired: DesiredContent) -> bool:
        """当前连接纪元内是否已有该内容的下发或回报证据。"""
        if screen.last_sent_at is not None and screen.last_sent_epoch == screen.epoch:
            return True
        return any(
            slot.epoch == screen.epoch and slot.provable_state() != "unknown"
            for slot in screen.slots.values()
        )

    @staticmethod
    def _screen_supports(screen: ScreenState, desired: DesiredContent) -> bool:
        if desired.icon and desired.icon not in screen.icons:
            return False
        if desired.locales:
            # 语言可子集化：屏能显示其中至少一种本地译文即可
            if not (set(desired.locales) & screen.locales):
                return False
        return True

    def _offline_commitment_covers(self, screen: ScreenState, desired: DesiredContent,
                                   now: int) -> bool:
        """离线终端只能按最后收到的有效期行动。"""
        if screen.connectivity is Connectivity.ONLINE:
            return True
        last = self._current_epoch_slot(screen)
        if last is None or last.usable_until is None:
            return False
        if now >= last.usable_until:
            return False  # 超过该时段必须退到停止引导
        if desired.mode is ScreenMode.STOP:
            return True
        return last.revision in desired.revisions

    def _current_epoch_slot(self, screen: ScreenState) -> Optional[ScreenSlot]:
        """当前连接纪元内最后可证实的槽位（旧纪元回报不作数）。"""
        candidates = [
            s for s in screen.slots.values()
            if s.provable_state() != "unknown" and s.epoch == screen.epoch
        ]
        if not candidates:
            return None

        def stamp(slot: ScreenSlot) -> int:
            for value in (slot.displaying, slot.installed, slot.received):
                if value is not None:
                    return value
            if slot.frozen and slot.frozen.get("at") is not None:
                return slot.frozen["at"]
            return -1

        return max(candidates, key=stamp)

    # ------------------------------------------------------------------
    # 方向查询：何时可以开始引导、还欠哪些终端确认
    # ------------------------------------------------------------------
    def direction_status(self, route_id: str) -> dict:
        now = self.clock
        admittance = self.entry_admittance(route_id, now)
        next_start = self._next_guidance_time(route_id, now)
        pending = self._pending_confirmations(route_id)
        return {
            "route": route_id,
            "at": now,
            "can_guide_now": admittance["admit"],
            "blockers": admittance["blockers"],
            "next_guidance_at": next_start,
            "pending_confirmations": pending,
        }

    def _next_guidance_time(self, route_id: str, now: int) -> Optional[int]:
        """扫描今天剩余时间，找最早可引导入内的分钟。"""
        route = self.routes[route_id]
        upper = max(route["hours"][1], now + 1)
        upper = min(upper, 1440)
        for minute in range(now + 1, upper):
            if self.entry_admittance(route_id, minute)["admit"]:
                return minute
        return None

    def _pending_confirmations(self, route_id: str) -> list:
        """已下发但终端尚未证实显示的内容版本。"""
        pending = []
        for screen in self.screens_for_route(route_id):
            rendered = self.render_screen(screen)
            desired = rendered.get(route_id)
            if desired is None or desired.mode is ScreenMode.NONE:
                continue
            for revision in desired.revisions:
                slot = screen.slots.get(revision)
                if slot is None or slot.displaying is None:
                    pending.append({
                        "screen": screen.id,
                        "revision": revision,
                        "last_provable": screen.last_provable().provable_state()
                        if screen.last_provable() else "unknown",
                        "connectivity": screen.connectivity.value,
                    })
        return pending

    # ------------------------------------------------------------------
    # 在途提示：入口回撤/停引导不清此内容
    # ------------------------------------------------------------------
    def in_transit_notices(self, route_id: str) -> list:
        """各中间指路点当前给在途旅客的走完/退出提示。"""
        now = self.clock
        route = self.routes[route_id]
        notices = []
        for index, site in enumerate(route["sites"][1:-1], start=1):
            link = route["links"][index]
            desired = self.render_link(route_id, link, now)
            screens = [s for s in self.screens.values() if s.site == site]
            notices.append({
                "site": site,
                "link": link,
                "mode": desired.mode.value,
                "in_transit": desired.in_transit,
                "alternate": desired.alternate,
                "revisions": desired.revisions,
                "screens": [
                    {
                        "id": s.id,
                        "connectivity": s.connectivity.value,
                        "last_provable": s.last_provable().provable_state()
                        if s.last_provable() else "unknown",
                        "usable_until": (s.last_provable().usable_until
                                         if s.last_provable() else None),
                    } for s in screens
                ],
            })
        return notices

    # ------------------------------------------------------------------
    # 下发计划：只在期望内容变化时生成下发意图
    # ------------------------------------------------------------------
    def delivery_plan(self) -> list:
        """重算全部在线终端的期望配置，返回需要下发的意图列表。"""
        plan = []
        for screen in self.screens.values():
            rendered = self.render_screen(screen)
            signature = self._combined_signature(rendered)
            if signature == screen.last_sent_signature:
                continue
            payload = self._build_payload(screen, rendered)
            if screen.connectivity is Connectivity.ONLINE:
                screen.last_sent_signature = signature
                screen.last_sent_at = self.clock
                screen.last_sent_epoch = screen.epoch
                plan.append({"screen": screen.id, "payload": payload, "deferred": False})
                self.pending_delivery.pop(screen.id, None)
            else:
                self.pending_delivery[screen.id] = (self.clock, payload)
                plan.append({"screen": screen.id, "payload": payload, "deferred": True})
        return plan

    def flush_pending(self) -> list:
        """重连后补发滞留配置（旧回执不参与，内容按当前时刻重算）。"""
        plan = []
        for screen_id, (at, old_payload) in list(self.pending_delivery.items()):
            screen = self.screens[screen_id]
            if screen.connectivity is not Connectivity.ONLINE:
                continue
            rendered = self.render_screen(screen)
            signature = self._combined_signature(rendered)
            payload = self._build_payload(screen, rendered)
            screen.last_sent_signature = signature
            screen.last_sent_at = self.clock
            screen.last_sent_epoch = screen.epoch
            plan.append({"screen": screen_id, "payload": payload, "deferred": False})
            del self.pending_delivery[screen_id]
        return plan

    @staticmethod
    def _combined_signature(rendered: dict) -> str:
        parts = []
        for route, content in sorted(rendered.items()):
            parts.append(route + ":" + content.signature())
        return ";".join(parts)

    def _build_payload(self, screen: ScreenState, rendered: dict) -> dict:
        directions = {}
        stop_revisions = []
        for route_id, content in rendered.items():
            if content.mode is ScreenMode.NONE:
                continue
            item = {
                "mode": content.mode.value,
                "revisions": list(content.revisions),
                "in_transit": content.in_transit,
                "stop_entry": content.stop_entry,
            }
            if content.mode is ScreenMode.GUIDE:
                item.update({
                    "locales": content.locales,
                    "icon": content.icon,
                    "alternate": content.alternate,
                    "usable_window": self._content_window(screen, route_id, content),
                })
                # 老屏能力裁剪：能力表达不了的语言/图形在屏端兜底为停止引导，
                # 控制端同时标注不兼容，便于现场解释“为何只在部分区域生效”。
                item["capability_warnings"] = self._capability_warnings(screen, content)
            else:
                stop_rev = f"{STOP_REVISION_PREFIX}{route_id}@{self.clock}"
                item["stop_revision"] = stop_rev
                item["revisions"] = [stop_rev]
                stop_revisions.append(stop_rev)
            directions[route_id] = item
        return {
            "screen": screen.id,
            "issued_at": self.clock,
            "directions": directions,
            "fallback": "stop",
            "stop_revisions": stop_revisions,
        }

    @staticmethod
    def _capability_warnings(screen: ScreenState, content: DesiredContent) -> list:
        warnings = []
        if content.icon and content.icon not in screen.icons:
            warnings.append(f"不支持图形 {content.icon}")
        if content.locales:
            missing = sorted(set(content.locales) - screen.locales)
            if missing:
                # 只是少显示几种译文，不阻断；完全无交集才由相容性判定阻断
                warnings.append(f"不显示语言 {','.join(missing)}（按支持语言子集呈现）")
        return warnings

    def _content_window(self, screen: ScreenState, route_id: str,
                        content: DesiredContent) -> list:
        """该指引内容自身的固定生效窗口（左闭右开），不随下发时刻漂移。

        取路线时段、各内容票券时段、封闭段和基线有效期的交集边界；
        屏端超过右端点必须本地退到停止引导，即使仍处离线。
        """
        route = self.routes[route_id]
        start, end = route["hours"]
        if content.revisions:
            baseline_rev = self.facts["baseline"]["revision"]
            for ticket in self.tickets:
                if ticket.revision in content.revisions:
                    end = min(end, ticket.hours[1])
                    start = max(start, ticket.hours[0])
            if baseline_rev in content.revisions:
                end = min(end, self.facts["baseline"]["usable_until"])
        for closure in self.closures:
            if closure.link in route["links"]:
                # 封闭段把窗口在封闭起点处截断（解封后的有效性由后续配置给出）
                if start < closure.from_at < end:
                    end = min(end, closure.from_at)
        # 路线级停引导时点同样约束入口内容
        hours_ticket = self._active_hours_ticket(route_id, self.clock)
        if hours_ticket and hours_ticket.stop_entry_at is not None:
            rendered = self.render_screen(screen).get(route_id)
            if rendered and rendered.stop_entry:
                end = min(end, hours_ticket.stop_entry_at)
        return [start, max(start + 1, end)]

    # ------------------------------------------------------------------
    # 事件总入口与时钟
    # ------------------------------------------------------------------
    def ingest(self, event: dict) -> Decision:
        kind = event["kind"]
        if kind == "publish":
            decision = self.publish(event)
        elif kind == "withdraw":
            decision = self.withdraw(event)
        elif kind == "closure":
            decision = self.close(event)
        elif kind == "device_report":
            decision = self.device_report(event)
        elif kind == "offline":
            decision = self.mark_offline(event)
        elif kind == "reconnect":
            decision = self.mark_reconnect(event)
        elif kind == "field_disabled":
            decision = self.field_disable(event)
        elif kind == "query":
            decision = Decision(event["at"], "query", None, "query",
                                detail=self.answer_query(event))
        else:
            raise ValueError(f"未知事件类型 {kind}")
        # 全部事件进入决策历史，供现场追溯“为何被挡住/仅部分生效”
        self.decisions.append(decision)
        return decision

    def answer_query(self, event: dict) -> dict:
        route_id = event["direction"]
        return {
            "site": event.get("site"),
            "direction_status": self.direction_status(route_id),
            "in_transit": self.in_transit_notices(route_id),
            "entry": self.entry_admittance(route_id),
        }

    def advance(self, now: int) -> list:
        """推进时钟并返回因时刻变化（到期/解封/时段切换）产生的下发意图。"""
        if now < self.clock:
            raise ValueError("时钟只能前进")
        self.clock = now
        return self.delivery_plan()

    # ------------------------------------------------------------------
    # 现场视角的汇总
    # ------------------------------------------------------------------
    def overview(self) -> dict:
        screens_view = []
        for screen in self.screens.values():
            last = screen.last_provable()
            screens_view.append({
                "id": screen.id,
                "site": screen.site,
                "operator": screen.terminal["operator"],
                "connectivity": screen.connectivity.value,
                "epoch": screen.epoch,
                "last_provable": {
                    "state": last.provable_state() if last else "unknown",
                    "revision": last.revision if last else None,
                    "usable_until": last.usable_until if last else None,
                    "frozen": last.frozen if last else None,
                },
                "slots": [
                    {
                        "revision": slot.revision,
                        "received": slot.received,
                        "installed": slot.installed,
                        "displaying": slot.displaying,
                        "usable_until": slot.usable_until,
                        "epoch": slot.epoch,
                        "frozen": slot.frozen,
                    }
                    for slot in sorted(screen.slots.values(),
                                       key=lambda s: s.revision)
                ],
            })
        return {
            "at": self.clock,
            "routes": [self.direction_status(rid) for rid in self.routes],
            "screens": screens_view,
            "pending_signs": list(self.pending_signs.values()),
            "decisions": [
                {
                    "at": d.at, "kind": d.kind, "revision": d.revision,
                    "status": d.status, "reasons": d.reasons,
                    "affected_screens": d.affected_screens, "detail": d.detail,
                }
                for d in self.decisions
            ],
            "tickets": [
                {
                    "revision": t.revision, "operator": t.operator,
                    "route": t.route_id, "link": t.link,
                    "type": t.ticket_type.value, "hours": list(t.hours),
                    "withdrawn": t.withdrawn,
                }
                for t in self.tickets
            ],
            "closures": [
                {"link": c.link, "from_at": c.from_at, "until": c.until}
                for c in self.closures
            ],
        }
