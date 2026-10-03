"""疏散通道编排核心算法。

时间语义
--------
* 当前分钟从 0 起按整数推进。
* 一组在第 ``t`` 分钟进入通行分钟数为 ``w`` 的通道，则该组占用此通道的
  分钟区间为左闭右开 ``[t, t+w)``，并在第 ``t+w`` 分钟到达通道另一端。
* 封闭时段同样为左闭右开 ``[a, b)``：只要 ``[t, t+w)`` 与某个 ``[a,b)``
  相交（即 ``t < b 且 a < t+w``），就不允许在第 ``t`` 分钟进入该通道；
  最早可尝试进入的下一分钟为 ``b``。
* 容量按每分钟核算：同一通道同一分钟，所有在途组（已出发固定组 + 本轮
  已排入的待出发组）人数之和不得超过该通道该分钟的有效上限。
* 容量管制：可为通道登记左闭右开的管制时段 ``[a, b)`` 及时段内每分钟
  上限；同一通道的管制时段互不重叠。管制时段内有效上限取
  ``min(原容量, 管制上限)``（管制只收紧不放松），时段外仍为原容量。
* 烟雾暴露：可为通道登记左闭右开的烟雾暴露时段 ``[a, b)`` 及时段内每
  分钟暴露值（非负），同一通道的暴露时段互不重叠；未登记的分钟暴露值
  为零，在节点等待不计暴露。可登记非负累计暴露上限的组，其沿路线每
  分钟占用通道的暴露值之和不得超过上限（节点等待可让烟雾消散后通行）；
  未设上限的组不受此约束。
* 节点滞留限流：可为**非出口节点**登记左闭右开的限流时段 ``[a, b)`` 及
  时段内每分钟最多停留人数，同一节点的限流时段互不重叠；未登记的分钟
  不限额。一组从前一通道离开到进入下一通道之间按人数占用该节点每分钟
  名额；**出发前在起点等待不占名额，通道在途与抵达出口后也不计**。
  ``fixed_node_load`` 存放 (节点, 分钟) -> 已固定（已出发）组在该节点
  的滞留人数，搜索中的组逐组累加到同一份字典（与通道容量的处理方式一
  致）。限流只覆盖中途等待，故从起点出发的第一段不做节点名额检查。

择优规则
--------
每组在各自允许出发的时间范围内寻找：到达分钟最早；同到达时出口编号最小；
同出口同到达时通道编号序列字典序最小。由于通道在节点处允许等待（等待由
封闭/容量/烟雾/节点滞留限流迫使），边到达时间关于进入时间满足 FIFO，因此
用时变最短路求解。为正确处理"到达相同、通道序列字典序更小"、"晚出发但
累计暴露更低"以及"早到但需占用拥挤节点等待、晚到反而可立即通行"的并列
情形，每个节点保留 (到达分钟, 通道序列, 累计暴露) 的 Pareto 标签集；对
同一条边，除最早可进入分钟外，还在暴露取值发生变化或节点等待名额可行性
发生变化的每个区间尝试延后进入（等待烟雾消散 / 避开限流分钟）。
"""
from __future__ import annotations

import heapq
from dataclasses import dataclass, field
from typing import Optional

# 单节点标签数安全上限（演示规模远不会触及）
MAX_LABELS_PER_NODE = 20000


@dataclass(frozen=True)
class Edge:
    id: int
    src: int
    dst: int
    minutes: int
    capacity: int


@dataclass
class World:
    edges: dict[int, Edge]
    adjacency: dict[int, list[int]]
    exits: list[int]
    # edge_id -> 已按开始分钟升序排列的 [(a, b), ...]
    closures: dict[int, list[tuple[int, int]]] = field(default_factory=dict)
    # (edge_id, minute) -> 已固定（已出发）组在该分钟占用的总人数
    fixed_load: dict[tuple[int, int], int] = field(default_factory=dict)
    # edge_id -> 已按开始分钟升序排列、互不重叠的 [(a, b, 时段内上限), ...]
    capacity_controls: dict[int, list[tuple[int, int, int]]] = \
        field(default_factory=dict)
    # edge_id -> 已按开始分钟升序排列、互不重叠的
    # [(a, b, 时段内每分钟暴露值), ...]；未列入的分钟暴露值为 0
    smoke_windows: dict[int, list[tuple[int, int, int]]] = \
        field(default_factory=dict)
    # node_id -> 已按开始分钟升序排列、互不重叠的
    # [(a, b, 时段内每分钟最多停留人数), ...]；仅非出口节点可登记
    node_controls: dict[int, list[tuple[int, int, int]]] = \
        field(default_factory=dict)
    # (node_id, minute) -> 已固定（已出发）组在该节点计划滞留的总人数。
    # 同一轮重排中，已排入组的中途等待也逐组累加进来（贪婪口径，与
    # fixed_load 相同）；出发前起点等待不计入。
    fixed_node_load: dict[tuple[int, int], int] = field(default_factory=dict)
    # 本次搜索的起点（origin）：起点等待不占节点名额，需要等待名额的
    # 只是从前一通道到达后、进入下一通道前的中途节点。节点滞留改编以
    # 滞留节点为起点重找余程，该节点的等待此时属于中途等待，调用方会
    # 通过 start_is_intermediate=True 告知搜索。
    start_node: Optional[int] = None
    start_is_intermediate: bool = False


class PlanningError(Exception):
    """规划过程内部错误（如图结构异常等）。"""


def effective_capacity(base_capacity: int,
                       controls: list[tuple[int, int, int]] | tuple,
                       minute: int) -> int:
    """通道某分钟的有效上限。

    管制时段 ``[a, b)`` 内取 ``min(原容量, 管制上限)``（管制只收紧不
    放松），时段外为原容量。``controls`` 需按开始分钟升序且互不重叠。
    """
    for a, b, cap in controls:
        if a > minute:
            break
        if minute < b:
            return min(base_capacity, cap)
    return base_capacity


def edge_minute_exposure(world: World, edge_id: int, minute: int) -> int:
    """通道某分钟的烟雾暴露值（未登记烟雾时段的分钟为 0）。"""
    for a, b, value in world.smoke_windows.get(edge_id, ()):
        if a > minute:
            break
        if minute < b:
            return value
    return 0


def edge_traversal_exposure(world: World, edge_id: int, entry: int,
                            leave: int) -> int:
    """一次通行 [entry, leave) 在该通道上累计的烟雾暴露。"""
    windows = world.smoke_windows.get(edge_id, ())
    if not windows:
        return 0
    return sum(edge_minute_exposure(world, edge_id, minute)
               for minute in range(entry, leave))


def node_limit(world: World, node_id: int, minute: int) -> Optional[int]:
    """节点某分钟的计划滞留上限；该分钟未登记限流时段时返回 None（不限额）。"""
    for a, b, cap in world.node_controls.get(node_id, ()):
        if a > minute:
            break
        if minute < b:
            return cap
    return None


def node_wait_ok(world: World, node_id: int, arrival: int, entry: int,
                 size: int,
                 extra_load: Optional[dict[tuple[int, int], int]] = None
                 ) -> bool:
    """一组在节点上的等待区间 [arrival, entry) 是否逐分钟满足滞留名额。

    名额按"已固定占用 + 本轮已排入组占用 + 本组人数"逐分钟核算；未登记
    限流的分钟不限额（天然通过）。区间为空（到达即可进入）时也通过。
    ``extra_load`` 为额外计入的占用（节点滞留改编时，本组在滞留节点上
    恢复前必须占用的等待名额——该槽位不能再被本组余程等待复用）。
    """
    if entry <= arrival:
        return True
    for minute in range(arrival, entry):
        cap = node_limit(world, node_id, minute)
        if cap is None:
            continue
        used = world.fixed_node_load.get((node_id, minute), 0)
        if extra_load:
            used += extra_load.get((node_id, minute), 0)
        if used + size > cap:
            return False
    return True


def _node_wait_breakpoints(world: World, node_id: int,
                           arrival: int) -> list[int]:
    """节点等待名额可行性随"进入分钟"发生变化的候选分界点。

    等待区间为 [arrival, entry)：进入分钟从 t 变为 t+1 时，等待区间失去
    分钟 t。限流时段 [a,b) 两侧的阶跃产生候选：a（自 a 起等待不再包含
    a）与 b（等待延伸到 b 才开始再次占用 b）；早于到达分钟的候选无意义。
    """
    points: list[int] = []
    for a, b, _cap in world.node_controls.get(node_id, ()):
        if b > arrival:
            points.append(max(a, arrival))
            points.append(b)
    return sorted(p for p in set(points) if p >= arrival)


def _node_arrival_entry_breakpoints(world: World, node_id: int,
                                    edge_minutes: int, lo: int,
                                    horizon: int) -> list[int]:
    """进入"终点为 node_id 的入边"的候选进入分钟（用于把等待前移到上游）。

    节点限流使得"晚到该节点、到了立即走下一条边"可能优于"早到后在拥挤
    节点上等待"，因此入边除最早可进入分钟外，还需提供使**到达分钟**
    （= 进入分钟 + 通行分钟）落在该节点关键时刻的候选：

    * 本节点各限流时段 [a,b) 的两端 a、b；
    * 各出边封闭结束分钟（封闭结束当分钟即可进入）；
    * 各出边容量管制时段两端 a、b（有效上限阶跃）；
    * 各出边烟雾断点（与零等待结合仍可得到烟雾最优标签）。

    只在该节点登记了限流时段时有意义；调用方负责门控。返回的是入边的
    **进入分钟**（已裁剪到 [lo, horizon - edge_minutes]），可行性（入边
    自身封闭/容量与上游节点等待名额）仍由 :func:`earliest_entry` 复核。
    """
    arrivals: set[int] = set()
    for a, b, _cap in world.node_controls.get(node_id, ()):
        arrivals.update((a, b))
    for eid in world.adjacency.get(node_id, ()):
        for a, b in world.closures.get(eid, ()):
            arrivals.update((a, b))
        for a, b, _cap in world.capacity_controls.get(eid, ()):
            arrivals.update((a, b))
        for p in _entry_breakpoints(world.edges[eid], world):
            arrivals.add(p)
    entries = set()
    for s in arrivals:
        t = s - edge_minutes
        if lo <= t and s <= horizon:
            entries.add(t)
    return sorted(entries)


def _dominates(a_time: int, a_seq: tuple[int, ...], a_exposure: int,
               b_time: int, b_seq: tuple[int, ...], b_exposure: int,
               *, world: "World | None" = None, node: Optional[int] = None,
               size: int = 0, check_node_wait: bool = False,
               extra_load: Optional[dict[tuple[int, int], int]] = None
               ) -> bool:
    """标签 a 是否支配标签 b：到达不晚、通道序列字典序不更大、暴露不更高。

    早到标签要复现晚到标签的未来，必须在该节点上等待 ``[a_time, b_time)``。
    存在节点滞留限流时，这段等待未必逐分钟有名额：若等待区间任一分钟
    已固定/已排入占用加本组人数超过该分钟名额，则 a 不能支配 b
    （``check_node_wait=True`` 且区间非空时额外校验；起点出发前的等待不
    占名额，调用方对根标签传 False）。``a_time == b_time`` 时区间为空，
    退化为原口径。``extra_load`` 为本组恢复前已必须占用的滞留名额。
    """
    if not (a_time <= b_time and a_seq <= b_seq
            and a_exposure <= b_exposure):
        return False
    if check_node_wait and world is not None and a_time < b_time:
        if not node_wait_ok(world, node, a_time, b_time, size, extra_load):
            return False
    return True


def _node_wait_bound(world: World, node_id: int, arrival: int,
                     size: int, horizon: int,
                     extra_load: Optional[dict[tuple[int, int], int]] = None
                     ) -> Optional[int]:
    """返回该标签在该节点上的最晚可进入分钟。

    从 ``arrival`` 起逐分钟考察等待名额：设 m 为第一个"已登记限流且
    已占用 + 本组人数超过名额"的分钟，则等待区间包含 m 即不可行，故可进
    入分钟 t 必须满足 ``t <= m``（区间 [arrival, t) 不含 m）。可行性随
    t 单调：一旦越过 m，更晚进入只会继续包含 m，不可能恢复。不存在这样
    的分钟时返回 None（节点等待不受限）。``extra_load`` 含义同
    :func:`node_wait_ok`（本组恢复前已必须占用的滞留名额）。
    """
    for minute in range(arrival, horizon + 1):
        cap = node_limit(world, node_id, minute)
        if cap is None:
            continue
        used = world.fixed_node_load.get((node_id, minute), 0)
        if extra_load:
            used += extra_load.get((node_id, minute), 0)
        if used + size > cap:
            return minute
    return None


def earliest_entry(edge: Edge, start: int, size: int, world: World,
                   horizon: int, *, use_closures: bool,
                   use_loads: bool) -> Optional[int]:
    """返回最早的可进入分钟 t >= start，使 [t, t+w) 完全可用。

    不可用包括：与封闭时段相交、某一分钟占用达到该分钟有效上限（容量
    管制时段内为 min(原容量, 管制上限)）。均不满足（或无法在 horizon
    前完成通行）时返回 None。烟雾暴露不在此处拦截：是否延后进入以规避
    暴露由 ``entry_options`` 在调用处枚举。
    """
    eid = edge.id
    w = edge.minutes
    t = start
    if t + w > horizon:
        return None
    closures = world.closures.get(eid, ())
    while t + w <= horizon:
        advanced = False
        if use_closures:
            for a, b in closures:
                if b <= t:
                    continue
                if a >= t + w:
                    break  # 闭区间已排序，之后的都在通行窗口之后
                # [t, t+w) 与 [a, b) 相交，跳到封闭结束分钟 b 再试
                t = b
                advanced = True
                break
            if advanced:
                continue
        if use_loads:
            if size > edge.capacity:
                return None  # 单组人数已超通道每分钟容量，永不可通行
            controls = world.capacity_controls.get(eid, ())
            for minute in range(t, t + w):
                cap = effective_capacity(edge.capacity, controls, minute)
                used = world.fixed_load.get((eid, minute), 0)
                if used + size > cap:
                    if size > cap:
                        # 单组人数已超该分钟有效上限（管制收紧所致，此时
                        # size <= 原容量，故该分钟必在管制时段内）：整个
                        # 管制时段均不可进入，直接跳到管制结束分钟再试
                        t = next(b for a, b, _c in controls
                                 if a <= minute < b)
                    else:
                        # 容量冲突的这一分钟不能进入；下一分钟再尝试
                        t = minute + 1
                    advanced = True
                    break
            if advanced:
                continue
        return t
    return None


def _entry_breakpoints(edge: Edge, world: World) -> list[int]:
    """暴露值随进入分钟发生变化的候选分界点集合。

    第 ``t`` 分钟进入、通行 ``w`` 分钟占用 ``{t, ..., t+w-1}``；进入分钟
    从 t 变为 t+1 时失去分钟 t、加入分钟 t+w。烟雾时段 [a,b) 两侧的暴露
    阶跃各产生两个候选边界：

    * a（不再包含 a 的最早进入）与 a-1（仍包含 a 的最晚进入）；
    * b（包含 b 的最早进入）与 b-1（不包含 b 的最晚进入）；
    * 加入侧对应平移 -w：a-w、a-w-1、b-w、b-w-1。

    之后由 :func:`entry_options` 在每个暴露恒定区间取最早可行进入并做
    Pareto 过滤，重复或被支配的点自然丢弃。
    """
    w = edge.minutes
    points: set[int] = set()
    for a, b, _value in world.smoke_windows.get(edge.id, ()):
        points.update((a, a - 1, b, b - 1,
                       a - w, a - w - 1, b - w, b - w - 1))
    return sorted(points)


def entry_options(edge: Edge, start: int, size: int, world: World,
                  horizon: int, *, use_closures: bool, use_loads: bool,
                  use_smoke: bool,
                  wait_bound: Optional[int] = None
                  ) -> list[tuple[int, int]]:
    """枚举该边 Pareto 最优的 (进入分钟, 本次通行累计暴露) 候选。

    封闭/容量只允许"跳到下一个可进入分钟"；烟雾不禁止进入，但等待烟雾
    消散可能以更晚到达换取更低暴露，因此在暴露取值恒定的每个进入分钟
    区间内取最早的可行进入分钟，再按 (进入更早且暴露不更高) 做 Pareto
    过滤。``use_smoke=False``（归因用的放松搜索）或该边没有烟雾时段时，
    只需最早可进入分钟这一个候选。

    ``wait_bound`` 给出在边起点节点等待的最晚可进入分钟（节点滞留限流
    所致；起点出发前等待不占名额，此时为 None）：可进入分钟晚于该界的
    候选一律丢弃（等待区间包含超额分钟，更晚进入不可能恢复）。
    """
    def bounded(t0: int) -> Optional[int]:
        t = earliest_entry(edge, t0, size, world, horizon,
                           use_closures=use_closures,
                           use_loads=use_loads)
        if t is not None and wait_bound is not None and t > wait_bound:
            return None
        return t

    earliest = bounded(start)
    if earliest is None:
        return []
    # 边的终点登记了节点滞留限流时："晚到立即走"可能优于"早到后在拥挤
    # 节点等待"，需额外枚举使到达分钟落在终点关键时刻的入边进入候选，并
    # 保留全部（不再用"越晚暴露越低"过滤，否则会误删为节点名额而推迟的
    # 候选）；终点无限流时节点等待名额不改变可行性，沿用原口径即可。
    dst_controlled = bool(world.node_controls.get(edge.dst))
    if (not dst_controlled
            and (not use_smoke or not world.smoke_windows.get(edge.id))):
        return [(earliest, edge_traversal_exposure(
            world, edge.id, earliest, earliest + edge.minutes))]

    candidates: dict[int, int] = {}
    wanted = [earliest]
    for p in _entry_breakpoints(edge, world):
        if p >= start and (wait_bound is None or p <= wait_bound):
            wanted.append(p)
    if dst_controlled:
        for p in _node_arrival_entry_breakpoints(
                world, edge.dst, edge.minutes, start, horizon):
            if wait_bound is None or p <= wait_bound:
                wanted.append(p)
    for t0 in sorted(set(wanted)):
        t = bounded(t0)
        if t is None:
            continue
        candidates.setdefault(
            t, edge_traversal_exposure(world, edge.id, t, t + edge.minutes))

    # 终点无限流时：按进入分钟升序保留暴露严格下降的候选（更晚但暴露不
    # 更低的进入被更早或等暴露的候选支配）。终点限流时不过滤——推迟进入
    # 可能以更高暴露换取终点节点等待名额，交由标签层支配判定。
    if not dst_controlled:
        options: list[tuple[int, int]] = []
        best_exposure: Optional[int] = None
        for t, exposure in sorted(candidates.items()):
            if best_exposure is None or exposure < best_exposure:
                options.append((t, exposure))
                best_exposure = exposure
        return options
    return [(t, candidates[t]) for t in sorted(candidates)]


@dataclass
class _Label:
    """多标签时变搜索中的节点标签。

    arrival 为到达该节点的分钟；seq 为通道编号序列；exposure 为从起点沿
    该标签累计的烟雾暴露；parent/entry/edge_id 记录上一跳，用于选定路线
    后重放逐段时刻。``at_origin`` 标记该标签是否位于本次搜索起点：从起点
    出发前的等待不占节点名额，其余（中途节点）等待均受节点滞留限流约束。
    标签对象身份参与堆项去重，被支配移除的旧标签其堆项自动过期。
    """
    arrival: int
    seq: tuple[int, ...]
    exposure: int
    node: int
    visited: frozenset[int]
    parent: Optional["_Label"] = None
    entry: Optional[int] = None
    edge_id: Optional[int] = None
    at_origin: bool = False


def _search(world: World, *, origin: int, size: int, start: int,
            horizon: int, use_closures: bool, use_loads: bool,
            use_smoke: bool, use_node_loads: bool = True,
            smoke_limit: Optional[int] = None,
            exposure_used: int = 0,
            start_is_intermediate: bool = False,
            reserved_node_load:
            Optional[dict[tuple[int, int], int]] = None
            ) -> dict[int, "_Label"]:
    """多标签时变 Dijkstra。

    返回每个可达出口的最优标签（累计暴露不超上限的前提下，到达最早、
    通道序列字典序最小）。路径不允许重复经过节点（疏散路径无需绕环，
    同时保证标签集有限）。

    ``use_node_loads=False`` 为归因用的放松搜索：忽略节点滞留名额
    （起点等待本就不占名额）。``start_is_intermediate`` 用于节点滞留改编：
    以滞留节点为起点重找余程时，该节点恢复前的等待属于"前一通道离开后、
    下一通道进入前"的中途等待，仍须受名额约束。
    """
    # node -> Pareto 标签集
    labels: dict[int, list[_Label]] = {}
    heap: list[tuple] = []
    counter = 0

    root = _Label(arrival=start, seq=(), exposure=0, node=origin,
                  visited=frozenset({origin}), at_origin=True)
    labels[origin] = [root]
    heapq.heappush(heap, (start, (), 0, counter, root))

    def dominates(a: "_Label", b: "_Label") -> bool:
        return _dominates(
            a.arrival, a.seq, a.exposure, b.arrival, b.seq, b.exposure,
            world=world, node=a.node, size=size,
            check_node_wait=(use_node_loads and not a.at_origin),
            extra_load=reserved_node_load)

    while heap:
        arrive, _sq, _exp, _idx, label = heapq.heappop(heap)
        current = labels.get(label.node, [])
        if label not in current:
            continue  # 已被更优标签支配的过期堆项
        # 起点出发前等待：普通规划不占名额；节点滞留改编（start_is_
        # intermediate）时该节点等待属中途等待，且恢复前 [当前分钟, 恢复
        # 分钟) 的滞留已作为本组预占（reserved_node_load），故仍需按名额
        # 设界。其余中途节点的等待一律受节点滞留限流约束。
        if use_node_loads and (
                not label.at_origin or start_is_intermediate):
            wait_bound = _node_wait_bound(
                world, label.node, arrive, size, horizon, reserved_node_load)
        else:
            wait_bound = None
        for eid in world.adjacency.get(label.node, ()):
            edge = world.edges[eid]
            if edge.dst in label.visited:
                continue
            for entry, added in entry_options(
                    edge, arrive, size, world, horizon,
                    use_closures=use_closures, use_loads=use_loads,
                    use_smoke=use_smoke, wait_bound=wait_bound):
                total_exposure = label.exposure + added
                # 累计暴露上限：已走前缀额度 + 余程暴露不得超过组上限。
                # use_smoke=False 为归因用的放松搜索，此时不施加上限过滤。
                if (use_smoke and smoke_limit is not None
                        and exposure_used + total_exposure > smoke_limit):
                    continue
                nxt_time = entry + edge.minutes
                nxt_seq = label.seq + (eid,)
                nxt_node = edge.dst
                nxt = _Label(arrival=nxt_time, seq=nxt_seq,
                             exposure=total_exposure, node=nxt_node,
                             visited=label.visited | {nxt_node},
                             parent=label, entry=entry, edge_id=eid,
                             at_origin=False)

                bucket = labels.setdefault(nxt_node, [])
                if any(dominates(lb, nxt) for lb in bucket):
                    continue
                # 移除被新标签支配的旧标签
                bucket[:] = [lb for lb in bucket if not dominates(nxt, lb)]
                bucket.append(nxt)
                if len(bucket) > MAX_LABELS_PER_NODE:
                    raise PlanningError(
                        f"节点 {nxt_node} 标签数超过上限 "
                        f"{MAX_LABELS_PER_NODE}")
                counter += 1
                heapq.heappush(
                    heap,
                    (nxt_time, nxt_seq, total_exposure, counter, nxt))

    result: dict[int, _Label] = {}
    for exit_id in world.exits:
        bucket = labels.get(exit_id)
        if bucket:
            result[exit_id] = min(bucket,
                                  key=lambda lb: (lb.arrival, lb.seq))
    return result


def _pick(best_by_exit: dict[int, "_Label"]
          ) -> Optional[tuple[int, int, tuple[int, ...], "_Label"]]:
    """跨出口按 (到达分钟, 出口编号, 通道序列) 取最优。"""
    if not best_by_exit:
        return None
    exit_id, label = min(
        best_by_exit.items(),
        key=lambda kv: (kv[1].arrival, kv[0], kv[1].seq))
    return label.arrival, exit_id, label.seq, label


def _reconstruct(world: World, size: int, label: "_Label"
                 ) -> tuple[list[dict], int, dict[tuple[int, int], int],
                            dict[tuple[int, int], int]]:
    """沿标签的父指针重放，得到每段进入/离开分钟、通道负载与节点等待增量。

    返回 (steps, 到达分钟, load_delta, node_wait_delta)。node_wait_delta
    的键为 (节点, 分钟)，只计入"前一通道离开到进入下一通道之间"的中途
    等待（含节点滞留改编时以滞留节点为起点的等待）；出发前起点等待、
    通道在途与抵达出口后的时间均不计入。
    """
    chain: list[_Label] = []
    node_label = label
    while node_label.parent is not None:
        chain.append(node_label)
        node_label = node_label.parent
    chain.reverse()

    steps: list[dict] = []
    load_delta: dict[tuple[int, int], int] = {}
    node_wait_delta: dict[tuple[int, int], int] = {}
    for lb in chain:
        parent = lb.parent
        edge = world.edges[lb.edge_id]
        entry = lb.entry
        leave = entry + edge.minutes
        if leave != lb.arrival:  # 防御：标签与重放时刻必须一致
            raise PlanningError("路线重放失败")
        steps.append({
            "edge_id": lb.edge_id,
            "from_node": edge.src,
            "to_node": edge.dst,
            "enter_minute": entry,
            "exit_minute": leave,
        })
        for minute in range(entry, leave):
            load_delta[(lb.edge_id, minute)] = (
                load_delta.get((lb.edge_id, minute), 0) + size)
        # 进入该段前在其起点节点上的等待；根标签在起点（普通规划）的等待
        # 属于"出发前起点等待"，不计；节点滞留改编的根标签 at_origin=False
        if not parent.at_origin:
            for minute in range(parent.arrival, entry):
                node_wait_delta[(edge.src, minute)] = (
                    node_wait_delta.get((edge.src, minute), 0) + size)
    return steps, label.arrival, load_delta, node_wait_delta


def plan_group(*, group_id: int, size: int, origin: int,
               earliest_departure: int, deadline: int, current_minute: int,
               world: World, smoke_limit: Optional[int] = None,
               exposure_used: int = 0,
               start_is_intermediate: bool = False,
               fixed_node_wait:
               Optional[dict[tuple[int, int], int]] = None) -> dict:
    """为单个组规划，返回 scheduled / failed 结果。

    world.fixed_load / world.fixed_node_load 中应已包含此前已规划（同批
    高优先级）组的通道占用与中途节点等待；调用方在拿到 scheduled 结果后
    须把 load_delta / node_wait_delta 分别累加进对应字典。

    smoke_limit 为该组非负累计暴露上限（未设上限传 None）；
    exposure_used 为已走前缀按原计划时刻累计的暴露（节点滞留改编寻找
    余程时使用，普通未出发组规划传 0）；start_is_intermediate 用于节点
    滞留改编（以滞留节点为起点的等待仍属中途等待、占节点名额）；
    fixed_node_wait 给出本组在余程起点节点上**恢复前已必须占用**的等待
    名额（节点滞留报告后 [当前分钟, 恢复分钟) 的滞留），该槽位不能再被
    本组余程等待复用。
    """
    start = max(current_minute, earliest_departure)
    if start > deadline:
        return {
            "group_id": group_id,
            "status": "failed",
            "reason": "DEADLINE_PASSED",
            "detail": f"当前分钟 {current_minute} 已超过规划截止分钟 {deadline}",
        }

    # 节点滞留改编：本组在滞留节点上"自当前分钟到恢复分钟"的等待是已固定
    # 的事实（在根标签到达时刻 start 之前），若其中任一分钟的名额已无法
    # 容纳本组，则恢复前滞留本身超额——再找余程也无法消除，直接判无解。
    if start_is_intermediate and fixed_node_wait:
        for (nid, minute), add in fixed_node_wait.items():
            if minute < current_minute or minute >= start:
                continue
            cap = node_limit(world, nid, minute)
            if cap is None:
                continue
            used = world.fixed_node_load.get((nid, minute), 0)
            if used + add > cap:
                return {
                    "group_id": group_id,
                    "status": "failed",
                    "reason": "INSUFFICIENT_NODE_CAPACITY",
                    "detail": (
                        f"组在滞留节点 {nid} 恢复前的固定等待在第 {minute} "
                        f"分钟超过该节点限流名额（{used + add} > {cap}），"
                        "无法通过余程安排消除")}

    def search(*, closures: bool, loads: bool, smoke: bool,
               node_loads: bool = True):
        return _search(world, origin=origin, size=size, start=start,
                       horizon=deadline, use_closures=closures,
                       use_loads=loads, use_smoke=smoke,
                       use_node_loads=node_loads,
                       smoke_limit=smoke_limit, exposure_used=exposure_used,
                       start_is_intermediate=start_is_intermediate,
                       reserved_node_load=fixed_node_wait)

    # 1) 完整约束下寻路
    picked = _pick(search(closures=True, loads=True, smoke=True))
    if picked is not None:
        arrive, exit_id, seq, label = picked
        steps, _, load_delta, node_wait_delta = _reconstruct(
            world, size, label)
        departure = steps[0]["enter_minute"] if steps else start
        return {
            "group_id": group_id,
            "status": "scheduled",
            "exit_id": exit_id,
            "departure_minute": departure,
            "arrival_minute": arrive,
            "steps": steps,
            "load_delta": load_delta,
            "node_wait_delta": node_wait_delta,
        }

    # 2) 归因：分别放松单一约束
    relaxed_closure = search(closures=False, loads=True, smoke=True)
    relaxed_capacity = search(closures=True, loads=False, smoke=True)
    relaxed_nodes = search(closures=True, loads=True, smoke=True,
                           node_loads=False)
    relaxed_smoke = search(closures=True, loads=True, smoke=False)

    if _pick(relaxed_closure) is not None:
        reason, detail = (
            "BLOCKED_BY_CLOSURE",
            "忽略封闭时段后可在截止前到达，当前封闭时段阻断了所有可行路线")
    elif _pick(relaxed_capacity) is not None:
        reason, detail = (
            "INSUFFICIENT_CAPACITY",
            "忽略通道容量限制后可在截止前到达，容量竞争阻断了所有可行路线")
    elif _pick(relaxed_nodes) is not None:
        reason, detail = (
            "INSUFFICIENT_NODE_CAPACITY",
            "忽略中途节点计划滞留名额后可在截止前到达，节点限流（可推迟"
            "上游通行或改走路线）与既有封闭、通道容量共同阻断了截止前的"
            "所有可行路线")
    elif smoke_limit is not None and _pick(relaxed_smoke) is not None:
        reason, detail = (
            "EXPOSURE_LIMIT_EXCEEDED",
            "忽略烟雾累计暴露上限后可在截止前到达，现有烟雾暴露时段使"
            "截止前所有可行路线（含在节点等待烟雾消散）的累计暴露超过"
            f"组上限 {smoke_limit}（已走前缀已用 {exposure_used}）")
    else:
        # 3) 完全放松（含时间窗），判断出口是否物理可达
        total_w = sum(e.minutes for e in world.edges.values())
        far_horizon = deadline + total_w + 1
        plain = _search(world, origin=origin, size=size, start=start,
                        horizon=far_horizon, use_closures=False,
                        use_loads=False, use_smoke=False,
                        use_node_loads=False,
                        start_is_intermediate=start_is_intermediate,
                        reserved_node_load=fixed_node_wait)
        if _pick(plain) is None:
            reason, detail = (
                "UNREACHABLE_EXIT",
                f"起点 {origin} 与任何出口之间不存在连通路径")
        else:
            reason, detail = (
                "INSUFFICIENT_CAPACITY",
                "封闭时段、通道容量、中途节点滞留名额与烟雾暴露上限共同"
                "阻断了截止前的所有可行路线")

    return {
        "group_id": group_id,
        "status": "failed",
        "reason": reason,
        "detail": detail,
    }
