"""先导组（放行前置）关系与非负放行间隔集成测试。

覆盖：
* 登记/替换：每组至多一项，再次登记替换旧关系；成功返回完整编排并写一条
  group-prerequisites 修订（detail 含被替换的旧关系）；
* 校验：目标组/先导组不存在 422、负间隔 422；目标组已出发、自依赖、
  多级成环 409（PREREQUISITE_NOT_ACCEPTABLE，原因码区分），整次拒绝、
  状态与修订历史不变；
* 放行依据：后继最早出发不早于自身最早出发与"先导规划到达+间隔"；
  多级关系按依赖顺序安排（先安排先导，即使后继优先级更高）；每轮在可
  安排组中沿用优先级/最早出发/组编号/路线并列规则；截止与容量继续生效；
* 前置未满足：先导待救援/救援中/无可行路线/自身前置未满足时后继为
  blocked（不写路线、不占通道），原因码随状态变化；先导落定后随重排转正，
  阻塞沿多级链传播；
* 已人工救援送达的先导以实际送达分钟为锚点（不回写规划到达）；结案重排；
* 时间推进只固定已出发组，未出发后继继续按锚点重排，已出发决定不追溯；
* 场景重置清空全部先导关系。
"""
import os
import tempfile

import pytest
from fastapi.testclient import TestClient

_tmpdir = tempfile.mkdtemp(prefix="evac-test-pre-")
os.environ["EVAC_DB_PATH"] = os.path.join(_tmpdir, "test.db")
os.environ["EVAC_SEED"] = "false"

from app.main import app  # noqa: E402


# 1->2(e1,4,cap10)->6(e2,2,cap10)；1->4(e3,3)->6(e4,2)
SCENARIO = {
    "passages": [
        {"id": 1, "src": 1, "dst": 2, "minutes": 4, "capacity": 10},
        {"id": 2, "src": 2, "dst": 6, "minutes": 2, "capacity": 10},
        {"id": 3, "src": 1, "dst": 4, "minutes": 3, "capacity": 10},
        {"id": 4, "src": 4, "dst": 6, "minutes": 2, "capacity": 10},
    ],
    "exits": [6], "closures": [],
    "groups": [
        {"id": 1, "size": 10, "origin": 1, "earliest_departure": 0,
         "priority": 10, "deadline": 30},
        {"id": 2, "size": 10, "origin": 1, "earliest_departure": 0,
         "priority": 20, "deadline": 30},
    ],
}


def by_id(plan):
    return {g["group_id"]: g for g in plan["groups"]}


@pytest.fixture()
def client():
    with TestClient(app) as c:
        resp = c.post("/scenario", json=SCENARIO)
        assert resp.status_code == 200
        yield c


def register(c, group_id, leader_id, gap, expected=201):
    resp = c.post("/group-prerequisites",
                  json={"group_id": group_id,
                        "leader_group_id": leader_id,
                        "release_gap": gap})
    assert resp.status_code == expected, resp.text
    return resp


# --------------------------------------------------------------------------
# 登记 / 替换 / 查询
# --------------------------------------------------------------------------

def test_register_basic_release_gap_and_basis(client):
    # 无关系：高优先级组2 占最快路线 [3,4]（5 分钟到达）；组1 走 [1,2] 6 到达
    plan = by_id(client.get("/plan").json())
    assert plan[2]["route"] == [3, 4] and plan[2]["arrival_minute"] == 5
    assert plan[1]["route"] == [1, 2] and plan[1]["arrival_minute"] == 6

    # 组2 以组1 为先导、间隔 2：依赖顺序使组1 先安排（拿到 [3,4] 到达5），
    # 组2 最早放行 5+2=7，同走最快 [3,4]（容量允许）到达 12
    resp = register(client, 2, 1, 2)
    plan = by_id(resp.json())
    g1, g2 = plan[1], plan[2]
    assert g1["route"] == [3, 4] and g1["arrival_minute"] == 5
    assert g2["status"] == "scheduled"
    assert g2["route"] == [3, 4]
    assert g2["departure_minute"] == 7 and g2["arrival_minute"] == 12
    assert g2["leader_group_id"] == 1 and g2["release_gap"] == 2
    basis = g2["release_basis"]
    assert basis["satisfied"] is True
    assert basis["basis_type"] == "planned_arrival"
    assert basis["leader_status"] == "scheduled"
    assert basis["leader_arrival_minute"] == 5
    assert basis["earliest_release_minute"] == 7
    # 无关系的组不附先导字段（兼容）
    assert g1["leader_group_id"] is None and g1["release_basis"] is None


def test_own_earliest_departure_also_limits(client):
    # 组2 自身最早出发 10、先导到达5+间隔0=5：受自身最早出发约束 10 放行
    client.post("/group-corrections",
                json=[{"group_id": 2, "earliest_departure": 10}])
    resp = register(client, 2, 1, 0)
    g2 = by_id(resp.json())[2]
    assert g2["departure_minute"] == 10


def test_register_replaces_old_relation(client):
    register(client, 2, 1, 4)
    resp = register(client, 2, 1, 0)
    g2 = by_id(resp.json())[2]
    # 间隔0：最早放行=先导到达5，同走 [3,4]，5->10
    assert g2["departure_minute"] == 5 and g2["arrival_minute"] == 10
    last = client.get("/revisions").json()["revisions"][-1]
    assert last["operation"] == "group-prerequisites"
    assert last["detail"] == {
        "group_id": 2, "leader_group_id": 1, "release_gap": 0,
        "replaced": {"leader_group_id": 1, "release_gap": 4}}
    # GET 只有当前一项
    rels = client.get("/group-prerequisites").json()["relations"]
    assert len(rels) == 1
    assert rels[0]["release_gap"] == 0


def test_replace_leader_removes_old_edge(client):
    # 加组3：1->2 起点；组2->组1，再把组2 的先导替换为组3。
    # 若旧边 2->1 未移除，登记 1->2 会误判成环
    client.post("/groups", json=[
        {"id": 3, "size": 10, "origin": 2, "earliest_departure": 0,
         "priority": 1, "deadline": 30}])
    register(client, 2, 1, 0)
    register(client, 3, 1, 0)
    # 替换为 2->3（此时图：3->1, 2->3，无环）
    resp = register(client, 2, 3, 0)
    assert resp.status_code == 201
    rels = client.get("/group-prerequisites").json()["relations"]
    edges = {r["group_id"]: r["leader_group_id"] for r in rels}
    assert edges == {2: 3, 3: 1}


# --------------------------------------------------------------------------
# 拒绝路径（整次拒绝、状态与修订历史不变）
# --------------------------------------------------------------------------

def test_unknown_group_or_negative_gap_422(client):
    assert client.post("/group-prerequisites",
                       json={"group_id": 99, "leader_group_id": 1,
                             "release_gap": 0}).status_code == 422
    assert client.post("/group-prerequisites",
                       json={"group_id": 2, "leader_group_id": 99,
                             "release_gap": 0}).status_code == 422
    assert client.post("/group-prerequisites",
                       json={"group_id": 2, "leader_group_id": 1,
                             "release_gap": -1}).status_code == 422


def test_self_dependency_rejected(client):
    resp = register(client, 2, 2, 0, expected=409)
    body = resp.json()
    assert body["error"] == "PREREQUISITE_NOT_ACCEPTABLE"
    assert body["conflicts"][0]["reason"] == "SELF_DEPENDENCY"
    # 未产生关系、未产生修订
    assert client.get("/group-prerequisites").json()["relations"] == []
    g2 = by_id(client.get("/plan").json())[2]
    assert g2["leader_group_id"] is None


def test_cycle_rejected_with_cycle_path(client):
    register(client, 2, 1, 0)
    resp = register(client, 1, 2, 0, expected=409)
    conflict = resp.json()["conflicts"][0]
    assert conflict["reason"] == "CYCLE_DETECTED"
    # 环路径包含两个组
    assert set(conflict["cycle"]) == {1, 2}
    # 组1 没有被写入关系
    assert by_id(client.get("/plan").json())[1]["leader_group_id"] is None


def test_multilevel_cycle_rejected(client):
    client.post("/groups", json=[
        {"id": 3, "size": 10, "origin": 2, "earliest_departure": 0,
         "priority": 1, "deadline": 30}])
    register(client, 2, 1, 0)
    register(client, 3, 2, 0)
    # 1->3 形成 1->3->2->1
    resp = register(client, 1, 3, 0, expected=409)
    conflict = resp.json()["conflicts"][0]
    assert conflict["reason"] == "CYCLE_DETECTED"
    assert conflict["cycle"] == [1, 3, 2, 1]
    # 组3 改先导为组1（替换旧边 3->2）后，图为 2->1、3->1，无环
    assert register(client, 3, 1, 0).status_code == 201
    # 此时 1->2 仍成环（2->1）
    assert register(client, 1, 2, 0, expected=409).status_code == 409


def test_target_departed_rejected_after_tick(client):
    # 组1 t=0 出发；tick 到 1 即 fixed
    client.post("/tick", json={"minutes": 1})
    resp = register(client, 1, 2, 0, expected=409)
    assert resp.json()["conflicts"][0]["reason"] == "TARGET_ALREADY_DEPARTED"
    assert client.get("/group-prerequisites").json()["relations"] == []


def test_rejected_request_creates_no_revision(client):
    revs_before = len(client.get("/revisions").json()["revisions"])
    register(client, 2, 2, 0, expected=409)
    register(client, 99, 1, 0, expected=422)
    revs_after = len(client.get("/revisions").json()["revisions"])
    assert revs_before == revs_after


# --------------------------------------------------------------------------
# 多级依赖顺序 / 并列规则 / 截止与容量
# --------------------------------------------------------------------------

def test_multi_level_chain_ordered_by_dependency(client):
    # 组3 起点2：组3 -> 组2 -> 组1，间隔全 0。
    client.post("/groups", json=[
        {"id": 3, "size": 10, "origin": 2, "earliest_departure": 0,
         "priority": 99, "deadline": 30}])
    register(client, 2, 1, 0)
    register(client, 3, 2, 0)
    plan = by_id(client.get("/plan").json())
    # 先导链按依赖顺序安排：组1 先拿到最快 [3,4] 到达5；
    # 组2 最早放行5，走 [3,4] 到达10；组3 在节点2 最早放行10，e2 10->12
    assert plan[1]["route"] == [3, 4] and plan[1]["arrival_minute"] == 5
    assert plan[2]["departure_minute"] == 5 and plan[2]["arrival_minute"] == 10
    assert plan[3]["departure_minute"] == 10 and plan[3]["arrival_minute"] == 12
    # 容量始终满足
    _assert_capacity_respected(client)


def test_capacity_and_deadline_still_enforced_with_relation(client):
    # 间隔 23：组2 最早放行 = 5+23=28，最快路线 [3,4] 需5 分钟 ->
    # 到达33 > deadline30；改走 [1,2] 需6 分钟同样超时。
    # 前置已满足但自身超截止，应按普通失败处理（不是 blocked）
    resp = register(client, 2, 1, 23)
    g2 = by_id(resp.json())[2]
    assert g2["status"] == "failed"
    assert g2["release_basis"]["satisfied"] is True
    assert g2["departure_minute"] is None


def test_scheduling_round_priority_tiebreak_within_ready_set(client):
    # 组1、组4 互不依赖且都在节点1；组2 依赖组1。第一轮可安排 {1,4}，
    # 按优先级：组4(priority 5) 与组1(priority 10) 中组1 先；组2 第二轮。
    client.post("/groups", json=[
        {"id": 4, "size": 10, "origin": 1, "earliest_departure": 0,
         "priority": 5, "deadline": 30}])
    register(client, 2, 1, 0)
    plan = by_id(client.get("/plan").json())
    # 组1 先占最快路线
    assert plan[1]["route"] == [3, 4] and plan[1]["arrival_minute"] == 5
    # 组4 与组2 随后依优先级（组2 priority 20）在各自可出发时间竞争
    assert plan[2]["departure_minute"] == 5
    _assert_capacity_respected(client)


def _assert_capacity_respected(c):
    caps = {1: 10, 2: 10, 3: 10, 4: 10}
    usage: dict[tuple[int, int], int] = {}
    for g in c.get("/plan").json()["groups"]:
        if g["status"] not in ("scheduled", "fixed"):
            continue
        for step in g["steps"]:
            for m in range(step["enter_minute"], step["exit_minute"]):
                key = (step["edge_id"], m)
                usage[key] = usage.get(key, 0) + 10
    for (edge_id, minute), used in usage.items():
        assert used <= caps[edge_id], (edge_id, minute, used)


# --------------------------------------------------------------------------
# 前置未满足（blocked）
# --------------------------------------------------------------------------

def test_failed_leader_marks_follower_blocked_not_occupying(client):
    # 组1 截止3（最快6 分钟）-> failed；组2 依赖组1 -> blocked
    client.post("/group-corrections",
                json=[{"group_id": 1, "deadline": 3}])
    plan = by_id(client.get("/plan").json())
    assert plan[1]["status"] == "failed"
    resp = register(client, 2, 1, 0)
    g2 = by_id(resp.json())[2]
    assert g2["status"] == "blocked"
    assert g2["reason"] == "LEADER_FAILED"
    assert g2["departure_minute"] is None and g2["steps"] == []
    basis = g2["release_basis"]
    assert basis["satisfied"] is False and basis["reason"] == "LEADER_FAILED"
    assert basis["earliest_release_minute"] is None
    # 不占任何通道
    for edge in (1, 2, 3, 4):
        load = client.get(f"/passages/{edge}/load").json()
        assert all(m["used"] == 0 for m in load["minutes"])


def test_blocked_propagates_through_chain(client):
    client.post("/group-corrections",
                json=[{"group_id": 1, "deadline": 3}])
    client.post("/groups", json=[
        {"id": 3, "size": 10, "origin": 2, "earliest_departure": 0,
         "priority": 1, "deadline": 30}])
    register(client, 2, 1, 0)
    resp = register(client, 3, 2, 1)
    plan = by_id(resp.json())
    assert plan[2]["status"] == "blocked"
    assert plan[2]["release_basis"]["reason"] == "LEADER_FAILED"
    assert plan[3]["status"] == "blocked"
    assert plan[3]["release_basis"]["reason"] == "LEADER_BLOCKED"


def test_blocked_resolves_after_leader_becomes_feasible(client):
    client.post("/group-corrections",
                json=[{"group_id": 1, "deadline": 3}])
    register(client, 2, 1, 0)
    assert by_id(client.get("/plan").json())[2]["status"] == "blocked"
    # 先导纠正为可行 -> 后继转正
    resp = client.post("/group-corrections",
                       json=[{"group_id": 1, "deadline": 30}])
    plan = by_id(resp.json())
    assert plan[1]["status"] == "scheduled"
    g2 = plan[2]
    assert g2["status"] == "scheduled"
    assert g2["departure_minute"] == plan[1]["arrival_minute"]


def test_blocked_group_replans_on_tick_without_departure(client):
    # 间隔10：先导到达5，后继最早15；tick 到3 不固定后继
    register(client, 2, 1, 10)
    resp = client.post("/tick", json={"minutes": 3})
    plan = by_id(resp.json())
    assert plan[1]["status"] == "fixed"      # t=0 出发
    assert plan[2]["status"] == "scheduled"  # 15 才出发，仍未出发
    assert plan[2]["departure_minute"] == 15


def test_blocked_group_is_undeparted_and_correctable(client):
    client.post("/group-corrections",
                json=[{"group_id": 1, "deadline": 3}])
    register(client, 2, 1, 0)
    # blocked 组可纠正（仍属未出发）
    resp = client.post("/group-corrections",
                       json=[{"group_id": 2, "size": 8}])
    assert resp.status_code == 200


# --------------------------------------------------------------------------
# 先导待救援 / 救援中 / 人工实际送达锚点
# --------------------------------------------------------------------------

RESCUE_SCENARIO = {
    "passages": [
        {"id": 1, "src": 1, "dst": 2, "minutes": 4, "capacity": 10},
        {"id": 2, "src": 2, "dst": 6, "minutes": 1, "capacity": 10},
    ],
    "exits": [6],
    "closures": [{"edge_id": 2, "start_minute": 5, "end_minute": 30}],
    "groups": [
        {"id": 1, "size": 10, "origin": 1, "earliest_departure": 0,
         "priority": 10, "deadline": 30},
        # 后继组3 起点即出口6：零步路线，放行分钟=到达分钟，便于断言锚点
        {"id": 3, "size": 10, "origin": 6, "earliest_departure": 0,
         "priority": 5, "deadline": 40},
    ],
}


def _make_leader_awaiting_rescue(c):
    """组1 tick4 固定在节点2，报告恢复6 -> e2 封闭，awaiting_rescue。"""
    c.post("/tick", json={"minutes": 4})
    resp = c.post("/groups/1/delay-report",
                  json={"node_id": 2, "recovery_minute": 6})
    assert resp.status_code == 200
    return resp


def test_leader_awaiting_rescue_blocks_follower(client):
    client.post("/scenario", json=RESCUE_SCENARIO)
    register(client, 3, 1, 2)
    resp = _make_leader_awaiting_rescue(client)
    plan = by_id(resp.json())
    assert plan[1]["status"] == "awaiting_rescue"
    g3 = plan[3]
    assert g3["status"] == "blocked"
    assert g3["release_basis"]["reason"] == "LEADER_AWAITING_RESCUE"
    # 时间推进不自动转正
    resp = client.post("/tick", json={"minutes": 2})
    assert by_id(resp.json())[3]["status"] == "blocked"


def test_leader_rescuing_blocks_and_rescued_uses_actual_delivery(client):
    client.post("/scenario", json=RESCUE_SCENARIO)
    register(client, 3, 1, 2)
    _make_leader_awaiting_rescue(client)
    client.post("/rescue-teams", json={"team_id": 1, "capacity": 10})
    # 任务号取自派遣响应（全库 AUTOINCREMENT，不假定从 1 开始）
    task_id = client.post("/rescue-tasks", json={
        "group_id": 1, "team_id": 1, "node_id": 2,
        "observed_minute": 4}).json()["task_id"]
    # 救援中（派遣不触发重排，/plan 依据实时计算）
    g3 = by_id(client.get("/plan").json())[3]
    assert g3["status"] == "blocked"
    assert g3["release_basis"]["reason"] == "LEADER_RESCUING"

    # tick 到9 结案：实际送达9 -> 后继锚点 9+2=11，零步路线到达11
    client.post("/tick", json={"minutes": 5})
    resp = client.post(f"/rescue-tasks/{task_id}/completion",
                       json={"exit_id": 6, "delivery_minute": 9})
    assert resp.status_code == 200
    plan = by_id(client.get("/plan").json())
    g1, g3 = plan[1], plan[3]
    assert g1["status"] == "rescued"
    # 人工送达不回写规划字段（含旧的规划出发分钟也清空）
    assert g1["exit_id"] is None and g1["arrival_minute"] is None
    assert g1["departure_minute"] is None
    basis = g3["release_basis"]
    assert basis["satisfied"] is True
    assert basis["basis_type"] == "actual_delivery"
    assert basis["leader_arrival_minute"] == 9
    assert basis["earliest_release_minute"] == 11
    assert g3["status"] == "scheduled"
    assert g3["departure_minute"] == 11 and g3["arrival_minute"] == 11
    # 结案修订快照中后继转正字段可见
    rev = client.get("/revisions").json()["revisions"][-1]
    assert rev["operation"] == "rescue-completions"


def test_departed_follower_not_retroactively_changed(client):
    # 组2 先导组1、间隔0；tick6 后两组均 fixed；随后改先导关系对组2 被拒，
    # 且任何重排不追溯组2 的既有路线
    register(client, 2, 1, 0)
    before = by_id(client.get("/plan").json())[2]
    client.post("/tick", json={"minutes": 6})
    after_tick = by_id(client.get("/plan").json())[2]
    assert after_tick["status"] == "fixed"
    assert after_tick["steps"] == before["steps"]
    # 与组2 无关的其他变更（新增组）也不改变其路线
    client.post("/groups", json=[
        {"id": 9, "size": 1, "origin": 4, "earliest_departure": 0,
         "priority": 1, "deadline": 30}])
    g2 = by_id(client.get("/plan").json())[2]
    assert g2["status"] == "fixed" and g2["steps"] == before["steps"]


# --------------------------------------------------------------------------
# 查询 / 重置 / 修订
# --------------------------------------------------------------------------

def test_list_prerequisites_sorted_with_basis(client):
    client.post("/groups", json=[
        {"id": 3, "size": 10, "origin": 2, "earliest_departure": 0,
         "priority": 1, "deadline": 30}])
    register(client, 3, 1, 1)
    register(client, 2, 1, 0)
    payload = client.get("/group-prerequisites").json()
    assert [r["group_id"] for r in payload["relations"]] == [2, 3]
    for r in payload["relations"]:
        assert r["release_basis"]["satisfied"] in (True, False)
        assert r["release_basis"]["leader_status"]


def test_scenario_reset_clears_prerequisites(client):
    register(client, 2, 1, 0)
    assert len(client.get("/group-prerequisites").json()["relations"]) == 1
    client.post("/scenario", json=SCENARIO)
    assert client.get("/group-prerequisites").json()["relations"] == []
    g2 = by_id(client.get("/plan").json())[2]
    assert g2["leader_group_id"] is None
    assert g2["release_gap"] is None and g2["release_basis"] is None


def test_successful_registration_revision_diff_fields(client):
    resp = register(client, 2, 1, 0)
    assert resp.status_code == 201
    rev = client.get("/revisions").json()["revisions"][-1]
    detail = client.get(f"/revisions/{rev['rev']}").json()
    ch = {c["group_id"]: c for c in detail["changes"]}[2]
    assert ch["change"] == "changed"
    assert "leader_group_id" in ch["changed_fields"]
    assert "release_basis" in ch["changed_fields"]
    assert ch["before"]["leader_group_id"] is None
    assert ch["after"]["leader_group_id"] == 1
