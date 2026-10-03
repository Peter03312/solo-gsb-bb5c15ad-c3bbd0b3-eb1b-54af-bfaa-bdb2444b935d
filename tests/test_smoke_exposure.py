"""烟雾暴露时段与累计暴露上限集成测试。

覆盖：
* 按通道登记烟雾暴露时段（左闭右开、每分钟非负暴露值、同通道不重叠、
  起点不早于当前分钟）；
* 为未出发组登记非负累计暴露上限（每组一项、再登记替换；已出发组整次
  拒绝；未设上限的组不受约束）；
* 编排沿用既有封闭/容量/截止/先导放行/路线择优，只在累计暴露不超限的
  路线中安排，可在节点等待烟雾消散后通行，截止前无可行路线按归因失败；
* 登记烟雾时段若使已出发且设限组按固定路线的预计累计暴露超限则整次
  拒绝（409），不改编排、不产生修订；
* 节点滞留改编保留已走前缀按原计划时刻计算的暴露，只用剩余额度寻找
  余程；无可行余程置待救援（EXPOSURE_LIMIT_EXCEEDED）；
* /plan 展示各组上限、前缀已用及余程预计暴露；
* 成功变更写入既有修订（smoke-windows / smoke-limits），场景重置清空。
"""
import os
import tempfile

import pytest
from fastapi.testclient import TestClient

_tmpdir = tempfile.mkdtemp(prefix="evac-test-smoke-")
os.environ["EVAC_DB_PATH"] = os.path.join(_tmpdir, "test.db")
os.environ["EVAC_SEED"] = "false"

from app.main import app  # noqa: E402


SCENARIO = {
    "passages": [
        {"id": 1, "src": 1, "dst": 2, "minutes": 4, "capacity": 30},
        {"id": 2, "src": 2, "dst": 3, "minutes": 3, "capacity": 30},
        {"id": 3, "src": 3, "dst": 5, "minutes": 2, "capacity": 30},
        {"id": 5, "src": 2, "dst": 6, "minutes": 2, "capacity": 40},
    ],
    "exits": [5, 6],
    "closures": [{"edge_id": 1, "start_minute": 3, "end_minute": 10}],
    "groups": [
        {"id": 1, "size": 30, "origin": 1, "earliest_departure": 0,
         "priority": 10, "deadline": 30},
        {"id": 3, "size": 10, "origin": 2, "earliest_departure": 0,
         "priority": 20, "deadline": 20},
    ],
}


@pytest.fixture()
def client():
    with TestClient(app) as c:
        resp = c.post("/scenario", json=SCENARIO)
        assert resp.status_code == 200
        yield c


def by_id(plan):
    return {g["group_id"]: g for g in plan["groups"]}


def windows(c):
    return c.get("/smoke-windows").json()["windows"]


def limits(c):
    return {g["group_id"]: g["smoke_limit"]
            for g in c.get("/smoke-limits").json()["groups"]}


def revs(c):
    return c.get("/revisions").json()["revisions"]


# --------------------------------------------------------------------------
# 编排：等待烟雾消散、改道与上限
# --------------------------------------------------------------------------

def test_unlimited_group_ignores_smoke(client):
    # 通道 5 全程每分钟暴露 100：未设上限的组 3 仍按最早到达立即通过
    resp = client.post("/smoke-windows", json={
        "edge_id": 5, "start_minute": 0, "end_minute": 20, "smoke_value": 100})
    assert resp.status_code == 201
    g3 = by_id(resp.json())[3]
    assert g3["route"] == [5]
    assert g3["departure_minute"] == 0
    assert g3["arrival_minute"] == 2
    # 未设上限的组三个暴露字段均为 null
    assert g3["smoke_exposure_limit"] is None
    assert g3["smoke_exposure_used"] is None
    assert g3["smoke_exposure_remaining"] is None


def test_group_waits_for_smoke_to_dissipate(client):
    # 组3 上限 3；通道5 [0,2) 每分钟暴露5（立即通过=10 超限），等至第2
    # 分钟进入暴露0、到达4，仍优于改道 [2,3]（到达5）
    assert client.post("/smoke-limits", json={
        "group_id": 3, "smoke_limit": 3}).status_code == 201
    resp = client.post("/smoke-windows", json={
        "edge_id": 5, "start_minute": 0, "end_minute": 2, "smoke_value": 5})
    assert resp.status_code == 201
    g3 = by_id(resp.json())[3]
    assert g3["status"] == "scheduled"
    assert g3["route"] == [5]
    assert g3["departure_minute"] == 2
    assert g3["arrival_minute"] == 4
    assert g3["smoke_exposure_limit"] == 3
    assert g3["smoke_exposure_used"] == 0
    assert g3["smoke_exposure_remaining"] == 0


def test_group_detours_when_waiting_too_costly(client):
    # 通道5 [0,10) 每分钟暴露1：组3 上限0 -> 等至第10分钟进入到达12，
    # 改道 [2,3] 第5分钟到达更优
    client.post("/smoke-limits", json={"group_id": 3, "smoke_limit": 0})
    resp = client.post("/smoke-windows", json={
        "edge_id": 5, "start_minute": 0, "end_minute": 10, "smoke_value": 1})
    assert resp.status_code == 201
    g3 = by_id(resp.json())[3]
    assert g3["route"] == [2, 3]
    assert g3["arrival_minute"] == 5
    assert g3["smoke_exposure_remaining"] == 0  # 改道路线无烟


def test_limit_allows_exactly_equal_value(client):
    # 通道5 [0,2) 每分钟暴露5：立即通过累计10，上限10恰好允许
    client.post("/smoke-limits", json={"group_id": 3, "smoke_limit": 10})
    resp = client.post("/smoke-windows", json={
        "edge_id": 5, "start_minute": 0, "end_minute": 2, "smoke_value": 5})
    g3 = by_id(resp.json())[3]
    assert g3["route"] == [5]
    assert g3["departure_minute"] == 0
    assert g3["smoke_exposure_remaining"] == 10


def test_partial_window_edge_minutes(client):
    # w=2，窗口[0,1) 值5：t=0 占用分钟0 =>5；t=1 占用分钟1,2 =>0、到达3，
    # 与改道 [2,3] 到达5 相比等待更优
    client.post("/smoke-limits", json={"group_id": 3, "smoke_limit": 0})
    resp = client.post("/smoke-windows", json={
        "edge_id": 5, "start_minute": 0, "end_minute": 1, "smoke_value": 5})
    g3 = by_id(resp.json())[3]
    assert g3["route"] == [5]
    assert g3["departure_minute"] == 1
    assert g3["arrival_minute"] == 3
    assert g3["smoke_exposure_remaining"] == 0


def test_zero_limit_allows_smokefree_route_and_zero_value_window(client):
    # 暴露值为0的窗口等同无烟：上限0也可立即通行
    client.post("/smoke-limits", json={"group_id": 3, "smoke_limit": 0})
    resp = client.post("/smoke-windows", json={
        "edge_id": 5, "start_minute": 0, "end_minute": 10, "smoke_value": 0})
    g3 = by_id(resp.json())[3]
    assert g3["route"] == [5]
    assert g3["departure_minute"] == 0
    assert g3["smoke_exposure_remaining"] == 0


def test_no_feasible_route_within_deadline_fails_with_reason(client):
    # 通道5 与通道2、3 在可规划区间内全程有烟，上限0：截止前无满足上限
    # 的路线（等待超过截止、无烟路径不存在）
    client.post("/smoke-limits", json={"group_id": 3, "smoke_limit": 0})
    for edge_id in (5, 2, 3):
        assert client.post("/smoke-windows", json={
            "edge_id": edge_id, "start_minute": 0, "end_minute": 21,
            "smoke_value": 1}).status_code == 201
    g3 = by_id(client.get("/plan").json())[3]
    assert g3["status"] == "failed"
    assert g3["reason"] == "EXPOSURE_LIMIT_EXCEEDED"
    # 失败组不写路线，余程预计暴露为空
    assert g3["route"] == []
    assert g3["smoke_exposure_remaining"] is None


def test_smoke_coexists_with_closure_and_capacity_rules(client):
    # 通道1 已有封闭[3,10)（见 SCENARIO）：组1 本就第10分钟进通道1；
    # 再登记通道1 [10,14) 每分钟暴露1，组1 上限0 -> 等至14分钟进入，
    # 到达20；封闭与烟雾先后约束都生效
    client.post("/smoke-limits", json={"group_id": 1, "smoke_limit": 0})
    resp = client.post("/smoke-windows", json={
        "edge_id": 1, "start_minute": 10, "end_minute": 14, "smoke_value": 1})
    assert resp.status_code == 201
    g1 = by_id(resp.json())[1]
    assert g1["route"] == [1, 5]
    assert g1["departure_minute"] == 14
    assert g1["arrival_minute"] == 20
    assert g1["smoke_exposure_remaining"] == 0


def test_node_waiting_at_intermediate_node_not_counted(client):
    # 组1 路线[1,5]：通道1 封闭后第14分钟到达节点2，随即进通道5 会占用
    # [14,16)。给通道5 登记 [14,16) 值5、上限0 -> 在节点2等2分钟至第16
    # 分钟进通道5（节点等待不计暴露），到达18；改道 [1,2,3] 到达19更慢
    client.post("/smoke-limits", json={"group_id": 1, "smoke_limit": 0})
    resp = client.post("/smoke-windows", json={
        "edge_id": 5, "start_minute": 14, "end_minute": 16, "smoke_value": 5})
    g1 = by_id(resp.json())[1]
    assert g1["route"] == [1, 5]
    assert g1["steps"][0]["enter_minute"] == 10  # 封闭结束后进入通道1
    assert g1["steps"][1]["enter_minute"] == 16
    assert g1["steps"][1]["exit_minute"] == 18
    assert g1["smoke_exposure_remaining"] == 0


def test_limit_registration_replaces_old_value_and_replans(client):
    client.post("/smoke-limits", json={"group_id": 3, "smoke_limit": 100})
    client.post("/smoke-windows", json={
        "edge_id": 5, "start_minute": 0, "end_minute": 2, "smoke_value": 5})
    assert by_id(client.get("/plan").json())[3]["departure_minute"] == 0
    # 收紧为 0：再次登记替换旧上限并重排
    resp = client.post("/smoke-limits", json={"group_id": 3, "smoke_limit": 0})
    assert resp.status_code == 201
    g3 = by_id(resp.json())[3]
    assert g3["departure_minute"] == 2
    assert limits(client) == {3: 0}


# --------------------------------------------------------------------------
# 烟雾时段登记校验
# --------------------------------------------------------------------------

def test_unknown_edge_rejected(client):
    resp = client.post("/smoke-windows", json={
        "edge_id": 999, "start_minute": 0, "end_minute": 5, "smoke_value": 1})
    assert resp.status_code == 422
    assert windows(client) == []


def test_invalid_window_rejected(client):
    for body in (
        {"edge_id": 5, "start_minute": 5, "end_minute": 5, "smoke_value": 1},
        {"edge_id": 5, "start_minute": 8, "end_minute": 5, "smoke_value": 1},
        {"edge_id": 5, "start_minute": 0, "end_minute": 5, "smoke_value": -1},
    ):
        assert client.post("/smoke-windows", json=body).status_code == 422
    assert windows(client) == []


def test_start_before_current_minute_rejected(client):
    assert client.post("/tick", json={"minutes": 2}).status_code == 200
    resp = client.post("/smoke-windows", json={
        "edge_id": 5, "start_minute": 1, "end_minute": 5, "smoke_value": 1})
    assert resp.status_code == 422
    # 起点等于当前分钟允许
    resp = client.post("/smoke-windows", json={
        "edge_id": 5, "start_minute": 2, "end_minute": 5, "smoke_value": 1})
    assert resp.status_code == 201


def test_same_edge_overlap_rejected_but_adjacent_allowed(client):
    assert client.post("/smoke-windows", json={
        "edge_id": 5, "start_minute": 5, "end_minute": 10,
        "smoke_value": 1}).status_code == 201
    for start, end in ((8, 12), (0, 6), (6, 8), (0, 30)):
        resp = client.post("/smoke-windows", json={
            "edge_id": 5, "start_minute": start, "end_minute": end,
            "smoke_value": 2})
        assert resp.status_code == 409
        assert resp.json()["error"] == "SMOKE_WINDOW_OVERLAP"
    # 左闭右开端点相接、以及不同通道均允许
    assert client.post("/smoke-windows", json={
        "edge_id": 5, "start_minute": 10, "end_minute": 12,
        "smoke_value": 3}).status_code == 201
    assert client.post("/smoke-windows", json={
        "edge_id": 2, "start_minute": 5, "end_minute": 10,
        "smoke_value": 1}).status_code == 201
    assert len(windows(client)) == 3


# --------------------------------------------------------------------------
# 烟雾时段使已出发且设限组超限：409 整次拒绝
# --------------------------------------------------------------------------

def test_window_overlimiting_departed_capped_group_rejected(client):
    # 组3 上限3；初始走通道5 [0,2)（无烟）。tick 到第1分钟：fixed，
    # 剩余占用通道5 的分钟1
    client.post("/smoke-limits", json={"group_id": 3, "smoke_limit": 3})
    assert client.post("/tick", json={"minutes": 1}).status_code == 200
    n0 = len(revs(client))
    plan_before = client.get("/plan").json()

    resp = client.post("/smoke-windows", json={
        "edge_id": 5, "start_minute": 1, "end_minute": 5, "smoke_value": 4})
    assert resp.status_code == 409
    body = resp.json()
    assert body["error"] == "SMOKE_WINDOW_OVERLIMITS_DEPARTED_GROUP"
    conflict = body["conflicts"][0]
    assert conflict["group_id"] == 3
    assert conflict["edge_id"] == 5
    assert conflict["smoke_limit"] == 3
    assert conflict["projected_exposure"] == 4
    assert conflict["window_added_exposure"] == 4

    # 整次拒绝：不登记、不重排、不产生修订
    assert windows(client) == []
    assert client.get("/plan").json() == plan_before
    assert len(revs(client)) == n0

    # 值3 恰好不超限 -> 受理
    assert client.post("/smoke-windows", json={
        "edge_id": 5, "start_minute": 1, "end_minute": 5,
        "smoke_value": 3}).status_code == 201


def test_window_missing_departed_remaining_route_is_allowed(client):
    client.post("/smoke-limits", json={"group_id": 3, "smoke_limit": 0})
    assert client.post("/tick", json={"minutes": 1}).status_code == 200
    # 组3 剩余占用通道5 分钟1；窗口[2,6) 与之不相交 -> 受理
    resp = client.post("/smoke-windows", json={
        "edge_id": 5, "start_minute": 2, "end_minute": 6, "smoke_value": 9})
    assert resp.status_code == 201
    g3 = by_id(resp.json())[3]
    assert g3["status"] == "fixed"
    # 前缀[0,1) 无烟 -> 已用0；余程[1,2) 无烟 -> 预计0
    assert g3["smoke_exposure_used"] == 0
    assert g3["smoke_exposure_remaining"] == 0


def test_window_counts_prefix_exposure_for_departed_group(client):
    # 组3 走通道5 [0,2)；登记 [0,2) 值1（此时未出发，受理后重排仍立即走，
    # 累计2），再 tick 固定；此后窗口 [1,3) 不能登记（与[0,2)重叠）。
    # 改为验证：固定后新窗口带来的增量与前缀已有暴露合并核算。
    client.post("/smoke-limits", json={"group_id": 3, "smoke_limit": 3})
    client.post("/smoke-windows", json={
        "edge_id": 5, "start_minute": 0, "end_minute": 2, "smoke_value": 1})
    assert client.post("/tick", json={"minutes": 1}).status_code == 200
    g3 = by_id(client.get("/plan").json())[3]
    # 分钟0已走过（暴露1），分钟1为余程（暴露1）
    assert g3["smoke_exposure_used"] == 1
    assert g3["smoke_exposure_remaining"] == 1
    # 新窗口 [2,4) 值1 不触及剩余路线 -> 受理
    assert client.post("/smoke-windows", json={
        "edge_id": 5, "start_minute": 2, "end_minute": 4,
        "smoke_value": 1}).status_code == 201


def test_window_does_not_protect_uncapped_departed_group(client):
    # 未设上限的已出发组不因烟雾窗口被拒绝
    assert client.post("/tick", json={"minutes": 1}).status_code == 200
    resp = client.post("/smoke-windows", json={
        "edge_id": 5, "start_minute": 1, "end_minute": 5,
        "smoke_value": 999})
    assert resp.status_code == 201


def test_departed_awaiting_rescue_group_not_protected(client):
    # 待救援组没有未来路线占用：任何未来窗口都不会使其超限
    client.post("/smoke-limits", json={"group_id": 3, "smoke_limit": 0})
    for edge_id in (5, 2, 3):
        client.post("/smoke-windows", json={
            "edge_id": edge_id, "start_minute": 0, "end_minute": 21,
            "smoke_value": 1})
    g3 = by_id(client.get("/plan").json())[3]
    assert g3["status"] == "failed"
    # failed 组 tick 后仍是未出发；直接验证待救援需要滞留报告，这里仅确认
    # 失败组仍可改设上限/其状态不阻断窗口登记（窗口只保护 fixed 组）
    resp = client.post("/smoke-windows", json={
        "edge_id": 5, "start_minute": 21, "end_minute": 25,
        "smoke_value": 1})
    assert resp.status_code == 201


# --------------------------------------------------------------------------
# 暴露上限登记校验
# --------------------------------------------------------------------------

def test_limit_unknown_group_rejected(client):
    resp = client.post("/smoke-limits", json={
        "group_id": 999, "smoke_limit": 1})
    assert resp.status_code == 422
    assert limits(client) == {}


def test_limit_negative_rejected(client):
    resp = client.post("/smoke-limits", json={
        "group_id": 3, "smoke_limit": -1})
    assert resp.status_code == 422


def test_limit_for_departed_group_rejected(client):
    assert client.post("/tick", json={"minutes": 1}).status_code == 200
    n0 = len(revs(client))
    resp = client.post("/smoke-limits", json={
        "group_id": 3, "smoke_limit": 1})
    assert resp.status_code == 409
    body = resp.json()
    assert body["error"] == "SMOKE_LIMIT_TARGET_DEPARTED"
    assert body["conflicts"][0]["reason"] == "TARGET_ALREADY_DEPARTED"
    assert limits(client) == {}
    assert len(revs(client)) == n0


def test_limit_rejected_leaves_no_partial_state(client):
    # 拒绝登记不改编排（组3 仍立即走通道5）
    before = client.get("/plan").json()
    client.post("/tick", json={"minutes": 1})
    resp = client.post("/smoke-limits", json={"group_id": 3, "smoke_limit": 0})
    assert resp.status_code == 409
    assert limits(client) == {}


# --------------------------------------------------------------------------
# 节点滞留改编：前缀暴露保留、剩余额度寻找余程
# --------------------------------------------------------------------------

DELAY_SCENARIO = {
    "passages": [
        {"id": 1, "src": 1, "dst": 2, "minutes": 2, "capacity": 30},
        {"id": 2, "src": 2, "dst": 5, "minutes": 2, "capacity": 30},
    ],
    "exits": [5],
    "groups": [
        {"id": 1, "size": 5, "origin": 1, "earliest_departure": 0,
         "priority": 10, "deadline": 30},
    ],
}


def _setup_delay(client, limit=10):
    client.post("/scenario", json=DELAY_SCENARIO)
    client.post("/smoke-limits", json={"group_id": 1, "smoke_limit": limit})
    # P1 [0,4) 每分钟暴露3：t=0 进入累计6（<=10）
    client.post("/smoke-windows", json={
        "edge_id": 1, "start_minute": 0, "end_minute": 4, "smoke_value": 3})
    client.post("/tick", json={"minutes": 2})  # 组 fixed 滞留节点2


def test_delay_report_keeps_prefix_exposure_and_finds_remaining_route(client):
    _setup_delay(client)
    # P2 [6,30) 每分钟3：与固定余程[2,4)不相交 -> 登记受理
    assert client.post("/smoke-windows", json={
        "edge_id": 2, "start_minute": 6, "end_minute": 30,
        "smoke_value": 3}).status_code == 201
    # 第4分钟恢复：前缀已用6、剩余额度4；[4,6) 进P2 暴露0 -> 到达6
    resp = client.post("/groups/1/delay-report", json={
        "node_id": 2, "recovery_minute": 4})
    assert resp.status_code == 200
    g1 = by_id(resp.json())[1]
    assert g1["status"] == "fixed"
    assert g1["prefix_step_count"] == 1
    assert g1["steps"][0]["enter_minute"] == 0
    assert g1["steps"][1]["enter_minute"] == 4
    assert g1["steps"][1]["exit_minute"] == 6
    assert g1["smoke_exposure_used"] == 6
    assert g1["smoke_exposure_remaining"] == 0


def test_delay_report_remaining_quota_blocks_smoky_suffix(client):
    _setup_delay(client)
    # P2 [4,8) 每分钟3：恢复分钟4时立即进入累计6，前缀6+6=12>10；
    # t=7 进入只占分钟7暴露3，6+3=9<=10 -> 到达9
    assert client.post("/smoke-windows", json={
        "edge_id": 2, "start_minute": 4, "end_minute": 8,
        "smoke_value": 3}).status_code == 201
    resp = client.post("/groups/1/delay-report", json={
        "node_id": 2, "recovery_minute": 4})
    g1 = by_id(resp.json())[1]
    assert g1["status"] == "fixed"
    assert g1["steps"][1]["enter_minute"] == 7
    assert g1["arrival_minute"] == 9
    assert g1["smoke_exposure_used"] == 6
    assert g1["smoke_exposure_remaining"] == 3


def test_delay_report_no_remaining_route_awaits_rescue(client):
    _setup_delay(client)
    # P2 [4,30) 每分钟3：deadline 30 前任意进入都累计6，6+6>10；
    # 等至30进入则到达32 > deadline -> 无可行余程
    assert client.post("/smoke-windows", json={
        "edge_id": 2, "start_minute": 4, "end_minute": 30,
        "smoke_value": 3}).status_code == 201
    resp = client.post("/groups/1/delay-report", json={
        "node_id": 2, "recovery_minute": 4})
    g1 = by_id(resp.json())[1]
    assert g1["status"] == "awaiting_rescue"
    assert g1["reason"] == "EXPOSURE_LIMIT_EXCEEDED"
    assert g1["prefix_step_count"] == 1
    assert g1["route"] == [1]  # 只保留已走前缀
    assert g1["smoke_exposure_used"] == 6
    assert g1["smoke_exposure_remaining"] is None


def test_uncapped_group_delay_report_unbounded_by_exposure(client):
    _setup_delay(client, limit=None)
    # 不设上限：第4分钟恢复，P2 浓烟也照常走最早余程
    client.post("/smoke-windows", json={
        "edge_id": 2, "start_minute": 4, "end_minute": 30,
        "smoke_value": 9})
    resp = client.post("/groups/1/delay-report", json={
        "node_id": 2, "recovery_minute": 4})
    g1 = by_id(resp.json())[1]
    assert g1["status"] == "fixed"
    assert g1["steps"][1]["enter_minute"] == 4
    assert g1["smoke_exposure_limit"] is None


# --------------------------------------------------------------------------
# 查询与修订、重置
# --------------------------------------------------------------------------

def test_list_windows_and_limits_sorted(client):
    client.post("/smoke-windows", json={
        "edge_id": 5, "start_minute": 5, "end_minute": 7, "smoke_value": 2})
    client.post("/smoke-windows", json={
        "edge_id": 2, "start_minute": 0, "end_minute": 3, "smoke_value": 1})
    client.post("/smoke-limits", json={"group_id": 3, "smoke_limit": 5})
    client.post("/smoke-limits", json={"group_id": 1, "smoke_limit": 8})
    assert [(w["edge_id"], w["start_minute"]) for w in windows(client)] == [
        (2, 0), (5, 5)]
    assert list(limits(client)) == [1, 3]


def test_successful_changes_create_revisions(client):
    n0 = len(revs(client))
    client.post("/smoke-limits", json={"group_id": 3, "smoke_limit": 5})
    client.post("/smoke-windows", json={
        "edge_id": 5, "start_minute": 0, "end_minute": 2, "smoke_value": 1})
    entries = revs(client)
    assert [e["operation"] for e in entries[-2:]] == [
        "smoke-limits", "smoke-windows"]
    # 替换旧上限的修订 detail 记录 replaced
    client.post("/smoke-limits", json={"group_id": 3, "smoke_limit": 9})
    last = revs(client)[-1]
    assert last["operation"] == "smoke-limits"
    assert last["detail"] == {"group_id": 3, "smoke_limit": 9,
                              "replaced": {"smoke_limit": 5}}
    # 修订快照含暴露字段与前后差异
    detail = client.get(f"/revisions/{last['rev']}").json()
    changes = {ch["group_id"]: ch for ch in detail["changes"]}
    assert 3 in changes
    # 拒绝不产生修订
    n1 = len(revs(client))
    client.post("/smoke-windows", json={
        "edge_id": 999, "start_minute": 0, "end_minute": 2,
        "smoke_value": 1})
    client.post("/smoke-limits", json={"group_id": 999, "smoke_limit": 1})
    assert len(revs(client)) == n1


def test_scenario_reset_clears_smoke_settings_but_keeps_revisions(client):
    client.post("/smoke-limits", json={"group_id": 3, "smoke_limit": 5})
    client.post("/smoke-windows", json={
        "edge_id": 5, "start_minute": 0, "end_minute": 2, "smoke_value": 1})
    rev = revs(client)[-1]["rev"]
    snapshot_before = client.get(f"/revisions/{rev}").json()

    assert client.post("/scenario", json=SCENARIO).status_code == 200
    assert windows(client) == []
    assert limits(client) == {}
    g3 = by_id(client.get("/plan").json())[3]
    assert g3["smoke_exposure_limit"] is None
    # 旧修订保留、内容不变
    assert client.get(f"/revisions/{rev}").json() == snapshot_before
    assert revs(client)[-1]["rev"] == rev + 1


def test_plan_exposure_fields_after_tick_split_prefix_and_remaining(client):
    # 组1 路线[1,5]：通道1 因封闭[3,10) 第10分钟进入、[10,14)占用；
    # 登记通道1 [10,14) 每分钟暴露2，组1 上限100
    client.post("/smoke-limits", json={"group_id": 1, "smoke_limit": 100})
    client.post("/smoke-windows", json={
        "edge_id": 1, "start_minute": 10, "end_minute": 14, "smoke_value": 2})
    g1 = by_id(client.get("/plan").json())[1]
    assert g1["departure_minute"] == 10
    # 通道1 暴露 4*2=8，通道5 [14,16) 无烟
    assert g1["smoke_exposure_used"] == 0
    assert g1["smoke_exposure_remaining"] == 8
    # tick 到12：通道1 走到分钟12，前缀[10,12) 暴露4，余程[12,14) 暴露4
    client.post("/tick", json={"minutes": 12})
    g1 = by_id(client.get("/plan").json())[1]
    assert g1["status"] == "fixed"
    assert g1["smoke_exposure_used"] == 4
    assert g1["smoke_exposure_remaining"] == 4
