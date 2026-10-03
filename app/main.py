"""场馆疏散通道编排服务 —— FastAPI 应用。

并发安全
========
所有会改变编排结果的请求（初始化场景 / 新增组 / 纠正组 / 新增封闭 /
登记容量管制 / 现场节点滞留报告 / 推进分钟）由同一把 asyncio 锁串行化，
且每个变更在单个 SQLite 事务内整体提交：
* 重排未出发组时，先删除这些组的全部旧路线再整体写入新路线，
  不会出现只重排了一部分的中间状态；
* 容量校验基于"已固定组占用 + 本轮已排入组占用"，锁内逐组累加，
  并发变更不可能超用容量；
* 新增封闭若与任一已出发（fixed）组剩余路线相交，直接返回 409，
  数据原样保留、不触发任何重排；
* 登记容量管制若与同通道已有管制重叠，或新上限使已出发组剩余路线在
  任一分钟超载，同样整次拒绝（409），不改动任何状态；
* 纠正人群组时任一项无效（未知组/最终字段不合法/起点非场馆节点），
  或目标组在受理前已因时间推进而出发，整批拒绝（422/409），
  状态与修订历史不变；
* 现场报告节点滞留仅当该组按现行计划已到达该节点、尚未进入下一通道且
  未抵达出口时受理；报告成功后保留已走前缀及时间，释放该组从当前分钟
  起的旧预约，从该节点、恢复分钟起重找截止前余程；无余程则标记待救援，
  不恢复旧预约。其他未出发组随之整体重排，其他已出发组路线与占用不变。
* 待救援组可整组交接给登记在册的人工救援队：仅当所见分钟未变、组仍在
  该节点待救援、人数不超载员量、队伍空闲且同组同队无进行中任务时派遣
  受理；受理后组置救援中（保留前缀、不产生未来通道占用、不自动重排、
  滞留报告对其拒绝）。结案须给出既有出口与不早于派遣、不晚于当前分钟的
  实际送达分钟：核销后队伍空闲、组置已救援，实际出口与时刻单列，
  不当作规划到达。无效编号、状态/时序冲突与并发抢占均整次拒绝。
* 现场可为尚未出发的组登记先导组及非负放行间隔（每组至多一项，再次
  登记替换旧关系）：先导组须存在，禁止自依赖和成环，目标组已出发、
  编号或间隔无效、形成环时整次拒绝。后继组的可出发分钟不早于自身最早
  出发分钟及先导组规划到达分钟加间隔；先导已人工救援送达时改用实际
  送达分钟，待救援、救援中或无可行路线时后继标记前置未满足（blocked，
  不占通道）。多级关系按依赖顺序安排，每轮在可安排组中沿用现有优先级、
  最早出发分钟、组编号与路线并列规则。关系变更、时间推进、现场滞留及
  救援结案后立即重排未出发组，已出发组路线及既有放行决定不追溯；成功
  登记与编排修订在同一事务原子落库。
* 现场在出口按组清点实际撤离人数：每条清点凭非空唯一流水号幂等登记
  （同内容重试返回原记录、异内容整次拒绝），仅已按固定路线抵达该出口
  或已由人工救援实际送达该出口的组可登记，清点分钟不早于相应到达/送达
  分钟、不晚于当前分钟，同组累计人数不得超过组人数；可凭原流水号、
  记录当前版本与非空原因把一条清点更正为非负人数（保留原值与更正历史，
  版本失配、对同一版本重复更正或更正后累计超额均整次拒绝）。清点与
  更正不改编排、不产生修订；场景重置清空全部清点记录。
* 现场对已出发组逐段核报实际通行：每次提交唯一报送号、组号、现行路线段
  序号与通道号、实际进入/离开分钟；分钟非负、进入早于离开且离开不晚于
  当前分钟，同组沿路线顺序核报、后段进入不早于前段实际离开。段不存在、
  通道不符、越序、同段重复或时序不符一律拒绝且不留记录；同报送号同内容
  重试返回原记录、异内容拒绝。fixed 组核报全部现行段，待救援/救援中/
  已人工送达组只核报已有通道前缀。节点滞留改编时已核报固定前缀保留可
  查，被释放余程段的核报标记失效留痕、新余程重新核报。查询并列计划与
  实绩时刻并按实绩汇总通道逐分钟人数，标出超当时有效容量或命中封闭的
  异常；偏离或超额只留痕，不反向修改编排、占用、封闭/管制与修订；场景
  重置清空本场全部实绩。
* 现场可按通道登记烟雾暴露时段（通道号、起止分钟、每分钟非负暴露值；
  时段自当前分钟起、同通道不重叠），并为尚未出发的组登记非负累计暴露
  上限（每组一项、再登记替换，未设上限的组不受约束）。通道占用的每一
  分钟计入该通道该分钟暴露值，未登记为零，节点等待不计；编排须在暴露
  不超限的路线中沿用既有封闭、容量、截止、先导放行与路线择优规则，可在
  节点等待烟雾消散后通行。暴露时段若使已出发且设限组按固定路线的预计
  累计暴露超限，或暴露上限登记时目标组已出发，均整次拒绝、不改编排与
  修订。节点滞留改编保留已走前缀按原计划时刻累计的暴露，只用剩余额度
  寻找余程；无可行余程沿用待救援流程。编排查询展示各组上限、前缀已用
  及余程预计暴露；场景重置清空本场全部烟雾暴露设置。

修订历史
========
上述变更成功提交时，在同一事务内追加一条修订记录：全局递增修订号、
触发操作、提交后的当前分钟、提交前后完整编排快照（纠正类修订另在
detail 中逐项记录纠正前后的字段值）。被拒绝的请求（409/422）不产生
修订。修订记录不随场景重置或服务重启丢失，历史快照按落库内容
原样返回，不按当前场景重算。
"""
from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager

import aiosqlite
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, ValidationError

from . import db
from .planner import edge_traversal_exposure, effective_capacity, plan_group
from .schemas import (CapacityControlIn, CapacityControlListOut,
                      CapacityControlOut, ClosureIn, ConflictOut,
                      DelayReportAliasIn, DelayReportIn,
                      ExitCountArrivalOut, ExitCountCorrectionIn,
                      ExitCountCorrectionOut, ExitCountGroupOut,
                      ExitCountIn, ExitCountListOut, ExitCountRecordOut,
                      GroupChangeOut, GroupCorrectionIn, GroupIn,
                      GroupPlanOut, GroupTallyOut,
                      PlanResponse, PrerequisiteBasisOut, PrerequisiteIn,
                      PrerequisiteListOut, PrerequisiteOut,
                      RescueCompletionIn, RescueDispatchIn,
                      RescueTaskListOut, RescueTaskOut, RescueTeamIn,
                      RescueTeamListOut, RescueTeamOut, RevisionDetailOut,
                      RevisionListOut, RevisionSummaryOut, ScenarioIn,
                      SmokeLimitIn, SmokeLimitListOut, SmokeLimitOut,
                      SmokeWindowIn, SmokeWindowListOut, SmokeWindowOut,
                      StepOut, TraversalActualStepOut, TraversalEdgeMinuteOut,
                      TraversalGroupDetailOut, TraversalGroupOut,
                      TraversalReportIn, TraversalReportListOut,
                      TraversalRecordOut)
from .seed import SEED_SCENARIO

DB_PATH = os.getenv("EVAC_DB_PATH", "/data/evacuation.db")
SEED_ON_START = os.getenv("EVAC_SEED", "true").lower() in ("1", "true", "yes")


@asynccontextmanager
async def lifespan(app: FastAPI):
    directory = os.path.dirname(DB_PATH)
    if directory:
        os.makedirs(directory, exist_ok=True)
    conn = await aiosqlite.connect(DB_PATH, isolation_level=None)
    await conn.execute("PRAGMA journal_mode=WAL")
    await conn.execute("PRAGMA foreign_keys=ON")
    await db.init_db(conn)
    app.state.db = conn
    app.state.lock = asyncio.Lock()

    async with app.state.lock:
        async with conn.execute("SELECT COUNT(*) FROM edges") as cur:
            edge_count = (await cur.fetchone())[0]
        if SEED_ON_START and edge_count == 0:
            await _load_scenario(
                conn, ScenarioIn.model_validate(SEED_SCENARIO))

    yield
    await conn.close()


app = FastAPI(
    title="场馆疏散通道编排服务",
    version="1.7.0",
    description="按优先级逐组编排截止前最早到达的疏散路线，"
                "支持封闭时段、容量管制时段、每分钟容量约束、增量重排、"
                "先导组放行间隔（多级依赖按依赖顺序安排）、"
                "按通道登记烟雾暴露时段与按组登记非负累计暴露上限"
                "（可等待烟雾消散后通行、超限路线不可安排）、"
                "现场节点滞留报告与人工救援队交接核销、"
                "出口按组清点实际撤离人数（幂等登记、版本化更正），"
                "以及已出发组逐段实际通行核报（顺序核报、幂等留痕、"
                "封闭/超容量异常标出，不反向修改编排）。",
    lifespan=lifespan,
)


# --------------------------------------------------------------------------
# 内部编排逻辑（均在持锁状态下调用）
# --------------------------------------------------------------------------

async def _load_scenario(conn: aiosqlite.Connection,
                         scenario: ScenarioIn) -> PlanResponse:
    """全量写入一个场景，随后立即编排（编排与修订记录整体为一个事务）。"""
    edge_ids = [p.id for p in scenario.passages]
    if len(edge_ids) != len(set(edge_ids)):
        raise HTTPException(422, "通道编号重复")
    group_ids = [g.id for g in scenario.groups]
    if len(group_ids) != len(set(group_ids)):
        raise HTTPException(422, "人群组编号重复")
    if len(scenario.exits) != len(set(scenario.exits)):
        raise HTTPException(422, "出口节点编号重复")

    nodes = {n for p in scenario.passages for n in (p.src, p.dst)}
    nodes.update(scenario.exits)
    for g in scenario.groups:
        nodes.add(g.origin)
    for c in scenario.closures:
        if c.edge_id not in edge_ids:
            raise HTTPException(422, f"封闭时段引用了不存在的通道 {c.edge_id}")
    for g in scenario.groups:
        if g.origin not in nodes:
            raise HTTPException(422, f"组 {g.id} 的起点 {g.origin} 不在场馆节点中")

    detail = {"passages": len(scenario.passages),
              "exits": list(scenario.exits),
              "closures": len(scenario.closures),
              "groups": len(scenario.groups)}
    before = await _build_plan_response(conn)
    async with db.transaction(conn):
        await db.reset_db(conn)
        for p in scenario.passages:
            await conn.execute(
                "INSERT INTO edges(id, src, dst, minutes, capacity) VALUES(?,?,?,?,?)",
                (p.id, p.src, p.dst, p.minutes, p.capacity))
        for node in scenario.exits:
            await conn.execute(
                "INSERT INTO exits(node_id) VALUES(?)", (node,))
        for c in scenario.closures:
            await conn.execute(
                "INSERT INTO closures(edge_id, start_minute, end_minute) VALUES(?,?,?)",
                (c.edge_id, c.start_minute, c.end_minute))
        for g in scenario.groups:
            await conn.execute(
                """INSERT INTO groups
                   (id, size, origin, earliest_departure, priority, deadline, status)
                   VALUES(?,?,?,?,?,?,'pending')""",
                (g.id, g.size, g.origin, g.earliest_departure,
                 g.priority, g.deadline))
        await _replan_pending(conn, in_transaction=True)
        after = await _build_plan_response(conn)
        await db.insert_revision(conn, "scenario", detail,
                                 _plan_dump(before), _plan_dump(after))
    return after


# 先导前置未满足（blocked 状态，不占通道）的原因码与说明。后继组在先导
# 组没有可用到达锚点（规划到达或人工实际送达）时标记前置未满足，并随先导
# 状态/编排变化在后续重排中转正。
PREREQUISITE_NOT_SATISFIED = "PREREQUISITE_NOT_SATISFIED"

_BLOCKED_REASONS = {
    "LEADER_PENDING":
        "先导组 {leader} 尚未安排出截止前可行路线（{leader_state}），"
        "放行前置未满足，暂不安排、不占用通道",
    "LEADER_AWAITING_RESCUE":
        "先导组 {leader} 已滞留待救援，尚无规划到达分钟，放行前置未满足，"
        "暂不安排、不占用通道",
    "LEADER_RESCUING":
        "先导组 {leader} 正在人工救援中，尚无实际送达分钟，放行前置未满足，"
        "暂不安排、不占用通道",
    "LEADER_FAILED":
        "先导组 {leader} 无法在截止前安排（{leader_reason}），"
        "没有可用的规划到达分钟，放行前置未满足，暂不安排、不占用通道",
    "LEADER_BLOCKED":
        "先导组 {leader} 的放行前置未满足，依赖沿多级关系向上传递，"
        "本组暂不安排、不占用通道",
}


def _blocked_result(group_id: int, reason: str, **fmt) -> dict:
    """构造"前置未满足"的编排结果（blocked，无路线、无占用）。"""
    return {"group_id": group_id, "status": "blocked",
            "reason": reason, "detail": _BLOCKED_REASONS[reason].format(**fmt)}


def _leader_anchor(state: dict | None):
    """先导组当前能否给后继提供放行锚点。

    返回 (kind, arrival_minute)：
    * ("ok", minute)  —— scheduled/fixed 的规划到达，或 rescued 的实际送达；
    * ("failed", None)——先导编排失败（截止前无可行路线）；
    * ("block", reason, None) —— 前置未满足（待安排/待救援/救援中/自身前置
      未满足），reason 为对应的 LEADER_* 原因码。
    """
    if state is None:
        return "block", "LEADER_PENDING", None
    status = state["status"]
    if status in ("scheduled", "fixed") and state["arrival_minute"] is not None:
        return "ok", state["arrival_minute"]
    if status == "rescued" and state["delivery_minute"] is not None:
        return "ok", state["delivery_minute"]
    if status == "failed":
        return "failed", None
    if status == "awaiting_rescue":
        return "block", "LEADER_AWAITING_RESCUE", None
    if status == "rescuing":
        return "block", "LEADER_RESCUING", None
    if status == "blocked":
        return "block", "LEADER_BLOCKED", None
    # pending / scheduled 但尚无到达分钟：先导还在本轮待安排
    return "block", "LEADER_PENDING", None


async def _replan_pending(conn: aiosqlite.Connection,
                          in_transaction: bool = False) -> None:
    """重排全部未出发组，整体替换（多级先导关系按依赖顺序安排）。

    每轮在"先导前置已满足"的可安排组中，沿用既有 优先级 DESC、最早出发
    ASC、组编号 ASC、路线并列规则逐组贪婪安排；先导尚无可用到达锚点（未
    安排/待救援/救援中/无可行路线/先导自身前置未满足）的后继标记
    ``blocked``（前置未满足，不写路线、不占通道），并在先导落定后的后续
    轮次转正。已固定（fixed）与已人工送达（rescued）的先导分别提供规划
    到达分钟与实际送达分钟作为放行锚点。

    读阶段在事务外完成（SQLite autocommit 下读到的是最近一致快照），
    写阶段整体置于一个事务中，杜绝部分重排。
    """
    minute_now = await db.get_current_minute(conn)
    world, pending, prerequisites, db_states, smoke_limits = \
        await db.load_world(conn)

    node_set: set[int] = set()
    for e in world.edges.values():
        node_set.add(e.src)
        node_set.add(e.dst)
    node_set.update(world.exits)
    for g in pending:
        if g["origin"] not in node_set:
            raise HTTPException(422, f"组 {g['id']} 的起点 {g['origin']} 不连通")

    # 本轮待安排组：id -> 属性；states 持续记录先导/后继的落定结果
    todo = {g["id"]: g for g in pending}
    states: dict[int, dict] = {}
    # 已不会在本轮变动的组（fixed/awaiting_rescue/rescuing/rescued）预置锚点
    for gid, st in db_states.items():
        if gid not in todo and st["status"] in (
                "fixed", "awaiting_rescue", "rescuing", "rescued"):
            states[gid] = st

    results: dict[int, dict] = {}
    while todo:
        progressed = False

        # 1) 先导已在前面轮次落定且未提供可用锚点的后继，标记前置未满足。
        #    立即写入 states，使多级依赖的阻塞在本轮沿链传播到不动点。
        changed = True
        while changed:
            changed = False
            for gid in sorted(todo):
                pre = prerequisites.get(gid)
                if pre is None:
                    continue
                leader = pre["leader_group_id"]
                if leader in todo:
                    continue  # 先导尚待本轮安排，等其落定后再判
                anchor = _leader_anchor(states.get(leader))
                if anchor[0] == "ok":
                    continue
                if anchor[0] == "failed":
                    fmt = {"leader": leader,
                           "leader_reason": states.get(leader, {}).get(
                               "fail_reason", "无可行路线")}
                    reason = "LEADER_FAILED"
                else:
                    fmt, reason = {"leader": leader}, anchor[1]
                results[gid] = _blocked_result(gid, reason, **fmt)
                states[gid] = {
                    "status": "blocked", "arrival_minute": None,
                    "delivery_minute": None,
                    "fail_reason": PREREQUISITE_NOT_SATISFIED}
                del todo[gid]
                changed = progressed = True

        if not todo:
            break

        # 2) 收集本轮可安排组：无先导，或先导已有可用到达锚点
        ready = []
        for g in todo.values():
            pre = prerequisites.get(g["id"])
            if pre is None:
                ready.append((g, None, 0))
                continue
            anchor = _leader_anchor(states.get(pre["leader_group_id"]))
            if anchor[0] == "ok":
                ready.append((g, anchor[1], pre["release_gap"]))

        if not ready:
            # 防御：登记时已拒绝成环/自依赖，DAG 下不应到达这里；
            # 剩余组全部按前置未满足落定，避免死循环
            for gid in sorted(todo):
                results[gid] = _blocked_result(gid, "LEADER_PENDING",
                                               leader="?")
                states[gid] = {"status": "blocked", "arrival_minute": None,
                               "delivery_minute": None,
                               "fail_reason": PREREQUISITE_NOT_SATISFIED}
            break

        # 可安排组沿用现有优先级、最早出发分钟、组编号并列规则
        ready.sort(key=lambda item: (-item[0]["priority"],
                                     item[0]["earliest_departure"],
                                     item[0]["id"]))
        for g, leader_arrival, gap in ready:
            earliest = g["earliest_departure"]
            if leader_arrival is not None:
                earliest = max(earliest, leader_arrival + gap)
            result = plan_group(
                group_id=g["id"], size=g["size"], origin=g["origin"],
                earliest_departure=earliest,
                deadline=g["deadline"], current_minute=minute_now, world=world,
                smoke_limit=smoke_limits.get(g["id"]))
            results[g["id"]] = result
            if result["status"] == "scheduled":
                states[g["id"]] = {
                    "status": "scheduled",
                    "arrival_minute": result["arrival_minute"],
                    "delivery_minute": None}
                for (eid, minute), add in result["load_delta"].items():
                    key = (eid, minute)
                    world.fixed_load[key] = world.fixed_load.get(key, 0) + add
            else:
                states[g["id"]] = {"status": "failed",
                                   "arrival_minute": None,
                                   "delivery_minute": None,
                                   "fail_reason": result["reason"]}
            del todo[g["id"]]
            progressed = True

        if not progressed:  # 防御：保证循环必然终止
            for gid in sorted(todo):
                results[gid] = _blocked_result(gid, "LEADER_PENDING",
                                               leader="?")
            break

    # 输出顺序沿用 优先级、最早出发、组编号（与历史 /plan 口径一致）
    ordered = sorted(results.values(),
                     key=lambda r: next(
                         (-g["priority"], g["earliest_departure"], g["id"])
                         for g in pending if g["id"] == r["group_id"]))

    if in_transaction:
        await db.persist_replan(conn, ordered, minute_now)
    else:
        async with db.transaction(conn):
            await db.persist_replan(conn, ordered, minute_now)


async def _closure_conflicts(conn: aiosqlite.Connection, edge_id: int,
                             a: int, b: int):
    """检查 [a,b) 是否触及已出发组在该通道上的剩余路线。"""
    minute_now, fixed_routes = await db.load_fixed_routes(conn)
    conflicts = []
    for gid, eid, enter_m, exit_m in fixed_routes:
        if eid != edge_id:
            continue
        remain_start = max(enter_m, minute_now)
        if remain_start >= exit_m:
            continue  # 该段已完全在过去，无剩余路线
        # 剩余区间 [remain_start, exit_m) 与封闭 [a,b) 相交
        if remain_start < b and a < exit_m:
            conflicts.append(ConflictOut(
                group_id=gid, edge_id=eid, enter_minute=enter_m,
                exit_minute=exit_m, closure=[a, b]))
    return conflicts


async def _capacity_control_conflicts(conn: aiosqlite.Connection,
                                      edge_id: int, a: int, b: int,
                                      cap: int) -> list[dict]:
    """检查新管制上限是否使已出发组在该通道的剩余路线任一分钟超载。

    已出发组的占用永久固定、不参与重排，故新上限必须能容纳其剩余路线
    的逐分钟占用。返回逐分钟冲突列表：冲突分钟、该分钟占用量、新有效
    上限与相关组（未出发组会随管制重排，不在检查之列）。
    """
    minute_now = await db.get_current_minute(conn)
    async with conn.execute(
            "SELECT capacity FROM edges WHERE id=?", (edge_id,)) as cur:
        base_cap = int((await cur.fetchone())[0])
    effective = min(base_cap, cap)
    async with conn.execute(
            """SELECT g.id, g.size, rs.enter_minute, rs.exit_minute
               FROM route_steps rs JOIN groups g ON g.id = rs.group_id
               WHERE g.status='fixed' AND rs.edge_id=?""",
            (edge_id,)) as cur:
        rows = await cur.fetchall()
    per_minute: dict[int, dict] = {}
    for gid, size, enter_m, exit_m in rows:
        # 剩余路线 [max(enter, 当前分钟), exit) 与管制 [a, b) 的相交部分
        for m in range(max(enter_m, minute_now, a), min(exit_m, b)):
            slot = per_minute.setdefault(m, {"used": 0, "groups": []})
            slot["used"] += size
            slot["groups"].append({"group_id": gid, "size": size})
    return [
        {"edge_id": edge_id, "minute": m, "used": slot["used"],
         "effective_capacity": effective, "groups": slot["groups"]}
        for m, slot in sorted(per_minute.items())
        if slot["used"] > effective
    ]


# --------------------------------------------------------------------------
# 现场节点滞留报告
# --------------------------------------------------------------------------

# 余程无解时，面向待救援标记的原因说明（复用既有失败原因码）
_RESCUE_DETAIL = {
    "DEADLINE_PASSED":
        "组自节点 {node} 第 {recovery} 分钟恢复时已晚于截止分钟 {deadline}，"
        "截止前没有可安排的余程，标记待救援",
    "BLOCKED_BY_CLOSURE":
        "组自节点 {node} 第 {recovery} 分钟恢复：忽略封闭后可在截止前到达，"
        "现有封闭阻断了自该节点出发的全部余程，标记待救援",
    "INSUFFICIENT_CAPACITY":
        "组自节点 {node} 第 {recovery} 分钟恢复：忽略容量限制后可在截止前"
        "到达，现有封闭/限流与其他已出发组的固定占用阻断了截止前全部余程，"
        "标记待救援",
    "UNREACHABLE_EXIT":
        "滞留节点 {node} 与任何出口之间不存在连通路径，标记待救援",
    "EXPOSURE_LIMIT_EXCEEDED":
        "组自节点 {node} 第 {recovery} 分钟恢复：忽略烟雾累计暴露上限后"
        "可在截止前到达，现有烟雾暴露时段使截止前全部余程（含等待烟雾"
        "消散）的暴露超过剩余额度（上限 {limit}，已走前缀已用 {used}），"
        "标记待救援",
}


async def _delay_report_rejection(conn: aiosqlite.Connection,
                                  group_id: int, node_id: int,
                                  recovery_minute: int):
    """受理前校验。返回 (HTTP 状态码, 错误体)；接受时返回 None。

    仅当该组按现行计划已到达所报节点、尚未进入下一通道且未抵达出口时
    接受：组未出发、组不在节点（通道中/已抵出口）或节点与报告不符均拒绝。
    """
    minute_now = await db.get_current_minute(conn)
    if recovery_minute < minute_now:
        raise HTTPException(
            422, f"恢复分钟 {recovery_minute} 早于当前分钟 {minute_now}")

    async with conn.execute(
            """SELECT id, status, departure_minute FROM groups
               WHERE id=?""", (group_id,)) as cur:
        row = await cur.fetchone()
    if row is None:
        raise HTTPException(422, f"人群组 {group_id} 不存在")
    gid, status, dep_minute = row

    base = {"group_id": group_id, "status": status,
            "node_id": node_id, "current_minute": minute_now,
            "recovery_minute": recovery_minute}

    # 已交接给人工救援队（救援中）或已由人工结案送达（已救援）的组脱离
    # 规划通道：既有节点滞留报告接口对其一律拒绝，不改编排
    if status in ("rescuing", "rescued"):
        open_task = await db.load_open_rescue_task_for_group(conn, group_id)
        if status == "rescuing":
            return 409, {
                "error": "DELAY_REPORT_NOT_ACCEPTABLE",
                "message": "该组已交接人工救援队、正在救援中，节点滞留报告"
                           "不再受理，整次拒绝",
                "conflicts": [{**base, "reason": "GROUP_RESCUING",
                               "rescue_task_id": open_task["task_id"],
                               "rescue_team_id": open_task["team_id"]}]}
        return 409, {
            "error": "DELAY_REPORT_NOT_ACCEPTABLE",
            "message": "该组已由人工救援队送达核销，不再接受节点滞留报告，"
                       "整次拒绝",
            "conflicts": [{**base, "reason": "GROUP_RESCUED"}]}

    if status not in ("fixed", "awaiting_rescue"):
        # scheduled / pending / failed：组未出发，不受理且不改编排
        return 409, {
            "error": "DELAY_REPORT_NOT_ACCEPTABLE",
            "message": "该组尚未出发，现场滞留报告不受理，整次拒绝",
            "conflicts": [{**base, "reason": "NOT_DEPARTED",
                           "departure_minute": dep_minute}]}

    async with conn.execute(
            """SELECT seq, edge_id, from_node, to_node, enter_minute,
                      exit_minute
               FROM route_steps WHERE group_id=? ORDER BY seq""",
            (group_id,)) as cur:
        steps = await cur.fetchall()

    # 待救援组此前已在滞留节点：未排入任何后续通道，时机恒成立，只需节点
    # 与其报告时的滞留节点一致（重复报告恢复分钟须不早于当前分钟）
    if status == "awaiting_rescue":
        report = await db.load_delay_report(conn, group_id)
        if report is None:  # 防御：状态与报告表不一致不应出现
            return 409, {
                "error": "DELAY_REPORT_NOT_ACCEPTABLE",
                "message": "该组当前没有可复核的滞留位置，整次拒绝",
                "conflicts": [{**base, "reason": "POSITION_UNKNOWN"}]}
        at_node = report["node_id"]
        if node_id != at_node:
            return 409, {
                "error": "DELAY_REPORT_NOT_ACCEPTABLE",
                "message": "报告节点与该组待救援位置不符，整次拒绝",
                "conflicts": [{**base, "reason": "NODE_MISMATCH",
                               "expected_node_id": at_node}]}
        return None

    # status == 'fixed'：按现行计划逐段判断当前位置
    if not steps:
        # 零步路线只可能在出口处原地"到达"
        async with conn.execute("SELECT origin FROM groups WHERE id=?",
                                (group_id,)) as cur:
            origin = (await cur.fetchone())[0]
        return 409, {
            "error": "DELAY_REPORT_NOT_ACCEPTABLE",
            "message": "该组已抵达出口，不再接受节点滞留报告，整次拒绝",
            "conflicts": [{**base, "reason": "ALREADY_AT_EXIT",
                           "expected_node_id": origin}]}

    # k = 已走完（到达分钟 <= 当前分钟）的连续前缀段数；
    # 第 k 段（若存在）为"下一通道"
    k = 0
    for s in steps:
        if s[5] <= minute_now:  # exit_minute
            k += 1
        else:
            break

    if k == len(steps):
        return 409, {
            "error": "DELAY_REPORT_NOT_ACCEPTABLE",
            "message": "该组已抵达出口，不再接受节点滞留报告，整次拒绝",
            "conflicts": [{**base, "reason": "ALREADY_AT_EXIT",
                           "expected_node_id": steps[-1][3]}]}

    next_step = steps[k]
    # 进入分钟 == 当前分钟时该组仍可在节点被报告（与 tick 时"出发分钟 ==
    # 当前分钟仍属未出发"的口径一致）；进入分钟 < 当前分钟则已在通道中
    if next_step[4] < minute_now:  # enter_minute
        return 409, {
            "error": "DELAY_REPORT_NOT_ACCEPTABLE",
            "message": "该组已进入下一通道、不在节点上，报告时机不符，"
                       "整次拒绝",
            "conflicts": [{**base, "reason": "GROUP_IN_TRANSIT",
                           "edge_id": next_step[1],
                           "enter_minute": next_step[4],
                           "exit_minute": next_step[5]}]}

    at_node = next_step[2]  # 下一通道的起点即当前所在节点
    if node_id != at_node:
        return 409, {
            "error": "DELAY_REPORT_NOT_ACCEPTABLE",
            "message": "报告节点与该组按现行计划所在节点不符，整次拒绝",
            "conflicts": [{**base, "reason": "NODE_MISMATCH",
                           "expected_node_id": at_node}]}
    return None


async def _apply_delay_report(conn: aiosqlite.Connection, *,
                              group_id: int, node_id: int,
                              recovery_minute: int) -> PlanResponse:
    """受理并应用一次现场滞留报告（调用方已持锁）。

    与未出发组整体重排、修订记录处于同一事务：
    * 保留已走前缀（段时间不变），删除并释放该组从当前分钟起的旧预约；
    * 以所报节点为起点、恢复分钟为最早出发，按现行封闭、限流与其他已出发
      组的固定占用重找截止前余程（沿用最早到达/出口编号/通道序列择优）；
    * 有解则组保持 fixed，已走前缀 + 新余程一并作为固定占用；无解则置
      awaiting_rescue（不恢复旧预约）；
    * 其他未出发组随后整体重排；其他已出发组路线与占用不变。
    """
    minute_now = await db.get_current_minute(conn)
    rejection = await _delay_report_rejection(
        conn, group_id, node_id, recovery_minute)
    if rejection is not None:
        code, body = rejection
        return JSONResponse(status_code=code, content=body)

    async with conn.execute(
            """SELECT size, deadline, status, departure_minute
               FROM groups WHERE id=?""", (group_id,)) as cur:
        size, deadline, status, old_dep = await cur.fetchone()
    # 待救援组重复报告时，前缀步数沿用首次报告的记录
    if status == "awaiting_rescue":
        report = await db.load_delay_report(conn, group_id)
        k = report["prefix_step_count"]
    else:
        async with conn.execute(
                """SELECT COUNT(*) FROM route_steps
                   WHERE group_id=? AND exit_minute<=?""",
                (group_id, minute_now)) as cur:
            k = (await cur.fetchone())[0]

    before = await _build_plan_response(conn)
    smoke_map = await _load_smoke_map(conn)
    smoke_limits = await db.load_smoke_limits(conn)
    smoke_limit = smoke_limits.get(group_id)
    # 已走前缀按原计划时刻累计的烟雾暴露（余程段即将被删除，先核算保留）
    prefix_steps = []
    async with conn.execute(
            """SELECT edge_id, enter_minute, exit_minute FROM route_steps
               WHERE group_id=? AND seq<? ORDER BY seq""",
            (group_id, k)) as cur:
        prefix_steps = await cur.fetchall()
    exposure_used = _steps_exposure(smoke_map, prefix_steps)
    async with db.transaction(conn):
        # 1) 释放旧预约：删除当前分钟起的余程段，清除未来计划字段；
        #    组先置 fixed，使稍后 load_world 不把它计入待重排集合
        await conn.execute(
            "DELETE FROM route_steps WHERE group_id=? AND seq>=?",
            (group_id, k))
        # 被释放余程段上已核报的逐段实绩不再对应现行路线：标记失效留痕
        # （仍可查询、不再参与并列与逐分钟汇总）；已走前缀核报保持有效，
        # 新余程可重新对同一 seq 核报
        await db.detach_traversal_suffix(
            conn, group_id=group_id, seq_from=k, minute=minute_now)
        await conn.execute(
            """UPDATE groups SET status='fixed', arrival_minute=NULL,
               exit_id=NULL, fail_reason=NULL, fail_detail=NULL
               WHERE id=?""", (group_id,))

        # 2) 以滞留节点为起点、恢复分钟为最早出发重找余程。此时 world 的
        #    固定占用含其他已出发组（本组旧余程已删除，不会重复计数）
        world, _pending, _pre, _states, _limits = await db.load_world(conn)
        if recovery_minute > deadline:
            result = {
                "group_id": group_id,
                "status": "failed",
                "reason": "DEADLINE_PASSED",
                "detail": "deadline_passed",
            }
        else:
            result = plan_group(
                group_id=group_id, size=size, origin=node_id,
                earliest_departure=recovery_minute, deadline=deadline,
                current_minute=minute_now, world=world,
                smoke_limit=smoke_limit, exposure_used=exposure_used)

        if result["status"] == "scheduled":
            arrival = result["arrival_minute"]
            exit_id = result["exit_id"]
            await conn.execute(
                """UPDATE groups SET status='fixed', departure_minute=?,
                   arrival_minute=?, exit_id=?, fail_reason=NULL,
                   fail_detail=NULL WHERE id=?""",
                (old_dep, arrival, exit_id, group_id))
            for offset, step in enumerate(result["steps"]):
                await conn.execute(
                    """INSERT INTO route_steps
                       (group_id, seq, edge_id, from_node, to_node,
                        enter_minute, exit_minute)
                       VALUES (?,?,?,?,?,?,?)""",
                    (group_id, k + offset, step["edge_id"],
                     step["from_node"], step["to_node"],
                     step["enter_minute"], step["exit_minute"]))
            outcome = "resumed"
        else:
            format_kwargs = {"node": node_id, "recovery": recovery_minute,
                             "deadline": deadline}
            if result["reason"] == "EXPOSURE_LIMIT_EXCEEDED":
                format_kwargs.update(
                    limit=smoke_limit, used=exposure_used)
            detail = _RESCUE_DETAIL[result["reason"]].format(**format_kwargs)
            await conn.execute(
                """UPDATE groups SET status='awaiting_rescue',
                   arrival_minute=NULL, exit_id=NULL, fail_reason=?,
                   fail_detail=? WHERE id=?""",
                (result["reason"], detail, group_id))
            outcome = "awaiting_rescue"

        await db.upsert_delay_report(
            conn, group_id=group_id, node_id=node_id,
            recovery_minute=recovery_minute, reported_minute=minute_now,
            prefix_step_count=k)

        # 3) 其他未出发组整体重排（本报告组为 fixed/awaiting_rescue，
        #    不在待重排集合；新余程已落库，load_world 会将其计入固定占用）
        await _replan_pending(conn, in_transaction=True)

        after = await _build_plan_response(conn)
        await db.insert_revision(
            conn, "delay-reports",
            {"group_id": group_id, "node_id": node_id,
             "recovery_minute": recovery_minute, "outcome": outcome,
             "prefix_step_count": k},
            _plan_dump(before), _plan_dump(after))
    return after


# --------------------------------------------------------------------------
# 烟雾暴露核算
# --------------------------------------------------------------------------

async def _load_smoke_map(conn: aiosqlite.Connection
                          ) -> dict[int, list[tuple[int, int, int]]]:
    """通道编号 -> 按开始分钟升序、互不重叠的烟雾暴露时段 [(a,b,值), ...]。"""
    smoke_map: dict[int, list[tuple[int, int, int]]] = {}
    for w in await db.load_smoke_windows(conn):
        smoke_map.setdefault(w["edge_id"], []).append(
            (w["start_minute"], w["end_minute"], w["smoke_value"]))
    return smoke_map


def _steps_exposure(smoke_map: dict[int, list[tuple[int, int, int]]],
                    steps, *, lo: int | None = None,
                    hi: int | None = None) -> int:
    """逐段（edge_id, enter, exit 三元组序列）按给定分钟窗口累计烟雾暴露。

    每段计入 [max(enter, lo), min(exit, hi)) 区间内逐分钟暴露值；lo/hi
    缺省为整个通行区间。节点等待不在任何通道段上，自然不计。
    """
    total = 0
    for edge_id, enter_m, exit_m in steps:
        start = enter_m if lo is None else max(enter_m, lo)
        end = exit_m if hi is None else min(exit_m, hi)
        for minute in range(start, end):
            for a, b, value in smoke_map.get(edge_id, ()):
                if a > minute:
                    break
                if minute < b:
                    total += value
                    break
    return total


# --------------------------------------------------------------------------
# 响应组装
# --------------------------------------------------------------------------

async def _build_plan_response(conn: aiosqlite.Connection) -> PlanResponse:
    minute_now = await db.get_current_minute(conn)
    prefix_by_group = await db.load_delay_prefixes(conn)
    rescue_by_group = await db.load_group_rescue_map(conn)
    prerequisites = await db.load_prerequisites(conn)
    smoke_limits = await db.load_smoke_limits(conn)
    smoke_map = await _load_smoke_map(conn)
    async with conn.execute(
            """SELECT id, status, departure_minute, arrival_minute, exit_id,
                      fail_reason, fail_detail
               FROM groups ORDER BY priority DESC,
                    earliest_departure ASC, id ASC""") as cur:
        group_rows = await cur.fetchall()
    group_index = {row[0]: row for row in group_rows}
    out = []
    for gid, status, dep, arr, exit_id, reason, detail in group_rows:
        steps = []
        raw_steps = []
        async with conn.execute(
                """SELECT edge_id, from_node, to_node, enter_minute, exit_minute
                   FROM route_steps WHERE group_id=? ORDER BY seq""", (gid,)) as sc:
            for row in await sc.fetchall():
                raw_steps.append((row[0], row[3], row[4]))
                steps.append(StepOut(
                    edge_id=row[0], from_node=row[1], to_node=row[2],
                    enter_minute=row[3], exit_minute=row[4]))
        rescue = rescue_by_group.get(gid, {})
        pre = prerequisites.get(gid)
        basis = None
        if pre is not None:
            basis = _build_release_basis(
                pre["leader_group_id"], pre["release_gap"], group_index,
                rescue_by_group)
            # blocked 组的前置说明按先导当前状态实时给出（派遣不触发重排，
            # 落库 detail 可能仍是更早的待救援说明）
            if status == "blocked" and not basis.satisfied:
                detail = _BLOCKED_REASONS[basis.reason].format(
                    leader=pre["leader_group_id"],
                    leader_reason=_leader_fail_reason(
                        pre["leader_group_id"], group_index))
        # 烟雾暴露展示：仅登记过非负上限的组给出。已走前缀按原计划时刻
        # （[enter, min(exit, now))）计已用暴露；余程（[max(enter, now),
        # exit)）给出预计暴露，无未来路线段时为 null。
        smoke_used = smoke_remaining = None
        smoke_limit = smoke_limits.get(gid)
        if smoke_limit is not None:
            smoke_used = _steps_exposure(
                smoke_map, raw_steps, hi=minute_now)
            remaining_steps = [
                (eid, enter_m, exit_m)
                for eid, enter_m, exit_m in raw_steps
                if exit_m > minute_now]
            smoke_remaining = (
                _steps_exposure(smoke_map, remaining_steps, lo=minute_now)
                if remaining_steps else None)
        out.append(GroupPlanOut(
            group_id=gid, status=status, exit_id=exit_id,
            departure_minute=dep, arrival_minute=arr,
            route=[s.edge_id for s in steps], steps=steps,
            prefix_step_count=prefix_by_group.get(gid),
            reason=reason, detail=detail,
            rescue_task_id=rescue.get("rescue_task_id"),
            rescue_team_id=rescue.get("rescue_team_id"),
            rescued_exit_id=rescue.get("rescued_exit_id"),
            delivery_minute=rescue.get("delivery_minute"),
            leader_group_id=(pre or {}).get("leader_group_id"),
            release_gap=(pre or {}).get("release_gap"),
            release_basis=basis,
            smoke_exposure_limit=smoke_limit,
            smoke_exposure_used=smoke_used,
            smoke_exposure_remaining=smoke_remaining))
    return PlanResponse(current_minute=minute_now, groups=out)


def _leader_fail_reason(leader_id: int, group_index: dict[int, tuple]) -> str:
    row = group_index.get(leader_id)
    if row is not None and row[5]:  # fail_reason
        return row[5]
    return "无可行路线"


def _build_release_basis(leader_id: int, gap: int,
                         group_index: dict[int, tuple],
                         rescue_by_group: dict[int, dict]
                         ) -> PrerequisiteBasisOut:
    """实时计算一组先导关系当前的放行依据。

    先导为 scheduled/fixed 取规划到达分钟（basis_type=planned_arrival）；
    先导已人工救援送达（rescued）取实际送达分钟（basis_type=
    actual_delivery）；先导待救援、救援中、尚未安排、编排失败或自身前置
    未满足时 satisfied=false 并给出原因码，最早放行分钟为空。
    """
    row = group_index.get(leader_id)
    if row is None:
        return PrerequisiteBasisOut(
            satisfied=False, basis_type=None, leader_status="unknown",
            reason="LEADER_PENDING")
    leader_status, _dep, arrival = row[1], row[2], row[3]
    rescue = rescue_by_group.get(leader_id, {})
    if leader_status in ("scheduled", "fixed") and arrival is not None:
        return PrerequisiteBasisOut(
            satisfied=True, basis_type="planned_arrival",
            leader_status=leader_status, leader_arrival_minute=arrival,
            earliest_release_minute=arrival + gap)
    if leader_status == "rescued":
        delivery = rescue.get("delivery_minute")
        if delivery is not None:
            return PrerequisiteBasisOut(
                satisfied=True, basis_type="actual_delivery",
                leader_status=leader_status,
                leader_arrival_minute=delivery,
                earliest_release_minute=delivery + gap)
        return PrerequisiteBasisOut(
            satisfied=False, basis_type=None, leader_status=leader_status,
            reason="LEADER_RESCUING")
    if leader_status == "awaiting_rescue":
        reason = "LEADER_AWAITING_RESCUE"
    elif leader_status == "rescuing":
        reason = "LEADER_RESCUING"
    elif leader_status == "failed":
        reason = "LEADER_FAILED"
    elif leader_status == "blocked":
        reason = "LEADER_BLOCKED"
    else:  # pending / scheduled 但尚无到达分钟
        reason = "LEADER_PENDING"
    return PrerequisiteBasisOut(
        satisfied=False, basis_type=None, leader_status=leader_status,
        reason=reason)


def _plan_dump(plan: PlanResponse) -> dict:
    """序列化编排快照（JSON 兼容 dict），供修订记录落库。"""
    return plan.model_dump(mode="json")


# diff 时逐字段比较的分组编排字段（route 由 steps 派生，仍单独列出以便阅读）
_GROUP_DIFF_FIELDS = ("status", "exit_id", "departure_minute",
                      "arrival_minute", "route", "steps",
                      "prefix_step_count", "reason", "detail",
                      "rescue_task_id", "rescue_team_id",
                      "rescued_exit_id", "delivery_minute",
                      "leader_group_id", "release_gap", "release_basis",
                      "smoke_exposure_limit", "smoke_exposure_used",
                      "smoke_exposure_remaining")


def _diff_plans(before: PlanResponse,
                after: PlanResponse) -> list[GroupChangeOut]:
    """对比提交前后的完整编排快照，逐组给出新增/移除/字段级差异。

    输入均为修订记录中落库的快照，纯函数计算，不触碰当前场景状态。
    """
    before_by_id = {g.group_id: g for g in before.groups}
    after_by_id = {g.group_id: g for g in after.groups}
    changes: list[GroupChangeOut] = []
    for gid in sorted(set(before_by_id) | set(after_by_id)):
        b = before_by_id.get(gid)
        a = after_by_id.get(gid)
        if b is None:
            changes.append(GroupChangeOut(
                group_id=gid, change="added", before=None, after=a))
        elif a is None:
            changes.append(GroupChangeOut(
                group_id=gid, change="removed", before=b, after=None))
        else:
            fields = [f for f in _GROUP_DIFF_FIELDS
                      if getattr(b, f) != getattr(a, f)]
            if fields:
                changes.append(GroupChangeOut(
                    group_id=gid, change="changed", changed_fields=fields,
                    before=b, after=a))
    return changes


# --------------------------------------------------------------------------
# HTTP API
# --------------------------------------------------------------------------

@app.get("/health", tags=["运维"])
async def health(request: Request):
    return {"status": "ok",
            "current_minute": await db.get_current_minute(request.app.state.db)}


@app.post("/scenario", tags=["场景"], response_model=PlanResponse,
          status_code=200)
async def replace_scenario(scenario: ScenarioIn, request: Request):
    """全量重置场景（通道/出口/封闭/组），随后立即编排并返回结果。

    成功时产生一条 scenario 修订；历史修订保留、编号继续递增。
    """
    async with request.app.state.lock:
        return await _load_scenario(request.app.state.db, scenario)


@app.post("/groups", tags=["变更"], response_model=PlanResponse,
          status_code=201)
async def add_groups(groups: list[GroupIn], request: Request):
    """新增一个或多个人群组；只对全部未出发组重新编排。"""
    conn = request.app.state.db
    async with request.app.state.lock:
        async with conn.execute("SELECT id FROM groups") as cur:
            existing = {row[0] for row in await cur.fetchall()}
        async with conn.execute(
                "SELECT src FROM edges UNION SELECT dst FROM edges") as cur:
            known_nodes = {row[0] for row in await cur.fetchall()}
        async with conn.execute("SELECT node_id FROM exits") as cur:
            known_nodes |= {row[0] for row in await cur.fetchall()}
        for g in groups:
            if g.id in existing:
                raise HTTPException(409, f"人群组编号 {g.id} 已存在")
            if g.origin not in known_nodes:
                raise HTTPException(422,
                    f"组 {g.id} 的起点 {g.origin} 不是场馆节点")
        before = await _build_plan_response(conn)
        async with db.transaction(conn):
            for g in groups:
                await conn.execute(
                    """INSERT INTO groups
                       (id, size, origin, earliest_departure, priority, deadline,
                        status)
                       VALUES(?,?,?,?,?,?,'pending')""",
                    (g.id, g.size, g.origin, g.earliest_departure,
                     g.priority, g.deadline))
            await _replan_pending(conn, in_transaction=True)
            after = await _build_plan_response(conn)
            await db.insert_revision(
                conn, "groups",
                {"groups": [g.model_dump() for g in groups]},
                _plan_dump(before), _plan_dump(after))
        return after


# 可被纠正的组字段（纠正项中未指定的字段沿用原值）
_CORRECTABLE_FIELDS = ("size", "origin", "earliest_departure",
                       "priority", "deadline")


@app.post("/group-corrections", tags=["变更"], response_model=PlanResponse)
async def correct_groups(corrections: list[GroupCorrectionIn],
                         request: Request):
    """批量纠正未出发人群组的属性（非空清单，整批原子生效）。

    每项指定一个已有组编号及至少一个待纠正字段（人数/起点/最早出发分钟/
    优先级/截止分钟），未指定字段沿用原值；同批组编号不得重复。
    仅未出发（含此前编排失败）的组可纠正：若并发时间推进已使目标组
    出发，整批拒绝（409）。每项的最终字段须满足与新增组相同的校验、
    起点须为场馆节点，否则整批拒绝（422）。拒绝时不改动任何状态、
    不产生修订。

    全部有效时在同一事务内应用全部最终值、整体重排未出发组（已出发组
    路线与占用保持固定），记录一条含各项纠正前后字段的修订，并返回
    完整编排。
    """
    conn = request.app.state.db
    async with request.app.state.lock:
        if not corrections:
            raise HTTPException(422, "纠正清单不能为空")
        ids = [c.group_id for c in corrections]
        if len(ids) != len(set(ids)):
            raise HTTPException(422, "同批纠正清单中的组编号不得重复")

        async with conn.execute(
                """SELECT id, size, origin, earliest_departure, priority,
                          deadline, status, departure_minute, arrival_minute
                   FROM groups""") as cur:
            rows = await cur.fetchall()
        existing = {row[0]: row for row in rows}

        async with conn.execute(
                "SELECT src FROM edges UNION SELECT dst FROM edges") as cur:
            known_nodes = {row[0] for row in await cur.fetchall()}
        async with conn.execute("SELECT node_id FROM exits") as cur:
            known_nodes |= {row[0] for row in await cur.fetchall()}

        # 存在性与"已出发"检查：并发时间推进使目标组在受理前出发时，
        # 这里读到的即是 fixed；已被现场报告置为待救援、已交接人工救援
        # （rescuing）或已由人工送达（rescued）的组同样不可纠正，均整批拒绝
        departed = []
        for c in corrections:
            row = existing.get(c.group_id)
            if row is None:
                raise HTTPException(422, f"人群组 {c.group_id} 不存在")
            if row[6] in ("fixed", "awaiting_rescue",
                          "rescuing", "rescued"):
                departed.append({"group_id": c.group_id, "status": row[6],
                                 "departure_minute": row[7],
                                 "arrival_minute": row[8]})
        if departed:
            return JSONResponse(
                status_code=409,
                content={
                    "error": "GROUP_ALREADY_DEPARTED",
                    "message": "目标组已出发，不可纠正，整批拒绝",
                    "conflicts": departed,
                })

        # 合并最终值并按既有组校验复核（任一项无效即整批拒绝）
        merged = []
        for c in corrections:
            row = existing[c.group_id]
            before_fields = dict(zip(_CORRECTABLE_FIELDS, row[1:6]))
            after_fields = dict(before_fields)
            for f in _CORRECTABLE_FIELDS:
                value = getattr(c, f)
                if value is not None:
                    after_fields[f] = value
            try:
                GroupIn(id=c.group_id, **after_fields)
            except ValidationError as exc:
                msgs = "；".join(e["msg"] for e in exc.errors())
                raise HTTPException(
                    422, f"组 {c.group_id} 纠正后的字段不满足校验：{msgs}")
            if after_fields["origin"] not in known_nodes:
                raise HTTPException(
                    422, f"组 {c.group_id} 的起点 "
                         f"{after_fields['origin']} 不是场馆节点")
            merged.append({"group_id": c.group_id,
                           "before": before_fields, "after": after_fields})

        before = await _build_plan_response(conn)
        async with db.transaction(conn):
            for m in merged:
                f = m["after"]
                await conn.execute(
                    """UPDATE groups SET size=?, origin=?,
                       earliest_departure=?, priority=?, deadline=?
                       WHERE id=?""",
                    (f["size"], f["origin"], f["earliest_departure"],
                     f["priority"], f["deadline"], m["group_id"]))
            await _replan_pending(conn, in_transaction=True)
            after = await _build_plan_response(conn)
            await db.insert_revision(
                conn, "group-corrections",
                {"corrections": [
                    {"group_id": m["group_id"],
                     "changed_fields": [
                         f for f in _CORRECTABLE_FIELDS
                         if m["before"][f] != m["after"][f]],
                     "before": m["before"],
                     "after": m["after"]}
                    for m in merged]},
                _plan_dump(before), _plan_dump(after))
        return after


@app.post("/closures", tags=["变更"], status_code=201)
async def add_closure(closure: ClosureIn, request: Request):
    """新增左闭右开封闭时段。

    若封闭触及任一已出发组尚未走完的路线，整次拒绝（409）且不改任何数据、
    不产生修订；否则登记封闭并重排全部未出发组，同时产生一条修订。
    """
    conn = request.app.state.db
    async with request.app.state.lock:
        async with conn.execute("SELECT 1 FROM edges WHERE id=?", (closure.edge_id,)) as cur:
            if await cur.fetchone() is None:
                raise HTTPException(422, f"通道 {closure.edge_id} 不存在")

        conflicts = await _closure_conflicts(
            conn, closure.edge_id, closure.start_minute, closure.end_minute)
        if conflicts:
            return JSONResponse(
                status_code=409,
                content={
                    "error": "CLOSURE_CONFLICTS_WITH_DEPARTED_GROUP",
                    "message": "封闭时段与已出发组的剩余路线相交，整次拒绝",
                    "conflicts": [cf.model_dump() for cf in conflicts],
                })

        before = await _build_plan_response(conn)
        async with db.transaction(conn):
            await conn.execute(
                "INSERT INTO closures(edge_id, start_minute, end_minute) VALUES(?,?,?)",
                (closure.edge_id, closure.start_minute, closure.end_minute))
            await _replan_pending(conn, in_transaction=True)
            after = await _build_plan_response(conn)
            await db.insert_revision(conn, "closures", closure.model_dump(),
                                     _plan_dump(before), _plan_dump(after))
        return after


@app.post("/capacity-controls", tags=["变更"], response_model=PlanResponse,
          status_code=201)
async def add_capacity_control(control: CapacityControlIn, request: Request):
    """登记左闭右开的容量管制时段（临时限流）。

    管制时段内该通道每分钟有效上限收紧为 min(原容量, 指定上限)，时段外
    仍为原容量；编排按通行期间每一分钟的有效上限核算，封闭规则照常生效。

    通道不存在、起止无效或起点早于当前分钟时返回 422；与同通道已有管制
    时段重叠时返回 409；新上限使已出发组剩余路线在任一分钟超载时返回
    409 并给出冲突分钟、占用量与相关组。以上均整次拒绝、不改动任何
    状态、不产生修订。成功后仅重排未出发组（已出发组路线保留），
    登记管制并产生一条修订。
    """
    conn = request.app.state.db
    async with request.app.state.lock:
        minute_now = await db.get_current_minute(conn)
        async with conn.execute(
                "SELECT 1 FROM edges WHERE id=?",
                (control.edge_id,)) as cur:
            if await cur.fetchone() is None:
                raise HTTPException(422, f"通道 {control.edge_id} 不存在")
        if control.start_minute < minute_now:
            raise HTTPException(
                422, f"管制起点 {control.start_minute} 早于当前分钟 "
                     f"{minute_now}")

        async with conn.execute(
                """SELECT start_minute, end_minute FROM capacity_controls
                   WHERE edge_id=? ORDER BY start_minute""",
                (control.edge_id,)) as cur:
            existing = await cur.fetchall()
        overlaps = [(a, b) for a, b in existing
                    if control.start_minute < b and a < control.end_minute]
        if overlaps:
            return JSONResponse(
                status_code=409,
                content={
                    "error": "CAPACITY_CONTROL_OVERLAP",
                    "message": "同一通道的管制时段不得重叠，整次拒绝",
                    "conflicts": [
                        {"edge_id": control.edge_id,
                         "existing": [a, b],
                         "requested": [control.start_minute,
                                       control.end_minute]}
                        for a, b in overlaps],
                })

        conflicts = await _capacity_control_conflicts(
            conn, control.edge_id, control.start_minute, control.end_minute,
            control.capacity)
        if conflicts:
            return JSONResponse(
                status_code=409,
                content={
                    "error": "CAPACITY_CONTROL_OVERLOADS_DEPARTED_GROUP",
                    "message": "新上限使已出发组剩余路线在部分分钟超载，"
                               "整次拒绝",
                    "conflicts": conflicts,
                })

        before = await _build_plan_response(conn)
        async with db.transaction(conn):
            await conn.execute(
                """INSERT INTO capacity_controls
                   (edge_id, start_minute, end_minute, capacity)
                   VALUES(?,?,?,?)""",
                (control.edge_id, control.start_minute, control.end_minute,
                 control.capacity))
            await _replan_pending(conn, in_transaction=True)
            after = await _build_plan_response(conn)
            await db.insert_revision(conn, "capacity-controls",
                                     control.model_dump(),
                                     _plan_dump(before), _plan_dump(after))
        return after


@app.get("/capacity-controls", tags=["查询"],
         response_model=CapacityControlListOut)
async def list_capacity_controls(request: Request):
    """列出当前全部容量管制时段（按通道、开始分钟排序）。

    场景重置会清除全部管制记录；历史管制可通过修订快照追溯。
    """
    conn = request.app.state.db
    async with request.app.state.lock:
        minute_now = await db.get_current_minute(conn)
        async with conn.execute(
                """SELECT edge_id, start_minute, end_minute, capacity
                   FROM capacity_controls
                   ORDER BY edge_id, start_minute""") as cur:
            rows = await cur.fetchall()
    return CapacityControlListOut(
        current_minute=minute_now,
        controls=[CapacityControlOut(
            edge_id=row[0], start_minute=row[1], end_minute=row[2],
            capacity=row[3]) for row in rows])


# --------------------------------------------------------------------------
# 烟雾暴露时段与累计暴露上限
# --------------------------------------------------------------------------

async def _smoke_window_conflicts(conn: aiosqlite.Connection,
                                  smoke_map: dict,
                                  smoke_limits: dict[int, int],
                                  payload: SmokeWindowIn) -> list[dict]:
    """新烟雾时段是否使已出发且设限组按固定路线的预计累计暴露超限。

    已出发组（fixed）路线永久固定、不参与重排：把拟登记时段与既有时段
    合并后，按其固定路线**全程**（含已走前缀，按原计划时刻）核算预计
    累计暴露；已设非负上限且超限的组逐组列入冲突。待救援/救援中/已
    救援组没有余程，新时段只能落在未来而其未来无路线占用，不受影响；
    未出发组会随登记重排，不在检查之列。
    """
    eid = payload.edge_id
    candidate = (payload.start_minute, payload.end_minute,
                 payload.smoke_value)
    conflicts: list[dict] = []
    async with conn.execute(
            """SELECT g.id
               FROM groups g
               WHERE g.status='fixed' AND EXISTS (
                   SELECT 1 FROM route_steps rs
                   WHERE rs.group_id=g.id AND rs.edge_id=?)
               ORDER BY g.id""", (eid,)) as cur:
        capped_fixed = [gid for (gid,) in await cur.fetchall()
                        if gid in smoke_limits]
    for gid in capped_fixed:
        async with conn.execute(
                """SELECT edge_id, enter_minute, exit_minute
                   FROM route_steps WHERE group_id=? ORDER BY seq""",
                (gid,)) as cur:
            group_steps = await cur.fetchall()
        projected = _steps_exposure(
            _merged_smoke_map(smoke_map, {eid: [candidate]}),
            group_steps)
        limit = smoke_limits[gid]
        if projected > limit:
            # 仅拟登记时段本身落在该组路线上的逐分钟暴露增量，便于现场核对
            added = 0
            for step_eid, enter_m, exit_m in group_steps:
                if step_eid != eid:
                    continue
                for minute in range(max(enter_m, payload.start_minute),
                                    min(exit_m, payload.end_minute)):
                    added += payload.smoke_value
            conflicts.append({
                "group_id": gid, "edge_id": eid,
                "smoke_limit": limit,
                "projected_exposure": projected,
                "window_added_exposure": added,
                "window": [payload.start_minute, payload.end_minute,
                           payload.smoke_value]})
    return conflicts


def _merged_smoke_map(base: dict[int, list[tuple[int, int, int]]],
                      extra: dict[int, list[tuple[int, int, int]]]
                      ) -> dict[int, list[tuple[int, int, int]]]:
    """合并既有烟雾时段与拟登记时段（调用前已保证互不重叠）。"""
    merged: dict[int, list[tuple[int, int, int]]] = {
        eid: list(segs) for eid, segs in base.items()}
    for eid, segs in extra.items():
        merged.setdefault(eid, []).extend(segs)
        merged[eid].sort()
    return merged


@app.post("/smoke-windows", tags=["烟雾暴露"], response_model=PlanResponse,
          status_code=201,
          responses={
              409: {"description": "同通道烟雾时段重叠，或新时段使已出发且"
                                   "设限组按固定路线的预计累计暴露超限，整次拒绝"},
              422: {"description": "通道不存在、起止无效、起点早于当前分钟或暴露值为负"}})
async def add_smoke_window(payload: SmokeWindowIn, request: Request):
    """现场按通道登记一条烟雾暴露时段。

    时段为左闭右开 ``[start_minute, end_minute)``，起点须不早于当前分钟，
    ``smoke_value`` 为时段内每分钟非负暴露值；同一通道的烟雾时段互不
    重叠（端点相接允许）。通道占用的每分钟计入该分钟暴露值，未登记为
    零，节点等待不计。通道不存在、起止无效或暴露值为负返回 422；与同
    通道已有时段重叠返回 409 ``SMOKE_WINDOW_OVERLAP``。

    若新时段使**已出发且设了非负累计暴露上限**的组按其固定路线的预计
    累计暴露超限，整次拒绝（409
    ``SMOKE_WINDOW_OVERLIMITS_DEPARTED_GROUP``，``conflicts`` 给出组号、
    上限、预计累计暴露与该时段带来的增量暴露），不写入时段、不触发重排、
    不产生修订。成功后仅重排未出发组（可在节点等待烟雾消散后通行；
    已出发组路线保留），登记时段并产生一条 ``smoke-windows`` 修订。
    """
    conn = request.app.state.db
    async with request.app.state.lock:
        minute_now = await db.get_current_minute(conn)
        async with conn.execute(
                "SELECT 1 FROM edges WHERE id=?",
                (payload.edge_id,)) as cur:
            if await cur.fetchone() is None:
                raise HTTPException(
                    422, f"通道 {payload.edge_id} 不存在")
        if payload.start_minute < minute_now:
            raise HTTPException(
                422, f"烟雾暴露起点 {payload.start_minute} 早于当前分钟 "
                     f"{minute_now}")

        smoke_map = await _load_smoke_map(conn)
        overlaps = [(a, b, value)
                    for a, b, value in smoke_map.get(payload.edge_id, ())
                    if payload.start_minute < b
                    and a < payload.end_minute]
        if overlaps:
            return JSONResponse(
                status_code=409,
                content={
                    "error": "SMOKE_WINDOW_OVERLAP",
                    "message": "同一通道的烟雾暴露时段不得重叠，整次拒绝",
                    "conflicts": [
                        {"edge_id": payload.edge_id,
                         "existing": [a, b, value],
                         "requested": [payload.start_minute,
                                       payload.end_minute,
                                       payload.smoke_value]}
                        for a, b, value in overlaps]})

        smoke_limits = await db.load_smoke_limits(conn)
        conflicts = await _smoke_window_conflicts(
            conn, smoke_map, smoke_limits, payload)
        if conflicts:
            return JSONResponse(
                status_code=409,
                content={
                    "error": "SMOKE_WINDOW_OVERLIMITS_DEPARTED_GROUP",
                    "message": "新烟雾暴露时段使已出发组按固定路线的预计"
                               "累计暴露超过其登记上限，整次拒绝",
                    "conflicts": conflicts})

        before = await _build_plan_response(conn)
        async with db.transaction(conn):
            await db.insert_smoke_window(
                conn, edge_id=payload.edge_id,
                start_minute=payload.start_minute,
                end_minute=payload.end_minute,
                smoke_value=payload.smoke_value)
            await _replan_pending(conn, in_transaction=True)
            after = await _build_plan_response(conn)
            await db.insert_revision(conn, "smoke-windows",
                                     payload.model_dump(),
                                     _plan_dump(before), _plan_dump(after))
        return after


@app.get("/smoke-windows", tags=["烟雾暴露"],
         response_model=SmokeWindowListOut)
async def list_smoke_windows(request: Request):
    """列出当前全部烟雾暴露时段（按通道、开始分钟排序）。

    场景重置会清空全部烟雾暴露时段；历史时段可通过修订快照追溯。
    """
    conn = request.app.state.db
    async with request.app.state.lock:
        minute_now = await db.get_current_minute(conn)
        windows = await db.load_smoke_windows(conn)
    return SmokeWindowListOut(
        current_minute=minute_now,
        windows=[SmokeWindowOut(**w) for w in windows])


@app.post("/smoke-limits", tags=["烟雾暴露"], response_model=PlanResponse,
          status_code=201,
          responses={
              409: {"description": "目标组已出发（fixed/awaiting_rescue/"
                                   "rescuing/rescued），整次拒绝"},
              422: {"description": "组不存在或暴露上限为负"}})
async def register_smoke_limit(payload: SmokeLimitIn, request: Request):
    """为**尚未出发**的人群组登记非负累计暴露上限。

    每组至多一项：再次登记替换旧上限（修订 ``detail.replaced`` 记录被
    替换的旧值）；未设上限的组不受烟雾暴露约束。通道占用的每分钟计入
    该通道该分钟暴露值（未登记为零），节点等待不计；编排须在累计暴露
    不超限的路线中沿用既有封闭、容量、截止、先导放行与路线择优规则，
    可在节点等待烟雾消散后通行，截止前无满足上限的路线时该组失败
    （``EXPOSURE_LIMIT_EXCEEDED`` 等既有归因）。

    目标组已出发（含已被现场报告置为待救援、已交接救援队或已人工
    送达）时整次拒绝（409 ``SMOKE_LIMIT_TARGET_DEPARTED``），旧上限、
    编排与修订历史不变；组不存在或上限为负返回 422。成功登记原子重排
    全部未出发组、写入一条 ``smoke-limits`` 修订，返回完整编排。
    """
    conn = request.app.state.db
    async with request.app.state.lock:
        minute_now = await db.get_current_minute(conn)
        async with conn.execute(
                "SELECT status FROM groups WHERE id=?",
                (payload.group_id,)) as cur:
            row = await cur.fetchone()
        if row is None:
            raise HTTPException(
                422, f"人群组 {payload.group_id} 不存在")
        status = row[0]
        if status in _DEPARTED_STATUSES:
            return JSONResponse(
                status_code=409,
                content={
                    "error": "SMOKE_LIMIT_TARGET_DEPARTED",
                    "message": "目标组已出发，累计暴露上限不可再登记，"
                               "整次拒绝",
                    "conflicts": [{
                        "group_id": payload.group_id,
                        "smoke_limit": payload.smoke_limit,
                        "current_minute": minute_now,
                        "status": status,
                        "reason": "TARGET_ALREADY_DEPARTED"}]})

        before = await _build_plan_response(conn)
        old_limit = (await db.load_smoke_limits(conn)).get(payload.group_id)
        async with db.transaction(conn):
            await db.upsert_smoke_limit(
                conn, group_id=payload.group_id,
                smoke_limit=payload.smoke_limit)
            await _replan_pending(conn, in_transaction=True)
            after = await _build_plan_response(conn)
            detail = {"group_id": payload.group_id,
                      "smoke_limit": payload.smoke_limit}
            if old_limit is not None:
                detail["replaced"] = {"smoke_limit": old_limit}
            await db.insert_revision(
                conn, "smoke-limits", detail,
                _plan_dump(before), _plan_dump(after))
        return after


@app.get("/smoke-limits", tags=["烟雾暴露"],
         response_model=SmokeLimitListOut)
async def list_smoke_limits(request: Request):
    """列出当前各组登记的非负累计暴露上限（按组号升序）。

    未列入响应的组即未设上限、不受烟雾暴露约束；各组前缀已用与余程
    预计暴露见 ``GET /plan`` 中对应字段。场景重置清空全部上限。
    """
    conn = request.app.state.db
    async with request.app.state.lock:
        minute_now = await db.get_current_minute(conn)
        smoke_limits = await db.load_smoke_limits(conn)
    return SmokeLimitListOut(
        current_minute=minute_now,
        groups=[SmokeLimitOut(group_id=gid, smoke_limit=limit)
                for gid, limit in sorted(smoke_limits.items())])


# --------------------------------------------------------------------------
# 先导关系（放行前置）
# --------------------------------------------------------------------------

# 目标组已出发（fixed/awaiting_rescue/rescuing/rescued）时不可再登记
_DEPARTED_STATUSES = ("fixed", "awaiting_rescue", "rescuing", "rescued")


def _prerequisite_conflict(reason: str, message: str,
                           payload: PrerequisiteIn, minute_now: int,
                           *, target_status: str | None = None,
                           extra: dict | None = None) -> JSONResponse:
    """构造统一的先导关系登记拒绝响应（HTTP 409）。"""
    conflict = {"group_id": payload.group_id,
                "leader_group_id": payload.leader_group_id,
                "release_gap": payload.release_gap,
                "current_minute": minute_now, "reason": reason}
    if target_status is not None:
        conflict["status"] = target_status
    if extra:
        conflict.update(extra)
    return JSONResponse(
        status_code=409,
        content={"error": "PREREQUISITE_NOT_ACCEPTABLE",
                 "message": message, "conflicts": [conflict]})


def _prerequisite_cycle(graph: dict[int, int], target: int,
                        leader: int) -> list[int]:
    """在假设 target -> leader 下检测成环；成环时返回环上的组号序列。

    自依赖（leader == target）返回 [target, target]。沿 leader 链自后继
    向上游走，回到 target 即成环（每组至多一个先导，链唯一）。
    """
    if leader == target:
        return [target, target]
    path = [leader]
    node = leader
    seen = {leader}
    while node in graph:
        node = graph[node]
        path.append(node)
        if node == target:
            return [target] + path
        if node in seen:
            return []  # 既有关系里已有环，登记接口不会产生这种数据
        seen.add(node)
    return []


@app.post("/group-prerequisites", tags=["先导关系"],
          response_model=PlanResponse, status_code=201,
          responses={
              409: {"description": "目标组已出发/自依赖/形成环，整次拒绝"},
              422: {"description": "目标组或先导组编号不存在、间隔为负"}})
async def register_prerequisite(payload: PrerequisiteIn, request: Request):
    """现场为**尚未出发**的人群组登记先导组与非负放行间隔。

    同一组至多一项关系：再次登记替换旧关系（组号或间隔无效、目标组已
    出发或形成环时整次拒绝，旧关系与修订历史不变）。先导组必须存在，
    禁止自依赖和成环（含多级关系形成的环）。

    登记成功后立即重排全部未出发组：后继组的可出发分钟不早于自身最早
    出发分钟及"先导组规划到达分钟 + 间隔"；先导组已由人工救援送达时
    改用实际送达分钟；先导待救援、救援中或无可行路线（含先导自身前置
    未满足）时后继标记 ``blocked``（前置未满足，不占通道），待先导落
    定后的后续重排中转正。多级关系按依赖顺序安排，每轮在可安排组中沿
    用现有优先级、最早出发分钟、组编号与路线并列规则。成功登记原子写
    入一条 ``group-prerequisites`` 修订，返回完整编排。
    """
    conn = request.app.state.db
    async with request.app.state.lock:
        minute_now = await db.get_current_minute(conn)
        async with conn.execute(
                "SELECT id, status FROM groups") as cur:
            group_rows = await cur.fetchall()
        group_status = {gid: status for gid, status in group_rows}

        if payload.group_id not in group_status:
            raise HTTPException(
                422, f"目标人群组 {payload.group_id} 不存在")
        if payload.leader_group_id not in group_status:
            raise HTTPException(
                422, f"先导组 {payload.leader_group_id} 不存在")
        target_status = group_status[payload.group_id]
        if target_status in _DEPARTED_STATUSES:
            return _prerequisite_conflict(
                "TARGET_ALREADY_DEPARTED",
                "目标组已出发，先导关系不可再登记，整次拒绝",
                payload, minute_now, target_status=target_status)

        prerequisites = await db.load_prerequisites(conn)
        # 以"替换旧关系"后的图做环检测：先移除目标组当前的出边
        graph = {gid: pre["leader_group_id"]
                 for gid, pre in prerequisites.items()}
        graph.pop(payload.group_id, None)
        cycle = _prerequisite_cycle(
            graph, payload.group_id, payload.leader_group_id)
        if cycle:
            if cycle == [payload.group_id, payload.group_id]:
                reason, message = (
                    "SELF_DEPENDENCY",
                    "先导组不能是目标组自身，禁止自依赖，整次拒绝")
            else:
                reason, message = (
                    "CYCLE_DETECTED",
                    "登记后先导关系将形成环，多级依赖禁止成环，整次拒绝")
            return _prerequisite_conflict(
                reason, message, payload, minute_now,
                target_status=target_status,
                extra={"cycle": cycle} if reason == "CYCLE_DETECTED" else None)

        before = await _build_plan_response(conn)
        old = prerequisites.get(payload.group_id)
        async with db.transaction(conn):
            await db.upsert_prerequisite(
                conn, group_id=payload.group_id,
                leader_group_id=payload.leader_group_id,
                release_gap=payload.release_gap)
            await _replan_pending(conn, in_transaction=True)
            after = await _build_plan_response(conn)
            detail = {"group_id": payload.group_id,
                      "leader_group_id": payload.leader_group_id,
                      "release_gap": payload.release_gap}
            if old is not None:
                detail["replaced"] = {
                    "leader_group_id": old["leader_group_id"],
                    "release_gap": old["release_gap"]}
            await db.insert_revision(
                conn, "group-prerequisites", detail,
                _plan_dump(before), _plan_dump(after))
        return after


@app.get("/group-prerequisites", tags=["先导关系"],
         response_model=PrerequisiteListOut)
async def list_prerequisites(request: Request):
    """列出当前全部先导关系及其放行依据（按目标组编号升序）。

    每项给出目标组、先导组、非负放行间隔，以及实时计算的放行依据：
    先导状态、采用的到达分钟（规划到达 / 人工实际送达）与后继最早放行
    分钟；先导待救援、救援中、尚未安排或无可行路线时标记前置未满足及
    原因。场景重置清空全部关系。
    """
    conn = request.app.state.db
    async with request.app.state.lock:
        minute_now = await db.get_current_minute(conn)
        prerequisites = await db.load_prerequisites(conn)
        async with conn.execute(
                """SELECT id, status, departure_minute, arrival_minute,
                          exit_id, fail_reason, fail_detail
                   FROM groups ORDER BY id""") as cur:
            group_index = {row[0]: row for row in await cur.fetchall()}
        rescue_by_group = await db.load_group_rescue_map(conn)
        relations = []
        for gid in sorted(prerequisites):
            pre = prerequisites[gid]
            relations.append(PrerequisiteOut(
                group_id=gid,
                leader_group_id=pre["leader_group_id"],
                release_gap=pre["release_gap"],
                release_basis=_build_release_basis(
                    pre["leader_group_id"], pre["release_gap"],
                    group_index, rescue_by_group)))
    return PrerequisiteListOut(current_minute=minute_now,
                               relations=relations)


# --------------------------------------------------------------------------
# 现场节点滞留报告
# --------------------------------------------------------------------------

@app.post("/groups/{group_id}/delay-report", tags=["现场报告"],
          response_model=PlanResponse,
          responses={409: {"description": "组未出发或节点/时机不符，整次拒绝"},
                     422: {"description": "组不存在或恢复分钟早于当前分钟"}})
async def report_group_delay(group_id: int, payload: DelayReportIn,
                             request: Request):
    """现场指挥按组号、所在节点与预计恢复分钟报告已出发组的节点滞留。

    仅当该组按现行计划**已到达该节点**、**尚未进入下一通道**且**未抵达
    出口**时受理（待救援组可在其滞留节点重复报告新的恢复分钟）；组未出发
    （409 ``NOT_DEPARTED``）、节点不符（409 ``NODE_MISMATCH``）、已进入
    下一通道（409 ``GROUP_IN_TRANSIT``）或已抵出口（409
    ``ALREADY_AT_EXIT``）均整次拒绝、不改编排；组不存在或恢复分钟早于
    当前分钟返回 422。

    受理后在同一事务内：保留已走前缀及其时间、释放该组从当前分钟起的旧
    预约，再依据现有封闭、限流和其他已出发组的固定占用，从该节点、恢复
    分钟起按既有"最早到达 / 出口编号 / 通道序列"规则寻找截止前余程：
    有解则组保持 ``fixed``（已走前缀 + 新余程）；无解则标记
    ``awaiting_rescue`` 并给出原因，不恢复旧预约。其他未出发组随之整体
    重排，其他已出发组路线与占用不变。成功产生一条 ``delay-reports`` 修订。
    """
    conn = request.app.state.db
    async with request.app.state.lock:
        return await _apply_delay_report(
            conn, group_id=group_id, node_id=payload.node_id,
            recovery_minute=payload.recovery_minute)


@app.post("/delay-reports", tags=["现场报告"],
          response_model=PlanResponse,
          responses={409: {"description": "组未出发或节点/时机不符，整次拒绝"},
                     422: {"description": "组不存在或恢复分钟早于当前分钟"}},
          description="与 POST /groups/{group_id}/delay-report 等价的集合式"
                      "写法（组号放在请求体内），便于统一批量客户端调用。")
async def report_group_delay_alias(payload: DelayReportAliasIn,
                                   request: Request):
    """集合式节点滞留报告：请求体含 group_id/node_id/recovery_minute。"""
    conn = request.app.state.db
    async with request.app.state.lock:
        return await _apply_delay_report(
            conn, group_id=payload.group_id, node_id=payload.node_id,
            recovery_minute=payload.recovery_minute)


class TickIn(BaseModel):
    minutes: int = Field(1, gt=0, description="推进的分钟数（正整数）")


# --------------------------------------------------------------------------
# 人工救援队：登记 / 派遣 / 查询 / 结案核销
# --------------------------------------------------------------------------

def _dispatch_conflict(reason: str, payload: RescueDispatchIn,
                       minute_now: int, *, status: str | None = None,
                       group_size: int | None = None,
                       team_capacity: int | None = None,
                       expected_node: int | None = None,
                       current_task_id: int | None = None
                       ) -> JSONResponse:
    """构造统一的派遣拒绝响应（HTTP 409 + error/message/conflicts）。"""
    messages = {
        "OBSERVED_MINUTE_STALE":
            "所见当前分钟与实际当前分钟不一致（可能已被时间推进抢占），"
            "整次拒绝",
        "GROUP_NOT_AWAITING_RESCUE":
            "该组当前不在待救援状态，不受理人工救援派遣，整次拒绝",
        "GROUP_RESCUE_IN_PROGRESS":
            "该组已有进行中的人工救援任务，同组只允许一项，整次拒绝",
        "GROUP_ALREADY_RESCUED":
            "该组已由人工救援队送达核销，不可再次派遣，整次拒绝",
        "NODE_MISMATCH":
            "滞留节点与该组待救援位置不符，整次拒绝",
        "TEAM_CAPACITY_EXCEEDED":
            "组人数超过救援队载员量，整次拒绝",
        "TEAM_BUSY":
            "救援队当前不空闲（已有进行中任务），同队只允许一项，整次拒绝",
    }
    conflict = {"group_id": payload.group_id, "team_id": payload.team_id,
                "node_id": payload.node_id,
                "observed_minute": payload.observed_minute,
                "current_minute": minute_now, "reason": reason}
    if status is not None:
        conflict["status"] = status
    if expected_node is not None:
        conflict["expected_node_id"] = expected_node
    if group_size is not None:
        conflict["group_size"] = group_size
    if team_capacity is not None:
        conflict["team_capacity"] = team_capacity
    if current_task_id is not None:
        conflict["current_task_id"] = current_task_id
    return JSONResponse(
        status_code=409,
        content={"error": "RESCUE_DISPATCH_NOT_ACCEPTABLE",
                 "message": messages[reason], "conflicts": [conflict]})


async def _apply_rescue_dispatch(conn: aiosqlite.Connection,
                                 payload: RescueDispatchIn):
    """受理一次人工救援派遣（调用方已持锁）。

    仅当所见分钟未变、组仍在该节点待救援、人数不超载员量、队伍空闲且
    同组同队均无进行中任务时受理。任一条件不满足整次拒绝（409），不改编排
    与修订历史；无效组/队编号返回 422。受理后组置 rescuing，保留已走
    通道前缀、不产生任何未来通道占用，与修订记录同一事务原子写入。
    """
    minute_now = await db.get_current_minute(conn)

    async with conn.execute(
            "SELECT status, size FROM groups WHERE id=?",
            (payload.group_id,)) as cur:
        group_row = await cur.fetchone()
    if group_row is None:
        raise HTTPException(422, f"人群组 {payload.group_id} 不存在")
    status, group_size = group_row

    async with conn.execute(
            "SELECT capacity FROM rescue_teams WHERE id=?",
            (payload.team_id,)) as cur:
        team_row = await cur.fetchone()
    if team_row is None:
        raise HTTPException(422, f"救援队 {payload.team_id} 不存在")
    team_capacity = team_row[0]

    # 分钟未变：现场所见分钟须恰为当前分钟（时间推进并发抢占即在此拦截）
    if payload.observed_minute != minute_now:
        return _dispatch_conflict(
            "OBSERVED_MINUTE_STALE", payload, minute_now, status=status)
    # 组仍在待救援状态：救援中/已救援的组分别给出精确原因
    if status == "rescuing":
        open_group_task = await db.load_open_rescue_task_for_group(
            conn, payload.group_id)
        return _dispatch_conflict(
            "GROUP_RESCUE_IN_PROGRESS", payload, minute_now, status=status,
            current_task_id=(open_group_task or {}).get("task_id"))
    if status == "rescued":
        return _dispatch_conflict(
            "GROUP_ALREADY_RESCUED", payload, minute_now, status=status)
    if status != "awaiting_rescue":
        return _dispatch_conflict(
            "GROUP_NOT_AWAITING_RESCUE", payload, minute_now,
            status=status)
    # 组仍在该节点：待救援组的滞留节点取自其现场滞留报告
    report = await db.load_delay_report(conn, payload.group_id)
    if report is None:  # 防御：状态与报告表不一致不应出现
        return _dispatch_conflict(
            "NODE_MISMATCH", payload, minute_now, status=status)
    if payload.node_id != report["node_id"]:
        return _dispatch_conflict(
            "NODE_MISMATCH", payload, minute_now, status=status,
            expected_node=report["node_id"])
    # 同组进行中任务（防御：awaiting_rescue 状态下不应存在，索引亦兜底）
    open_group_task = await db.load_open_rescue_task_for_group(
        conn, payload.group_id)
    if open_group_task is not None:
        return _dispatch_conflict(
            "GROUP_RESCUE_IN_PROGRESS", payload, minute_now, status=status,
            current_task_id=open_group_task["task_id"])
    # 人数不超载员量
    if group_size > team_capacity:
        return _dispatch_conflict(
            "TEAM_CAPACITY_EXCEEDED", payload, minute_now, status=status,
            group_size=group_size, team_capacity=team_capacity)
    # 队伍空闲：同队只允许一项进行中任务
    async with conn.execute(
            "SELECT id, group_id FROM rescue_tasks "
            "WHERE team_id=? AND status='in_progress'",
            (payload.team_id,)) as cur:
        busy = await cur.fetchone()
    if busy is not None:
        return _dispatch_conflict(
            "TEAM_BUSY", payload, minute_now, status=status,
            current_task_id=busy[0])

    before = await _build_plan_response(conn)
    async with db.transaction(conn):
        task_id = await db.insert_rescue_task(
            conn, group_id=payload.group_id, team_id=payload.team_id,
            node_id=payload.node_id, observed_minute=payload.observed_minute,
            dispatch_minute=minute_now)
        # 组标记救援中：已走前缀保留（route_steps 不动），不写入任何未来
        # 通道占用；planning 字段（exit_id/arrival_minute）保持为空
        await conn.execute(
            "UPDATE groups SET status='rescuing', fail_reason=NULL, "
            "fail_detail=NULL WHERE id=?", (payload.group_id,))
        after = await _build_plan_response(conn)
        await db.insert_revision(
            conn, "rescue-dispatches",
            {"task_id": task_id, "group_id": payload.group_id,
             "team_id": payload.team_id, "node_id": payload.node_id,
             "observed_minute": payload.observed_minute,
             "dispatch_minute": minute_now},
            _plan_dump(before), _plan_dump(after))
    return JSONResponse(
        status_code=201,
        content=RescueTaskOut(
            task_id=task_id, group_id=payload.group_id,
            team_id=payload.team_id, node_id=payload.node_id,
            observed_minute=payload.observed_minute,
            dispatch_minute=minute_now, status="in_progress").model_dump())


async def _apply_rescue_completion(conn: aiosqlite.Connection,
                                   task_id: int,
                                   payload: RescueCompletionIn):
    """结案核销一次人工救援（调用方已持锁）。

    实际送达分钟须不早于派遣分钟且不晚于当前分钟；送达出口须为既有出口。
    成功后队伍立即空闲（任务置 completed），组标记 rescued，实际出口与
    时刻单列（不写入规划到达字段），与修订记录同一事务原子写入。
    """
    minute_now = await db.get_current_minute(conn)
    task = await db.load_rescue_task(conn, task_id)
    if task is None:
        raise HTTPException(404, f"救援任务 {task_id} 不存在")

    async with conn.execute("SELECT 1 FROM exits WHERE node_id=?",
                            (payload.exit_id,)) as cur:
        if await cur.fetchone() is None:
            raise HTTPException(
                422, f"出口 {payload.exit_id} 不是当前场景的既有出口")

    def _conflict(reason: str, message: str, extra: dict) -> JSONResponse:
        return JSONResponse(
            status_code=409,
            content={
                "error": "RESCUE_COMPLETION_NOT_ACCEPTABLE",
                "message": message,
                "conflicts": [{
                    "task_id": task_id, "group_id": task["group_id"],
                    "team_id": task["team_id"], "status": task["status"],
                    "dispatch_minute": task["dispatch_minute"],
                    "current_minute": minute_now,
                    "exit_id": payload.exit_id,
                    "delivery_minute": payload.delivery_minute,
                    "reason": reason, **extra}]})

    if task["status"] != "in_progress":
        return _conflict(
            "TASK_ALREADY_COMPLETED",
            "该救援任务已结案核销，不可重复结案，整次拒绝",
            {"completed_exit_id": task["exit_id"],
             "completed_delivery_minute": task["delivery_minute"]})
    if payload.delivery_minute < task["dispatch_minute"]:
        return _conflict(
            "DELIVERY_BEFORE_DISPATCH",
            "实际送达分钟早于派遣分钟，时序冲突，整次拒绝", {})
    if payload.delivery_minute > minute_now:
        return _conflict(
            "DELIVERY_IN_FUTURE",
            "实际送达分钟晚于当前分钟，时序冲突，整次拒绝", {})

    before = await _build_plan_response(conn)
    async with db.transaction(conn):
        await db.complete_rescue_task(
            conn, task_id=task_id, exit_id=payload.exit_id,
            delivery_minute=payload.delivery_minute)
        # 组标记已救援：规划到达字段（exit_id/arrival_minute）保持为空，
        # 人工送达不当作规划到达；实际出口与时刻仅由救援字段单列。
        # 旧的规划出发分钟同样清空（待救援前的残留），人工送达时刻以
        # rescue_tasks.delivery_minute 为准。
        await conn.execute(
            "UPDATE groups SET status='rescued', departure_minute=NULL "
            "WHERE id=?", (task["group_id"],))
        # 结案后立即重排未出发组：以该组为先导的后继改用实际送达分钟作为
        # 放行锚点（前置未满足的后继随之转正）；已出发组不追溯
        await _replan_pending(conn, in_transaction=True)
        after = await _build_plan_response(conn)
        await db.insert_revision(
            conn, "rescue-completions",
            {"task_id": task_id, "group_id": task["group_id"],
             "team_id": task["team_id"], "exit_id": payload.exit_id,
             "delivery_minute": payload.delivery_minute,
             "dispatch_minute": task["dispatch_minute"]},
            _plan_dump(before), _plan_dump(after))
    task.update(status="completed", exit_id=payload.exit_id,
                delivery_minute=payload.delivery_minute)
    return RescueTaskOut(**task)


@app.post("/rescue-teams", tags=["人工救援"],
          response_model=RescueTeamOut, status_code=201,
          responses={409: {"description": "救援队编号已登记，整次拒绝"}})
async def register_rescue_team(payload: RescueTeamIn, request: Request):
    """登记一支人工救援队：唯一编号 + 正整数载员量。

    编号重复返回 409 ``RESCUE_TEAM_ALREADY_REGISTERED``，整次拒绝、不产生
    修订。队伍登记不改变编排；是否空闲由其有无进行中任务决定。
    """
    conn = request.app.state.db
    async with request.app.state.lock:
        async with conn.execute(
                "SELECT 1 FROM rescue_teams WHERE id=?",
                (payload.team_id,)) as cur:
            if await cur.fetchone() is not None:
                return JSONResponse(
                    status_code=409,
                    content={
                        "error": "RESCUE_TEAM_ALREADY_REGISTERED",
                        "message": f"救援队 {payload.team_id} 已登记，"
                                   f"重复登记整次拒绝",
                        "conflicts": [{"team_id": payload.team_id,
                                       "capacity": payload.capacity,
                                       "reason": "DUPLICATE_TEAM_ID"}]})
        async with db.transaction(conn):
            await db.insert_rescue_team(
                conn, team_id=payload.team_id, capacity=payload.capacity)
        return RescueTeamOut(team_id=payload.team_id,
                             capacity=payload.capacity, task_id=None)


@app.get("/rescue-teams", tags=["人工救援"],
         response_model=RescueTeamListOut)
async def list_rescue_teams(request: Request):
    """列出全部人工救援队（含载员量与当前进行中任务编号；空闲任务号为 null）。"""
    conn = request.app.state.db
    async with request.app.state.lock:
        minute_now = await db.get_current_minute(conn)
        teams = await db.load_rescue_teams(conn)
    return RescueTeamListOut(
        current_minute=minute_now,
        teams=[RescueTeamOut(**t) for t in teams])


@app.post("/rescue-tasks", tags=["人工救援"],
          response_model=RescueTaskOut, status_code=201,
          responses={
              409: {"description": "分钟失步/组状态或节点不符/超载/队伍忙，"
                                   "整次拒绝"},
              422: {"description": "无效组号或队号"}})
async def dispatch_rescue(payload: RescueDispatchIn, request: Request):
    """现场指挥把一个**待救援**组交接给人工救援队。

    请求体给出组号、队号、滞留节点与所见当前分钟。仅当所见分钟未变、
    组仍在该节点处于 ``awaiting_rescue``、组人数不超队伍载员量、队伍空闲
    且同组同队均无进行中任务时受理，返回任务号（201）。受理后组置
    ``rescuing``：保留已走通道前缀、不产生未来通道占用，时间推进不自动
    重排该组，其既有节点滞留报告接口被拒绝（409
    ``GROUP_RESCUING``）。任一条件不符返回 409
    ``RESCUE_DISPATCH_NOT_ACCEPTABLE``（``conflicts[0].reason`` 给出
    OBSERVED_MINUTE_STALE / GROUP_NOT_AWAITING_RESCUE / NODE_MISMATCH /
    GROUP_RESCUE_IN_PROGRESS / TEAM_CAPACITY_EXCEEDED / TEAM_BUSY），
    整次拒绝、不改编排与修订；无效组/队编号返回 422。成功派遣原子写入
    一条 ``rescue-dispatches`` 修订。
    """
    conn = request.app.state.db
    async with request.app.state.lock:
        return await _apply_rescue_dispatch(conn, payload)


@app.get("/rescue-tasks", tags=["人工救援"],
         response_model=RescueTaskListOut)
async def list_rescue_tasks(request: Request,
                            status: str | None = None):
    """列出全部救援任务（按任务号升序），可加 ``?status=in_progress`` 或
    ``?status=completed`` 过滤；其他状态值返回 422。"""
    if status is not None and status not in ("in_progress", "completed"):
        raise HTTPException(
            422, "status 仅支持 in_progress 或 completed")
    conn = request.app.state.db
    async with request.app.state.lock:
        minute_now = await db.get_current_minute(conn)
        tasks = await db.list_rescue_tasks(conn, status)
    return RescueTaskListOut(
        current_minute=minute_now,
        tasks=[RescueTaskOut(**t) for t in tasks])


@app.get("/rescue-tasks/{task_id}", tags=["人工救援"],
         response_model=RescueTaskOut,
         responses={404: {"description": "救援任务不存在"}})
async def get_rescue_task(task_id: int, request: Request):
    """按任务号查询救援任务当前状态（in_progress/completed 及结案信息）。"""
    conn = request.app.state.db
    async with request.app.state.lock:
        task = await db.load_rescue_task(conn, task_id)
    if task is None:
        raise HTTPException(404, f"救援任务 {task_id} 不存在")
    return RescueTaskOut(**task)


@app.post("/rescue-tasks/{task_id}/completion", tags=["人工救援"],
          response_model=RescueTaskOut,
          responses={
              409: {"description": "任务已结案或送达分钟时序冲突，整次拒绝"},
              404: {"description": "救援任务不存在"},
              422: {"description": "出口编号非当前场景既有出口"}})
async def complete_rescue(task_id: int, payload: RescueCompletionIn,
                          request: Request):
    """人工救援队结案核销：提交任务号、**既有出口编号**与实际送达分钟。

    送达分钟须不早于派遣分钟且不晚于当前分钟（晚于该组原规划截止也如实
    记录）。成功后任务置 completed、队伍立即空闲，组置 ``rescued``，实际
    出口与送达时刻在任务与编排中单列（``rescued_exit_id`` /
    ``delivery_minute``），**不**回写为规划到达（``exit_id`` /
    ``arrival_minute`` 保持为空）。任务不存在 404；出口非既有出口 422；
    任务已结案（``TASK_ALREADY_COMPLETED``）、送达早于派遣
    （``DELIVERY_BEFORE_DISPATCH``）或晚于当前分钟（``DELIVERY_IN_FUTURE``）
    返回 409 ``RESCUE_COMPLETION_NOT_ACCEPTABLE``，整次拒绝。成功结案原子
    写入一条 ``rescue-completions`` 修订。
    """
    conn = request.app.state.db
    async with request.app.state.lock:
        return await _apply_rescue_completion(conn, task_id, payload)


# --------------------------------------------------------------------------
# 出口清点（按组清点实际撤离人数）
# --------------------------------------------------------------------------

async def _arrival_anchor(conn: aiosqlite.Connection, group_id: int,
                          status: str, exit_id: int | None,
                          arrival_minute: int | None):
    """组的可清点到达锚点：(basis, exit_id, minute)；不可清点时返回 None。

    * fixed（已出发、路线固定）：按固定路线的规划到达出口与到达分钟
      （basis=planned_arrival）；
    * rescued（人工结案送达）：实际送达出口与实际送达分钟
      （basis=actual_delivery，不取规划字段）；
    * 其余状态（未出发/待救援/救援中等）：None。
    """
    if status == "fixed" and arrival_minute is not None:
        return "planned_arrival", exit_id, arrival_minute
    if status == "rescued":
        async with conn.execute(
                """SELECT exit_id, delivery_minute FROM rescue_tasks
                   WHERE group_id=? AND status='completed'""",
                (group_id,)) as cur:
            task = await cur.fetchone()
        if task is not None and task[0] is not None:
            return "actual_delivery", task[0], task[1]
    return None


async def _exit_count_record_out(conn: aiosqlite.Connection,
                                 serial: str) -> ExitCountRecordOut:
    """组装一条清点记录的完整输出（含更正历史）。"""
    rec = await db.load_exit_count(conn, serial)
    corrections = await db.load_exit_count_corrections(conn, serial)
    return ExitCountRecordOut(
        serial=rec["serial"], group_id=rec["group_id"],
        exit_id=rec["exit_id"], count=rec["counted"],
        submitted_count=rec["submitted_count"],
        counted_minute=rec["counted_minute"], version=rec["version"],
        created_minute=rec["created_minute"],
        corrections=[ExitCountCorrectionOut(**c) for c in corrections])


async def _build_group_tally(conn: aiosqlite.Connection, group_row,
                             minute_now: int) -> GroupTallyOut:
    """按组汇总：应到（组人数）、已清点（当前生效累计）、未清点与逐条记录。"""
    gid, size, status, g_exit, g_arrival = group_row
    records = await db.load_exit_counts(conn, gid)
    counted = sum(r["counted"] for r in records)
    anchor = await _arrival_anchor(conn, gid, status, g_exit, g_arrival)
    arrival = ExitCountArrivalOut(
        basis=anchor[0] if anchor else None,
        exit_id=anchor[1] if anchor else None,
        minute=anchor[2] if anchor else None)
    return GroupTallyOut(
        group_id=gid, expected=size, counted=counted,
        remaining=size - counted, arrival=arrival,
        records=[await _exit_count_record_out(conn, r["serial"])
                 for r in records])


@app.post("/exit-counts", tags=["出口清点"], response_model=ExitCountRecordOut,
          status_code=201,
          responses={
              200: {"description": "同流水号同内容的幂等重试，返回原记录"},
              409: {"description": "流水号异内容/组未抵达/出口不符/分钟越窗/"
                                   "累计超额，整次拒绝"},
              422: {"description": "流水号为空、人数或分钟非法、组或出口不存在"}})
async def submit_exit_count(payload: ExitCountIn, request: Request):
    """现场在出口按组清点实际撤离人数（幂等登记）。

    每条清点提交非空唯一流水号、组号、出口号、正整数人数与非负清点分钟。
    **仅**已按固定路线抵达该出口（``fixed`` 且到达分钟不晚于当前分钟）或
    已由人工救援实际送达该出口（``rescued``，采用实际出口与实际送达分钟）
    的组可登记；清点分钟不得早于相应到达/送达分钟、不得晚于当前分钟。
    同一流水号同内容重试返回原记录（HTTP 200），异内容整次拒绝（409
    ``EXIT_COUNT_SERIAL_CONFLICT``）；同组累计人数不得超过组人数。
    登记不改编排、不产生修订。
    """
    conn = request.app.state.db
    async with request.app.state.lock:
        minute_now = await db.get_current_minute(conn)

        # 幂等：同一流水号同内容重试返回原记录；异内容整次拒绝
        existing = await db.load_exit_count(conn, payload.serial)
        if existing is not None:
            same = (existing["group_id"] == payload.group_id
                    and existing["exit_id"] == payload.exit_id
                    and existing["submitted_count"] == payload.count
                    and existing["counted_minute"] == payload.counted_minute)
            if same:
                record = await _exit_count_record_out(conn, payload.serial)
                return JSONResponse(status_code=200,
                                    content=record.model_dump())
            return JSONResponse(
                status_code=409,
                content={
                    "error": "EXIT_COUNT_SERIAL_CONFLICT",
                    "message": "流水号已登记且提交内容不一致，整次拒绝",
                    "conflicts": [{
                        "serial": payload.serial,
                        "group_id": payload.group_id,
                        "exit_id": payload.exit_id,
                        "count": payload.count,
                        "counted_minute": payload.counted_minute,
                        "current_minute": minute_now,
                        "reason": "SERIAL_CONTENT_MISMATCH",
                        "existing": {
                            "group_id": existing["group_id"],
                            "exit_id": existing["exit_id"],
                            "count": existing["submitted_count"],
                            "counted_minute": existing["counted_minute"]}}]})

        async with conn.execute(
                """SELECT size, status, exit_id, arrival_minute
                   FROM groups WHERE id=?""", (payload.group_id,)) as cur:
            group = await cur.fetchone()
        if group is None:
            raise HTTPException(422, f"人群组 {payload.group_id} 不存在")
        size, status, g_exit, g_arrival = group
        async with conn.execute("SELECT 1 FROM exits WHERE node_id=?",
                                (payload.exit_id,)) as cur:
            if await cur.fetchone() is None:
                raise HTTPException(
                    422, f"出口 {payload.exit_id} 不是当前场景的既有出口")

        def _conflict(reason: str, message: str,
                      extra: dict) -> JSONResponse:
            return JSONResponse(
                status_code=409,
                content={
                    "error": "EXIT_COUNT_NOT_ACCEPTABLE",
                    "message": message,
                    "conflicts": [{
                        "serial": payload.serial,
                        "group_id": payload.group_id,
                        "exit_id": payload.exit_id,
                        "count": payload.count,
                        "counted_minute": payload.counted_minute,
                        "current_minute": minute_now,
                        "reason": reason, **extra}]})

        # 仅已按固定路线抵达该出口，或已由人工救援实际送达该出口的组可登记
        anchor = await _arrival_anchor(conn, payload.group_id, status,
                                       g_exit, g_arrival)
        if anchor is None or anchor[2] > minute_now:
            return _conflict(
                "GROUP_NOT_ARRIVED",
                "该组尚未按固定路线抵达出口，也未由人工救援实际送达，"
                "不可登记清点，整次拒绝",
                {"status": status})
        basis, arrival_exit, arrival_minute = anchor
        if payload.exit_id != arrival_exit:
            return _conflict(
                "EXIT_MISMATCH",
                "清点出口与该组抵达/送达出口不符，整次拒绝",
                {"status": status, "arrival_basis": basis,
                 "expected_exit_id": arrival_exit})
        if payload.counted_minute < arrival_minute:
            return _conflict(
                "COUNTED_MINUTE_BEFORE_ARRIVAL",
                "清点分钟早于该组到达/送达分钟，整次拒绝",
                {"status": status, "arrival_basis": basis,
                 "arrival_minute": arrival_minute})
        if payload.counted_minute > minute_now:
            return _conflict(
                "COUNTED_MINUTE_IN_FUTURE",
                "清点分钟晚于当前分钟，整次拒绝",
                {"status": status})
        already = await db.group_counted_total(conn, payload.group_id)
        if already + payload.count > size:
            return _conflict(
                "COUNT_EXCEEDS_GROUP_SIZE",
                "同组累计清点人数将超过组人数，整次拒绝",
                {"status": status, "group_size": size,
                 "already_counted": already,
                 "submitted_count": payload.count})

        async with db.transaction(conn):
            await db.insert_exit_count(
                conn, serial=payload.serial, group_id=payload.group_id,
                exit_id=payload.exit_id, count=payload.count,
                counted_minute=payload.counted_minute,
                created_minute=minute_now)
        record = await _exit_count_record_out(conn, payload.serial)
        return JSONResponse(status_code=201, content=record.model_dump())


@app.post("/exit-counts/{serial}/correction", tags=["出口清点"],
          response_model=ExitCountRecordOut,
          responses={
              404: {"description": "清点流水号不存在"},
              409: {"description": "版本失配/对同一版本重复更正/更正后累计超额，"
                                   "整次拒绝"},
              422: {"description": "版本、人数或原因非法"}})
async def correct_exit_count(serial: str, payload: ExitCountCorrectionIn,
                             request: Request):
    """凭原流水号、原记录版本与非空原因，把一条清点更正为非负人数。

    更正保留原值与历史（每次更正追加一条历史记录，含原值、新值、原因与
    受理分钟），记录版本 +1，并按更正后的同组累计数复核组人数上限。
    版本失配（``VERSION_MISMATCH``）、对同一版本重复更正
    （``VERSION_ALREADY_CORRECTED``）或更正后累计超额
    （``COUNT_EXCEEDS_GROUP_SIZE``）均整次拒绝（409），记录与历史不变；
    未知流水号 404。更正不改编排、不产生修订。
    """
    conn = request.app.state.db
    async with request.app.state.lock:
        minute_now = await db.get_current_minute(conn)
        record = await db.load_exit_count(conn, serial)
        if record is None:
            raise HTTPException(404, f"清点流水号 {serial} 不存在")

        def _conflict(reason: str, message: str,
                      extra: dict) -> JSONResponse:
            return JSONResponse(
                status_code=409,
                content={
                    "error": "EXIT_COUNT_CORRECTION_NOT_ACCEPTABLE",
                    "message": message,
                    "conflicts": [{
                        "serial": serial, "group_id": record["group_id"],
                        "submitted_version": payload.version,
                        "current_version": record["version"],
                        "count": payload.count,
                        "current_minute": minute_now,
                        "reason": reason, **extra}]})

        if payload.version < record["version"]:
            return _conflict(
                "VERSION_ALREADY_CORRECTED",
                "该版本已被更正过（对同一版本重复更正），整次拒绝", {})
        if payload.version > record["version"]:
            return _conflict(
                "VERSION_MISMATCH",
                "提交版本与记录当前版本不符，整次拒绝", {})
        async with conn.execute("SELECT size FROM groups WHERE id=?",
                                (record["group_id"],)) as cur:
            size = int((await cur.fetchone())[0])
        already = await db.group_counted_total(conn, record["group_id"])
        new_total = already - record["counted"] + payload.count
        if new_total > size:
            return _conflict(
                "COUNT_EXCEEDS_GROUP_SIZE",
                "更正后同组累计清点人数将超过组人数，整次拒绝",
                {"group_size": size, "already_counted": already,
                 "corrected_count": payload.count})

        async with db.transaction(conn):
            await db.update_exit_count(
                conn, serial=serial, counted=payload.count,
                version=record["version"] + 1)
            await db.insert_exit_count_correction(
                conn, serial=serial, from_version=record["version"],
                to_version=record["version"] + 1,
                old_count=record["counted"], new_count=payload.count,
                reason=payload.reason, minute=minute_now)
        return await _exit_count_record_out(conn, serial)


@app.get("/exit-counts", tags=["出口清点"], response_model=ExitCountListOut)
async def list_exit_counts(request: Request):
    """按组列出全部清点台账：应到、已清点、未清点人数及逐条记录。

    已按固定路线抵达的组采用规划到达出口与分钟；人工救援送达的组采用
    实际出口与实际送达分钟（``actual_delivery``）；尚未抵达的组到达锚点
    为 null。场景重置清空全部清点记录。
    """
    conn = request.app.state.db
    async with request.app.state.lock:
        minute_now = await db.get_current_minute(conn)
        async with conn.execute(
                """SELECT id, size, status, exit_id, arrival_minute
                   FROM groups ORDER BY id""") as cur:
            rows = await cur.fetchall()
        groups = [await _build_group_tally(conn, row, minute_now)
                  for row in rows]
    return ExitCountListOut(current_minute=minute_now, groups=groups)


@app.get("/exit-counts/{group_id}", tags=["出口清点"],
         response_model=ExitCountGroupOut,
         responses={404: {"description": "人群组不存在"}})
async def get_group_exit_counts(group_id: int, request: Request):
    """按组查询清点台账：应到、已清点、未清点人数及逐条记录（含更正历史）。

    救援送达组采用实际出口与实际送达分钟作为到达锚点；未知组号 404。
    """
    conn = request.app.state.db
    async with request.app.state.lock:
        minute_now = await db.get_current_minute(conn)
        async with conn.execute(
                """SELECT id, size, status, exit_id, arrival_minute
                   FROM groups WHERE id=?""", (group_id,)) as cur:
            row = await cur.fetchone()
        if row is None:
            raise HTTPException(404, f"人群组 {group_id} 不存在")
        tally = await _build_group_tally(conn, row, minute_now)
    return ExitCountGroupOut(current_minute=minute_now,
                             **tally.model_dump())


# --------------------------------------------------------------------------
# 逐段实际通行核报（已出发组沿现行路线逐段上报实际进入/离开分钟）
# --------------------------------------------------------------------------

# 可受理逐段核报的组状态：已出发并保留路线的组。scheduled/pending/failed/
# blocked 为尚未出发（NOT_DEPARTED）；awaiting_rescue 已无余程，只能核报
# 已保留的通道前缀（其 seq 仍在 route_steps 中）。
_TRAVERSAL_DEPARTED_STATUSES = (
    "fixed", "awaiting_rescue", "rescuing", "rescued")


def _traversal_conflict(reason: str, message: str, payload,
                        minute_now: int, *, extra: dict | None = None
                        ) -> JSONResponse:
    """构造统一的逐段核报拒绝响应（HTTP 409 + error/message/conflicts）。"""
    conflict = {"report_id": payload.report_id,
                "group_id": payload.group_id, "seq": payload.seq,
                "edge_id": payload.edge_id,
                "enter_minute": payload.enter_minute,
                "exit_minute": payload.exit_minute,
                "current_minute": minute_now, "reason": reason}
    if extra:
        conflict.update(extra)
    return JSONResponse(
        status_code=409,
        content={"error": "TRAVERSAL_REPORT_NOT_ACCEPTABLE",
                 "message": message, "conflicts": [conflict]})


_TRAVERSAL_REJECTION_MESSAGES = {
    "NOT_DEPARTED":
        "该组尚未出发，逐段实际通行核报不受理，整次拒绝",
    "STEP_NOT_FOUND":
        "现行路线中不存在该序号的路段，整次拒绝（可能已被节点滞留改编，"
        "请按现行路线重新核报）",
    "EDGE_MISMATCH":
        "核报通道与该段现行路线通道不符，整次拒绝",
    "OUT_OF_ORDER":
        "未按路线顺序核报：前序路段尚未核报，整次拒绝",
    "DUPLICATE_SEQ":
        "该段已核报，同段重复报送整次拒绝",
    "ENTER_BEFORE_PREVIOUS_EXIT":
        "本段实际进入分钟早于前段实际离开分钟，时序不符，整次拒绝",
    "EXIT_IN_FUTURE":
        "实际离开分钟晚于当前分钟，整次拒绝",
}


async def _load_group_steps(conn: aiosqlite.Connection, group_id: int):
    """读取一组现行路线段（按 seq 升序）：seq/edge_id/from/to/enter/exit。"""
    async with conn.execute(
            """SELECT seq, edge_id, from_node, to_node, enter_minute,
                      exit_minute
               FROM route_steps WHERE group_id=? ORDER BY seq""",
            (group_id,)) as cur:
        return await cur.fetchall()


async def _apply_traversal_report(conn: aiosqlite.Connection,
                                  payload: TraversalReportIn):
    """受理一次逐段实际通行核报（调用方已持锁）。

    校验（任一不符整次拒绝、不留记录）：组已出发且仍保留现行路线段；
    段序号存在；核报通道与现行路线该段通道一致；沿路线顺序核报（前序段
    均已核报，同段不可重复）；本段进入不早于前段实际离开；离开不晚于
    当前分钟。同一报送号同内容重试返回原记录，同号异内容整次拒绝。
    核报仅留痕，不反向修改编排、占用与容量管制，也不产生修订。
    """
    minute_now = await db.get_current_minute(conn)

    # 幂等优先：同一报送号同内容重试返回原记录；异内容整次拒绝
    existing = await db.load_traversal_report(conn, payload.report_id)
    if existing is not None:
        same = (existing["group_id"] == payload.group_id
                and existing["seq"] == payload.seq
                and existing["edge_id"] == payload.edge_id
                and existing["enter_minute"] == payload.enter_minute
                and existing["exit_minute"] == payload.exit_minute)
        if same:
            return JSONResponse(
                status_code=200,
                content=(await _traversal_record_out(conn, payload.report_id)
                         ).model_dump())
        return JSONResponse(
            status_code=409,
            content={
                "error": "TRAVERSAL_REPORT_ID_CONFLICT",
                "message": "报送号已使用且提交内容不一致，整次拒绝",
                "conflicts": [{
                    "report_id": payload.report_id,
                    "reason": "REPORT_ID_CONTENT_MISMATCH",
                    "current_minute": minute_now,
                    "existing": {
                        "group_id": existing["group_id"],
                        "seq": existing["seq"],
                        "edge_id": existing["edge_id"],
                        "enter_minute": existing["enter_minute"],
                        "exit_minute": existing["exit_minute"]}}]})

    async with conn.execute(
            "SELECT status FROM groups WHERE id=?",
            (payload.group_id,)) as cur:
        group_row = await cur.fetchone()
    if group_row is None:
        raise HTTPException(422, f"人群组 {payload.group_id} 不存在")
    status = group_row[0]
    if status not in _TRAVERSAL_DEPARTED_STATUSES:
        return _traversal_conflict(
            "NOT_DEPARTED", _TRAVERSAL_REJECTION_MESSAGES["NOT_DEPARTED"],
            payload, minute_now, extra={"status": status})

    steps = await _load_group_steps(conn, payload.group_id)
    step_by_seq = {s[0]: s for s in steps}
    step = step_by_seq.get(payload.seq)
    if step is None:
        # 待救援/救援中/已救援组只有已走通道前缀，其后序号不存在；
        # fixed 组则可能因节点滞留改编使该序号对应别的段，按现行路线核对
        return _traversal_conflict(
            "STEP_NOT_FOUND", _TRAVERSAL_REJECTION_MESSAGES["STEP_NOT_FOUND"],
            payload, minute_now,
            extra={"status": status, "max_seq": steps[-1][0] if steps else None})
    _, planned_edge, from_node, to_node, plan_enter, plan_exit = step
    if planned_edge != payload.edge_id:
        return _traversal_conflict(
            "EDGE_MISMATCH", _TRAVERSAL_REJECTION_MESSAGES["EDGE_MISMATCH"],
            payload, minute_now,
            extra={"status": status, "expected_edge_id": planned_edge,
                   "from_node": from_node, "to_node": to_node})

    active_reports = {
        r["seq"]: r
        for r in await db.load_traversal_reports(
            conn, payload.group_id, active_only=True)}
    if payload.seq in active_reports:
        prior = active_reports[payload.seq]
        return _traversal_conflict(
            "DUPLICATE_SEQ", _TRAVERSAL_REJECTION_MESSAGES["DUPLICATE_SEQ"],
            payload, minute_now,
            extra={"existing_report_id": prior["report_id"],
                   "enter_minute": prior["enter_minute"],
                   "exit_minute": prior["exit_minute"]})
    # 沿路线顺序：前序段（seq-1）须已核报；不允许跳段
    if payload.seq > 0 and payload.seq - 1 not in active_reports:
        return _traversal_conflict(
            "OUT_OF_ORDER", _TRAVERSAL_REJECTION_MESSAGES["OUT_OF_ORDER"],
            payload, minute_now,
            extra={"expected_seq": _next_expected_seq(active_reports)})
    # 后段进入不得早于前段离开
    if payload.seq > 0:
        prev = active_reports[payload.seq - 1]
        if payload.enter_minute < prev["exit_minute"]:
            return _traversal_conflict(
                "ENTER_BEFORE_PREVIOUS_EXIT",
                _TRAVERSAL_REJECTION_MESSAGES[
                    "ENTER_BEFORE_PREVIOUS_EXIT"],
                payload, minute_now,
                extra={"previous_report_id": prev["report_id"],
                       "previous_exit_minute": prev["exit_minute"]})
    # 实际离开不晚于当前分钟（进入早于离开由 pydantic 模型保证）
    if payload.exit_minute > minute_now:
        return _traversal_conflict(
            "EXIT_IN_FUTURE", _TRAVERSAL_REJECTION_MESSAGES["EXIT_IN_FUTURE"],
            payload, minute_now)

    async with db.transaction(conn):
        await db.insert_traversal_report(
            conn, report_id=payload.report_id, group_id=payload.group_id,
            seq=payload.seq, edge_id=payload.edge_id,
            enter_minute=payload.enter_minute,
            exit_minute=payload.exit_minute, reported_minute=minute_now)
    record = await _traversal_record_out(conn, payload.report_id)
    return JSONResponse(status_code=201, content=record.model_dump())


def _next_expected_seq(active_reports: dict[int, dict]) -> int:
    """下一个应核报的段序号：从 0 起首个未核报的序号。"""
    seq = 0
    while seq in active_reports:
        seq += 1
    return seq


async def _traversal_record_out(conn: aiosqlite.Connection,
                                report_id: str) -> TraversalRecordOut:
    rec = await db.load_traversal_report(conn, report_id)
    return TraversalRecordOut(**rec)


def _segments_intersect(a0: int, a1: int, b0: int, b1: int) -> bool:
    """左闭右开区间 [a0,a1) 与 [b0,b1) 是否相交。"""
    return a0 < b1 and b0 < a1


async def _build_traversal_edge_minutes(
        conn: aiosqlite.Connection,
        reports: list[dict]) -> tuple[list[TraversalEdgeMinuteOut], dict]:
    """按已核报（仍有效）实绩汇总通道逐分钟人数并标出异常。

    仅统计实际通行区间 [enter, exit) 内逐分钟的实绩人数（不看计划占用）：
    超过该分钟当时有效容量（容量管制时为 min(原容量, 管制上限)）标
    OVER_CAPACITY；实绩区间命中通道封闭的分钟标 CLOSED。返回逐分钟列表
    （按通道、分钟升序）与 (edge_id, minute) -> anomalies 映射，供段级
    异常标记复用。
    """
    async with conn.execute(
            "SELECT id, capacity FROM edges") as cur:
        base_caps = {eid: cap for eid, cap in await cur.fetchall()}
    controls: dict[int, list[tuple[int, int, int]]] = {}
    async with conn.execute(
            "SELECT edge_id, start_minute, end_minute, capacity "
            "FROM capacity_controls ORDER BY edge_id, start_minute") as cur:
        for eid, a, b, cap in await cur.fetchall():
            controls.setdefault(eid, []).append((a, b, cap))
    closures: dict[int, list[tuple[int, int]]] = {}
    async with conn.execute(
            "SELECT edge_id, start_minute, end_minute FROM closures") as cur:
        for eid, a, b in await cur.fetchall():
            closures.setdefault(eid, []).append((a, b))
    async with conn.execute("SELECT id, size FROM groups") as cur:
        sizes = {gid: size for gid, size in await cur.fetchall()}

    per_minute: dict[tuple[int, int], dict] = {}
    for rec in reports:
        if not rec["active"]:
            continue
        eid, size = rec["edge_id"], sizes.get(rec["group_id"], 0)
        for minute in range(rec["enter_minute"], rec["exit_minute"]):
            slot = per_minute.setdefault(
                (eid, minute),
                {"groups": {}, "closed": False})
            slot["groups"][rec["group_id"]] = size
            for a, b in closures.get(eid, ()):
                if a <= minute < b:
                    slot["closed"] = True
                    break

    minutes_out: list[TraversalEdgeMinuteOut] = []
    anomaly_map: dict[tuple[int, int], list[str]] = {}
    for eid, minute in sorted(per_minute):
        slot = per_minute[(eid, minute)]
        used = sum(slot["groups"].values())
        cap = effective_capacity(
            base_caps.get(eid, 0), controls.get(eid, ()), minute)
        anomalies = []
        if used > cap:
            anomalies.append("OVER_CAPACITY")
        if slot["closed"]:
            anomalies.append("CLOSED")
        anomaly_map[(eid, minute)] = anomalies
        minutes_out.append(TraversalEdgeMinuteOut(
            edge_id=eid, minute=minute, used=used,
            effective_capacity=cap,
            groups=[{"group_id": gid, "size": gsize}
                    for gid, gsize in sorted(slot["groups"].items())],
            anomalies=anomalies))
    return minutes_out, anomaly_map


async def _build_traversal_group(
        conn: aiosqlite.Connection, group_id: int, status: str, size: int,
        prefix_step_count: int | None,
        anomaly_map: dict[tuple[int, int], list[str]]
        ) -> TraversalGroupOut:
    """组装单组的计划/实绩并列段、未核报段与全部核报记录（含失效留痕）。"""
    steps = await _load_group_steps(conn, group_id)
    all_reports = await db.load_traversal_reports(conn, group_id)
    active_by_seq = {r["seq"]: r for r in all_reports if r["active"]}
    step_out: list[TraversalActualStepOut] = []
    for seq, eid, fnode, tnode, p_enter, p_exit in steps:
        rec = active_by_seq.get(seq)
        anomalies = []
        if rec is not None:
            hit_minutes = set()
            for minute in range(rec["enter_minute"], rec["exit_minute"]):
                for code in anomaly_map.get((eid, minute), ()):
                    hit_minutes.add(code)
            anomalies = sorted(hit_minutes)
        step_out.append(TraversalActualStepOut(
            seq=seq, edge_id=eid, from_node=fnode, to_node=tnode,
            planned_enter_minute=p_enter, planned_exit_minute=p_exit,
            actual_enter_minute=(rec or {}).get("enter_minute"),
            actual_exit_minute=(rec or {}).get("exit_minute"),
            report_id=(rec or {}).get("report_id"),
            deviates=(rec is not None and (
                rec["enter_minute"] != p_enter
                or rec["exit_minute"] != p_exit)),
            anomalies=anomalies))
    unreported = [s[0] for s in steps if s[0] not in active_by_seq]
    # 未出发组尚不可核报：不把计划段列为未核报段（其路线仍可能整体重排）
    if status not in _TRAVERSAL_DEPARTED_STATUSES:
        unreported = []
    next_seq = unreported[0] if unreported else None
    return TraversalGroupOut(
        group_id=group_id, status=status, size=size,
        prefix_step_count=prefix_step_count,
        steps=step_out, unreported_seqs=unreported, next_seq=next_seq,
        reports=[TraversalRecordOut(**r) for r in all_reports if r["active"]],
        detached_reports=[TraversalRecordOut(**r)
                          for r in all_reports if not r["active"]])


async def _build_traversal_response(conn: aiosqlite.Connection,
                                   group_id: int | None = None):
    """组装逐段核报查询：各段计划/实绩并列、未核报段与通道逐分钟实绩汇总。"""
    minute_now = await db.get_current_minute(conn)
    prefixes = await db.load_delay_prefixes(conn)
    active_reports = await db.load_traversal_reports(conn, active_only=True)
    edge_minutes, anomaly_map = await _build_traversal_edge_minutes(
        conn, active_reports)

    async with conn.execute(
            "SELECT id, status, size FROM groups "
            "ORDER BY priority DESC, earliest_departure ASC, id ASC") as cur:
        group_rows = await cur.fetchall()
    groups_out: list[TraversalGroupOut] = []
    for gid, status, size in group_rows:
        if group_id is not None and gid != group_id:
            continue
        groups_out.append(await _build_traversal_group(
            conn, gid, status, size, prefixes.get(gid),
            anomaly_map))

    if group_id is not None:
        # 单组视图：edge_minutes 只保留与该组现行路线及实绩相关的通道分钟
        related_edges = {s.edge_id for g in groups_out for s in g.steps}
        related_edges |= {r["edge_id"] for r in active_reports
                          if r["group_id"] == group_id}
        group_minutes = [m for m in edge_minutes
                         if m.edge_id in related_edges]
        return TraversalGroupDetailOut(
            current_minute=minute_now, **groups_out[0].model_dump(),
            edge_minutes=group_minutes)
    return TraversalReportListOut(
        current_minute=minute_now, groups=groups_out,
        edge_minutes=edge_minutes)


@app.post("/traversal-reports", tags=["逐段通行核报"],
          response_model=TraversalRecordOut, status_code=201,
          responses={
              200: {"description": "同报送号同内容的幂等重试，返回原记录"},
              409: {"description": "报送号异内容/组未出发/段不存在/通道不符/"
                                   "越序/同段重复/时序不符，整次拒绝"},
              422: {"description": "报送号为空、分钟非法（负或进入不早于离开）"
                                   "或组不存在"}})
async def submit_traversal_report(payload: TraversalReportIn, request: Request):
    """现场核报已出发组沿现行路线某段的**实际**进入/离开分钟。

    每次提交唯一报送号、组号、现行路线段序号（从 0 起）与通道号、实际
    进入与离开分钟：分钟非负、进入早于离开且离开不晚于当前分钟；同组须
    沿路线顺序核报，后段进入不得早于前段实际离开。路线段不存在
    （``STEP_NOT_FOUND``）、通道不符（``EDGE_MISMATCH``）或越序
    （``OUT_OF_ORDER``；同段重复为 ``DUPLICATE_SEQ``）时报错且不留记录；
    同一报送号同内容重试返回原记录（HTTP 200），同号异内容或同段重复
    报送拒绝（409）。

    节点滞留改编后，已核报的固定前缀仍可查（继续参与并列展示与汇总），
    被释放余程段的旧核报标记失效留痕，后续按新余程重新核报；人工救援
    （救援中/已送达）组只核报其已有的通道前缀。偏离计划时刻、命中封闭或
    实绩超当时有效容量均如实留痕并在查询中标出异常，**不**反向修改编排、
    通道占用、容量管制或修订。场景重置清空本场全部实绩。
    """
    conn = request.app.state.db
    async with request.app.state.lock:
        return await _apply_traversal_report(conn, payload)


@app.get("/traversal-reports", tags=["逐段通行核报"],
         response_model=TraversalReportListOut)
async def list_traversal_reports(request: Request):
    """并列各段计划与实绩时刻、列出未核报段，并按已核报实绩汇总通道逐分钟
    人数，标出超出当时有效容量（``OVER_CAPACITY``）或命中封闭（``CLOSED``）
    的异常分钟。只统计仍对应现行路线的有效核报；节点滞留改编失效的历史
    核报在各组 ``detached_reports`` 中留痕。"""
    conn = request.app.state.db
    async with request.app.state.lock:
        return await _build_traversal_response(conn)


@app.get("/traversal-reports/{group_id}", tags=["逐段通行核报"],
         response_model=TraversalGroupDetailOut,
         responses={404: {"description": "人群组不存在"}})
async def get_group_traversal_reports(group_id: int, request: Request):
    """单组逐段核报台账：各段计划/实绩时刻并列、未核报段、全部核报记录
    （含失效留痕）及与该组相关通道的逐分钟实绩人数与异常。"""
    conn = request.app.state.db
    async with request.app.state.lock:
        async with conn.execute("SELECT 1 FROM groups WHERE id=?",
                                (group_id,)) as cur:
            if await cur.fetchone() is None:
                raise HTTPException(404, f"人群组 {group_id} 不存在")
        return await _build_traversal_response(conn, group_id=group_id)


@app.post("/tick", tags=["时间"], response_model=PlanResponse)
async def advance_minute(request: Request, payload: TickIn | None = None):
    """推进当前分钟（默认 +1，可传 {"minutes": n}）。

    推进时，旧计划中出发分钟已过（< 新当前分钟）的组视为已经出发，
    路线与占用立即固定、不参与重排；其余未出发组再按新当前分钟整体重排
    （新计划允许在当前分钟立即出发）。先导前置未满足（blocked）的组仍属
    未出发，在先导到达锚点可用后随重排转正。被现场报告置为待救援
    （awaiting_rescue）的组、已交接人工救援队（rescuing）与已由人工结案
    送达（rescued）的组均不参与重排：待救援组只能由新的节点滞留报告携带
    新的恢复分钟重新受理；救援中的组由人工救援队结案核销，时间推进不会
    自动为其寻找余程。
    """
    conn = request.app.state.db
    delta = payload.minutes if payload is not None else 1

    async with request.app.state.lock:
        minute_now = await db.get_current_minute(conn) + delta
        before = await _build_plan_response(conn)
        async with db.transaction(conn):
            await db.set_current_minute(conn, minute_now)
            # 1) 旧计划中已经出发的组立即固定，不参与本次重排
            await conn.execute(
                """UPDATE groups SET status='fixed'
                   WHERE status='scheduled' AND departure_minute < ?""",
                (minute_now,))
            # 2) 其余未出发组以当前分钟为最早出发重新安排
            await _replan_pending(conn, in_transaction=True)
            after = await _build_plan_response(conn)
            await db.insert_revision(conn, "tick", {"minutes": delta},
                                     _plan_dump(before), _plan_dump(after))
        return after


@app.get("/plan", tags=["查询"], response_model=PlanResponse)
async def get_plan(request: Request):
    """查询当前编排：每组路线、出发/到达分钟或无法安排的原因。"""
    async with request.app.state.lock:
        return await _build_plan_response(request.app.state.db)


@app.get("/passages/{edge_id}/load", tags=["查询"])
async def get_edge_load(edge_id: int, request: Request,
                        minute: int | None = None):
    """查询通道逐分钟占用与每分钟有效上限，便于核验容量；可加 ?minute=n 只看某一分钟。

    每分钟的有效上限：容量管制时段内为 min(原容量, 管制上限)，时段外为
    原容量。管制覆盖的分钟即使无占用也会列出（used=0），响应顶层同时
    给出该通道当前生效的管制时段列表。
    """
    conn = request.app.state.db
    async with conn.execute("SELECT 1 FROM edges WHERE id=?", (edge_id,)) as cur:
        if await cur.fetchone() is None:
            raise HTTPException(404, f"通道 {edge_id} 不存在")
    async with request.app.state.lock:
        async with conn.execute(
                """SELECT rs.enter_minute, rs.exit_minute, g.size, g.status, g.id
                   FROM route_steps rs JOIN groups g ON g.id=rs.group_id
                   WHERE rs.edge_id=?""", (edge_id,)) as cur:
            rows = await cur.fetchall()
        per_minute: dict[int, dict] = {}
        for enter_m, exit_m, size, status, gid in rows:
            for m in range(enter_m, exit_m):
                slot = per_minute.setdefault(
                    m, {"minute": m, "used": 0, "groups": []})
                slot["used"] += size
                slot["groups"].append(
                    {"group_id": gid, "size": size, "status": status})
        async with conn.execute(
                "SELECT capacity, minutes FROM edges WHERE id=?", (edge_id,)) as cur:
            cap_row = await cur.fetchone()
        base_cap = cap_row[0]
        async with conn.execute(
                """SELECT start_minute, end_minute, capacity
                   FROM capacity_controls WHERE edge_id=?
                   ORDER BY start_minute""", (edge_id,)) as cur:
            control_rows = await cur.fetchall()
        controls = [(a, b, cap) for a, b, cap in control_rows]
        # 管制覆盖的分钟即使无占用也列出，便于核对管制期间的有效上限
        for a, b, _cap in controls:
            for m in range(a, b):
                per_minute.setdefault(m, {"minute": m, "used": 0, "groups": []})
        slots = []
        for m in sorted(per_minute):
            if minute is not None and m != minute:
                continue
            slot = per_minute[m]
            slot["effective_capacity"] = effective_capacity(
                base_cap, controls, m)
            slots.append(slot)
        return {"edge_id": edge_id, "capacity": base_cap,
                "traversal_minutes": cap_row[1],
                "capacity_controls": [
                    {"edge_id": edge_id, "start_minute": a, "end_minute": b,
                     "capacity": cap} for a, b, cap in controls],
                "minutes": slots}


@app.get("/revisions", tags=["追溯"], response_model=RevisionListOut)
async def list_revisions(request: Request):
    """列出全部修订（按修订号升序）。

    修订号全局递增、按实际提交顺序编号；场景重置与服务重启后均保留。
    """
    async with request.app.state.lock:
        rows = await db.list_revisions(request.app.state.db)
    return RevisionListOut(
        revisions=[RevisionSummaryOut(**row) for row in rows])


@app.get("/revisions/{rev}", tags=["追溯"], response_model=RevisionDetailOut)
async def get_revision(rev: int, request: Request):
    """按修订号查询当时的编排快照与各组前后差异。

    快照为修订提交时落库的完整编排（含当时固定组状态与逐段通行时刻），
    原样返回、不按当前场景重算；未知修订号返回 404。
    """
    async with request.app.state.lock:
        row = await db.load_revision(request.app.state.db, rev)
    if row is None:
        raise HTTPException(404, f"修订号 {rev} 不存在")
    before = PlanResponse.model_validate(row["plan_before"])
    after = PlanResponse.model_validate(row["plan_after"])
    return RevisionDetailOut(
        rev=row["rev"], operation=row["operation"], minute=row["minute"],
        detail=row["detail"], snapshot=after,
        changes=_diff_plans(before, after))
