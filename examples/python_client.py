#!/usr/bin/env python3
"""场馆疏散通道编排服务 —— Python 调用示例（仅依赖标准库）。

运行：  python3 examples/python_client.py [BASE_URL]
默认 BASE_URL=http://localhost:8000
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000"


def call(method: str, path: str, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


def show(title, status, payload):
    print(f"\n==== {title}（HTTP {status}）====")
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def summarize(plan):
    print(f"\n当前分钟: {plan['current_minute']}")
    for g in plan["groups"]:
        if g["status"] in ("scheduled", "fixed"):
            print(f"  组 {g['group_id']:>2} [{g['status']:>9}] "
                  f"出口 {g['exit_id']}  出发 {g['departure_minute']:>3} "
                  f"到达 {g['arrival_minute']:>3}  通道序列 {g['route']}"
                  + (f"  已走前缀 {g['prefix_step_count']} 段"
                     if g.get("prefix_step_count") is not None else ""))
        elif g["status"] == "awaiting_rescue":
            print(f"  组 {g['group_id']:>2} [待救援    ] "
                  f"滞留于已走前缀（{g['prefix_step_count']} 段，"
                  f"通道序列 {g['route']}）原因 {g['reason']}：{g['detail']}")
        elif g["status"] == "blocked":
            basis = g.get("release_basis") or {}
            print(f"  组 {g['group_id']:>2} [前置未满足] "
                  f"先导 {g['leader_group_id']} 间隔 {g['release_gap']} "
                  f"({basis.get('reason')})，不占通道")
        else:
            print(f"  组 {g['group_id']:>2} [{g['status']:>9}] "
                  f"原因 {g['reason']}：{g['detail']}")


if __name__ == "__main__":
    show("健康检查", *call("GET", "/health"))

    scenario = {
        "passages": [
            {"id": 1, "src": 1, "dst": 2, "minutes": 4, "capacity": 30},
            {"id": 2, "src": 2, "dst": 3, "minutes": 3, "capacity": 30},
            {"id": 3, "src": 3, "dst": 5, "minutes": 2, "capacity": 30},
            {"id": 4, "src": 1, "dst": 4, "minutes": 5, "capacity": 20},
            {"id": 5, "src": 2, "dst": 6, "minutes": 2, "capacity": 40},
            {"id": 6, "src": 4, "dst": 6, "minutes": 3, "capacity": 30},
            {"id": 7, "src": 7, "dst": 5, "minutes": 5, "capacity": 20},
        ],
        "exits": [5, 6],
        "closures": [{"edge_id": 1, "start_minute": 3, "end_minute": 10}],
        "groups": [
            {"id": 1, "size": 30, "origin": 1, "earliest_departure": 0,
             "priority": 10, "deadline": 30},
            {"id": 2, "size": 20, "origin": 1, "earliest_departure": 0,
             "priority": 5, "deadline": 25},
            {"id": 3, "size": 10, "origin": 2, "earliest_departure": 0,
             "priority": 20, "deadline": 20},
            {"id": 4, "size": 15, "origin": 7, "earliest_departure": 0,
             "priority": 5, "deadline": 15},
        ],
    }
    status, plan = call("POST", "/scenario", scenario)
    show("初始化场景并编排", status, plan)
    summarize(plan)

    status, plan = call("POST", "/groups", [
        {"id": 9, "size": 40, "origin": 1, "earliest_departure": 0,
         "priority": 1, "deadline": 12}])
    show("新增组 9（人数 40，截止很紧，演示无法安排的原因）", status, plan)
    summarize(plan)

    status, plan = call("POST", "/tick", {"minutes": 1})
    show("推进 1 分钟", status, plan)
    summarize(plan)

    # 组 3 在 t=0 进入通道 5（2->6），占用 [0,2)；当前分钟 1 时仍在途，
    # 与其剩余区间 [1,2) 相交的封闭应被整次拒绝
    status, payload = call("POST", "/closures",
                           {"edge_id": 5, "start_minute": 1, "end_minute": 3})
    show("触及已出发组剩余路线的封闭（预期 409）", status, payload)

    status, plan = call("GET", "/plan")
    show("拒绝后查询，原编排保持不变", status, plan)
    summarize(plan)

    # ---- 容量管制（临时限流）----
    # 组 3 已出发，剩余占用通道 5 的 [1,2) 共 10 人；上限 5 会使其超载，
    # 整次拒绝并返回冲突分钟、占用量与相关组
    status, payload = call("POST", "/capacity-controls",
                           {"edge_id": 5, "start_minute": 1, "end_minute": 3,
                            "capacity": 5})
    show("使已出发组超载的容量管制（预期 409）", status, payload)

    # 通道 1 第 10..14 分钟限流至 10 人/分钟：组 1（30 人）在管制时段内
    # 无法进入通道 1，重排后推迟到管制结束的第 14 分钟进入
    status, plan = call("POST", "/capacity-controls",
                        {"edge_id": 1, "start_minute": 10, "end_minute": 14,
                         "capacity": 10})
    show("登记容量管制（通道 1 第 10..14 分钟限流 10 人/分钟）",
         status, plan)
    summarize(plan)

    status, payload = call("GET", "/passages/1/load")
    show("通道 1 逐分钟占用与每分钟有效上限", status, payload)
    for m in payload["minutes"]:
        print(f"  第 {m['minute']:>2} 分钟：占用 {m['used']:>2} / "
              f"有效上限 {m['effective_capacity']}")

    status, payload = call("GET", "/capacity-controls")
    show("当前容量管制列表", status, payload)

    # 同一通道的管制时段不得重叠：与 [10,14) 相交 -> 409
    status, payload = call("POST", "/capacity-controls",
                           {"edge_id": 1, "start_minute": 12,
                            "end_minute": 16, "capacity": 20})
    show("与已有管制重叠的时段（预期 409）", status, payload)

    # ---- 人群组纠正（批量纠正未出发组的属性）----
    # 组 9 此前因人数 40 超容量而编排失败；纠正人数与截止分钟后可安排
    status, plan = call("POST", "/group-corrections",
                        [{"group_id": 9, "size": 20, "deadline": 30}])
    show("纠正组 9（人数 40->20、截止 12->30，整批生效并重排）",
         status, plan)
    summarize(plan)

    # 组 3 已出发（fixed）：纠正整批拒绝，状态与修订历史不变
    status, payload = call("POST", "/group-corrections",
                           [{"group_id": 3, "size": 5}])
    show("纠正已出发的组（预期 409）", status, payload)

    # 同批组编号重复 / 最终字段不合法等无效请求：422，整批拒绝
    status, payload = call("POST", "/group-corrections",
                           [{"group_id": 9, "size": 15},
                            {"group_id": 9, "priority": 3}])
    show("同批组编号重复的纠正批（预期 422）", status, payload)

    # ---- 现场节点滞留报告 ----
    # 重置为一个便于演示的小场景：组 1 的路线为通道1[0,4)+通道2[4,6)
    status, plan = call("POST", "/scenario", {
        "passages": [
            {"id": 1, "src": 1, "dst": 2, "minutes": 4, "capacity": 10},
            {"id": 2, "src": 2, "dst": 6, "minutes": 2, "capacity": 10},
            {"id": 3, "src": 2, "dst": 3, "minutes": 2, "capacity": 10},
            {"id": 4, "src": 3, "dst": 5, "minutes": 2, "capacity": 10},
        ],
        "exits": [5, 6], "closures": [],
        "groups": [
            {"id": 1, "size": 10, "origin": 1, "earliest_departure": 0,
             "priority": 10, "deadline": 30},
            {"id": 2, "size": 10, "origin": 1, "earliest_departure": 0,
             "priority": 5, "deadline": 30},
        ],
    })
    assert status == 200
    # 推进到第 4 分钟：组 1 已到达节点 2（下一通道进入分钟恰为当前分钟，
    # 仍可在节点被报告）
    status, plan = call("POST", "/tick", {"minutes": 4})
    show("推进到第 4 分钟（组 1 已到达节点 2）", status, plan)
    summarize(plan)

    # 未出发组 / 错误节点 / 恢复分钟在过去：均被拒绝，不改编排
    status, payload = call("POST", "/groups/2/delay-report",
                           {"node_id": 1, "recovery_minute": 4})
    show("报告尚未出发的组 2（预期 409 NOT_DEPARTED）", status, payload)
    status, payload = call("POST", "/groups/1/delay-report",
                           {"node_id": 1, "recovery_minute": 4})
    show("组 1 报告错误节点（实际在节点 2，预期 409 NODE_MISMATCH）",
         status, payload)
    status, payload = call("POST", "/groups/1/delay-report",
                           {"node_id": 2, "recovery_minute": 3})
    show("恢复分钟早于当前分钟（预期 422）", status, payload)

    # 正式报告：节点 2 滞留，预计第 7 分钟恢复。已走前缀通道1[0,4) 保留，
    # 旧预约通道2[4,6) 释放，新余程通道2[7,9) 作为固定占用；组 2 随之重排
    status, plan = call("POST", "/groups/1/delay-report",
                        {"node_id": 2, "recovery_minute": 7})
    show("报告组 1 在节点 2 滞留、第 7 分钟恢复（前缀保留+重找余程）",
         status, plan)
    summarize(plan)
    g1 = next(g for g in plan["groups"] if g["group_id"] == 1)
    print(f"  已走前缀步数 prefix_step_count={g1['prefix_step_count']}："
          f"steps[0] 已走过固定保留，steps[1] 为恢复后的新余程")

    # 等价的集合式写法（组号放在请求体内）
    status, payload = call("POST", "/delay-reports",
                           {"group_id": 2, "node_id": 1,
                            "recovery_minute": 4})
    show("集合式写法 /delay-reports（组 2 未出发，预期同样 409）",
         status, payload)

    # 待救援：唯一余程通道自第 5 分钟封闭，报告恢复 6 -> 无余程
    status, plan = call("POST", "/scenario", {
        "passages": [
            {"id": 1, "src": 1, "dst": 2, "minutes": 4, "capacity": 10},
            {"id": 2, "src": 2, "dst": 6, "minutes": 1, "capacity": 10},
        ],
        "exits": [6],
        "closures": [{"edge_id": 2, "start_minute": 5,
                      "end_minute": 30}],
        "groups": [
            {"id": 1, "size": 10, "origin": 1, "earliest_departure": 0,
             "priority": 10, "deadline": 30}],
    })
    assert status == 200
    call("POST", "/tick", {"minutes": 4})
    status, plan = call("POST", "/groups/1/delay-report",
                        {"node_id": 2, "recovery_minute": 6})
    show("报告组 1 恢复 6（余程被封闭阻断，预期 awaiting_rescue）",
         status, plan)
    summarize(plan)

    # 后续时间推进不会自动重排待救援组
    status, plan = call("POST", "/tick", {"minutes": 6})
    show("再推进 6 分钟（待救援组不自动重排）", status, plan)
    summarize(plan)

    # ---- 人工救援队：登记 / 派遣 / 结案核销 ----
    status, payload = call("POST", "/rescue-teams",
                           {"team_id": 1, "capacity": 10})
    show("登记救援队 1（载员量 10）", status, payload)
    status, payload = call("POST", "/rescue-teams",
                           {"team_id": 1, "capacity": 20})
    show("重复登记同编号队伍（预期 409）", status, payload)
    call("POST", "/rescue-teams", {"team_id": 2, "capacity": 5})

    # 派遣校验：节点不符 / 载员不足 / 所见分钟失步，均整次拒绝
    status, payload = call("POST", "/rescue-tasks",
                           {"group_id": 1, "team_id": 1, "node_id": 1,
                            "observed_minute": 10})
    show("派遣滞留节点不符（预期 409 NODE_MISMATCH）", status, payload)
    status, payload = call("POST", "/rescue-tasks",
                           {"group_id": 1, "team_id": 2, "node_id": 2,
                            "observed_minute": 10})
    show("组人数超队伍载员量（预期 409 TEAM_CAPACITY_EXCEEDED）",
         status, payload)
    status, payload = call("POST", "/rescue-tasks",
                           {"group_id": 1, "team_id": 1, "node_id": 2,
                            "observed_minute": 9})
    show("所见分钟与当前分钟不一致（预期 409 OBSERVED_MINUTE_STALE）",
         status, payload)

    # 正式派遣：组1（节点2，所见当前分钟10）-> 救援队1
    status, task = call("POST", "/rescue-tasks",
                        {"group_id": 1, "team_id": 1, "node_id": 2,
                         "observed_minute": 10})
    show("派遣成功（返回任务号，组置 rescuing）", status, task)
    task_id = task["task_id"]

    status, payload = call("GET", "/rescue-teams")
    show("救援队列表（队1 的 task_id 已回填）", status, payload)

    # 同组重复派遣 / 救援中组的滞留报告，均被拒绝
    status, payload = call("POST", "/rescue-tasks",
                           {"group_id": 1, "team_id": 2, "node_id": 2,
                            "observed_minute": 10})
    show("同组重复派遣（预期 409 GROUP_RESCUE_IN_PROGRESS）",
         status, payload)
    status, payload = call("POST", "/groups/1/delay-report",
                           {"node_id": 2, "recovery_minute": 12})
    show("救援中的组提交滞留报告（预期 409 GROUP_RESCUING）",
         status, payload)

    # 结案时序校验
    status, payload = call("POST", f"/rescue-tasks/{task_id}/completion",
                           {"exit_id": 6, "delivery_minute": 12})
    show("送达分钟晚于当前分钟（预期 409 DELIVERY_IN_FUTURE）",
         status, payload)
    status, payload = call("POST", f"/rescue-tasks/{task_id}/completion",
                           {"exit_id": 7, "delivery_minute": 10})
    show("送达出口非既有出口（预期 422）", status, payload)

    # 推进到第 12 分钟并结案：实际由出口 6 在第 12 分钟送达
    call("POST", "/tick", {"minutes": 2})
    status, task = call("POST", f"/rescue-tasks/{task_id}/completion",
                        {"exit_id": 6, "delivery_minute": 12})
    show("结案核销成功（队伍释放，组置 rescued）", status, task)

    status, payload = call("GET", f"/rescue-tasks/{task_id}")
    show("查询任务当前状态（completed，含实际出口与时刻）", status, payload)
    status, plan = call("GET", "/plan")
    g1 = next(g for g in plan["groups"] if g["group_id"] == 1)
    print("\n  组 1 最终编排：")
    print(f"    status={g1['status']}，规划到达 exit_id={g1['exit_id']} / "
          f"arrival_minute={g1['arrival_minute']}（人工送达不回写）")
    print(f"    单列实际送达：rescued_exit_id={g1['rescued_exit_id']} / "
          f"delivery_minute={g1['delivery_minute']}（晚于原截止也如实记录）")

    status, payload = call("POST", f"/rescue-tasks/{task_id}/completion",
                           {"exit_id": 6, "delivery_minute": 12})
    show("重复结案（预期 409 TASK_ALREADY_COMPLETED）", status, payload)
    status, payload = call("GET", "/rescue-tasks/999999")
    show("未知任务号（预期 404）", status, payload)

    # ---- 出口清点：按组清点实际撤离人数 ----
    # 组 1 已由人工救援实际送达出口 6（送达分钟 12，当前分钟 12）：
    # 清点采用实际出口与实际送达分钟（actual_delivery）
    count_body = {"serial": "CNT-0001", "group_id": 1, "exit_id": 6,
                  "count": 6, "counted_minute": 12}
    status, payload = call("POST", "/exit-counts", count_body)
    show("登记出口清点（救援送达组按实际出口与时刻）", status, payload)

    status, payload = call("POST", "/exit-counts", count_body)
    show("同流水号同内容重试（预期 200 返回原记录）", status, payload)
    status, payload = call("POST", "/exit-counts",
                           {**count_body, "count": 7})
    show("同流水号异内容（预期 409 EXIT_COUNT_SERIAL_CONFLICT）",
         status, payload)
    status, payload = call("POST", "/exit-counts",
                           {"serial": "CNT-0002", "group_id": 1, "exit_id": 6,
                            "count": 1, "counted_minute": 11})
    show("清点分钟早于实际送达分钟（预期 409）", status, payload)

    # 凭原流水号、记录版本与非空原因更正为非负人数（保留原值与历史）
    status, payload = call("POST", "/exit-counts/CNT-0001/correction",
                           {"version": 1, "count": 5,
                            "reason": "复核实为 5 人"})
    show("更正清点为 5 人（版本 1->2，原值保留）", status, payload)
    status, payload = call("POST", "/exit-counts/CNT-0001/correction",
                           {"version": 1, "count": 4, "reason": "重复更正"})
    show("对同一版本重复更正（预期 409 VERSION_ALREADY_CORRECTED）",
         status, payload)

    status, payload = call("GET", "/exit-counts/1")
    show("按组查询清点台账（应到/已清点/未清点/逐条记录）", status, payload)

    # 固定路线抵达组：重置为直达场景，组1 t=2 抵达出口6，推进到第3分钟
    status, plan = call("POST", "/scenario", {
        "passages": [
            {"id": 1, "src": 1, "dst": 6, "minutes": 2, "capacity": 20},
        ],
        "exits": [5, 6], "closures": [],
        "groups": [
            {"id": 1, "size": 10, "origin": 1, "earliest_departure": 0,
             "priority": 10, "deadline": 30},
            {"id": 2, "size": 5, "origin": 1, "earliest_departure": 10,
             "priority": 5, "deadline": 30},
        ],
    })
    assert status == 200
    call("POST", "/tick", {"minutes": 3})
    status, payload = call("POST", "/exit-counts",
                           {"serial": "CNT-1001", "group_id": 1, "exit_id": 6,
                            "count": 4, "counted_minute": 2})
    show("固定路线抵达组登记清点（planned_arrival）", status, payload)
    status, payload = call("POST", "/exit-counts",
                           {"serial": "CNT-1002", "group_id": 2, "exit_id": 6,
                            "count": 1, "counted_minute": 3})
    show("未抵达组登记（预期 409 GROUP_NOT_ARRIVED）", status, payload)
    status, payload = call("POST", "/exit-counts",
                           {"serial": "CNT-1003", "group_id": 1, "exit_id": 6,
                            "count": 99, "counted_minute": 3})
    show("累计超组人数（预期 409 COUNT_EXCEEDS_GROUP_SIZE）", status, payload)
    status, payload = call("GET", "/exit-counts")
    show("全部组清点台账汇总", status, payload)

    # ---- 先导组关系与非负放行间隔 ----
    # 双路场景：1->2(4)->6(2) 与 1->4(3)->6(2)
    status, plan = call("POST", "/scenario", {
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
    })
    assert status == 200

    # 组2 以组1 为先导、放行间隔 3：依赖顺序使组1 先安排，组2 最早放行
    # = 先导规划到达分钟 5 + 3 = 8
    status, plan = call("POST", "/group-prerequisites",
                        {"group_id": 2, "leader_group_id": 1,
                         "release_gap": 3})
    show("登记先导关系（组2->组1，间隔3，按依赖顺序安排）", status, plan)
    summarize(plan)
    g2 = next(g for g in plan["groups"] if g["group_id"] == 2)
    print(f"  放行依据： basis={g2['release_basis']['basis_type']} "
          f"先导到达 {g2['release_basis']['leader_arrival_minute']} "
          f"最早放行 {g2['release_basis']['earliest_release_minute']}")

    status, payload = call("GET", "/group-prerequisites")
    show("查询全部先导关系及实时放行依据", status, payload)

    # 再次登记替换旧关系（间隔 3 -> 0），修订 detail.replaced 记录旧关系
    status, plan = call("POST", "/group-prerequisites",
                        {"group_id": 2, "leader_group_id": 1,
                         "release_gap": 0})
    show("替换旧关系（间隔改为0）", status, plan)

    # 拒绝路径：自依赖 / 成环 / 不存在编号 / 负间隔
    status, payload = call("POST", "/group-prerequisites",
                           {"group_id": 2, "leader_group_id": 2,
                            "release_gap": 0})
    show("自依赖（预期 409 SELF_DEPENDENCY）", status, payload)
    status, payload = call("POST", "/group-prerequisites",
                           {"group_id": 1, "leader_group_id": 2,
                            "release_gap": 0})
    show("形成环（预期 409 CYCLE_DETECTED，附环路径）", status, payload)
    status, payload = call("POST", "/group-prerequisites",
                           {"group_id": 2, "leader_group_id": 99,
                            "release_gap": 0})
    show("先导组不存在（预期 422）", status, payload)
    status, payload = call("POST", "/group-prerequisites",
                           {"group_id": 2, "leader_group_id": 1,
                            "release_gap": -1})
    show("负放行间隔（预期 422）", status, payload)

    # 先导无可行路线 -> 后继 blocked（前置未满足，不占通道）；先导恢复后转正
    call("POST", "/group-corrections", [{"group_id": 1, "deadline": 3}])
    status, plan = call("POST", "/group-prerequisites",
                        {"group_id": 2, "leader_group_id": 1,
                         "release_gap": 0})
    show("先导无可行路线（后继预期 blocked / LEADER_FAILED，不占通道）",
         status, plan)
    summarize(plan)
    status, plan = call("POST", "/group-corrections",
                        [{"group_id": 1, "deadline": 30}])
    show("先导恢复可行，后继随重排自动转正", status, plan)
    summarize(plan)

    # 多级依赖：组3 在节点2，链 3->2->1 按依赖顺序安排
    call("POST", "/groups", [
        {"id": 3, "size": 10, "origin": 2, "earliest_departure": 0,
         "priority": 1, "deadline": 30}])
    status, plan = call("POST", "/group-prerequisites",
                        {"group_id": 3, "leader_group_id": 2,
                         "release_gap": 1})
    show("多级关系（组3->组2->组1，按依赖顺序安排）", status, plan)
    summarize(plan)

    # ---- 逐段实际通行核报：已出发组沿现行路线逐段核报实际时刻 ----
    call("POST", "/scenario", {
        "passages": [
            {"id": 1, "src": 1, "dst": 2, "minutes": 4, "capacity": 10},
            {"id": 2, "src": 2, "dst": 6, "minutes": 2, "capacity": 10},
        ],
        "exits": [5, 6], "closures": [],
        "groups": [
            {"id": 1, "size": 10, "origin": 1, "earliest_departure": 0,
             "priority": 10, "deadline": 30},
            {"id": 2, "size": 5, "origin": 1, "earliest_departure": 10,
             "priority": 5, "deadline": 30},
        ],
    })
    call("POST", "/tick", {"minutes": 6})

    def traversal(report_id, gid, seq, edge_id, enter, leave):
        return call("POST", "/traversal-reports",
                    {"report_id": report_id, "group_id": gid, "seq": seq,
                     "edge_id": edge_id, "enter_minute": enter,
                     "exit_minute": leave})

    # 拒绝路径：未出发 / 越序 / 通道不符 / 段不存在
    status, payload = traversal("PY-NA", 2, 0, 1, 0, 4)
    show("未出发组核报（预期 409 NOT_DEPARTED）", status, payload)
    status, payload = traversal("PY-OO", 1, 1, 2, 4, 6)
    show("越序核报 seq=1（预期 409 OUT_OF_ORDER）", status, payload)
    status, payload = traversal("PY-EM", 1, 0, 2, 0, 4)
    show("通道不符（预期 409 EDGE_MISMATCH）", status, payload)

    # 顺序核报两段
    status, payload = traversal("PY-T1", 1, 0, 1, 0, 4)
    show("核报 seq=0 通道1 实际 [0,4)", status, payload)
    status, replay = traversal("PY-T1", 1, 0, 1, 0, 4)
    print(f"\n同报送号同内容重试： HTTP {status}（幂等返回原记录）")
    status, payload = traversal("PY-T2", 1, 1, 2, 4, 6)
    show("核报 seq=1 通道2 实际 [4,6)", status, payload)
    status, payload = traversal("PY-DUP", 1, 0, 1, 0, 4)
    show("同段重复报送（预期 409 DUPLICATE_SEQ）", status, payload)

    status, payload = call("GET", "/traversal-reports/1")
    show("单组计划/实绩并列、未核报段与逐分钟实绩", status, payload)
    for s in payload["steps"]:
        print(f"  seq={s['seq']} 通道{s['edge_id']} "
              f"计划[{s['planned_enter_minute']},{s['planned_exit_minute']}) "
              f"实绩[{s['actual_enter_minute']},{s['actual_exit_minute']}) "
              f"偏离={s['deviates']} 异常={s['anomalies']}")
    status, payload = call("GET", "/traversal-reports")
    show("全部组逐段台账 + 通道逐分钟实绩人数与异常", status, payload)
    print("  异常分钟：", [(m["edge_id"], m["minute"], m["anomalies"])
                        for m in payload["edge_minutes"] if m["anomalies"]])

    # 节点滞留改编：固定前缀保留、旧余程核报失效留痕、新余程重报
    call("POST", "/scenario", {
        "passages": [
            {"id": 1, "src": 1, "dst": 2, "minutes": 4, "capacity": 10},
            {"id": 2, "src": 2, "dst": 6, "minutes": 2, "capacity": 10},
        ],
        "exits": [5, 6], "closures": [],
        "groups": [
            {"id": 1, "size": 10, "origin": 1, "earliest_departure": 0,
             "priority": 10, "deadline": 30}],
    })
    call("POST", "/tick", {"minutes": 4})
    traversal("PY-R1", 1, 0, 1, 0, 2)
    traversal("PY-R2", 1, 1, 2, 2, 4)
    status, plan = call("POST", "/groups/1/delay-report",
                        {"node_id": 2, "recovery_minute": 6})
    show("节点2滞留、第6分钟恢复（旧 seq1 核报将失效留痕）", status, plan)
    status, payload = call("GET", "/traversal-reports/1")
    show("改编后：前缀实绩保留、失效记录在 detached_reports", status, payload)
    print("  有效核报：", [(r["report_id"], r["seq"])
                        for r in payload["reports"]])
    print("  失效留痕：", [(r["report_id"], r["seq"], r["detach_reason"])
                        for r in payload["detached_reports"]])

    # ---- 烟雾暴露时段与累计暴露上限 ----
    call("POST", "/scenario", {
        "passages": [
            {"id": 1, "src": 1, "dst": 2, "minutes": 4, "capacity": 30},
            {"id": 2, "src": 2, "dst": 3, "minutes": 3, "capacity": 30},
            {"id": 3, "src": 3, "dst": 5, "minutes": 2, "capacity": 30},
            {"id": 5, "src": 2, "dst": 6, "minutes": 2, "capacity": 40},
        ],
        "exits": [5, 6], "closures": [],
        "groups": [
            {"id": 1, "size": 30, "origin": 1, "earliest_departure": 0,
             "priority": 10, "deadline": 30},
            {"id": 3, "size": 10, "origin": 2, "earliest_departure": 0,
             "priority": 20, "deadline": 20}],
    })
    status, plan = call("POST", "/smoke-limits",
                        {"group_id": 3, "smoke_limit": 3})
    show("为组3登记累计暴露上限3", status, plan)
    status, plan = call("POST", "/smoke-windows",
                        {"edge_id": 5, "start_minute": 0,
                         "end_minute": 2, "smoke_value": 5})
    show("通道5 [0,2) 每分钟暴露5：组3等待烟雾消散，第2分钟进入", status, plan)
    g3 = next(g for g in plan["groups"] if g["group_id"] == 3)
    print("  组3 出发/到达：", g3["departure_minute"], g3["arrival_minute"],
          " 上限/前缀已用/余程预计：", g3["smoke_exposure_limit"],
          g3["smoke_exposure_used"], g3["smoke_exposure_remaining"])

    show("烟雾时段列表", *call("GET", "/smoke-windows"))
    show("各组暴露上限列表", *call("GET", "/smoke-limits"))
    status, payload = call("POST", "/smoke-windows",
                           {"edge_id": 5, "start_minute": 1,
                            "end_minute": 3, "smoke_value": 1})
    show("同通道时段重叠（预期 409 SMOKE_WINDOW_OVERLAP）", status, payload)

    call("POST", "/tick", {"minutes": 1})  # 组3 fixed，通道5 剩余占用分钟1
    status, payload = call("POST", "/smoke-windows",
                           {"edge_id": 5, "start_minute": 1,
                            "end_minute": 5, "smoke_value": 4})
    show("新窗口使已出发组3预计暴露4>上限3（预期 409 整次拒绝）",
         status, payload)
    status, plan = call("POST", "/smoke-limits",
                        {"group_id": 3, "smoke_limit": 99})
    show("已出发组再登记上限（预期 409 SMOKE_LIMIT_TARGET_DEPARTED）",
         status, plan)

    # ---- 修订历史：追溯每次编排变更 ----
    status, payload = call("GET", "/revisions")
    show("修订列表（每次成功变更一条，拒绝不产生）", status, payload)

    latest = payload["revisions"][-1]["rev"]
    status, detail = call("GET", f"/revisions/{latest}")
    show(f"最新修订 rev={latest} 的快照与逐组差异", status, detail)
    for ch in detail["changes"]:
        if ch["change"] == "changed":
            print(f"  组 {ch['group_id']} 变化字段 {ch['changed_fields']}: "
                  f"{ch['before']['status']} -> {ch['after']['status']}")
        else:
            g = ch["after"] or ch["before"]
            print(f"  组 {ch['group_id']} {ch['change']} "
                  f"(status={g['status']})")

    status, payload = call("GET", "/revisions/999999")
    show("未知修订号（预期 404）", status, payload)
