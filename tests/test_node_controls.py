"""中途节点计划滞留限流（node-controls）集成测试。

覆盖：
* 限流只计"前一通道离开到进入下一通道之间"的中途等待：出发前起点等待、
  通道在途与抵达出口后不计；相邻段首尾相接无等待也不占名额；
* 限流迫使等待推迟、上游通行推迟或改走路线，在既有截止/优先级/择优规则
  下选最优；多组按优先级共享节点每分钟名额（高优先级先占，低优先级错峰）；
* 节点不存在 / 出口节点 / 时段无效 / 上限非正 / 起点早于当前分钟 -> 422；
* 同节点时段重叠 -> 409；新上限使已出发组固定余程的计划滞留超额 -> 409；
* 逐分钟计划停留人数查询、修订（node-controls）与场景重置清除。
"""
import os
import tempfile

import pytest
from fastapi.testclient import TestClient

_tmpdir = tempfile.mkdtemp(prefix="evac-test-nodes-")
os.environ["EVAC_DB_PATH"] = os.path.join(_tmpdir, "test.db")
os.environ["EVAC_SEED"] = "false"

from app.main import app  # noqa: E402


def sc_with(group=None, closures=None, passages=None):
    base_passages = [
        {"id": 1, "src": 1, "dst": 2, "minutes": 4, "capacity": 100},
        {"id": 2, "src": 2, "dst": 3, "minutes": 9, "capacity": 100},
        {"id": 3, "src": 3, "dst": 5, "minutes": 9, "capacity": 100},
        {"id": 5, "src": 2, "dst": 6, "minutes": 1, "capacity": 100},
    ]
    return {
        "passages": passages or base_passages,
        "exits": [5, 6],
        "closures": closures or [],
        "groups": group if group is not None else [{
            "id": 1, "size": 10, "origin": 1, "earliest_departure": 0,
            "priority": 10, "deadline": 40}],
    }


@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c


def reset(c, payload=None):
    resp = c.post("/scenario", json=payload or sc_with())
    assert resp.status_code == 200, resp.text
    return resp


def by_id(plan):
    return {g["group_id"]: g for g in plan["groups"]}


def controls(c):
    return c.get("/node-controls").json()["controls"]


def stays(c, node, minute=None):
    url = f"/nodes/{node}/stays"
    if minute is not None:
        url += f"?minute={minute}"
    return c.get(url).json()


# --------------------------------------------------------------------------
# 语义：只计中途等待
# --------------------------------------------------------------------------

def test_origin_wait_is_not_counted(client):
    # 组的起点就是节点 2，第 0..10 分钟限流到 1 人：起点等待免费，立即出发
    reset(client, sc_with(group=[{
        "id": 1, "size": 10, "origin": 2, "earliest_departure": 0,
        "priority": 10, "deadline": 40}]))
    resp = client.post("/node-controls", json={
        "node_id": 2, "start_minute": 0, "end_minute": 10, "capacity": 1})
    assert resp.status_code == 201
    g = by_id(resp.json())[1]
    assert g["route"] == [5]
    assert g["departure_minute"] == 0
    assert g["arrival_minute"] == 1
    # 起点等待不产生任何计划停留
    assert all(m["used"] == 0 for m in stays(client, 2)["minutes"])


def test_back_to_back_steps_not_counted(client):
    # 1->2 第 4 分钟到达，立即进入通道 5（无等待间隙）：不占节点 2 名额
    reset(client)
    resp = client.post("/node-controls", json={
        "node_id": 2, "start_minute": 4, "end_minute": 6, "capacity": 1})
    assert resp.status_code == 201
    g = by_id(resp.json())[1]
    assert g["route"] == [1, 5]
    assert g["steps"][1]["enter_minute"] == 4
    assert [m for m in stays(client, 2)["minutes"] if m["used"]] == []


def test_after_exit_not_counted(client):
    # 抵达出口（节点 6）后不计：出口根本不可登记限流（422）
    reset(client)
    resp = client.post("/node-controls", json={
        "node_id": 6, "start_minute": 0, "end_minute": 10, "capacity": 1})
    assert resp.status_code == 422


# --------------------------------------------------------------------------
# 限流迫使推迟上游 / 改道 / 错峰
# --------------------------------------------------------------------------

def test_limit_forces_detour_when_upstream_shift_blocked(client):
    # 1->2(4)，通道5封闭[4,8) 等待走6到达9；绕行[2(4),3(4)]到12。节点2
    # 限流[4,8) 5人使等待超额；通道1也封闭[4,8)，无法推迟上游在第8分钟恰
    # 好到达节点2（最早第8进入通道1、13到达）-> 第4分钟绕行到达12最优
    passages = [
        {"id": 1, "src": 1, "dst": 2, "minutes": 4, "capacity": 100},
        {"id": 2, "src": 2, "dst": 3, "minutes": 4, "capacity": 100},
        {"id": 3, "src": 3, "dst": 5, "minutes": 4, "capacity": 100},
        {"id": 5, "src": 2, "dst": 6, "minutes": 1, "capacity": 100}]
    reset(client, sc_with(passages=passages, closures=[
        {"edge_id": 5, "start_minute": 4, "end_minute": 8},
        {"edge_id": 1, "start_minute": 4, "end_minute": 8}]))
    resp = client.post("/node-controls", json={
        "node_id": 2, "start_minute": 4, "end_minute": 8, "capacity": 5})
    assert resp.status_code == 201
    g = by_id(resp.json())[1]
    assert g["route"] == [1, 2, 3]
    assert g["arrival_minute"] == 12


def test_limit_shifts_wait_upstream_to_origin_for_better_arrival(client):
    # 通道1通行2分钟：第2分钟到节点2，通道5封闭[2,8)。限流节点2[2,8)
    # 5人使在节点2等待超额；最优为在起点把通道1推迟到第6分钟进入、第8
    # 分钟到节点2立即进通道5，第9分钟到达（绕行需14）。起点等待免费。
    passages = [dict(p) for p in sc_with()["passages"]]
    passages[0]["minutes"] = 2
    passages[1]["minutes"] = 4
    passages[2]["minutes"] = 4
    reset(client, sc_with(
        passages=passages,
        closures=[{"edge_id": 5, "start_minute": 2, "end_minute": 8}]))
    resp = client.post("/node-controls", json={
        "node_id": 2, "start_minute": 2, "end_minute": 8, "capacity": 5})
    assert resp.status_code == 201
    g = by_id(resp.json())[1]
    assert g["route"] == [1, 5]
    assert [(s["enter_minute"], s["exit_minute"]) for s in g["steps"]] == [
        (6, 8), (8, 9)]
    assert g["arrival_minute"] == 9


def test_groups_stagger_sharing_node_quota(client):
    # 两组各10人，通道5封闭[4,6)：高优先级组1在节点2等待[4,6)；节点2第
    # 4、5分钟名额15，组2不能在第4分钟等（20>15），推迟通道1到第2分钟
    # 进入 -> 第6分钟到节点2立即进通道5，两组均第7分钟到达
    reset(client, sc_with(group=[
        {"id": 1, "size": 10, "origin": 1, "earliest_departure": 0,
         "priority": 10, "deadline": 40},
        {"id": 2, "size": 10, "origin": 1, "earliest_departure": 0,
         "priority": 5, "deadline": 40}],
        closures=[{"edge_id": 5, "start_minute": 4, "end_minute": 6}]))
    resp = client.post("/node-controls", json={
        "node_id": 2, "start_minute": 4, "end_minute": 6, "capacity": 15})
    assert resp.status_code == 201
    groups = by_id(resp.json())
    assert groups[1]["steps"][1]["enter_minute"] == 6
    assert groups[2]["steps"][0]["enter_minute"] == 2
    assert groups[2]["steps"][1]["enter_minute"] == 6
    used = {m["minute"]: m["used"] for m in stays(client, 2)["minutes"]
            if m["used"]}
    assert used == {4: 10, 5: 10}  # 错峰后每分钟仅组1的10人


# --------------------------------------------------------------------------
# 无效输入：422
# --------------------------------------------------------------------------

def test_unknown_node_rejected(client):
    reset(client)
    resp = client.post("/node-controls", json={
        "node_id": 99, "start_minute": 0, "end_minute": 5, "capacity": 3})
    assert resp.status_code == 422
    assert controls(client) == []


def test_exit_node_rejected(client):
    reset(client)
    for node in (5, 6):
        resp = client.post("/node-controls", json={
            "node_id": node, "start_minute": 0, "end_minute": 5,
            "capacity": 3})
        assert resp.status_code == 422
    assert controls(client) == []


def test_invalid_window_and_capacity_rejected(client):
    reset(client)
    assert client.post("/node-controls", json={
        "node_id": 2, "start_minute": 5, "end_minute": 5,
        "capacity": 3}).status_code == 422
    assert client.post("/node-controls", json={
        "node_id": 2, "start_minute": 8, "end_minute": 5,
        "capacity": 3}).status_code == 422
    assert client.post("/node-controls", json={
        "node_id": 2, "start_minute": 0, "end_minute": 5,
        "capacity": 0}).status_code == 422
    assert client.post("/node-controls", json={
        "node_id": 2, "start_minute": 0, "end_minute": 5,
        "capacity": -2}).status_code == 422
    assert controls(client) == []


def test_start_before_current_minute_rejected(client):
    reset(client)
    assert client.post("/tick", json={"minutes": 3}).status_code == 200
    assert client.post("/node-controls", json={
        "node_id": 2, "start_minute": 2, "end_minute": 5,
        "capacity": 3}).status_code == 422
    # 起点等于当前分钟允许
    assert client.post("/node-controls", json={
        "node_id": 2, "start_minute": 3, "end_minute": 5,
        "capacity": 100}).status_code == 201


# --------------------------------------------------------------------------
# 时段重叠：409
# --------------------------------------------------------------------------

def test_overlap_rejected_and_half_open_endpoint_allowed(client):
    reset(client)
    assert client.post("/node-controls", json={
        "node_id": 2, "start_minute": 5, "end_minute": 10,
        "capacity": 10}).status_code == 201
    for start, end in ((8, 12), (0, 6), (6, 8), (0, 30)):
        resp = client.post("/node-controls", json={
            "node_id": 2, "start_minute": start, "end_minute": end,
            "capacity": 10})
        assert resp.status_code == 409
        assert resp.json()["error"] == "NODE_CONTROL_OVERLAP"
        assert resp.json()["conflicts"][0]["existing"] == [5, 10]
    # 左闭右开：起点等于已有终点不重叠
    assert client.post("/node-controls", json={
        "node_id": 2, "start_minute": 10, "end_minute": 12,
        "capacity": 10}).status_code == 201
    # 不同节点互不相干
    assert client.post("/node-controls", json={
        "node_id": 3, "start_minute": 5, "end_minute": 10,
        "capacity": 10}).status_code == 201
    assert len(controls(client)) == 3


# --------------------------------------------------------------------------
# 已出发组固定余程超额：409（整次拒绝、无修订、状态不变）
# --------------------------------------------------------------------------

def test_overloading_departed_group_rejected(client):
    # 通道5封闭[4,6)：组固定后会在节点2等待 [4,6)，每分钟10人
    reset(client, sc_with(closures=[
        {"edge_id": 5, "start_minute": 4, "end_minute": 6}]))
    assert client.post("/tick", json={"minutes": 4}).status_code == 200
    n0 = len(client.get("/revisions").json()["revisions"])
    plan_before = client.get("/plan").json()

    resp = client.post("/node-controls", json={
        "node_id": 2, "start_minute": 4, "end_minute": 6, "capacity": 5})
    assert resp.status_code == 409
    body = resp.json()
    assert body["error"] == "NODE_CONTROL_OVERLOADS_DEPARTED_GROUP"
    conflict = body["conflicts"][0]
    assert conflict["node_id"] == 2
    assert conflict["minute"] == 4
    assert conflict["used"] == 10
    assert conflict["capacity"] == 5
    assert conflict["groups"] == [{"group_id": 1, "size": 10}]

    assert controls(client) == []
    assert client.get("/plan").json() == plan_before
    assert len(client.get("/revisions").json()["revisions"]) == n0

    # 名额能容纳固定等待（10 人）时允许登记
    assert client.post("/node-controls", json={
        "node_id": 2, "start_minute": 4, "end_minute": 6,
        "capacity": 10}).status_code == 201


def test_window_missing_fixed_wait_is_allowed(client):
    # 固定等待为 [4,6)；限流 [6,10) 与之不相交 -> 允许，固定组路线不变
    reset(client, sc_with(closures=[
        {"edge_id": 5, "start_minute": 4, "end_minute": 6}]))
    assert client.post("/tick", json={"minutes": 4}).status_code == 200
    resp = client.post("/node-controls", json={
        "node_id": 2, "start_minute": 6, "end_minute": 10, "capacity": 1})
    assert resp.status_code == 201
    g = by_id(resp.json())[1]
    assert g["status"] == "fixed"
    assert g["route"] == [1, 5]


# --------------------------------------------------------------------------
# 查询、修订、重置
# --------------------------------------------------------------------------

def test_stays_query_lists_per_minute_planned_and_capacity(client):
    reset(client, sc_with(closures=[
        {"edge_id": 5, "start_minute": 4, "end_minute": 6}]))
    assert client.post("/node-controls", json={
        "node_id": 2, "start_minute": 4, "end_minute": 8,
        "capacity": 12}).status_code == 201
    data = stays(client, 2)
    assert data["node_id"] == 2
    assert data["node_controls"] == [
        {"node_id": 2, "start_minute": 4, "end_minute": 8, "capacity": 12}]
    minutes = {m["minute"]: m for m in data["minutes"]}
    # 限流覆盖分钟即使无等待也列出
    for m in (4, 5, 6, 7):
        assert m in minutes
        assert minutes[m]["capacity"] == 12
    # 组在节点2等待 [4,6)，每分钟10人
    assert minutes[4]["used"] == 10
    assert minutes[5]["used"] == 10
    assert minutes[4]["groups"] == [
        {"group_id": 1, "size": 10, "status": "scheduled"}]
    assert minutes[6]["used"] == 0
    # ?minute=n
    one = stays(client, 2, minute=5)["minutes"]
    assert [m["minute"] for m in one] == [5]
    # 未知节点 404
    assert client.get("/nodes/99/stays").status_code == 404


def test_registration_creates_revision_and_reset_clears(client):
    reset(client)
    n0 = len(client.get("/revisions").json()["revisions"])
    client.post("/node-controls", json={
        "node_id": 2, "start_minute": 4, "end_minute": 8, "capacity": 12})
    entries = client.get("/revisions").json()["revisions"]
    assert len(entries) == n0 + 1
    assert entries[-1]["operation"] == "node-controls"
    assert entries[-1]["detail"] == {
        "node_id": 2, "start_minute": 4, "end_minute": 8, "capacity": 12}

    # 场景重置清除节点限流（旧修订保留）
    assert client.post("/scenario", json=sc_with()).status_code == 200
    assert controls(client) == []
    assert client.get(f"/revisions/{entries[-1]['rev']}").status_code == 200


# --------------------------------------------------------------------------
# 与现场节点滞留报告的交互
# --------------------------------------------------------------------------

def test_delay_report_overquota_wait_becomes_awaiting_rescue(client):
    # 唯一路线 1->2(2)->6(1)：第2分钟到节点2。登记节点2限流[2,8) 9人后
    # 报告滞留至第8分钟：恢复前固定等待[2,8)每分钟10>9，无余程 -> 待救援
    passages = [
        {"id": 1, "src": 1, "dst": 2, "minutes": 2, "capacity": 30},
        {"id": 5, "src": 2, "dst": 6, "minutes": 1, "capacity": 30}]
    reset(client, sc_with(passages=passages))
    assert client.post("/tick", json={"minutes": 2}).status_code == 200
    assert client.post("/node-controls", json={
        "node_id": 2, "start_minute": 2, "end_minute": 8,
        "capacity": 9}).status_code == 201
    resp = client.post("/groups/1/delay-report", json={
        "node_id": 2, "recovery_minute": 8})
    assert resp.status_code == 200
    g = by_id(resp.json())[1]
    assert g["status"] == "awaiting_rescue"
    assert g["reason"] == "INSUFFICIENT_NODE_CAPACITY"


def test_delay_report_within_quota_resumes(client):
    passages = [
        {"id": 1, "src": 1, "dst": 2, "minutes": 2, "capacity": 30},
        {"id": 5, "src": 2, "dst": 6, "minutes": 1, "capacity": 30}]
    reset(client, sc_with(passages=passages))
    assert client.post("/tick", json={"minutes": 2}).status_code == 200
    assert client.post("/node-controls", json={
        "node_id": 2, "start_minute": 2, "end_minute": 8,
        "capacity": 10}).status_code == 201
    resp = client.post("/groups/1/delay-report", json={
        "node_id": 2, "recovery_minute": 8})
    g = by_id(resp.json())[1]
    assert g["status"] == "fixed"
    assert g["arrival_minute"] == 9
    assert g["prefix_step_count"] == 1
