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

择优规则
--------
每组在各自允许出发的时间范围内寻找：到达分钟最早；同到达时出口编号最小；
同出口同到达时通道编号序列字典序最小。由于通道在节点处允许等待（等待由
封闭/容量/烟雾迫使），边到达时间关于进入时间满足 FIFO，因此用时变最短路
求解。为正确处理"到达相同、通道序列字典序更小"以及"晚出发但累计暴露更
低"的并列情形，每个节点保留 (到达分钟, 通道序列, 累计暴露) 的 Pareto
标签集；对同一条边，除最早可进入分钟外，还在暴露取值发生变化的每个区间
尝试延后进入（等待烟雾消散）。
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


def _dominates(a_time: int, a_seq: tuple[int, ...], a_exposure: int,
               b_time: int, b_seq: tuple[int, ...], b_exposure: int) -> bool:
    """标签 a 是否支配标签 b：到达不晚、通道序列字典序不更大、暴露不更高。"""
    return (a_time <= b_time and a_seq <= b_seq
            and a_exposure <= b_exposure)


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
                  use_smoke: bool) -> list[tuple[int, int]]:
    """枚举该边 Pareto 最优的 (进入分钟, 本次通行累计暴露) 候选。

    封闭/容量只允许"跳到下一个可进入分钟"；烟雾不禁止进入，但等待烟雾
    消散可能以更晚到达换取更低暴露，因此在暴露取值恒定的每个进入分钟
    区间内取最早的可行进入分钟，再按 (进入更早且暴露不更高) 做 Pareto
    过滤。``use_smoke=False``（归因用的放松搜索）或该边没有烟雾时段时，
    只需最早可进入分钟这一个候选。
    """
    earliest = earliest_entry(edge, start, size, world, horizon,
                              use_closures=use_closures,
                              use_loads=use_loads)
    if earliest is None:
        return []
    if not use_smoke or not world.smoke_windows.get(edge.id):
        return [(earliest, edge_traversal_exposure(
            world, edge.id, earliest, earliest + edge.minutes))]

    candidates: dict[int, int] = {}
    wanted = [earliest]
    for p in _entry_breakpoints(edge, world):
        if p >= start:
            wanted.append(p)
    for t0 in sorted(set(wanted)):
        t = earliest_entry(edge, t0, size, world, horizon,
                           use_closures=use_closures,
                           use_loads=use_loads)
        if t is None:
            continue
        candidates.setdefault(
            t, edge_traversal_exposure(world, edge.id, t, t + edge.minutes))

    # 按进入分钟升序保留暴露严格下降的候选：更晚但暴露不更低的进入被
    # （更早或等暴露的）候选支配
    options: list[tuple[int, int]] = []
    best_exposure: Optional[int] = None
    for t, exposure in sorted(candidates.items()):
        if best_exposure is None or exposure < best_exposure:
            options.append((t, exposure))
            best_exposure = exposure
    return options


@dataclass
class _Label:
    """多标签时变搜索中的节点标签。

    arrival 为到达该节点的分钟；seq 为通道编号序列；exposure 为从起点沿
    该标签累计的烟雾暴露；parent/entry/edge_id 记录上一跳，用于选定路线
    后重放逐段时刻。标签对象身份参与堆项去重，被支配移除的旧标签其堆项
    自动过期。
    """
    arrival: int
    seq: tuple[int, ...]
    exposure: int
    node: int
    visited: frozenset[int]
    parent: Optional["_Label"] = None
    entry: Optional[int] = None
    edge_id: Optional[int] = None


def _search(world: World, *, origin: int, size: int, start: int,
            horizon: int, use_closures: bool, use_loads: bool,
            use_smoke: bool, smoke_limit: Optional[int] = None,
            exposure_used: int = 0
            ) -> dict[int, "_Label"]:
    """多标签时变 Dijkstra。

    返回每个可达出口的最优标签（累计暴露不超上限的前提下，到达最早、
    通道序列字典序最小）。路径不允许重复经过节点（疏散路径无需绕环，
    同时保证标签集有限）。
    """
    # node -> Pareto 标签集
    labels: dict[int, list[_Label]] = {}
    heap: list[tuple] = []
    counter = 0

    root = _Label(arrival=start, seq=(), exposure=0, node=origin,
                  visited=frozenset({origin}))
    labels[origin] = [root]
    heapq.heappush(heap, (start, (), 0, counter, root))

    while heap:
        arrive, _sq, _exp, _idx, label = heapq.heappop(heap)
        current = labels.get(label.node, [])
        if label not in current:
            continue  # 已被更优标签支配的过期堆项
        for eid in world.adjacency.get(label.node, ()):
            edge = world.edges[eid]
            if edge.dst in label.visited:
                continue
            for entry, added in entry_options(
                    edge, arrive, size, world, horizon,
                    use_closures=use_closures, use_loads=use_loads,
                    use_smoke=use_smoke):
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
                             parent=label, entry=entry, edge_id=eid)

                bucket = labels.setdefault(nxt_node, [])
                if any(_dominates(lb.arrival, lb.seq, lb.exposure,
                                  nxt_time, nxt_seq, total_exposure)
                       for lb in bucket):
                    continue
                # 移除被新标签支配的旧标签
                bucket[:] = [
                    lb for lb in bucket
                    if not _dominates(nxt_time, nxt_seq, total_exposure,
                                      lb.arrival, lb.seq, lb.exposure)]
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
                 ) -> tuple[list[dict], int, dict[tuple[int, int], int]]:
    """沿标签的父指针重放，得到每段进入/离开分钟与总负载增量。"""
    chain: list[_Label] = []
    node_label = label
    while node_label.parent is not None:
        chain.append(node_label)
        node_label = node_label.parent
    chain.reverse()

    steps: list[dict] = []
    load_delta: dict[tuple[int, int], int] = {}
    for lb in chain:
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
    return steps, label.arrival, load_delta


def plan_group(*, group_id: int, size: int, origin: int,
               earliest_departure: int, deadline: int, current_minute: int,
               world: World, smoke_limit: Optional[int] = None,
               exposure_used: int = 0) -> dict:
    """为单个组规划，返回 scheduled / failed 结果。

    world.fixed_load 中应已包含此前已规划（同批高优先级）组的占用；
    调用方在拿到 scheduled 结果后须把 load_delta 累加进 fixed_load。

    smoke_limit 为该组非负累计暴露上限（未设上限传 None）；
    exposure_used 为已走前缀按原计划时刻累计的暴露（节点滞留改编寻找
    余程时使用，普通未出发组规划传 0）。
    """
    start = max(current_minute, earliest_departure)
    if start > deadline:
        return {
            "group_id": group_id,
            "status": "failed",
            "reason": "DEADLINE_PASSED",
            "detail": f"当前分钟 {current_minute} 已超过规划截止分钟 {deadline}",
        }

    def search(*, closures: bool, loads: bool, smoke: bool):
        return _search(world, origin=origin, size=size, start=start,
                       horizon=deadline, use_closures=closures,
                       use_loads=loads, use_smoke=smoke,
                       smoke_limit=smoke_limit, exposure_used=exposure_used)

    # 1) 完整约束下寻路
    picked = _pick(search(closures=True, loads=True, smoke=True))
    if picked is not None:
        arrive, exit_id, seq, label = picked
        steps, _, load_delta = _reconstruct(world, size, label)
        departure = steps[0]["enter_minute"] if steps else start
        return {
            "group_id": group_id,
            "status": "scheduled",
            "exit_id": exit_id,
            "departure_minute": departure,
            "arrival_minute": arrive,
            "steps": steps,
            "load_delta": load_delta,
        }

    # 2) 归因：分别放松单一约束
    relaxed_closure = search(closures=False, loads=True, smoke=True)
    relaxed_capacity = search(closures=True, loads=False, smoke=True)
    relaxed_smoke = search(closures=True, loads=True, smoke=False)

    if _pick(relaxed_closure) is not None:
        reason, detail = (
            "BLOCKED_BY_CLOSURE",
            "忽略封闭时段后可在截止前到达，当前封闭时段阻断了所有可行路线")
    elif _pick(relaxed_capacity) is not None:
        reason, detail = (
            "INSUFFICIENT_CAPACITY",
            "忽略通道容量限制后可在截止前到达，容量竞争阻断了所有可行路线")
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
                        use_loads=False, use_smoke=False)
        if _pick(plain) is None:
            reason, detail = (
                "UNREACHABLE_EXIT",
                f"起点 {origin} 与任何出口之间不存在连通路径")
        else:
            reason, detail = (
                "INSUFFICIENT_CAPACITY",
                "封闭时段、通道容量与烟雾暴露上限共同阻断了截止前的"
                "所有可行路线")

    return {
        "group_id": group_id,
        "status": "failed",
        "reason": reason,
        "detail": detail,
    }
