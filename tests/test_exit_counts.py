"""出口清点（按组清点实际撤离人数）集成测试。

覆盖：
* 登记：非空唯一流水号 + 组号 + 出口号 + 正整数人数 + 非负清点分钟；
  仅已按固定路线抵达该出口（fixed 且到达分钟不晚于当前分钟）或已由人工
  救援实际送达该出口（rescued，采用实际出口与实际送达分钟）的组可登记；
  清点分钟不早于相应到达/送达分钟、不晚于当前分钟；
* 幂等：同一流水号同内容重试返回原记录（HTTP 200，不产生重复记录），
  异内容整次拒绝（409 EXIT_COUNT_SERIAL_CONFLICT）；
* 上限：同组累计人数不得超过组人数（登记与更正均按更正后口径复核）；
* 更正：凭原流水号、原记录版本与非空原因更正为非负人数；保留原值与
  更正历史；版本失配、对同一版本重复更正、更正后累计超额均整次拒绝；
* 查询：按组返回应到/已清点/未清点与逐条记录，救援送达采用实际出口与
  时刻；未知组号 404；
* 清点与更正不改编排、不产生修订；场景重置清空全部清点记录。
"""
import os
import tempfile

import pytest
from fastapi.testclient import TestClient

_tmpdir = tempfile.mkdtemp(prefix="evac-test-exit-counts-")
os.environ["EVAC_DB_PATH"] = os.path.join(_tmpdir, "test.db")
os.environ["EVAC_SEED"] = "false"

from app.main import app  # noqa: E402


@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c


def revs(c):
    return c.get("/revisions").json()["revisions"]


def make_arrived(c):
    """双出口场景：1->6(edge1,2min,cap20)，出口 [5,6]。

    组1（10人）t=0 出发、t=2 按固定路线抵达出口 6；组2（5人）最早出发
    分钟 10（尚未出发）。推进到第 3 分钟：组1 fixed 且已抵达。
    """
    resp = c.post("/scenario", json={
        "passages": [{"id": 1, "src": 1, "dst": 6, "minutes": 2,
                      "capacity": 20}],
        "exits": [5, 6], "closures": [],
        "groups": [
            {"id": 1, "size": 10, "origin": 1, "earliest_departure": 0,
             "priority": 10, "deadline": 30},
            {"id": 2, "size": 5, "origin": 1, "earliest_departure": 10,
             "priority": 5, "deadline": 30}]})
    assert resp.status_code == 200
    assert c.post("/tick", json={"minutes": 3}).status_code == 200


def make_rescued(c):
    """待救援 -> 派遣 -> 结案（出口 6、实际送达分钟 4）；当前分钟 4。"""
    assert c.post("/scenario", json={
        "passages": [
            {"id": 1, "src": 1, "dst": 2, "minutes": 4, "capacity": 10},
            {"id": 2, "src": 2, "dst": 6, "minutes": 1, "capacity": 10}],
        "exits": [5, 6],
        "closures": [{"edge_id": 2, "start_minute": 5, "end_minute": 30}],
        "groups": [{"id": 1, "size": 10, "origin": 1,
                    "earliest_departure": 0, "priority": 10,
                    "deadline": 30}]}).status_code == 200
    assert c.post("/tick", json={"minutes": 4}).status_code == 200
    assert c.post("/groups/1/delay-report",
                  json={"node_id": 2, "recovery_minute": 6}
                  ).status_code == 200
    assert c.post("/rescue-teams",
                  json={"team_id": 1, "capacity": 10}).status_code == 201
    task = c.post("/rescue-tasks",
                  json={"group_id": 1, "team_id": 1, "node_id": 2,
                        "observed_minute": 4}).json()
    assert c.post(f"/rescue-tasks/{task['task_id']}/completion",
                  json={"exit_id": 6, "delivery_minute": 4}
                  ).status_code == 200


def submit(c, serial, group_id=1, exit_id=6, count=1, counted_minute=2):
    return c.post("/exit-counts", json={
        "serial": serial, "group_id": group_id, "exit_id": exit_id,
        "count": count, "counted_minute": counted_minute})


def correct(c, serial, version, count, reason="复核更正"):
    return c.post(f"/exit-counts/{serial}/correction",
                  json={"version": version, "count": count,
                        "reason": reason})


# --------------------------------------------------------------------------
# 登记与查询
# --------------------------------------------------------------------------

def test_submit_and_query_group_tally(client):
    make_arrived(client)
    resp = submit(client, "A-1", count=4, counted_minute=2)
    assert resp.status_code == 201, resp.text
    rec = resp.json()
    assert rec["serial"] == "A-1"
    assert rec["group_id"] == 1 and rec["exit_id"] == 6
    assert rec["count"] == 4 and rec["submitted_count"] == 4
    assert rec["counted_minute"] == 2
    assert rec["version"] == 1 and rec["created_minute"] == 3
    assert rec["corrections"] == []

    # 按组查询：应到/已清点/未清点 + 逐条记录 + 到达锚点（规划到达）
    tally = client.get("/exit-counts/1").json()
    assert tally["current_minute"] == 3
    assert tally["group_id"] == 1
    assert tally["expected"] == 10
    assert tally["counted"] == 4
    assert tally["remaining"] == 6
    assert tally["arrival"] == {"basis": "planned_arrival",
                                "exit_id": 6, "minute": 2}
    assert [r["serial"] for r in tally["records"]] == ["A-1"]

    # 汇总列表包含全部组（未抵达组锚点为 null、记录为空）
    listing = client.get("/exit-counts").json()
    assert listing["current_minute"] == 3
    by_gid = {g["group_id"]: g for g in listing["groups"]}
    assert by_gid[1]["counted"] == 4 and by_gid[1]["remaining"] == 6
    assert by_gid[2]["expected"] == 5 and by_gid[2]["counted"] == 0
    assert by_gid[2]["remaining"] == 5
    assert by_gid[2]["arrival"] == {"basis": None, "exit_id": None,
                                    "minute": None}
    assert by_gid[2]["records"] == []


def test_counted_minute_boundaries(client):
    make_arrived(client)  # 到达 2，当前 3
    # 清点分钟 == 到达分钟、== 当前分钟均允许
    assert submit(client, "B-1", count=1, counted_minute=2).status_code == 201
    assert submit(client, "B-2", count=1, counted_minute=3).status_code == 201
    # 早于到达分钟
    resp = submit(client, "B-3", count=1, counted_minute=1)
    assert resp.status_code == 409
    conflict = resp.json()["conflicts"][0]
    assert resp.json()["error"] == "EXIT_COUNT_NOT_ACCEPTABLE"
    assert conflict["reason"] == "COUNTED_MINUTE_BEFORE_ARRIVAL"
    assert conflict["arrival_minute"] == 2
    # 晚于当前分钟
    resp = submit(client, "B-4", count=1, counted_minute=4)
    assert resp.status_code == 409
    assert resp.json()["conflicts"][0]["reason"] == "COUNTED_MINUTE_IN_FUTURE"


def test_idempotent_retry_same_content_returns_original(client):
    make_arrived(client)
    first = submit(client, "C-1", count=4, counted_minute=2)
    assert first.status_code == 201
    replay = submit(client, "C-1", count=4, counted_minute=2)
    assert replay.status_code == 200
    assert replay.json() == first.json()
    # 不产生重复记录
    tally = client.get("/exit-counts/1").json()
    assert len(tally["records"]) == 1
    assert tally["counted"] == 4


def test_same_serial_different_content_rejected(client):
    make_arrived(client)
    assert submit(client, "D-1", count=4).status_code == 201
    for changed in ({"count": 5}, {"exit_id": 5}, {"counted_minute": 3},
                    {"group_id": 2}):
        body = {"serial": "D-1", "group_id": 1, "exit_id": 6, "count": 4,
                "counted_minute": 2, **changed}
        resp = client.post("/exit-counts", json=body)
        assert resp.status_code == 409, body
        assert resp.json()["error"] == "EXIT_COUNT_SERIAL_CONFLICT"
        conflict = resp.json()["conflicts"][0]
        assert conflict["reason"] == "SERIAL_CONTENT_MISMATCH"
        assert conflict["existing"]["count"] == 4
    # 原记录不受影响
    tally = client.get("/exit-counts/1").json()
    assert len(tally["records"]) == 1
    assert tally["counted"] == 4


def test_group_not_arrived_rejected(client):
    make_arrived(client)
    # 组 2 尚未出发（scheduled）
    resp = submit(client, "E-1", group_id=2, exit_id=6, count=1,
                  counted_minute=3)
    assert resp.status_code == 409
    conflict = resp.json()["conflicts"][0]
    assert conflict["reason"] == "GROUP_NOT_ARRIVED"
    assert conflict["status"] == "scheduled"


def test_en_route_fixed_group_not_countable(client):
    # 组已出发（fixed）但到达分钟在未来：尚未抵达，不可登记
    client.post("/scenario", json={
        "passages": [{"id": 1, "src": 1, "dst": 6, "minutes": 10,
                      "capacity": 20}],
        "exits": [6], "closures": [],
        "groups": [{"id": 1, "size": 10, "origin": 1,
                    "earliest_departure": 0, "priority": 10,
                    "deadline": 30}]})
    client.post("/tick", json={"minutes": 3})  # fixed，在途，到达 10
    resp = submit(client, "E-2", count=1, counted_minute=3)
    assert resp.status_code == 409
    assert resp.json()["conflicts"][0]["reason"] == "GROUP_NOT_ARRIVED"


def test_exit_mismatch_rejected(client):
    make_arrived(client)  # 组 1 抵达出口 6；出口 5 也是既有出口
    resp = submit(client, "F-1", exit_id=5, count=1, counted_minute=2)
    assert resp.status_code == 409
    conflict = resp.json()["conflicts"][0]
    assert conflict["reason"] == "EXIT_MISMATCH"
    assert conflict["expected_exit_id"] == 6
    assert conflict["arrival_basis"] == "planned_arrival"


def test_cumulative_count_capped_by_group_size(client):
    make_arrived(client)  # 组 1 共 10 人
    assert submit(client, "G-1", count=6).status_code == 201
    resp = submit(client, "G-2", count=5)  # 6+5 > 10
    assert resp.status_code == 409
    conflict = resp.json()["conflicts"][0]
    assert conflict["reason"] == "COUNT_EXCEEDS_GROUP_SIZE"
    assert conflict["group_size"] == 10
    assert conflict["already_counted"] == 6
    assert conflict["submitted_count"] == 5
    # 恰至组人数允许；再超即拒
    assert submit(client, "G-2", count=4).status_code == 201
    resp = submit(client, "G-3", count=1)
    assert resp.status_code == 409
    assert resp.json()["conflicts"][0]["reason"] == "COUNT_EXCEEDS_GROUP_SIZE"
    tally = client.get("/exit-counts/1").json()
    assert tally["counted"] == 10 and tally["remaining"] == 0


def test_submit_validation_422(client):
    make_arrived(client)
    # 空流水号 / 非正人数 / 负分钟：pydantic 422
    assert submit(client, "", count=1).status_code == 422
    assert submit(client, "H-1", count=0).status_code == 422
    assert submit(client, "H-2", count=-3).status_code == 422
    assert submit(client, "H-3", count=1, counted_minute=-1).status_code == 422
    # 组不存在 / 出口非既有出口：422
    assert submit(client, "H-4", group_id=99, count=1).status_code == 422
    assert submit(client, "H-5", exit_id=9, count=1).status_code == 422
    # 均未登记
    assert client.get("/exit-counts/1").json()["records"] == []


# --------------------------------------------------------------------------
# 人工救援送达组：采用实际出口与时刻
# --------------------------------------------------------------------------

def test_rescued_group_counted_at_actual_exit_and_minute(client):
    make_rescued(client)  # 出口 6、实际送达 4，当前分钟 4
    # 送达出口不符（出口 5 是既有出口，但非实际送达出口）
    resp = submit(client, "R-1", exit_id=5, count=2, counted_minute=4)
    assert resp.status_code == 409
    conflict = resp.json()["conflicts"][0]
    assert conflict["reason"] == "EXIT_MISMATCH"
    assert conflict["expected_exit_id"] == 6
    assert conflict["arrival_basis"] == "actual_delivery"
    # 清点分钟早于实际送达分钟
    resp = submit(client, "R-2", count=2, counted_minute=3)
    assert resp.status_code == 409
    conflict = resp.json()["conflicts"][0]
    assert conflict["reason"] == "COUNTED_MINUTE_BEFORE_ARRIVAL"
    assert conflict["arrival_minute"] == 4
    # 按实际出口与实际送达分钟登记成功
    resp = submit(client, "R-3", count=6, counted_minute=4)
    assert resp.status_code == 201
    tally = client.get("/exit-counts/1").json()
    assert tally["arrival"] == {"basis": "actual_delivery",
                                "exit_id": 6, "minute": 4}
    assert tally["counted"] == 6 and tally["remaining"] == 4


# --------------------------------------------------------------------------
# 更正：版本、历史与上限
# --------------------------------------------------------------------------

def test_correction_keeps_history_and_bumps_version(client):
    make_arrived(client)
    assert submit(client, "K-1", count=8).status_code == 201
    resp = correct(client, "K-1", version=1, count=6, reason="复核实为 6 人")
    assert resp.status_code == 200, resp.text
    rec = resp.json()
    assert rec["count"] == 6
    assert rec["submitted_count"] == 8  # 原值保留
    assert rec["version"] == 2
    assert rec["corrections"] == [{
        "from_version": 1, "to_version": 2, "old_count": 8, "new_count": 6,
        "reason": "复核实为 6 人", "minute": 3}]

    # 再次更正（基于版本 2）：历史累计保留
    resp = correct(client, "K-1", version=2, count=3, reason="离队 3 人")
    assert resp.status_code == 200
    rec = resp.json()
    assert rec["count"] == 3 and rec["version"] == 3
    assert [c["to_version"] for c in rec["corrections"]] == [2, 3]
    assert rec["corrections"][1]["old_count"] == 6
    assert rec["corrections"][1]["new_count"] == 3

    # 查询反映更正后的口径
    tally = client.get("/exit-counts/1").json()
    assert tally["counted"] == 3 and tally["remaining"] == 7
    assert tally["records"][0]["corrections"][0]["old_count"] == 8


def test_correction_to_zero_allowed(client):
    make_arrived(client)
    assert submit(client, "K-0", count=5).status_code == 201
    resp = correct(client, "K-0", version=1, count=0, reason="误登记")
    assert resp.status_code == 200
    assert resp.json()["count"] == 0
    assert client.get("/exit-counts/1").json()["counted"] == 0


def test_correction_version_conflicts(client):
    make_arrived(client)
    assert submit(client, "K-2", count=4).status_code == 201
    assert correct(client, "K-2", version=1, count=3).status_code == 200
    # 对同一版本（v1）重复更正
    resp = correct(client, "K-2", version=1, count=2)
    assert resp.status_code == 409
    body = resp.json()
    assert body["error"] == "EXIT_COUNT_CORRECTION_NOT_ACCEPTABLE"
    conflict = body["conflicts"][0]
    assert conflict["reason"] == "VERSION_ALREADY_CORRECTED"
    assert conflict["submitted_version"] == 1
    assert conflict["current_version"] == 2
    # 版本失配（大于当前版本）
    resp = correct(client, "K-2", version=5, count=2)
    assert resp.status_code == 409
    assert resp.json()["conflicts"][0]["reason"] == "VERSION_MISMATCH"
    # 记录与历史不变
    rec = client.get("/exit-counts/1").json()["records"][0]
    assert rec["count"] == 3 and rec["version"] == 2
    assert len(rec["corrections"]) == 1


def test_correction_respects_group_size_cap(client):
    make_arrived(client)  # 组 1 共 10 人
    assert submit(client, "L-1", count=6).status_code == 201
    assert submit(client, "L-2", count=4).status_code == 201  # 累计 10
    # 更正后累计 7+4=11 > 10 -> 整次拒绝
    resp = correct(client, "L-1", version=1, count=7)
    assert resp.status_code == 409
    conflict = resp.json()["conflicts"][0]
    assert conflict["reason"] == "COUNT_EXCEEDS_GROUP_SIZE"
    assert conflict["group_size"] == 10
    assert conflict["already_counted"] == 10
    assert conflict["corrected_count"] == 7
    # 更正为 5（累计 9）允许
    assert correct(client, "L-1", version=1, count=5).status_code == 200
    tally = client.get("/exit-counts/1").json()
    assert tally["counted"] == 9 and tally["remaining"] == 1


def test_correction_unknown_serial_404_and_validation_422(client):
    make_arrived(client)
    assert submit(client, "M-1", count=2).status_code == 201
    assert client.post("/exit-counts/NOPE/correction",
                       json={"version": 1, "count": 1,
                             "reason": "x"}).status_code == 404
    # 空原因 / 负人数 / 非法版本：422
    assert correct(client, "M-1", version=1, count=1,
                   reason="").status_code == 422
    assert correct(client, "M-1", version=1, count=-1).status_code == 422
    assert correct(client, "M-1", version=0, count=1).status_code == 422
    # 记录未变
    rec = client.get("/exit-counts/1").json()["records"][0]
    assert rec["count"] == 2 and rec["version"] == 1


def test_get_unknown_group_404(client):
    make_arrived(client)
    assert client.get("/exit-counts/99").status_code == 404


# --------------------------------------------------------------------------
# 不改编排与修订；场景重置清空
# --------------------------------------------------------------------------

def test_counts_and_corrections_change_neither_plan_nor_revisions(client):
    make_arrived(client)
    plan0 = client.get("/plan").json()
    n0 = len(revs(client))
    assert submit(client, "N-1", count=4).status_code == 201
    assert correct(client, "N-1", version=1, count=3).status_code == 200
    assert client.get("/plan").json() == plan0
    assert len(revs(client)) == n0


def test_scenario_reset_clears_exit_counts(client):
    make_arrived(client)
    assert submit(client, "P-1", count=4).status_code == 201
    assert correct(client, "P-1", version=1, count=3).status_code == 200

    # 重置为同构场景（组 1 重新从 pending 开始）
    client.post("/scenario", json={
        "passages": [{"id": 1, "src": 1, "dst": 6, "minutes": 2,
                      "capacity": 20}],
        "exits": [5, 6], "closures": [],
        "groups": [{"id": 1, "size": 10, "origin": 1,
                    "earliest_departure": 0, "priority": 10,
                    "deadline": 30}]})
    tally = client.get("/exit-counts/1").json()
    assert tally["counted"] == 0 and tally["records"] == []
    assert client.get("/exit-counts").json()["groups"][0]["records"] == []
    # 旧流水号可重新使用（当前组尚未抵达，登记按新场景口径校验）
    assert submit(client, "P-1", count=4,
                  counted_minute=0).status_code == 409
    client.post("/tick", json={"minutes": 3})
    assert submit(client, "P-1", count=4, counted_minute=2).status_code == 201
    rec = client.get("/exit-counts/1").json()["records"][0]
    assert rec["version"] == 1 and rec["corrections"] == []


def test_rejected_submission_creates_no_record(client):
    make_arrived(client)
    n0 = len(revs(client))
    assert submit(client, "Q-1", exit_id=5, count=1).status_code == 409
    assert submit(client, "Q-1", count=99).status_code == 409
    # 失败提交不占用流水号：同流水号可再次提交合法内容
    assert submit(client, "Q-1", count=2).status_code == 201
    assert len(revs(client)) == n0
