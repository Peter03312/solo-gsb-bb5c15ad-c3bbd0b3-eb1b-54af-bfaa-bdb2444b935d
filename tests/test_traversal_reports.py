"""逐段实际通行核报集成测试。

覆盖：
* 受理：仅已出发组（fixed/awaiting_rescue/rescuing/rescued）可核报；
  分钟非负、进入早于离开、离开不晚于当前分钟；
* 顺序：沿现行路线从 seq=0 逐段核报，后段进入不早于前段实际离开；
  路线段不存在、通道不符、越序、同段重复一律 409 且不留记录；
* 幂等：同报送号同内容重试返回原记录（HTTP 200），同号异内容 409；
* 偏离：实绩与计划时刻不同只标记 deviates，不反向修改编排/占用/修订；
* 异常：命中封闭 CLOSED、实绩人数超当时有效容量 OVER_CAPACITY
  （容量管制时按 min(原容量, 管制上限)）在逐分钟汇总与段级标出；
* 节点滞留改编：已核报固定前缀保留可查；被释放余程段的核报置失效留痕，
  新余程可重新核报同序号；
* 人工救援组（待救援/救援中/已送达）只核报已有通道前缀；
* 查询：计划/实绩并列、未核报段、逐分钟实绩汇总；单组未知 404；
* 不改编排与修订；场景重置清空本场全部实绩。
"""
import os
import tempfile

import pytest
from fastapi.testclient import TestClient

_tmpdir = tempfile.mkdtemp(prefix="evac-test-traversal-")
os.environ["EVAC_DB_PATH"] = os.path.join(_tmpdir, "test.db")
os.environ["EVAC_SEED"] = "false"

from app.main import app  # noqa: E402


@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c


def revs(c):
    return c.get("/revisions").json()["revisions"]


def two_edge_scenario(c, *, cap1=10, cap2=10, closures=None,
                      minutes1=4, minutes2=2, size=10, tick=4):
    """1 --edge1--> 2 --edge2--> 6（出口）。默认推进到第 4 分钟（组1 fixed）。"""
    assert c.post("/scenario", json={
        "passages": [
            {"id": 1, "src": 1, "dst": 2, "minutes": minutes1, "capacity": cap1},
            {"id": 2, "src": 2, "dst": 6, "minutes": minutes2, "capacity": cap2}],
        "exits": [5, 6], "closures": closures or [],
        "groups": [
            {"id": 1, "size": size, "origin": 1, "earliest_departure": 0,
             "priority": 10, "deadline": 30},
            {"id": 2, "size": 5, "origin": 1, "earliest_departure": 10,
             "priority": 5, "deadline": 30}]}).status_code == 200
    if tick is not None:
        assert c.post("/tick", json={"minutes": tick}).status_code == 200


def report(c, report_id, *, group_id=1, seq=0, edge_id=1,
           enter=0, exit_=None):
    return c.post("/traversal-reports", json={
        "report_id": report_id, "group_id": group_id, "seq": seq,
        "edge_id": edge_id, "enter_minute": enter,
        "exit_minute": exit_ if exit_ is not None else enter + 1})


# --------------------------------------------------------------------------
# 受理与计划/实绩并列查询
# --------------------------------------------------------------------------

def test_submit_first_segment_and_query_side_by_side(client):
    # edge1 计划 [0,4)，当前分钟 4
    two_edge_scenario(client)
    resp = report(client, "T-1", seq=0, edge_id=1, enter=0, exit_=4)
    assert resp.status_code == 201, resp.text
    rec = resp.json()
    assert rec["report_id"] == "T-1"
    assert rec["group_id"] == 1 and rec["seq"] == 0 and rec["edge_id"] == 1
    assert rec["enter_minute"] == 0 and rec["exit_minute"] == 4
    assert rec["reported_minute"] == 4 and rec["active"] is True
    assert rec["detached_minute"] is None and rec["detach_reason"] is None

    detail = client.get("/traversal-reports/1").json()
    assert detail["current_minute"] == 4
    s0, s1 = detail["steps"]
    assert (s0["edge_id"], s0["planned_enter_minute"],
            s0["planned_exit_minute"]) == (1, 0, 4)
    assert (s0["actual_enter_minute"], s0["actual_exit_minute"],
            s0["report_id"]) == (0, 4, "T-1")
    assert s0["deviates"] is False and s0["anomalies"] == []
    # 第二段未核报：实绩字段为 null
    assert (s1["edge_id"], s1["planned_enter_minute"],
            s1["planned_exit_minute"]) == (2, 4, 6)
    assert s1["actual_enter_minute"] is None and s1["report_id"] is None
    assert detail["unreported_seqs"] == [1]
    assert detail["next_seq"] == 1
    assert [r["report_id"] for r in detail["reports"]] == ["T-1"]
    assert detail["detached_reports"] == []


def test_deviation_is_recorded_but_changes_nothing(client):
    two_edge_scenario(client, tick=5)  # 当前分钟 5；计划 edge1 [0,4)
    plan_before = client.get("/plan").json()
    # 实绩慢于计划：[0,5)
    assert report(client, "D-1", enter=0, exit_=5).status_code == 201
    detail = client.get("/traversal-reports/1").json()
    assert detail["steps"][0]["deviates"] is True
    # 编排不被反向修改
    assert client.get("/plan").json() == plan_before
    # 计划占用仍按原计划（edge1 的 [0,4)），实绩不进 /load：分钟 4 实绩有
    # 通行但计划占用为 0，编排不被实绩反向修改
    load = client.get("/passages/1/load").json()
    assert all(m["used"] == 10 for m in load["minutes"]
               if m["minute"] in (0, 1, 2, 3))
    assert all(m["used"] == 0 for m in load["minutes"]
               if m["minute"] == 4)


def test_list_endpoint_includes_all_groups_and_edge_minutes(client):
    two_edge_scenario(client)
    assert report(client, "L-1", enter=0, exit_=4).status_code == 201
    listing = client.get("/traversal-reports").json()
    assert listing["current_minute"] == 4
    by_id = {g["group_id"]: g for g in listing["groups"]}
    assert by_id[1]["steps"][0]["actual_exit_minute"] == 4
    # 未出发组 2：段为其现行计划，无实绩，不计未核报段（尚未可核报）
    assert by_id[2]["status"] == "scheduled"
    assert by_id[2]["unreported_seqs"] == []
    assert by_id[2]["next_seq"] is None
    assert all(s["actual_enter_minute"] is None for s in by_id[2]["steps"])
    minutes = {(m["edge_id"], m["minute"]): m for m in listing["edge_minutes"]}
    for minute in range(0, 4):
        slot = minutes[(1, minute)]
        assert slot["used"] == 10 and slot["effective_capacity"] == 10
        assert slot["groups"] == [{"group_id": 1, "size": 10}]
        assert slot["anomalies"] == []


def test_unknown_group_submit_422_query_404(client):
    two_edge_scenario(client)
    assert report(client, "U-1", group_id=99).status_code == 422
    assert client.get("/traversal-reports/99").status_code == 404


# --------------------------------------------------------------------------
# 拒绝：未出发 / 段不存在 / 通道不符 / 越序 / 时序
# --------------------------------------------------------------------------

def test_not_departed_rejected_and_no_record(client):
    two_edge_scenario(client, tick=None)  # 当前分钟 0：组1 scheduled
    resp = report(client, "N-1", enter=0, exit_=1)
    assert resp.status_code == 409
    assert resp.json()["error"] == "TRAVERSAL_REPORT_NOT_ACCEPTABLE"
    conflict = resp.json()["conflicts"][0]
    assert conflict["reason"] == "NOT_DEPARTED"
    assert conflict["status"] == "scheduled"
    assert client.get("/traversal-reports/1").json()["reports"] == []


def test_step_not_found_edge_mismatch_and_order(client):
    two_edge_scenario(client, tick=6)  # 组1 现行路线 [edge1, edge2]
    # 越序：直接报 seq=1（当前分钟 6，时序合法）
    resp = report(client, "O-1", seq=1, edge_id=2, enter=4, exit_=6)
    assert resp.status_code == 409
    assert resp.json()["conflicts"][0]["reason"] == "OUT_OF_ORDER"
    # 通道不符：seq=0 报 edge2
    resp = report(client, "O-2", seq=0, edge_id=2, enter=0, exit_=2)
    assert resp.status_code == 409
    conflict = resp.json()["conflicts"][0]
    assert conflict["reason"] == "EDGE_MISMATCH"
    assert conflict["expected_edge_id"] == 1
    # 段不存在：seq=9
    resp = report(client, "O-3", seq=9, edge_id=1, enter=0, exit_=2)
    assert resp.status_code == 409
    assert resp.json()["conflicts"][0]["reason"] == "STEP_NOT_FOUND"
    # 均不留记录、报送号可复用
    assert report(client, "O-3", seq=0, edge_id=1, enter=0, exit_=4
                  ).status_code == 201
    assert report(client, "O-1", seq=1, edge_id=2, enter=4, exit_=6
                  ).status_code == 201


def test_duplicate_segment_rejected(client):
    two_edge_scenario(client)
    assert report(client, "P-1", enter=0, exit_=4).status_code == 201
    # 同段换报送号重复报送（即使内容相同）：拒绝
    resp = report(client, "P-2", enter=0, exit_=4)
    assert resp.status_code == 409
    conflict = resp.json()["conflicts"][0]
    assert conflict["reason"] == "DUPLICATE_SEQ"
    assert conflict["existing_report_id"] == "P-1"
    assert len(client.get("/traversal-reports/1").json()["reports"]) == 1


def test_enter_not_before_previous_actual_exit(client):
    two_edge_scenario(client)  # 当前分钟 4，第二段计划 [4,6)
    # 第一段实绩 [0,3)（比计划快）
    assert report(client, "Q-1", enter=0, exit_=3).status_code == 201
    # 第二段进入分钟 2 早于前段实际离开 3：拒绝
    resp = report(client, "Q-2", seq=1, edge_id=2, enter=2, exit_=3)
    assert resp.status_code == 409
    conflict = resp.json()["conflicts"][0]
    assert conflict["reason"] == "ENTER_BEFORE_PREVIOUS_EXIT"
    assert conflict["previous_exit_minute"] == 3
    # 进入 == 前段离开允许
    assert report(client, "Q-3", seq=1, edge_id=2, enter=3, exit_=4
                  ).status_code == 201


def test_exit_in_future_rejected(client):
    two_edge_scenario(client, tick=4)  # 当前分钟 4
    resp = report(client, "F-1", enter=0, exit_=5)  # 离开 5 > 当前 4
    assert resp.status_code == 409
    assert resp.json()["conflicts"][0]["reason"] == "EXIT_IN_FUTURE"
    # 离开 == 当前分钟允许
    assert report(client, "F-2", enter=0, exit_=4).status_code == 201


def test_pydantic_validation_422(client):
    two_edge_scenario(client)
    assert report(client, "").status_code == 422  # 空报送号
    assert c_post(client, "V-1", enter=-1, exit_=1).status_code == 422
    assert c_post(client, "V-2", enter=2, exit_=1).status_code == 422  # 进入>=离开
    assert c_post(client, "V-3", enter=1, exit_=1).status_code == 422  # 进入==离开
    assert c_post(client, "V-4", seq=-1).status_code == 422
    assert client.get("/traversal-reports/1").json()["reports"] == []


def c_post(c, report_id, *, group_id=1, seq=0, edge_id=1, enter=0, exit_=4):
    return c.post("/traversal-reports", json={
        "report_id": report_id, "group_id": group_id, "seq": seq,
        "edge_id": edge_id, "enter_minute": enter,
        "exit_minute": exit_})


# --------------------------------------------------------------------------
# 幂等：同号同内容重试 / 同号异内容拒绝
# --------------------------------------------------------------------------

def test_idempotent_retry_returns_original(client):
    two_edge_scenario(client)
    first = report(client, "I-1", enter=0, exit_=4)
    assert first.status_code == 201
    replay = report(client, "I-1", enter=0, exit_=4)
    assert replay.status_code == 200
    assert replay.json() == first.json()
    assert len(client.get("/traversal-reports/1").json()["reports"]) == 1


def test_same_report_id_different_content_rejected(client):
    two_edge_scenario(client)
    assert report(client, "I-2", enter=0, exit_=4).status_code == 201
    base = {"report_id": "I-2", "group_id": 1, "seq": 0, "edge_id": 1,
            "enter_minute": 0, "exit_minute": 4}
    for changed in ({"exit_minute": 3}, {"enter_minute": 1},
                    {"edge_id": 2}, {"seq": 1}, {"group_id": 2}):
        body = {**base, **changed}
        resp = client.post("/traversal-reports", json=body)
        assert resp.status_code == 409, body
        assert resp.json()["error"] == "TRAVERSAL_REPORT_ID_CONFLICT"
        assert (resp.json()["conflicts"][0]["reason"]
                == "REPORT_ID_CONTENT_MISMATCH")
    assert len(client.get("/traversal-reports/1").json()["reports"]) == 1


# --------------------------------------------------------------------------
# 异常：命中封闭 / 超当时有效容量
# --------------------------------------------------------------------------

def test_actuals_through_closure_flagged_closed(client):
    # 计划因封闭 [3,10) 推迟到第 10 分钟进入 edge1；现场如实核报实绩 [2,6)
    assert client.post("/scenario", json={
        "passages": [
            {"id": 1, "src": 1, "dst": 2, "minutes": 4, "capacity": 30},
            {"id": 2, "src": 2, "dst": 6, "minutes": 1, "capacity": 30}],
        "exits": [6],
        "closures": [{"edge_id": 1, "start_minute": 3, "end_minute": 10}],
        "groups": [{"id": 1, "size": 10, "origin": 1,
                    "earliest_departure": 0, "priority": 10,
                    "deadline": 30}]}).status_code == 200
    client.post("/tick", json={"minutes": 15})
    assert report(client, "C-1", enter=2, exit_=6).status_code == 201
    detail = client.get("/traversal-reports/1").json()
    assert "CLOSED" in detail["steps"][0]["anomalies"]
    listing = client.get("/traversal-reports").json()
    closed_minutes = [(m["edge_id"], m["minute"]) for m in
                      listing["edge_minutes"] if "CLOSED" in m["anomalies"]]
    assert closed_minutes == [(1, 3), (1, 4), (1, 5)]
    # 分钟 2 不在封闭内，不标异常
    minute2 = next(m for m in listing["edge_minutes"] if m["minute"] == 2)
    assert minute2["anomalies"] == []


def test_over_capacity_flagged_by_effective_limit(client):
    # 两组（20+15 人）均已出发，实绩同在 edge1 的 [0,4)：35 > 容量 30
    assert client.post("/scenario", json={
        "passages": [
            {"id": 1, "src": 1, "dst": 2, "minutes": 4, "capacity": 30},
            {"id": 2, "src": 2, "dst": 6, "minutes": 1, "capacity": 30}],
        "exits": [6], "closures": [],
        "groups": [
            {"id": 1, "size": 20, "origin": 1, "earliest_departure": 0,
             "priority": 10, "deadline": 30},
            {"id": 2, "size": 15, "origin": 1, "earliest_departure": 0,
             "priority": 5, "deadline": 30}]}).status_code == 200
    client.post("/tick", json={"minutes": 20})
    assert report(client, "K-1", group_id=1, enter=0, exit_=4).status_code == 201
    assert report(client, "K-2", group_id=2, enter=0, exit_=4).status_code == 201
    listing = client.get("/traversal-reports").json()
    over = [m for m in listing["edge_minutes"]
            if "OVER_CAPACITY" in m["anomalies"]]
    assert [(m["minute"], m["used"], m["effective_capacity"]) for m in over] \
        == [(0, 35, 30), (1, 35, 30), (2, 35, 30), (3, 35, 30)]
    # 段级同样标出
    for gid in (1, 2):
        g = next(g for g in listing["groups"] if g["group_id"] == gid)
        assert "OVER_CAPACITY" in g["steps"][0]["anomalies"]


def test_capacity_control_tightens_effective_limit(client):
    # 第 0 分钟登记 edge1 [2,6) 限流 5；计划把组（10 人）推迟到第 6 分钟；
    # 现场如实核报实绩 [0,10)：分钟 2..5 有效上限 5、实绩 10 -> 超额
    assert client.post("/scenario", json={
        "passages": [{"id": 1, "src": 1, "dst": 2, "minutes": 10,
                      "capacity": 30}],
        "exits": [2], "closures": [],
        "groups": [{"id": 1, "size": 10, "origin": 1,
                    "earliest_departure": 0, "priority": 10,
                    "deadline": 40}]}).status_code == 200
    assert client.post("/capacity-controls", json={
        "edge_id": 1, "start_minute": 2, "end_minute": 6,
        "capacity": 5}).status_code == 201
    client.post("/tick", json={"minutes": 20})
    assert report(client, "K-3", enter=0, exit_=10).status_code == 201
    listing = client.get("/traversal-reports").json()
    by_minute = {m["minute"]: m for m in listing["edge_minutes"]}
    for minute in (2, 3, 4, 5):
        assert by_minute[minute]["effective_capacity"] == 5
        assert "OVER_CAPACITY" in by_minute[minute]["anomalies"]
    assert by_minute[1]["effective_capacity"] == 30
    assert by_minute[1]["anomalies"] == []


# --------------------------------------------------------------------------
# 节点滞留改编：前缀保留、余程失效留痕、新余程重报
# --------------------------------------------------------------------------

def test_delay_replan_keeps_prefix_and_detaches_suffix(client):
    # 计划 edge1[0,4)、edge2[4,6)；推进到第 4 分钟后现场核报比计划快的
    # 实绩（seq0 [0,2)、seq1 [2,4)），再在节点 2 报告滞留、第 6 分钟恢复
    two_edge_scenario(client, tick=4)
    assert report(client, "R-1", seq=0, edge_id=1, enter=0, exit_=2
                  ).status_code == 201
    assert report(client, "R-2", seq=1, edge_id=2, enter=2, exit_=4
                  ).status_code == 201

    resp = client.post("/groups/1/delay-report",
                       json={"node_id": 2, "recovery_minute": 6})
    assert resp.status_code == 200
    g = resp.json()["groups"][0]
    assert g["prefix_step_count"] == 1
    assert g["steps"][1]["enter_minute"] == 6  # 新余程

    detail = client.get("/traversal-reports/1").json()
    # 固定前缀实绩仍并列可查
    assert (detail["steps"][0]["actual_enter_minute"],
            detail["steps"][0]["actual_exit_minute"],
            detail["steps"][0]["report_id"]) == (0, 2, "R-1")
    # 被释放余程段的旧核报失效留痕
    detached = {(r["report_id"], r["seq"]): r for r in detail["detached_reports"]}
    assert set(detached) == {("R-2", 1)}
    assert detached[("R-2", 1)]["detached_minute"] == 4
    assert detached[("R-2", 1)]["detach_reason"] == "delay-replan"
    # 现行新余程的 seq=1 待重新核报
    assert detail["unreported_seqs"] == [1] and detail["next_seq"] == 1
    assert detail["steps"][1]["actual_enter_minute"] is None

    # 推进到第 8 分钟后按新余程重新核报 seq=1
    client.post("/tick", json={"minutes": 8})
    assert report(client, "R-3", seq=1, edge_id=2, enter=6, exit_=8
                  ).status_code == 201
    detail = client.get("/traversal-reports/1").json()
    assert [r["report_id"] for r in detail["reports"]] == ["R-1", "R-3"]
    assert [r["report_id"] for r in detail["detached_reports"]] == ["R-2"]
    assert detail["unreported_seqs"] == [] and detail["next_seq"] is None

    # 逐分钟汇总只计有效实绩：R-2 的 [2,4) 已排除
    minutes = {(m["edge_id"], m["minute"]) for m in detail["edge_minutes"]}
    assert (1, 0) in minutes and (1, 1) in minutes
    assert (2, 2) not in minutes and (2, 3) not in minutes
    assert (2, 6) in minutes and (2, 7) in minutes
    # 旧报送号同内容重试仍返回原（失效）记录
    replay = report(client, "R-2", seq=1, edge_id=2, enter=2, exit_=4)
    assert replay.status_code == 200 and replay.json()["active"] is False


def test_replan_to_rescue_prefix_remains_queryable(client):
    # 滞留后无余程 -> awaiting_rescue；已核报前缀保留
    assert client.post("/scenario", json={
        "passages": [
            {"id": 1, "src": 1, "dst": 2, "minutes": 4, "capacity": 10},
            {"id": 2, "src": 2, "dst": 6, "minutes": 1, "capacity": 10}],
        "exits": [5, 6],
        "closures": [{"edge_id": 2, "start_minute": 5, "end_minute": 30}],
        "groups": [{"id": 1, "size": 10, "origin": 1,
                    "earliest_departure": 0, "priority": 10,
                    "deadline": 30}]}).status_code == 200
    client.post("/tick", json={"minutes": 4})
    assert report(client, "W-1", enter=0, exit_=4).status_code == 201
    client.post("/groups/1/delay-report",
                json={"node_id": 2, "recovery_minute": 6})
    detail = client.get("/traversal-reports/1").json()
    assert len(detail["steps"]) == 1  # 只余前缀
    assert detail["steps"][0]["actual_exit_minute"] == 4
    assert detail["unreported_seqs"] == [] and detail["next_seq"] is None
    # 余程段序号不存在
    resp = report(client, "W-2", seq=1, edge_id=2, enter=4, exit_=5)
    assert resp.status_code == 409
    assert resp.json()["conflicts"][0]["reason"] == "STEP_NOT_FOUND"


# --------------------------------------------------------------------------
# 人工救援组：只核报已有通道前缀
# --------------------------------------------------------------------------

def test_rescuing_and_rescued_groups_report_prefix_only(client):
    assert client.post("/scenario", json={
        "passages": [
            {"id": 1, "src": 1, "dst": 2, "minutes": 4, "capacity": 10},
            {"id": 2, "src": 2, "dst": 6, "minutes": 1, "capacity": 10}],
        "exits": [5, 6],
        "closures": [{"edge_id": 2, "start_minute": 5, "end_minute": 30}],
        "groups": [{"id": 1, "size": 10, "origin": 1,
                    "earliest_departure": 0, "priority": 10,
                    "deadline": 30}]}).status_code == 200
    client.post("/tick", json={"minutes": 4})
    client.post("/groups/1/delay-report",
                json={"node_id": 2, "recovery_minute": 6})
    client.post("/rescue-teams", json={"team_id": 1, "capacity": 10})
    task = client.post("/rescue-tasks", json={
        "group_id": 1, "team_id": 1, "node_id": 2,
        "observed_minute": 4}).json()["task_id"]

    # rescuing：只可核报前缀 seq=0
    assert report(client, "M-1", enter=0, exit_=4).status_code == 201
    resp = report(client, "M-2", seq=1, edge_id=2, enter=4, exit_=5)
    assert resp.status_code == 409
    assert resp.json()["conflicts"][0]["reason"] == "STEP_NOT_FOUND"

    # 结案后 rescued：仍只核报前缀
    client.post("/tick", json={"minutes": 2})
    assert client.post(f"/rescue-tasks/{task}/completion",
                       json={"exit_id": 6,
                             "delivery_minute": 6}).status_code == 200
    assert client.get("/traversal-reports/1").json()["steps"][0][
        "report_id"] == "M-1"
    resp = report(client, "M-3", seq=1, edge_id=2, enter=4, exit_=5)
    assert resp.status_code == 409
    assert resp.json()["conflicts"][0]["reason"] == "STEP_NOT_FOUND"


# --------------------------------------------------------------------------
# 不改编排与修订；场景重置清空
# --------------------------------------------------------------------------

def test_traversal_reports_change_neither_plan_nor_revisions(client):
    two_edge_scenario(client)
    plan0 = client.get("/plan").json()
    n0 = len(revs(client))
    assert report(client, "Z-1", enter=0, exit_=4).status_code == 201
    assert report(client, "Z-2", seq=1, edge_id=2, enter=4, exit_=6
                  ).status_code == 409  # 被拒绝的同样不产生修订
    assert client.get("/plan").json() == plan0
    assert len(revs(client)) == n0


def test_scenario_reset_clears_traversal_reports(client):
    two_edge_scenario(client)
    assert report(client, "S-1", enter=0, exit_=4).status_code == 201
    client.post("/scenario", json={
        "passages": [
            {"id": 1, "src": 1, "dst": 2, "minutes": 4, "capacity": 10},
            {"id": 2, "src": 2, "dst": 6, "minutes": 2, "capacity": 10}],
        "exits": [5, 6], "closures": [],
        "groups": [{"id": 1, "size": 10, "origin": 1,
                    "earliest_departure": 0, "priority": 10,
                    "deadline": 30}]})
    listing = client.get("/traversal-reports").json()
    assert listing["edge_minutes"] == []
    assert listing["groups"][0]["reports"] == []
    assert listing["groups"][0]["detached_reports"] == []
    # 报送号可在新场景复用
    client.post("/tick", json={"minutes": 4})
    assert report(client, "S-1", enter=0, exit_=4).status_code == 201
