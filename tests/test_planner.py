"""规划算法单元测试。"""
from __future__ import annotations

import pytest

from app.planner import Edge, World, plan_group


def build_world(edges, exits, closures=(), fixed_load=None, smoke=(),
                node_controls=()):
    edge_map = {e.id: e for e in edges}
    adj: dict[int, list[int]] = {}
    for e in edges:
        adj.setdefault(e.src, []).append(e.id)
    for v in adj.values():
        v.sort()
    clo: dict[int, list[tuple[int, int]]] = {}
    for eid, a, b in closures:
        clo.setdefault(eid, []).append((a, b))
    smk: dict[int, list[tuple[int, int, int]]] = {}
    for eid, a, b, value in smoke:
        smk.setdefault(eid, []).append((a, b, value))
    nctrl: dict[int, list[tuple[int, int, int]]] = {}
    for nid, a, b, cap in node_controls:
        nctrl.setdefault(nid, []).append((a, b, cap))
    return World(edges=edge_map, adjacency=adj, exits=list(exits),
                 closures=clo, fixed_load=dict(fixed_load or {}),
                 smoke_windows=smk, node_controls=nctrl)


def plan(world, gid=1, size=10, origin=1, earliest=0, deadline=100, now=0,
         smoke_limit=None, exposure_used=0, start_is_intermediate=False,
         fixed_node_wait=None):
    return plan_group(
        group_id=gid, size=size, origin=origin,
        earliest_departure=earliest, deadline=deadline,
        current_minute=now, world=world, smoke_limit=smoke_limit,
        exposure_used=exposure_used,
        start_is_intermediate=start_is_intermediate,
        fixed_node_wait=fixed_node_wait)


def test_simple_earliest_route():
    # 1 --1(4)--> 2 --2(3)--> 5(出口)
    world = build_world([
        Edge(1, 1, 2, 4, 100), Edge(2, 2, 5, 3, 100),
    ], exits=[5])
    r = plan(world)
    assert r["status"] == "scheduled"
    assert r["exit_id"] == 5
    assert r["departure_minute"] == 0
    assert r["arrival_minute"] == 7
    assert [s["edge_id"] for s in r["steps"]] == [1, 2]


def test_closure_forces_wait_and_half_open_end():
    # 通道 1 封闭 [2,6)；t=0 进入占用 [0,4) 与封闭相交 -> 等；
    # 封闭 [2,6) 结束分钟 6 可进入（左闭右开），[6,10) 不相交
    world = build_world(
        [Edge(1, 1, 2, 4, 100), Edge(2, 2, 3, 1, 100)],
        exits=[3], closures=[(1, 2, 6)])
    r = plan(world)
    assert r["status"] == "scheduled"
    assert r["steps"][0]["enter_minute"] == 6
    assert r["arrival_minute"] == 11


def test_closure_blocked_before_deadline_reason():
    # 唯一通道封闭 [0,50)，截止 10 -> 无法在截止前安排，原因为封闭
    world = build_world(
        [Edge(1, 1, 2, 4, 100)], exits=[2], closures=[(1, 0, 50)])
    r = plan(world, deadline=10)
    assert r["status"] == "failed"
    assert r["reason"] == "BLOCKED_BY_CLOSURE"


def test_unreachable_reason():
    world = build_world([Edge(1, 1, 2, 1, 100)], exits=[9])
    r = plan(world)
    assert r["status"] == "failed"
    assert r["reason"] == "UNREACHABLE_EXIT"


def test_capacity_contention_and_reason():
    # 组 A 先占满通道 1 的分钟 0..4；组 B 同分钟无法进入
    world = build_world(
        [Edge(1, 1, 2, 4, 30), Edge(2, 2, 5, 1, 30)], exits=[5])
    a = plan(world, gid=1, size=30)
    assert a["status"] == "scheduled"
    for k, v in a["load_delta"].items():
        world.fixed_load[k] = world.fixed_load.get(k, 0) + v
    # 容量满且截止 4 -> 容量原因
    r = plan(world, gid=2, size=10, deadline=4)
    assert r["status"] == "failed"
    assert r["reason"] == "INSUFFICIENT_CAPACITY"
    # 放宽截止后可等待
    r2 = plan(world, gid=2, size=10, deadline=10)
    assert r2["status"] == "scheduled"
    assert r2["steps"][0]["enter_minute"] == 4


def test_group_larger_than_capacity():
    world = build_world([Edge(1, 1, 2, 1, 5)], exits=[2])
    r = plan(world, size=6)
    assert r["status"] == "failed"
    assert r["reason"] == "INSUFFICIENT_CAPACITY"


def test_exit_id_tiebreak():
    # 到出口 5 与 6 同为第 5 分钟 -> 出口编号小者 5
    world = build_world([
        Edge(1, 1, 5, 5, 100), Edge(2, 1, 6, 5, 100),
    ], exits=[5, 6])
    r = plan(world)
    assert r["exit_id"] == 5
    assert r["arrival_minute"] == 5


def test_edge_sequence_tiebreak_needs_lex_smaller():
    # 两条到同一出口、同到达的路径：
    #   直连通道 9：1->3 耗时 6
    #   通道 1+2：1->2(3) ->3(3)，序列 (1,2) 字典序小于 (9,)
    world = build_world([
        Edge(1, 1, 2, 3, 100),
        Edge(2, 2, 3, 3, 100),
        Edge(9, 1, 3, 6, 100),
    ], exits=[3])
    r = plan(world)
    assert r["arrival_minute"] == 6
    assert [s["edge_id"] for s in r["steps"]] == [1, 2]


def test_arrival_beats_sequence():
    # (9,) 到达 5；(1,2) 到达 6 -> 到达优先，选 9
    world = build_world([
        Edge(1, 1, 2, 3, 100),
        Edge(2, 2, 3, 3, 100),
        Edge(9, 1, 3, 5, 100),
    ], exits=[3])
    r = plan(world)
    assert [s["edge_id"] for s in r["steps"]] == [9]
    assert r["arrival_minute"] == 5


def test_origin_is_exit_zero_minute_route():
    world = build_world([Edge(1, 1, 2, 1, 100)], exits=[1, 2])
    r = plan(world, origin=1)
    assert r["status"] == "scheduled"
    assert r["exit_id"] == 1
    assert r["arrival_minute"] == 0
    assert r["departure_minute"] == 0
    assert r["steps"] == []


def test_deadline_boundary_inclusive():
    # 通行 4 分钟，截止 4：t=0 进入 [0,4)，第 4 分钟到达，合法
    world = build_world([Edge(1, 1, 2, 4, 100)], exits=[2])
    r = plan(world, deadline=4)
    assert r["status"] == "scheduled"
    assert r["arrival_minute"] == 4


def test_earliest_departure_respected():
    world = build_world([Edge(1, 1, 2, 2, 100)], exits=[2])
    r = plan(world, earliest=7, deadline=20)
    assert r["departure_minute"] == 7
    assert r["arrival_minute"] == 9


def test_deadline_passed():
    world = build_world([Edge(1, 1, 2, 1, 100)], exits=[2])
    r = plan(world, now=11, deadline=10)
    assert r["status"] == "failed"
    assert r["reason"] == "DEADLINE_PASSED"


def test_per_minute_capacity_partial_overlap():
    # A 占用 [2,6)（等待封闭后出发），B 不能与任何一分钟重叠超额
    world = build_world(
        [Edge(1, 1, 2, 4, 30), Edge(2, 2, 3, 1, 30)],
        exits=[3], closures=[(1, 0, 2)])
    a = plan(world, gid=1, size=20, deadline=20)
    assert a["steps"][0]["enter_minute"] == 2
    for k, v in a["load_delta"].items():
        world.fixed_load[k] = world.fixed_load.get(k, 0) + v
    b = plan(world, gid=2, size=20, deadline=20)
    # 容量 30，不能在 2..6 任一分钟与 A 同时在途（20+20>30），最早第 6 分钟
    assert b["steps"][0]["enter_minute"] == 6


# --------------------------------------------------------------------------
# 容量管制时段（有效上限 = min(原容量, 管制上限)，按每分钟核算）
# --------------------------------------------------------------------------

def test_capacity_control_forces_wait_until_window_end():
    # 通道 1 容量 30，管制 [2,6) 上限 10；组 20 人：
    # t=0 进入占用 [0,4)，分钟 2、3 有效上限 10 < 20 -> 不可；
    # 单组人数超管制上限，直接跳到管制结束分钟 6
    world = build_world(
        [Edge(1, 1, 2, 4, 30), Edge(2, 2, 3, 1, 30)], exits=[3])
    world.capacity_controls[1] = [(2, 6, 10)]
    r = plan(world, size=20)
    assert r["status"] == "scheduled"
    assert r["steps"][0]["enter_minute"] == 6
    assert r["arrival_minute"] == 11


def test_capacity_control_partial_window_blocks_entry():
    # 通行 4 分钟，管制 [3,5) 上限 10，组 20 人：
    # t=0/1/2 的占用区间均含管制分钟 -> 不可；t=5 起管制外 -> 可
    world = build_world([Edge(1, 1, 2, 4, 30)], exits=[2])
    world.capacity_controls[1] = [(3, 5, 10)]
    r = plan(world, size=20)
    assert r["steps"][0]["enter_minute"] == 5


def test_capacity_control_outside_window_uses_base_capacity():
    # 管制 [10,20) 上限 5，组 30 人（=原容量）：管制外分钟照常可通行
    world = build_world([Edge(1, 1, 2, 2, 30)], exits=[2])
    world.capacity_controls[1] = [(10, 20, 5)]
    r = plan(world, size=30)
    assert r["status"] == "scheduled"
    assert r["departure_minute"] == 0
    assert r["arrival_minute"] == 2


def test_capacity_control_above_base_capacity_is_noop():
    # 管制上限高于原容量：有效上限仍为原容量（管制只收紧不放松）
    world = build_world([Edge(1, 1, 2, 2, 30)], exits=[2])
    world.capacity_controls[1] = [(0, 100, 50)]
    r = plan(world, size=30)
    assert r["status"] == "scheduled"
    assert r["departure_minute"] == 0


def test_capacity_control_with_fixed_load():
    # 管制 [0,10) 上限 25；A 20 人 t=0 进入占用 [0,4)；
    # B 10 人：分钟 0..3 有效上限 25，20+10>25 -> 等到 A 走完后 t=4
    world = build_world([Edge(1, 1, 2, 4, 30)], exits=[2])
    world.capacity_controls[1] = [(0, 10, 25)]
    a = plan(world, gid=1, size=20)
    assert a["steps"][0]["enter_minute"] == 0
    for k, v in a["load_delta"].items():
        world.fixed_load[k] = world.fixed_load.get(k, 0) + v
    b = plan(world, gid=2, size=10)
    assert b["steps"][0]["enter_minute"] == 4


def test_capacity_control_blocking_all_windows_reason():
    # 唯一通道全程管制 [0,50) 上限 5，组 6 人截止 10 -> 容量原因失败
    world = build_world([Edge(1, 1, 2, 4, 100)], exits=[2])
    world.capacity_controls[1] = [(0, 50, 5)]
    r = plan(world, size=6, deadline=10)
    assert r["status"] == "failed"
    assert r["reason"] == "INSUFFICIENT_CAPACITY"


# --------------------------------------------------------------------------
# 烟雾暴露：逐分钟累计、等待消散、累计上限与前缀额度
# --------------------------------------------------------------------------

def test_smoke_unlimited_group_takes_earliest():
    world = build_world(
        [Edge(1, 1, 2, 2, 100)], exits=[2],
        smoke=[(1, 0, 10, 99)])
    r = plan(world)  # 不设上限
    assert r["status"] == "scheduled"
    assert r["departure_minute"] == 0
    assert r["arrival_minute"] == 2


def test_smoke_wait_until_window_end_meets_limit():
    # 通行2分钟，窗口[0,2) 每分钟5：t=0 进入暴露10超限；t=1 只占分钟1
    # 暴露5。上限9 -> t=1 进入、到达3；上限4 -> 须等至 t=2 暴露0、到达4
    world = build_world(
        [Edge(1, 1, 2, 2, 100)], exits=[2],
        smoke=[(1, 0, 2, 5)])
    r = plan(world, smoke_limit=9)
    assert r["status"] == "scheduled"
    assert r["steps"][0]["enter_minute"] == 1
    assert r["arrival_minute"] == 3
    r2 = plan(world, smoke_limit=4)
    assert r2["steps"][0]["enter_minute"] == 2
    assert r2["arrival_minute"] == 4
    # 上限5：t=1 暴露5恰好
    r3 = plan(world, smoke_limit=5)
    assert r3["steps"][0]["enter_minute"] == 1


def test_smoke_limit_equal_to_immediate_exposure_allowed():
    world = build_world(
        [Edge(1, 1, 2, 2, 100)], exits=[2],
        smoke=[(1, 0, 2, 5)])
    r = plan(world, smoke_limit=10)
    assert r["departure_minute"] == 0


def test_smoke_zero_limit_waits_for_clearance():
    world = build_world(
        [Edge(1, 1, 2, 2, 100)], exits=[2],
        smoke=[(1, 3, 6, 5)])
    r = plan(world, smoke_limit=0)
    # t=0 占用[0,2) 无烟，本可立即走；立即走到达2，与等待方案暴露同为0，
    # 最早出发仍为0
    assert r["departure_minute"] == 0


def test_smoke_window_aligned_with_later_occupancy_forces_wait():
    # t=0 占用[0,2) 无烟；但若封闭迫使第4分钟进入（占用[4,6)），
    # 窗口[4,6) 值1、上限0 -> 等至第6分钟
    world = build_world(
        [Edge(1, 1, 2, 2, 100)], exits=[2],
        closures=[(1, 0, 6)], smoke=[(1, 4, 6, 1)])
    r = plan(world, smoke_limit=0)
    assert r["steps"][0]["enter_minute"] == 6
    assert r["arrival_minute"] == 8


def test_smoke_partial_overlap_entry_minutes():
    # w=4，窗口[2,4) 值1：t=0 占用0..3 暴露2；t=1 占用1..4 暴露2；
    # t=2 占用2..5 暴露2；t=4 占用4..7 暴露0。上限0 -> t=4 进入
    world = build_world(
        [Edge(1, 1, 2, 4, 100)], exits=[2],
        smoke=[(1, 2, 4, 1)])
    r = plan(world, smoke_limit=0)
    assert r["steps"][0]["enter_minute"] == 4


def test_smoke_wait_beyond_deadline_fails_with_reason():
    world = build_world(
        [Edge(1, 1, 2, 2, 100)], exits=[2],
        smoke=[(1, 0, 20, 1)])
    r = plan(world, smoke_limit=0, deadline=10)
    assert r["status"] == "failed"
    assert r["reason"] == "EXPOSURE_LIMIT_EXCEEDED"


def test_smoke_prefix_quota_used_in_replan():
    # 节点滞留改编：已走前缀已用6，余程通道2 窗口[0,10) 每分钟3（2分钟
    # =6），上限10 -> 立即续走 6+6>10；t=9 进入只占分钟9暴露3（6+3=9
    # <=10）、到达11，早于等到窗口结束
    world = build_world(
        [Edge(2, 2, 5, 2, 100)], exits=[5],
        smoke=[(2, 0, 10, 3)])
    r = plan(world, origin=2, earliest=4, deadline=20,
             smoke_limit=10, exposure_used=6)
    assert r["status"] == "scheduled"
    assert r["steps"][0]["enter_minute"] == 9
    assert r["arrival_minute"] == 11


def test_smoke_prefix_quota_no_remaining_route_fails():
    world = build_world(
        [Edge(2, 2, 5, 2, 100)], exits=[5],
        smoke=[(2, 0, 20, 3)])
    r = plan(world, origin=2, earliest=4, deadline=12,
             smoke_limit=10, exposure_used=6)
    # 等至第12分钟进入则到达14 > deadline12；任何更早进入都+6 超限
    assert r["status"] == "failed"
    assert r["reason"] == "EXPOSURE_LIMIT_EXCEEDED"


def test_smoke_does_not_change_tiebreak_order_when_clear():
    # 两条同到达路线都无烟时，通道序列字典序规则保持不变
    world = build_world([
        Edge(1, 1, 2, 3, 100), Edge(2, 2, 3, 3, 100),
        Edge(9, 1, 3, 6, 100),
    ], exits=[3], smoke=[(9, 0, 10, 5)])
    # 通道9 有烟但组无上限：择优仍按到达/出口/序列 -> [1,2]
    r = plan(world)
    assert [s["edge_id"] for s in r["steps"]] == [1, 2]


def test_smoke_multi_hop_accumulates_across_edges():
    # P1 每分钟2（2分钟=4），P2 每分钟3（2分钟=6）：上限10 立即走=10 恰好
    world = build_world(
        [Edge(1, 1, 2, 2, 100), Edge(2, 2, 5, 2, 100)],
        exits=[5], smoke=[(1, 0, 10, 2), (2, 0, 10, 3)])
    r = plan(world, smoke_limit=10)
    assert r["status"] == "scheduled"
    assert r["arrival_minute"] == 4
    # 上限9：P1 暴露4；P2 t=9 进入只占窗口分钟9（暴露3），4+3=9、
    # 到达11（早于等至窗口结束 t=10 的到达12）
    r2 = plan(world, smoke_limit=9)
    assert r2["steps"][1]["enter_minute"] == 9
    assert r2["arrival_minute"] == 11


def test_smoke_zero_value_window_is_free():
    world = build_world(
        [Edge(1, 1, 2, 2, 100)], exits=[2],
        smoke=[(1, 0, 10, 0)])
    r = plan(world, smoke_limit=0)
    assert r["departure_minute"] == 0


def test_smoke_relaxed_search_ignores_limit_for_attribution():
    # 烟雾是唯一阻断因素：忽略暴露上限后可到达 -> EXPOSURE_LIMIT_EXCEEDED
    world = build_world(
        [Edge(1, 1, 2, 2, 100), Edge(2, 2, 3, 1, 100)], exits=[3],
        smoke=[(1, 0, 50, 1)])
    r = plan(world, smoke_limit=0, deadline=10)
    assert r["status"] == "failed"
    assert r["reason"] == "EXPOSURE_LIMIT_EXCEEDED"


# --------------------------------------------------------------------------
# 中途节点计划滞留限流（只计前一通道离开到进入下一通道之间的等待）
# --------------------------------------------------------------------------

def test_node_control_does_not_touch_back_to_back_steps():
    # 到达节点2立即进入下一通道：等待区间为空，不占名额
    world = build_world(
        [Edge(1, 1, 2, 2, 100), Edge(2, 2, 3, 3, 100)], exits=[3],
        node_controls=[(2, 0, 20, 1)])
    r = plan(world)
    assert r["status"] == "scheduled"
    assert r["arrival_minute"] == 5
    assert r["node_wait_delta"] == {}


def test_node_control_origin_wait_is_free():
    # 起点即节点2：出发前等待不占节点名额，限流再紧也不影响起点等待
    world = build_world([Edge(2, 2, 3, 2, 100)], exits=[3],
                        node_controls=[(2, 0, 20, 1)])
    r = plan(world, origin=2, earliest=5)
    assert r["status"] == "scheduled"
    assert r["departure_minute"] == 5
    assert r["node_wait_delta"] == {}


def test_node_control_forces_detour_when_waiting_overquota():
    # 1->2(4)；2->6(1) 封闭[4,8)（等待到8到达9）；2->3(4)->5(4) 绕行
    # 到达12。节点2限流[4,8) 5人：等待10人全程超额。上游通道1又封闭
    # [4,8)，无法把通行推迟到第4分钟进入以在第8分钟恰好到节点2（最早只
    # 能第8分钟进入、第13分钟到达），故最优改为第4分钟到节点2后立即绕行
    # [2,3]，第12分钟到达。
    world = build_world(
        [Edge(1, 1, 2, 4, 100), Edge(2, 2, 3, 4, 100),
         Edge(3, 3, 5, 4, 100), Edge(5, 2, 6, 1, 100)],
        exits=[5, 6], closures=[(5, 4, 8), (1, 4, 8)],
        node_controls=[(2, 4, 8, 5)])
    r = plan(world)
    assert r["status"] == "scheduled"
    assert [s["edge_id"] for s in r["steps"]] == [1, 2, 3]
    assert r["arrival_minute"] == 12
    assert r["node_wait_delta"] == {}


def test_node_control_can_shift_wait_upstream_to_origin():
    # 1->2(2)；2->6(1) 封闭[2,8)（等到8到达9）；绕行 2->3(4)->5(4) 到14。
    # 节点2限流[2,8) 5人：等待超额，但可在起点把通道1推迟到第6分钟进入、
    # 第8分钟到节点2立即进入通道5，仍第9分钟到达（优于绕行）。
    # 起点等待不占名额。
    world = build_world(
        [Edge(1, 1, 2, 2, 100), Edge(2, 2, 3, 4, 100),
         Edge(3, 3, 5, 4, 100), Edge(5, 2, 6, 1, 100)],
        exits=[5, 6], closures=[(5, 2, 8)],
        node_controls=[(2, 2, 8, 5)])
    r = plan(world)
    assert r["status"] == "scheduled"
    assert [s["edge_id"] for s in r["steps"]] == [1, 5]
    assert r["steps"][0]["enter_minute"] == 6
    assert r["steps"][1]["enter_minute"] == 8
    assert r["arrival_minute"] == 9
    # 到达节点2后零等待：无中途节点名额占用
    assert r["node_wait_delta"] == {}


def test_node_control_waiting_at_or_under_capacity_allowed():
    # 等待10人，限流名额10恰好：等待方案保留
    world = build_world(
        [Edge(1, 1, 2, 4, 100), Edge(2, 2, 3, 4, 100),
         Edge(3, 3, 5, 4, 100), Edge(5, 2, 6, 1, 100)],
        exits=[5, 6], closures=[(5, 4, 8)],
        node_controls=[(2, 4, 8, 10)])
    r = plan(world)
    assert [s["edge_id"] for s in r["steps"]] == [1, 5]
    assert r["arrival_minute"] == 9
    # 等待 [4,8) 逐分钟占 10
    assert r["node_wait_delta"] == {(2, m): 10 for m in range(4, 8)}


def test_node_control_contention_between_groups_staggers():
    # A、B 都 1->2(4)->6(1)，通道5封闭[4,6)；节点2第4、5分钟名额15。
    # A 10人高优先级等待[4,6)；B 10人不能在第4分钟等（20>15），
    # 推迟上游通道1到第2分钟进入 -> 第6分到节点2立即进通道5，两组都到达7
    world = build_world(
        [Edge(1, 1, 2, 4, 100), Edge(2, 2, 3, 9, 100),
         Edge(3, 3, 5, 9, 100), Edge(5, 2, 6, 1, 100)],
        exits=[5, 6], closures=[(5, 4, 6)],
        node_controls=[(2, 4, 6, 15)])
    a = plan(world, gid=1, size=10, deadline=40)
    for k, v in a["load_delta"].items():
        world.fixed_load[k] = world.fixed_load.get(k, 0) + v
    for k, v in a["node_wait_delta"].items():
        world.fixed_node_load[k] = world.fixed_node_load.get(k, 0) + v
    b = plan(world, gid=2, size=10, deadline=40)
    assert b["status"] == "scheduled"
    assert b["steps"][0]["enter_minute"] == 2
    assert b["steps"][1]["enter_minute"] == 6
    assert b["arrival_minute"] == 7


def test_delay_replan_reserved_pre_recovery_wait_overquota_fails():
    # 节点滞留改编：current=2、恢复分钟8，滞留节点2限流[2,8) 9人，组10人。
    # 恢复前固定等待[2,8)本身每分钟10>9，余程无解
    world = build_world(
        [Edge(5, 2, 6, 1, 100)], exits=[6],
        node_controls=[(2, 2, 8, 9)])
    reserved = {(2, m): 10 for m in range(2, 8)}
    r = plan(world, origin=2, earliest=8, now=2, deadline=40,
             start_is_intermediate=True, fixed_node_wait=reserved)
    assert r["status"] == "failed"
    assert r["reason"] == "INSUFFICIENT_NODE_CAPACITY"


def test_delay_replan_reserved_wait_within_capacity_succeeds():
    world = build_world(
        [Edge(5, 2, 6, 1, 100)], exits=[6],
        node_controls=[(2, 2, 8, 10)])
    reserved = {(2, m): 10 for m in range(2, 8)}
    r = plan(world, origin=2, earliest=8, now=2, deadline=40,
             start_is_intermediate=True, fixed_node_wait=reserved)
    assert r["status"] == "scheduled"
    assert r["steps"][0]["enter_minute"] == 8
    assert r["arrival_minute"] == 9
