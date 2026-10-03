"""人工救援队交接核销集成测试。

覆盖：
* 队伍登记：唯一编号 + 正整数载员量；重复登记 409，非法载员量 422；
* 派遣受理：仅当所见分钟未变、组仍在该节点待救援、人数不超载员量、
  队伍空闲且同组同队无进行中任务时受理，返回任务号；
* 受理后组为 rescuing：保留已走通道前缀、不产生未来通道占用、时间推进
  不自动重排、既有节点滞留报告与组纠正均拒绝；
* 结案核销：既有出口 + 不早于派遣、不晚于当前分钟的实际送达分钟；成功
  后队伍空闲、组为 rescued，实际出口与时刻单列（不当作规划到达，晚于
  原截止也如实记录）；重复结案/时序冲突 409，未知出口 422；
* 无效编号、状态/时序冲突与并发抢占均整次拒绝，不改编排与修订；
* 成功派遣与结案各原子写入一条修订；场景重置清空队伍与任务、旧修订保留。
"""
import os
import tempfile

import pytest
from fastapi.testclient import TestClient

_tmpdir = tempfile.mkdtemp(prefix="evac-test-rescue-")
os.environ["EVAC_DB_PATH"] = os.path.join(_tmpdir, "test.db")
os.environ["EVAC_SEED"] = "false"

from app.main import app  # noqa: E402


def make_awaiting_rescue(c, *, closure_start=5, closure_end=30,
                         group_size=10):
    """重置为 1->2(edge1,4min)->6(edge2,1min) 场景，edge2 自第 5 分钟
    封闭，推进到第 4 分钟并报告恢复 6，组 1 进入 awaiting_rescue。

    组 1 初始 edge1[0,4)+edge2[4,5) 恰在封闭前到达；第 4 分钟在节点 2
    报告恢复 6 后，edge2[6,..) 被封闭阻断，无余程 -> 待救援。
    """
    resp = c.post("/scenario", json={
        "passages": [
            {"id": 1, "src": 1, "dst": 2, "minutes": 4, "capacity": 10},
            {"id": 2, "src": 2, "dst": 6, "minutes": 1, "capacity": 10},
        ],
        "exits": [6],
        "closures": [{"edge_id": 2, "start_minute": closure_start,
                      "end_minute": closure_end}],
        "groups": [
            {"id": 1, "size": group_size, "origin": 1,
             "earliest_departure": 0, "priority": 10, "deadline": 30}]})
    assert resp.status_code == 200
    assert c.post("/tick", json={"minutes": 4}).status_code == 200
    resp = c.post("/groups/1/delay-report",
                  json={"node_id": 2, "recovery_minute": 6})
    assert resp.status_code == 200
    return resp


@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c


def by_id(plan):
    return {g["group_id"]: g for g in plan["groups"]}


def revs(c):
    return c.get("/revisions").json()["revisions"]


def register_team(c, team_id, capacity):
    return c.post("/rescue-teams",
                  json={"team_id": team_id, "capacity": capacity})


def dispatch(c, group_id=1, team_id=1, node=2, observed=None):
    if observed is None:
        observed = c.get("/health").json()["current_minute"]
    return c.post("/rescue-tasks",
                  json={"group_id": group_id, "team_id": team_id,
                        "node_id": node, "observed_minute": observed})


# --------------------------------------------------------------------------
# 队伍登记
# --------------------------------------------------------------------------

def test_register_team_and_list(client):
    client.post("/scenario", json={"passages": [], "exits": [],
                                   "closures": [], "groups": []})
    resp = register_team(client, 1, 12)
    assert resp.status_code == 201
    assert resp.json() == {"team_id": 1, "capacity": 12, "task_id": None}

    resp = register_team(client, 2, 5)
    assert resp.status_code == 201
    teams = client.get("/rescue-teams").json()
    assert teams["current_minute"] == 0
    assert teams["teams"] == [
        {"team_id": 1, "capacity": 12, "task_id": None},
        {"team_id": 2, "capacity": 5, "task_id": None}]


def test_duplicate_team_registration_rejected(client):
    client.post("/scenario", json={"passages": [], "exits": [],
                                   "closures": [], "groups": []})
    assert register_team(client, 7, 10).status_code == 201
    resp = register_team(client, 7, 20)
    assert resp.status_code == 409
    body = resp.json()
    assert body["error"] == "RESCUE_TEAM_ALREADY_REGISTERED"
    assert body["conflicts"][0]["reason"] == "DUPLICATE_TEAM_ID"
    # 原载员量保留
    teams = {t["team_id"]: t
             for t in client.get("/rescue-teams").json()["teams"]}
    assert teams[7]["capacity"] == 10


def test_team_capacity_must_be_positive(client):
    client.post("/scenario", json={"passages": [], "exits": [],
                                   "closures": [], "groups": []})
    resp = register_team(client, 1, 0)
    assert resp.status_code == 422
    resp = register_team(client, 1, -3)
    assert resp.status_code == 422
    assert client.get("/rescue-teams").json()["teams"] == []


def test_team_registration_creates_no_revision(client):
    client.post("/scenario", json={"passages": [], "exits": [],
                                   "closures": [], "groups": []})
    n0 = len(revs(client))
    register_team(client, 1, 10)
    assert len(revs(client)) == n0


# --------------------------------------------------------------------------
# 派遣受理
# --------------------------------------------------------------------------

def test_dispatch_accepted_returns_task_and_marks_rescuing(client):
    make_awaiting_rescue(client)
    register_team(client, 1, 10)
    n0 = len(revs(client))
    resp = dispatch(client)
    assert resp.status_code == 201, resp.text
    task = resp.json()
    assert task["status"] == "in_progress"
    assert task["group_id"] == 1 and task["team_id"] == 1
    assert task["node_id"] == 2
    assert task["observed_minute"] == 4
    assert task["dispatch_minute"] == 4
    assert task["exit_id"] is None and task["delivery_minute"] is None

    # 任务查询展示当前状态
    got = client.get(f"/rescue-tasks/{task['task_id']}").json()
    assert got == task
    listed = client.get("/rescue-tasks").json()
    assert listed["current_minute"] == 4
    assert listed["tasks"] == [task]

    # 队伍变为不空闲，任务号回填
    teams = {t["team_id"]: t
             for t in client.get("/rescue-teams").json()["teams"]}
    assert teams[1]["task_id"] == task["task_id"]

    # 组标记救援中：前缀保留、无规划到达、救援字段单列
    g1 = by_id(client.get("/plan").json())[1]
    assert g1["status"] == "rescuing"
    assert g1["route"] == [1]  # 只保留已走前缀
    assert g1["steps"] == [
        {"edge_id": 1, "from_node": 1, "to_node": 2,
         "enter_minute": 0, "exit_minute": 4}]
    assert g1["prefix_step_count"] == 1
    assert g1["exit_id"] is None and g1["arrival_minute"] is None
    assert g1["rescue_task_id"] == task["task_id"]
    assert g1["rescue_team_id"] == 1
    assert g1["rescued_exit_id"] is None and g1["delivery_minute"] is None

    # 产生一条 rescue-dispatches 修订
    entry = revs(client)[-1]
    assert len(revs(client)) == n0 + 1
    assert entry["operation"] == "rescue-dispatches"
    assert entry["minute"] == 4
    assert entry["detail"] == {
        "task_id": task["task_id"], "group_id": 1, "team_id": 1,
        "node_id": 2, "observed_minute": 4, "dispatch_minute": 4}


def test_dispatch_no_future_passage_occupancy(client):
    make_awaiting_rescue(client)
    register_team(client, 1, 10)
    assert dispatch(client).status_code == 201
    # 救援不产生任何未来通道占用：edge1 只有已走的历史占用 [0,4)
    used1 = {m["minute"]: m["used"]
             for m in client.get("/passages/1/load").json()["minutes"]}
    assert all(used1[m] == 10 for m in range(0, 4))
    assert all(m not in used1 for m in range(4, 20))
    # edge2 上没有任何占用
    used2 = client.get("/passages/2/load").json()["minutes"]
    assert used2 == []


def test_rescuing_group_not_replanned_on_tick(client):
    make_awaiting_rescue(client)
    register_team(client, 1, 10)
    resp = dispatch(client)
    assert resp.status_code == 201
    g_before = by_id(client.get("/plan").json())[1]
    # 时间推进不自动重排救援中的组
    client.post("/tick", json={"minutes": 10})
    g_after = by_id(client.get("/plan").json())[1]
    assert g_after == g_before
    assert g_after["status"] == "rescuing"


def test_delay_report_rejected_for_rescuing_and_rescued(client):
    make_awaiting_rescue(client)
    register_team(client, 1, 10)
    task = dispatch(client).json()

    resp = client.post("/groups/1/delay-report",
                       json={"node_id": 2, "recovery_minute": 6})
    assert resp.status_code == 409
    conflict = resp.json()["conflicts"][0]
    assert conflict["reason"] == "GROUP_RESCUING"
    assert conflict["rescue_task_id"] == task["task_id"]
    assert conflict["rescue_team_id"] == 1

    # 结案后同样拒绝，原因变为 GROUP_RESCUED
    client.post("/tick", json={"minutes": 2})
    ok = client.post(f"/rescue-tasks/{task['task_id']}/completion",
                     json={"exit_id": 6, "delivery_minute": 6})
    assert ok.status_code == 200
    resp = client.post("/groups/1/delay-report",
                       json={"node_id": 6, "recovery_minute": 7})
    assert resp.status_code == 409
    assert resp.json()["conflicts"][0]["reason"] == "GROUP_RESCUED"


def test_rescuing_group_cannot_be_corrected(client):
    make_awaiting_rescue(client)
    register_team(client, 1, 10)
    assert dispatch(client).status_code == 201
    resp = client.post("/group-corrections",
                       json=[{"group_id": 1, "priority": 99}])
    assert resp.status_code == 409
    assert resp.json()["error"] == "GROUP_ALREADY_DEPARTED"
    assert resp.json()["conflicts"][0]["status"] == "rescuing"


# --------------------------------------------------------------------------
# 派遣拒绝：无效编号、状态/节点/载员量/空闲/分钟失步
# --------------------------------------------------------------------------

def test_dispatch_unknown_group_or_team_422(client):
    make_awaiting_rescue(client)
    register_team(client, 1, 10)
    resp = client.post("/rescue-tasks", json={
        "group_id": 999, "team_id": 1, "node_id": 2,
        "observed_minute": 4})
    assert resp.status_code == 422
    resp = client.post("/rescue-tasks", json={
        "group_id": 1, "team_id": 999, "node_id": 2,
        "observed_minute": 4})
    assert resp.status_code == 422


def test_dispatch_wrong_node_rejected(client):
    make_awaiting_rescue(client)
    register_team(client, 1, 10)
    resp = dispatch(client, node=3)
    assert resp.status_code == 409
    conflict = resp.json()["conflicts"][0]
    assert resp.json()["error"] == "RESCUE_DISPATCH_NOT_ACCEPTABLE"
    assert conflict["reason"] == "NODE_MISMATCH"
    assert conflict["expected_node_id"] == 2
    # 未产生任务
    assert client.get("/rescue-tasks").json()["tasks"] == []


def test_dispatch_capacity_exceeded_rejected(client):
    make_awaiting_rescue(client)
    register_team(client, 1, 9)  # 组 10 人 > 载员 9
    resp = dispatch(client)
    assert resp.status_code == 409
    conflict = resp.json()["conflicts"][0]
    assert conflict["reason"] == "TEAM_CAPACITY_EXCEEDED"
    assert conflict["group_size"] == 10
    assert conflict["team_capacity"] == 9
    # 队伍仍空闲
    assert client.get("/rescue-teams").json()["teams"][0]["task_id"] is None


def test_dispatch_busy_team_rejected_and_freed_after_completion(client):
    c = client
    # 两个待救援组（各自起点独立，避免互相争用）：
    # 组1 origin 1 -> edge1(4) -> 2；组5 origin 3 -> edge3(4) -> 4；
    # edge2(2->6) 与 edge4(4->6) 自第 5 分钟封闭
    c.post("/scenario", json={
        "passages": [
            {"id": 1, "src": 1, "dst": 2, "minutes": 4, "capacity": 10},
            {"id": 2, "src": 2, "dst": 6, "minutes": 1, "capacity": 10},
            {"id": 3, "src": 3, "dst": 4, "minutes": 4, "capacity": 10},
            {"id": 4, "src": 4, "dst": 6, "minutes": 1, "capacity": 10},
        ],
        "exits": [6],
        "closures": [{"edge_id": 2, "start_minute": 5, "end_minute": 30},
                     {"edge_id": 4, "start_minute": 5, "end_minute": 30}],
        "groups": [
            {"id": 1, "size": 10, "origin": 1, "earliest_departure": 0,
             "priority": 10, "deadline": 30},
            {"id": 5, "size": 10, "origin": 3, "earliest_departure": 0,
             "priority": 5, "deadline": 30}]})
    c.post("/tick", json={"minutes": 4})
    assert c.post("/groups/1/delay-report",
                  json={"node_id": 2, "recovery_minute": 6}).status_code == 200
    assert c.post("/groups/5/delay-report",
                  json={"node_id": 4, "recovery_minute": 6}).status_code == 200

    register_team(c, 1, 10)
    register_team(c, 2, 10)
    first = dispatch(c, group_id=1, team_id=1, node=2)
    assert first.status_code == 201
    # 同队第二项进行中任务被拒
    resp = dispatch(c, group_id=5, team_id=1, node=4)
    assert resp.status_code == 409
    assert resp.json()["conflicts"][0]["reason"] == "TEAM_BUSY"
    # 另一支空闲队伍可派遣组 5
    second = dispatch(c, group_id=5, team_id=2, node=4)
    assert second.status_code == 201


def test_dispatch_non_awaiting_group_rejected(client):
    # 全新场景：组1尚未出发（scheduled），派遣拒绝
    client.post("/scenario", json={
        "passages": [{"id": 1, "src": 1, "dst": 6, "minutes": 1,
                      "capacity": 10}],
        "exits": [6], "closures": [],
        "groups": [{"id": 1, "size": 10, "origin": 1,
                    "earliest_departure": 0, "priority": 10,
                    "deadline": 30}]})
    register_team(client, 1, 10)
    resp = dispatch(client, group_id=1, team_id=1, node=1, observed=0)
    assert resp.status_code == 409
    assert resp.json()["conflicts"][0]["reason"] == \
        "GROUP_NOT_AWAITING_RESCUE"


def test_dispatch_same_group_twice_rejected(client):
    make_awaiting_rescue(client)
    register_team(client, 1, 10)
    register_team(client, 2, 10)
    assert dispatch(client, team_id=1).status_code == 201
    # 同组换一支队伍再派：同组只允许一项进行中任务
    resp = dispatch(client, team_id=2)
    assert resp.status_code == 409
    assert resp.json()["conflicts"][0]["reason"] == \
        "GROUP_RESCUE_IN_PROGRESS"


def test_dispatch_stale_observed_minute_rejected(client):
    make_awaiting_rescue(client)  # 当前分钟 4
    register_team(client, 1, 10)
    # 所见分钟 3 与实际 4 不一致
    resp = dispatch(client, observed=3)
    assert resp.status_code == 409
    assert resp.json()["conflicts"][0]["reason"] == "OBSERVED_MINUTE_STALE"
    # 先 tick 到 5，再拿旧的所见分钟 4 派遣（并发抢占口径）
    client.post("/tick", json={"minutes": 1})
    resp = dispatch(client, observed=4)
    assert resp.status_code == 409
    assert resp.json()["conflicts"][0]["reason"] == "OBSERVED_MINUTE_STALE"
    assert client.get("/rescue-tasks").json()["tasks"] == []


def test_dispatch_after_rescued_rejected(client):
    make_awaiting_rescue(client)
    register_team(client, 1, 10)
    task = dispatch(client).json()
    assert client.post(
        f"/rescue-tasks/{task['task_id']}/completion",
        json={"exit_id": 6, "delivery_minute": 4}).status_code == 200
    resp = dispatch(client)
    assert resp.status_code == 409
    assert resp.json()["conflicts"][0]["reason"] == "GROUP_ALREADY_RESCUED"


def test_rejected_dispatch_changes_nothing(client):
    make_awaiting_rescue(client)
    register_team(client, 1, 9)  # 载员不足
    plan0 = client.get("/plan").json()
    n0 = len(revs(client))
    assert dispatch(client).status_code == 409
    assert client.get("/plan").json() == plan0
    assert len(revs(client)) == n0


# --------------------------------------------------------------------------
# 结案核销
# --------------------------------------------------------------------------

def test_completion_success_releases_team_and_marks_rescued(client):
    make_awaiting_rescue(client)
    register_team(client, 1, 10)
    task = dispatch(client).json()
    task_id = task["task_id"]

    # 送达分钟 6（当前分钟 4）属于未来 -> 先拒
    resp = client.post(f"/rescue-tasks/{task_id}/completion",
                       json={"exit_id": 6, "delivery_minute": 6})
    assert resp.status_code == 409
    assert resp.json()["conflicts"][0]["reason"] == "DELIVERY_IN_FUTURE"
    # 队伍仍忙
    assert client.get("/rescue-teams").json()["teams"][0]["task_id"] == task_id

    client.post("/tick", json={"minutes": 2})  # 当前分钟 6
    n0 = len(revs(client))
    resp = client.post(f"/rescue-tasks/{task_id}/completion",
                       json={"exit_id": 6, "delivery_minute": 6})
    assert resp.status_code == 200, resp.text
    done = resp.json()
    assert done["status"] == "completed"
    assert done["exit_id"] == 6 and done["delivery_minute"] == 6

    # 队伍释放为空闲
    teams = {t["team_id"]: t
             for t in client.get("/rescue-teams").json()["teams"]}
    assert teams[1]["task_id"] is None

    # 组已救援：规划到达字段保持为空，人工送达单列
    g1 = by_id(client.get("/plan").json())[1]
    assert g1["status"] == "rescued"
    assert g1["exit_id"] is None and g1["arrival_minute"] is None
    assert g1["rescue_task_id"] == task_id
    assert g1["rescue_team_id"] == 1
    assert g1["rescued_exit_id"] == 6
    assert g1["delivery_minute"] == 6

    # 查询任务为 completed；过滤参数可用
    got = client.get(f"/rescue-tasks/{task_id}").json()
    assert got["status"] == "completed"
    open_tasks = client.get("/rescue-tasks",
                            params={"status": "in_progress"}).json()["tasks"]
    assert open_tasks == []
    completed = client.get("/rescue-tasks",
                           params={"status": "completed"}).json()["tasks"]
    assert [t["task_id"] for t in completed] == [task_id]
    bad = client.get("/rescue-tasks", params={"status": "bogus"})
    assert bad.status_code == 422

    # 产生一条 rescue-completions 修订
    entry = revs(client)[-1]
    assert len(revs(client)) == n0 + 1
    assert entry["operation"] == "rescue-completions"
    assert entry["detail"] == {
        "task_id": task_id, "group_id": 1, "team_id": 1,
        "exit_id": 6, "delivery_minute": 6, "dispatch_minute": 4}


def test_completion_delivery_equal_dispatch_minute_allowed(client):
    # 派遣发生在当前分钟；送达分钟等于派遣分钟（不早于即允许）
    make_awaiting_rescue(client)
    register_team(client, 1, 10)
    task_id = dispatch(client).json()["task_id"]
    resp = client.post(f"/rescue-tasks/{task_id}/completion",
                       json={"exit_id": 6, "delivery_minute": 4})
    assert resp.status_code == 200
    assert resp.json()["delivery_minute"] == 4


def test_completion_later_than_original_deadline_recorded_truthfully(client):
    # 截止分钟 5：tick 到第 8 分钟才送达，晚于原截止也如实记录
    make_awaiting_rescue(client)
    register_team(client, 1, 10)
    task_id = dispatch(client).json()["task_id"]
    client.post("/tick", json={"minutes": 4})  # 当前分钟 8
    resp = client.post(f"/rescue-tasks/{task_id}/completion",
                       json={"exit_id": 6, "delivery_minute": 8})
    assert resp.status_code == 200
    g1 = by_id(client.get("/plan").json())[1]
    assert g1["status"] == "rescued"
    assert g1["rescued_exit_id"] == 6
    assert g1["delivery_minute"] == 8


def test_completion_unknown_task_404_and_unknown_exit_422(client):
    make_awaiting_rescue(client)
    register_team(client, 1, 10)
    task_id = dispatch(client).json()["task_id"]
    assert client.post("/rescue-tasks/9999/completion",
                       json={"exit_id": 6,
                             "delivery_minute": 4}).status_code == 404
    assert client.get("/rescue-tasks/9999").status_code == 404
    # 出口 7 不是既有出口（既有出口只有 6）
    resp = client.post(f"/rescue-tasks/{task_id}/completion",
                       json={"exit_id": 7, "delivery_minute": 4})
    assert resp.status_code == 422
    # 任务仍在进行、队伍仍忙
    assert client.get(f"/rescue-tasks/{task_id}").json()["status"] == \
        "in_progress"


def test_completion_before_dispatch_and_double_close_rejected(client):
    make_awaiting_rescue(client)
    register_team(client, 1, 10)
    task_id = dispatch(client).json()["task_id"]
    # 派遣分钟为 4；送达 3 早于派遣
    resp = client.post(f"/rescue-tasks/{task_id}/completion",
                       json={"exit_id": 6, "delivery_minute": 3})
    assert resp.status_code == 409
    assert resp.json()["conflicts"][0]["reason"] == \
        "DELIVERY_BEFORE_DISPATCH"
    # 正常结案
    assert client.post(f"/rescue-tasks/{task_id}/completion",
                       json={"exit_id": 6,
                             "delivery_minute": 4}).status_code == 200
    # 重复结案
    resp = client.post(f"/rescue-tasks/{task_id}/completion",
                       json={"exit_id": 6, "delivery_minute": 4})
    assert resp.status_code == 409
    conflict = resp.json()["conflicts"][0]
    assert conflict["reason"] == "TASK_ALREADY_COMPLETED"
    assert conflict["completed_exit_id"] == 6
    assert conflict["completed_delivery_minute"] == 4


def test_team_can_take_new_task_after_completion(client):
    make_awaiting_rescue(client)
    register_team(client, 1, 10)
    task_id = dispatch(client).json()["task_id"]
    assert client.post(f"/rescue-tasks/{task_id}/completion",
                       json={"exit_id": 6,
                             "delivery_minute": 4}).status_code == 200
    # 已救援的组不可再派；但队伍已空闲（由另一待救援组验证）
    client.post("/scenario", json={
        "passages": [
            {"id": 1, "src": 1, "dst": 2, "minutes": 4, "capacity": 10},
            {"id": 2, "src": 2, "dst": 6, "minutes": 1, "capacity": 10},
        ],
        "exits": [6],
        "closures": [{"edge_id": 2, "start_minute": 5, "end_minute": 30}],
        "groups": [
            {"id": 1, "size": 10, "origin": 1, "earliest_departure": 0,
             "priority": 10, "deadline": 30},
            {"id": 2, "size": 10, "origin": 1, "earliest_departure": 0,
             "priority": 5, "deadline": 30}]})
    client.post("/tick", json={"minutes": 4})
    assert client.post("/groups/1/delay-report",
                       json={"node_id": 2,
                             "recovery_minute": 6}).status_code == 200
    # 场景重置清空了队伍与任务：同编号队伍可重新登记，新任务号不复用
    resp = register_team(client, 1, 10)
    assert resp.status_code == 201
    resp = dispatch(client, group_id=1, team_id=1, node=2)
    assert resp.status_code == 201
    assert resp.json()["task_id"] != task_id


# --------------------------------------------------------------------------
# 修订历史：快照/差异/旧修订不动
# --------------------------------------------------------------------------

def test_dispatch_revision_snapshot_and_diff(client):
    make_awaiting_rescue(client)
    register_team(client, 1, 10)
    n0 = len(revs(client))
    resp = dispatch(client)
    assert resp.status_code == 201
    task_id = resp.json()["task_id"]
    rev = revs(client)[-1]["rev"]
    assert len(revs(client)) == n0 + 1
    detail = client.get(f"/revisions/{rev}").json()
    # 快照即派遣后的当前编排
    assert detail["snapshot"] == client.get("/plan").json()
    ch = {c["group_id"]: c for c in detail["changes"]}[1]
    assert ch["change"] == "changed"
    assert ch["before"]["status"] == "awaiting_rescue"
    assert ch["after"]["status"] == "rescuing"
    assert "rescue_task_id" in ch["changed_fields"]
    assert "rescue_team_id" in ch["changed_fields"]
    assert ch["before"]["rescue_task_id"] is None
    assert ch["after"]["rescue_task_id"] == task_id
    # 前缀与路线未变
    assert ch["after"]["route"] == [1]
    assert ch["after"]["prefix_step_count"] == 1


def test_completion_revision_snapshot_and_old_revisions_untouched(client):
    make_awaiting_rescue(client)
    register_team(client, 1, 10)
    task_id = dispatch(client).json()["task_id"]
    dispatch_rev = revs(client)[-1]["rev"]
    before_snapshot = client.get(f"/revisions/{dispatch_rev}").json()
    client.post(f"/rescue-tasks/{task_id}/completion",
                json={"exit_id": 6, "delivery_minute": 4})
    rev = revs(client)[-1]["rev"]
    detail = client.get(f"/revisions/{rev}").json()
    ch = {c["group_id"]: c for c in detail["changes"]}[1]
    assert ch["before"]["status"] == "rescuing"
    assert ch["after"]["status"] == "rescued"
    assert "rescued_exit_id" in ch["changed_fields"]
    assert "delivery_minute" in ch["changed_fields"]
    # 规划到达字段不被人工送达污染
    assert ch["after"]["exit_id"] is None
    assert ch["after"]["arrival_minute"] is None
    assert ch["after"]["rescued_exit_id"] == 6
    assert ch["after"]["delivery_minute"] == 4
    # 旧修订快照原样保留
    assert client.get(f"/revisions/{dispatch_rev}").json() == before_snapshot


# --------------------------------------------------------------------------
# 场景重置：清空队伍与任务，旧修订保留
# --------------------------------------------------------------------------

def test_scenario_reset_clears_teams_and_tasks_keeps_revisions(client):
    make_awaiting_rescue(client)
    register_team(client, 1, 10)
    task_id = dispatch(client).json()["task_id"]
    client.post(f"/rescue-tasks/{task_id}/completion",
                json={"exit_id": 6, "delivery_minute": 4})
    revisions_before = revs(client)
    assert revisions_before[-1]["operation"] == "rescue-completions"

    # 重置为不含任何队伍/任务的新场景
    client.post("/scenario", json={
        "passages": [{"id": 1, "src": 1, "dst": 6, "minutes": 1,
                      "capacity": 10}],
        "exits": [6], "closures": [],
        "groups": [{"id": 1, "size": 1, "origin": 1,
                    "earliest_departure": 0, "priority": 1,
                    "deadline": 10}]})
    assert client.get("/rescue-teams").json()["teams"] == []
    assert client.get("/rescue-tasks").json()["tasks"] == []
    assert client.get(f"/rescue-tasks/{task_id}").status_code == 404

    # 旧修订保留，编号继续递增
    revisions_after = revs(client)
    old = revisions_after[:len(revisions_before)]
    assert old == revisions_before
    assert revisions_after[-1]["operation"] == "scenario"
    assert [e["rev"] for e in revisions_after] == \
        list(range(1, len(revisions_after) + 1))
    # 重置后登记同编号队伍不冲突，任务号不复用旧序列
    assert register_team(client, 1, 8).status_code == 201


# --------------------------------------------------------------------------
# 并发抢占
# --------------------------------------------------------------------------

def test_concurrent_dispatch_only_one_wins(client):
    from concurrent.futures import ThreadPoolExecutor

    make_awaiting_rescue(client)
    register_team(client, 1, 10)
    observed = client.get("/health").json()["current_minute"]
    payload = {"group_id": 1, "team_id": 1, "node_id": 2,
               "observed_minute": observed}
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(
            lambda _: client.post("/rescue-tasks", json=payload), range(2)))
    statuses = sorted(r.status_code for r in responses)
    assert statuses == [201, 409]
    # 唯一任务、队伍只关联它
    tasks = client.get("/rescue-tasks").json()["tasks"]
    assert len(tasks) == 1
    assert client.get("/rescue-teams").json()["teams"][0]["task_id"] == \
        tasks[0]["task_id"]
    # 修订号连续
    entries = revs(client)
    assert [e["rev"] for e in entries] == list(range(1, len(entries) + 1))


def test_dispatch_racing_tick_either_accepted_or_whole_rejected(client):
    from concurrent.futures import ThreadPoolExecutor

    for _ in range(6):
        make_awaiting_rescue(client)  # 当前分钟 4
        register_team(client, 1, 10)
        with ThreadPoolExecutor(max_workers=2) as pool:
            f_dispatch = pool.submit(
                client.post, "/rescue-tasks",
                json={"group_id": 1, "team_id": 1, "node_id": 2,
                      "observed_minute": 4})
            f_tick = pool.submit(client.post, "/tick",
                                 json={"minutes": 2})
            d, t = f_dispatch.result(), f_tick.result()
        assert t.status_code == 200
        if d.status_code == 201:
            # 派遣先受理：tick 后组仍为 rescuing，任务保留
            g1 = by_id(client.get("/plan").json())[1]
            assert g1["status"] == "rescuing"
        else:
            # tick 先提交：所见分钟失步，整次拒绝且无任务
            assert d.status_code == 409
            assert d.json()["conflicts"][0]["reason"] == \
                "OBSERVED_MINUTE_STALE"
            assert client.get("/rescue-tasks").json()["tasks"] == []
            g1 = by_id(client.get("/plan").json())[1]
            assert g1["status"] == "awaiting_rescue"
        break
