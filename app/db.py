"""SQLite 持久化层（经 aiosqlite 异步访问，服务层串行化所有变更）。"""
from __future__ import annotations
import json
from contextlib import asynccontextmanager

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS edges (
    id       INTEGER PRIMARY KEY,
    src      INTEGER NOT NULL,
    dst      INTEGER NOT NULL,
    minutes  INTEGER NOT NULL,
    capacity INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS exits (
    node_id INTEGER PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS closures (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    edge_id      INTEGER NOT NULL,
    start_minute INTEGER NOT NULL,
    end_minute   INTEGER NOT NULL
);

-- 容量管制时段（临时限流）：左闭右开 [start_minute, end_minute)，
-- 时段内该通道每分钟有效上限收紧为 capacity；同一通道的时段互不重叠
CREATE TABLE IF NOT EXISTS capacity_controls (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    edge_id      INTEGER NOT NULL,
    start_minute INTEGER NOT NULL,
    end_minute   INTEGER NOT NULL,
    capacity     INTEGER NOT NULL
);

-- 烟雾暴露时段：左闭右开 [start_minute, end_minute)，时段内该通道每分钟
-- 暴露值为非负整数 smoke_value；同一通道的时段互不重叠，未登记的分钟暴露
-- 值为零，节点等待不计暴露。
CREATE TABLE IF NOT EXISTS smoke_windows (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    edge_id      INTEGER NOT NULL,
    start_minute INTEGER NOT NULL,
    end_minute   INTEGER NOT NULL,
    smoke_value  INTEGER NOT NULL
);

-- 人群组非负累计暴露上限：仅登记过上限的组受烟雾暴露约束（其余无此行、
-- 不受约束）。同一组再次登记替换旧值（UPSERT 覆盖）。
CREATE TABLE IF NOT EXISTS group_smoke_limits (
    group_id     INTEGER PRIMARY KEY,
    smoke_limit  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS groups (
    id                 INTEGER PRIMARY KEY,
    size               INTEGER NOT NULL,
    origin             INTEGER NOT NULL,
    earliest_departure INTEGER NOT NULL,
    priority           INTEGER NOT NULL,
    deadline           INTEGER NOT NULL,
    -- pending | scheduled | fixed | failed | awaiting_rescue |
    -- rescuing | rescued
    status             TEXT NOT NULL,
    departure_minute   INTEGER,
    arrival_minute     INTEGER,
    exit_id            INTEGER,
    fail_reason        TEXT,
    fail_detail        TEXT
);

CREATE TABLE IF NOT EXISTS route_steps (
    group_id     INTEGER NOT NULL,
    seq          INTEGER NOT NULL,
    edge_id      INTEGER NOT NULL,
    from_node    INTEGER NOT NULL,
    to_node      INTEGER NOT NULL,
    enter_minute INTEGER NOT NULL,
    exit_minute  INTEGER NOT NULL,
    PRIMARY KEY (group_id, seq)
);

-- 现场节点滞留报告：每个被报告过的组最多一条（UPSERT 覆盖）。
-- prefix_step_count 为已走前缀步数：route_steps 中 seq < prefix_step_count
-- 的段落为报告时已经走完、固定保留的前缀；恢复后的余程从该 seq 续编。
-- 组随后恢复（fixed）或仍待救援（awaiting_rescue）均保留本行，使当前
-- 编排得以区分"已走前缀"与"新余程"。
CREATE TABLE IF NOT EXISTS delay_reports (
    group_id          INTEGER PRIMARY KEY,
    node_id           INTEGER NOT NULL,
    recovery_minute   INTEGER NOT NULL,
    reported_minute   INTEGER NOT NULL,
    prefix_step_count INTEGER NOT NULL
);

-- 先导关系（放行前置）：每个未出发组至多一项（UPSERT 覆盖）。
-- leader_group_id 为先导组编号，release_gap 为非负放行间隔：后继组的可
-- 出发分钟不早于自身最早出发分钟及"先导组规划到达分钟 + 间隔"（先导组
-- 已人工救援送达时改用实际送达分钟）。成环与自依赖由服务层在登记时拒绝。
CREATE TABLE IF NOT EXISTS group_prerequisites (
    group_id        INTEGER PRIMARY KEY,
    leader_group_id INTEGER NOT NULL,
    release_gap     INTEGER NOT NULL
);

-- 人工救援队：按唯一编号登记，capacity 为正整数载员量。
-- 队伍是否空闲由 rescue_tasks 中是否存在该队 in_progress 任务决定。
CREATE TABLE IF NOT EXISTS rescue_teams (
    id       INTEGER PRIMARY KEY,
    capacity INTEGER NOT NULL
);

-- 人工救援任务：派遣成功产生一条 in_progress 任务；结案核销后置为
-- completed。同组同队各只允许一项进行中任务（部分唯一索引兜底，
-- 常规判定由服务层在互斥锁内完成）。
CREATE TABLE IF NOT EXISTS rescue_tasks (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id        INTEGER NOT NULL,
    team_id         INTEGER NOT NULL,
    node_id         INTEGER NOT NULL,   -- 派遣时组的滞留节点
    observed_minute INTEGER NOT NULL,   -- 派遣提交时所见当前分钟
    dispatch_minute INTEGER NOT NULL,   -- 实际受理派遣的当前分钟
    status          TEXT NOT NULL,      -- in_progress | completed
    exit_id         INTEGER,            -- 结案：既有出口编号
    delivery_minute INTEGER             -- 结案：实际送达分钟
);

-- 同组同队各只允许一项进行中任务（并发抢占的数据库层兜底）
CREATE UNIQUE INDEX IF NOT EXISTS idx_rescue_tasks_open_group
    ON rescue_tasks(group_id) WHERE status='in_progress';
CREATE UNIQUE INDEX IF NOT EXISTS idx_rescue_tasks_open_team
    ON rescue_tasks(team_id) WHERE status='in_progress';

-- 出口清点记录：现场在出口按组清点的实际撤离人数。serial 为全库唯一
-- 流水号（幂等键）：同内容重试返回原记录，异内容拒绝。submitted_count 为
-- 首次提交人数（幂等比对用，不随更正改变）；counted 为当前生效人数
-- （更正后更新）；version 自 1 起，每次更正 +1。
CREATE TABLE IF NOT EXISTS exit_counts (
    serial          TEXT PRIMARY KEY,
    group_id        INTEGER NOT NULL,
    exit_id         INTEGER NOT NULL,
    submitted_count INTEGER NOT NULL,
    counted         INTEGER NOT NULL,
    counted_minute  INTEGER NOT NULL,
    version         INTEGER NOT NULL,
    created_minute  INTEGER NOT NULL
);

-- 清点更正历史：每次更正追加一条（保留原值与新值、非空原因与受理分钟），
-- 记录本身只更新生效人数与版本，历史永不改写。
CREATE TABLE IF NOT EXISTS exit_count_corrections (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    serial       TEXT NOT NULL,
    from_version INTEGER NOT NULL,
    to_version   INTEGER NOT NULL,
    old_count    INTEGER NOT NULL,
    new_count    INTEGER NOT NULL,
    reason       TEXT NOT NULL,
    minute       INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_exit_count_corrections_serial
    ON exit_count_corrections(serial);

-- 逐段实际通行核报：现场对已出发组沿现行路线逐段上报的实际进入/离开分钟。
-- report_id 为全库唯一报送号（幂等键）：同号同内容重试返回原记录，异内容
-- 拒绝。(group_id, seq) 对"仍有效（active=1）"记录唯一：同段重复报送拒绝；
-- 节点滞留改编释放某段时把对应记录置 active=0（历史留痕、可查，不参与逐
-- 分钟汇总与计划/实绩并列），改编后的新余程可重新占用同一 seq。
CREATE TABLE IF NOT EXISTS traversal_reports (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    report_id       TEXT NOT NULL UNIQUE,
    group_id        INTEGER NOT NULL,
    seq             INTEGER NOT NULL,
    edge_id         INTEGER NOT NULL,
    enter_minute    INTEGER NOT NULL,
    exit_minute     INTEGER NOT NULL,
    reported_minute INTEGER NOT NULL,
    active          INTEGER NOT NULL DEFAULT 1,
    detached_minute INTEGER,
    detach_reason   TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_traversal_reports_active_step
    ON traversal_reports(group_id, seq) WHERE active=1;

-- 修订历史：每次成功的编排变更（场景重置/新增组/纠正组/登记封闭/
-- 登记容量管制/现场节点滞留报告/推进时间）与编排变更在同一事务内写入；
-- 快照为提交时刻的完整编排 JSON，落库后不再改动、查询时不按当前场景重算。
-- 场景重置（reset_db）与服务重启均不影响本表。
CREATE TABLE IF NOT EXISTS revisions (
    rev         INTEGER PRIMARY KEY,  -- 全局递增修订号（按实际提交顺序）
    operation   TEXT NOT NULL,        -- scenario | groups | group-corrections |
                                      -- closures | capacity-controls |
                                      -- group-prerequisites |
                                      -- delay-reports | rescue-dispatches |
                                      -- rescue-completions | tick
    detail      TEXT NOT NULL,        -- JSON：触发操作的参数摘要
    minute      INTEGER NOT NULL,     -- 该修订提交后的当前分钟
    plan_before TEXT NOT NULL,        -- JSON：提交前完整编排（PlanResponse）
    plan_after  TEXT NOT NULL         -- JSON：提交后完整编排（PlanResponse）
);
"""


async def init_db(db: aiosqlite.Connection) -> None:
    await db.executescript(SCHEMA)
    await db.execute(
        "INSERT OR IGNORE INTO meta(key, value) VALUES('current_minute', 0)")
    await db.commit()


async def reset_db(db: aiosqlite.Connection) -> None:
    """清空全部业务表并归零当前分钟（调用方负责事务）。

    修订历史（revisions 表）不在清空之列：场景重置后旧修订仍可查询，
    修订号继续全局递增。
    """
    # 人工救援队与其救援任务随场景重置清空；先导关系与出口清点记录（含更正
    # 历史）亦清空；逐段实际通行核报记录同样清空；本场烟雾暴露时段与各组
    # 暴露上限一并清空；修订历史不在清空之列
    for table in ("rescue_tasks", "rescue_teams",
                  "delay_reports", "group_prerequisites",
                  "exit_count_corrections", "exit_counts",
                  "traversal_reports",
                  "route_steps", "groups", "closures",
                  "capacity_controls", "smoke_windows",
                  "group_smoke_limits", "exits", "edges", "meta"):
        await db.execute(f"DELETE FROM {table}")
    await db.execute(
        "INSERT INTO meta(key, value) VALUES('current_minute', 0)")


@asynccontextmanager
async def transaction(db: aiosqlite.Connection):
    """显式事务：连接以 autocommit 打开，提交/回滚由此处统一管理。"""
    await db.execute("BEGIN")
    try:
        yield
        await db.execute("COMMIT")
    except Exception:
        await db.execute("ROLLBACK")
        raise


async def get_current_minute(db: aiosqlite.Connection) -> int:
    async with db.execute(
            "SELECT value FROM meta WHERE key='current_minute'") as cur:
        row = await cur.fetchone()
    return int(row[0])


async def set_current_minute(db: aiosqlite.Connection, minute: int) -> None:
    await db.execute(
        "UPDATE meta SET value=? WHERE key='current_minute'", (minute,))


async def load_world(db: aiosqlite.Connection):
    """加载构造规划世界所需的全部静态/动态数据。"""
    from .planner import Edge, World

    edges: dict[int, Edge] = {}
    async with db.execute(
            "SELECT id, src, dst, minutes, capacity FROM edges") as cur:
        for row in await cur.fetchall():
            e = Edge(id=row[0], src=row[1], dst=row[2],
                     minutes=row[3], capacity=row[4])
            edges[e.id] = e

    adjacency: dict[int, list[int]] = {}
    for e in edges.values():
        adjacency.setdefault(e.src, []).append(e.id)
    for ids in adjacency.values():
        ids.sort()  # 按通道编号升序扩展

    async with db.execute("SELECT node_id FROM exits ORDER BY node_id") as cur:
        exits = [row[0] for row in await cur.fetchall()]

    closures: dict[int, list[tuple[int, int]]] = {}
    async with db.execute(
            "SELECT edge_id, start_minute, end_minute FROM closures") as cur:
        for edge_id, a, b in await cur.fetchall():
            closures.setdefault(edge_id, []).append((a, b))
    for segs in closures.values():
        segs.sort()

    capacity_controls: dict[int, list[tuple[int, int, int]]] = {}
    async with db.execute(
            """SELECT edge_id, start_minute, end_minute, capacity
               FROM capacity_controls
               ORDER BY edge_id, start_minute""") as cur:
        for edge_id, a, b, cap in await cur.fetchall():
            capacity_controls.setdefault(edge_id, []).append((a, b, cap))

    smoke_windows: dict[int, list[tuple[int, int, int]]] = {}
    async with db.execute(
            """SELECT edge_id, start_minute, end_minute, smoke_value
               FROM smoke_windows
               ORDER BY edge_id, start_minute""") as cur:
        for edge_id, a, b, value in await cur.fetchall():
            smoke_windows.setdefault(edge_id, []).append((a, b, value))

    # 已固定（已出发）组的占用：仅计当前分钟起尚未走完的部分
    minute_now = await get_current_minute(db)
    fixed_load: dict[tuple[int, int], int] = {}
    async with db.execute(
            """
            SELECT rs.edge_id, rs.enter_minute, rs.exit_minute, g.size
            FROM route_steps rs JOIN groups g ON g.id = rs.group_id
            WHERE g.status='fixed'
            """) as cur:
        for edge_id, enter_m, exit_m, size in await cur.fetchall():
            for minute in range(max(enter_m, minute_now), exit_m):
                key = (edge_id, minute)
                fixed_load[key] = fixed_load.get(key, 0) + size

    world = World(edges=edges, adjacency=adjacency, exits=exits,
                  closures=closures, fixed_load=fixed_load,
                  capacity_controls=capacity_controls,
                  smoke_windows=smoke_windows)

    async with db.execute(
            """SELECT id, size, origin, earliest_departure, priority,
                      deadline, status
               FROM groups WHERE status IN
                   ('scheduled', 'failed', 'pending', 'blocked')
               ORDER BY priority DESC, earliest_departure ASC, id ASC"""
            ) as cur:
        pending = [dict(zip(
            ("id", "size", "origin", "earliest_departure",
             "priority", "deadline", "status"),
            row)) for row in await cur.fetchall()]

    # 全部组当前状态 + 先导到达锚点（规划到达 / 人工实际送达）。未出发组
    # 的先导若是 scheduled/failed/blocked 或 pending，会在本轮多轮安排中
    # 先导落定后再就绪；这里只预置已不会在本轮变动的锚点。
    prerequisites = await load_prerequisites(db)
    smoke_limits = await load_smoke_limits(db)
    async with db.execute(
            """SELECT g.id, g.status, g.arrival_minute,
                      rt.delivery_minute
               FROM groups g
               LEFT JOIN rescue_tasks rt
                      ON rt.group_id = g.id AND rt.status='completed'"""
            ) as cur:
        states: dict[int, dict] = {}
        for gid, status, arrival, delivery in await cur.fetchall():
            states[gid] = {"status": status,
                           "arrival_minute": arrival,
                           "delivery_minute": delivery}

    return world, pending, prerequisites, states, smoke_limits


# --------------------------------------------------------------------------
# 烟雾暴露时段与各组累计暴露上限
# --------------------------------------------------------------------------

async def insert_smoke_window(db: aiosqlite.Connection, *, edge_id: int,
                              start_minute: int, end_minute: int,
                              smoke_value: int) -> None:
    """登记一条烟雾暴露时段（调用方负责事务与全部校验）。"""
    await db.execute(
        """INSERT INTO smoke_windows
           (edge_id, start_minute, end_minute, smoke_value)
           VALUES(?,?,?,?)""",
        (edge_id, start_minute, end_minute, smoke_value))


async def load_smoke_windows(db: aiosqlite.Connection
                             ) -> list[dict]:
    """按通道、开始分钟升序列出全部烟雾暴露时段。"""
    async with db.execute(
            """SELECT edge_id, start_minute, end_minute, smoke_value
               FROM smoke_windows
               ORDER BY edge_id, start_minute""") as cur:
        return [{"edge_id": eid, "start_minute": a, "end_minute": b,
                 "smoke_value": value}
                for eid, a, b, value in await cur.fetchall()]


async def load_smoke_limits(db: aiosqlite.Connection) -> dict[int, int]:
    """返回 group_id -> 非负累计暴露上限（仅登记过上限的组）。"""
    async with db.execute(
            "SELECT group_id, smoke_limit FROM group_smoke_limits") as cur:
        return {gid: int(limit) for gid, limit in await cur.fetchall()}


async def upsert_smoke_limit(db: aiosqlite.Connection, *, group_id: int,
                             smoke_limit: int) -> None:
    """登记/替换一组的累计暴露上限（调用方负责事务与全部校验）。"""
    await db.execute(
        """INSERT INTO group_smoke_limits(group_id, smoke_limit)
           VALUES(?,?)
           ON CONFLICT(group_id) DO UPDATE SET
               smoke_limit=excluded.smoke_limit""",
        (group_id, smoke_limit))


async def load_fixed_routes(db: aiosqlite.Connection):
    """返回已固定组的路线段及当前分钟（用于封闭冲突检查）。

    封闭只可能触及“剩余路线”：进入分钟早于当前分钟的占用部分已经过去，
    剩余占用区间取 [max(enter_minute, 当前分钟), exit_minute)。
    """
    minute_now = await get_current_minute(db)
    async with db.execute(
            """SELECT g.id, rs.edge_id, rs.enter_minute, rs.exit_minute
               FROM route_steps rs JOIN groups g ON g.id = rs.group_id
               WHERE g.status='fixed'""") as cur:
        rows = await cur.fetchall()
    return minute_now, rows


async def persist_replan(db: aiosqlite.Connection, results: list[dict],
                         minute_now: int) -> None:
    """整体替换所有待出发组的规划结果（调用方负责开启事务）。

    先删后插保证不会出现"部分重排"：事务提交前任何读取看到的都是旧状态。
    """
    pending_ids = [r["group_id"] for r in results]
    if pending_ids:
        placeholders = ",".join("?" for _ in pending_ids)
        await db.execute(
            f"DELETE FROM route_steps WHERE group_id IN ({placeholders})",
            pending_ids)
        await db.execute(
            f"""UPDATE groups SET status='pending', departure_minute=NULL,
                arrival_minute=NULL, exit_id=NULL,
                fail_reason=NULL, fail_detail=NULL
                WHERE id IN ({placeholders})""", pending_ids)
    for r in results:
        gid = r["group_id"]
        if r["status"] == "scheduled":
            await db.execute(
                """UPDATE groups SET status='scheduled',
                   departure_minute=?, arrival_minute=?, exit_id=?,
                   fail_reason=NULL, fail_detail=NULL WHERE id=?""",
                (r["departure_minute"], r["arrival_minute"],
                 r["exit_id"], gid))
            for seq, step in enumerate(r["steps"]):
                await db.execute(
                    """INSERT INTO route_steps
                       (group_id, seq, edge_id, from_node, to_node,
                        enter_minute, exit_minute)
                       VALUES (?,?,?,?,?,?,?)""",
                    (gid, seq, step["edge_id"], step["from_node"],
                     step["to_node"], step["enter_minute"],
                     step["exit_minute"]))
        else:
            status = r.get("status", "failed")
            if status == "blocked":
                # 前置未满足：不写入任何路线/占用，原因说明前置状态
                await db.execute(
                    """UPDATE groups SET status='blocked',
                       departure_minute=NULL, arrival_minute=NULL, exit_id=NULL,
                       fail_reason=?, fail_detail=? WHERE id=?""",
                    (r["reason"], r["detail"], gid))
            else:
                await db.execute(
                    """UPDATE groups SET status='failed',
                       departure_minute=NULL, arrival_minute=NULL, exit_id=NULL,
                       fail_reason=?, fail_detail=? WHERE id=?""",
                    (r["reason"], r["detail"], gid))


# --------------------------------------------------------------------------
# 现场节点滞留报告
# --------------------------------------------------------------------------

async def load_delay_prefixes(db: aiosqlite.Connection) -> dict[int, int]:
    """返回 group_id -> 已走前缀步数（仅被报告过的组）。"""
    async with db.execute(
            "SELECT group_id, prefix_step_count FROM delay_reports") as cur:
        return {gid: k for gid, k in await cur.fetchall()}


async def load_delay_report(db: aiosqlite.Connection,
                            group_id: int) -> dict | None:
    async with db.execute(
            """SELECT group_id, node_id, recovery_minute, reported_minute,
                      prefix_step_count
               FROM delay_reports WHERE group_id=?""", (group_id,)) as cur:
        row = await cur.fetchone()
    if row is None:
        return None
    return {"group_id": row[0], "node_id": row[1],
            "recovery_minute": row[2], "reported_minute": row[3],
            "prefix_step_count": row[4]}


async def upsert_delay_report(db: aiosqlite.Connection, *, group_id: int,
                              node_id: int, recovery_minute: int,
                              reported_minute: int,
                              prefix_step_count: int) -> None:
    """登记/覆盖一组的现场滞留报告（调用方负责事务）。"""
    await db.execute(
        """INSERT INTO delay_reports(group_id, node_id, recovery_minute,
                                     reported_minute, prefix_step_count)
           VALUES(?,?,?,?,?)
           ON CONFLICT(group_id) DO UPDATE SET
               node_id=excluded.node_id,
               recovery_minute=excluded.recovery_minute,
               reported_minute=excluded.reported_minute,
               prefix_step_count=excluded.prefix_step_count""",
        (group_id, node_id, recovery_minute, reported_minute,
         prefix_step_count))


# --------------------------------------------------------------------------
# 先导关系（放行前置）
# --------------------------------------------------------------------------

async def load_prerequisites(db: aiosqlite.Connection
                             ) -> dict[int, dict]:
    """返回 group_id -> {"leader_group_id", "release_gap"}（每组至多一项）。"""
    async with db.execute(
            "SELECT group_id, leader_group_id, release_gap "
            "FROM group_prerequisites") as cur:
        return {gid: {"leader_group_id": leader, "release_gap": gap}
                for gid, leader, gap in await cur.fetchall()}


async def upsert_prerequisite(db: aiosqlite.Connection, *, group_id: int,
                              leader_group_id: int,
                              release_gap: int) -> None:
    """登记/替换一组的先导关系（调用方负责事务与全部校验）。"""
    await db.execute(
        """INSERT INTO group_prerequisites(group_id, leader_group_id,
                                           release_gap)
           VALUES(?,?,?)
           ON CONFLICT(group_id) DO UPDATE SET
               leader_group_id=excluded.leader_group_id,
               release_gap=excluded.release_gap""",
        (group_id, leader_group_id, release_gap))


# --------------------------------------------------------------------------
# 人工救援队与救援任务
# --------------------------------------------------------------------------

async def insert_rescue_team(db: aiosqlite.Connection, *, team_id: int,
                             capacity: int) -> None:
    """登记一支人工救援队（调用方负责事务与编号查重）。"""
    await db.execute(
        "INSERT INTO rescue_teams(id, capacity) VALUES(?,?)",
        (team_id, capacity))


async def load_rescue_teams(db: aiosqlite.Connection) -> list[dict]:
    """列出全部人工救援队，并附当前进行中任务编号（空闲为 null）。"""
    async with db.execute(
            """SELECT t.id, t.capacity, r.id AS task_id
               FROM rescue_teams t
               LEFT JOIN rescue_tasks r
                      ON r.team_id=t.id AND r.status='in_progress'
               ORDER BY t.id""") as cur:
        rows = await cur.fetchall()
    return [{"team_id": r[0], "capacity": r[1], "task_id": r[2]}
            for r in rows]


async def insert_rescue_task(db: aiosqlite.Connection, *, group_id: int,
                             team_id: int, node_id: int,
                             observed_minute: int,
                             dispatch_minute: int) -> int:
    """登记一条进行中救援任务（调用方负责事务与全部受理校验），返回任务号。"""
    async with db.execute(
            """INSERT INTO rescue_tasks
               (group_id, team_id, node_id, observed_minute, dispatch_minute,
                status)
               VALUES(?,?,?,?,?,'in_progress')""",
            (group_id, team_id, node_id, observed_minute,
             dispatch_minute)) as cur:
        return int(cur.lastrowid)


async def load_rescue_task(db: aiosqlite.Connection,
                           task_id: int) -> dict | None:
    async with db.execute(
            """SELECT id, group_id, team_id, node_id, observed_minute,
                      dispatch_minute, status, exit_id, delivery_minute
               FROM rescue_tasks WHERE id=?""", (task_id,)) as cur:
        row = await cur.fetchone()
    if row is None:
        return None
    return {"task_id": row[0], "group_id": row[1], "team_id": row[2],
            "node_id": row[3], "observed_minute": row[4],
            "dispatch_minute": row[5], "status": row[6],
            "exit_id": row[7], "delivery_minute": row[8]}


async def list_rescue_tasks(db: aiosqlite.Connection,
                            status: str | None = None) -> list[dict]:
    """按任务号升序列出救援任务，可按状态过滤。"""
    if status is None:
        query = ("SELECT id, group_id, team_id, node_id, observed_minute, "
                 "dispatch_minute, status, exit_id, delivery_minute "
                 "FROM rescue_tasks ORDER BY id")
        params: tuple = ()
    else:
        query = ("SELECT id, group_id, team_id, node_id, observed_minute, "
                 "dispatch_minute, status, exit_id, delivery_minute "
                 "FROM rescue_tasks WHERE status=? ORDER BY id")
        params = (status,)
    async with db.execute(query, params) as cur:
        rows = await cur.fetchall()
    return [{"task_id": r[0], "group_id": r[1], "team_id": r[2],
             "node_id": r[3], "observed_minute": r[4],
             "dispatch_minute": r[5], "status": r[6],
             "exit_id": r[7], "delivery_minute": r[8]} for r in rows]


async def load_open_rescue_task_for_group(db: aiosqlite.Connection,
                                          group_id: int) -> dict | None:
    """返回该组当前进行中的救援任务（无则 None）。"""
    async with db.execute(
            """SELECT id, team_id FROM rescue_tasks
               WHERE group_id=? AND status='in_progress'""",
            (group_id,)) as cur:
        row = await cur.fetchone()
    if row is None:
        return None
    return {"task_id": row[0], "team_id": row[1]}


async def complete_rescue_task(db: aiosqlite.Connection, *, task_id: int,
                               exit_id: int,
                               delivery_minute: int) -> None:
    """核销一条进行中救援任务（调用方负责事务与全部时序校验）。"""
    await db.execute(
        """UPDATE rescue_tasks SET status='completed', exit_id=?,
           delivery_minute=? WHERE id=?""",
        (exit_id, delivery_minute, task_id))


async def load_group_rescue_map(db: aiosqlite.Connection) -> dict[int, dict]:
    """返回 group_id -> 最新救援任务摘要（含完成后的实际出口与时刻）。

    每组至多一条进行中任务、完成后也不会再开新任务（组已救援），因此
    每组最多一行；该映射用于在 /plan 中单列人工救援信息。
    """
    async with db.execute(
            """SELECT group_id, id, team_id, status, exit_id,
                      delivery_minute
               FROM rescue_tasks""") as cur:
        rows = await cur.fetchall()
    return {
        row[0]: {"rescue_task_id": row[1], "rescue_team_id": row[2],
                 "rescue_status": row[3],
                 "rescued_exit_id": row[4],
                 "delivery_minute": row[5]}
        for row in rows}


# --------------------------------------------------------------------------
# 出口清点（按组清点实际撤离人数）
# --------------------------------------------------------------------------

async def insert_exit_count(db: aiosqlite.Connection, *, serial: str,
                            group_id: int, exit_id: int, count: int,
                            counted_minute: int, created_minute: int) -> None:
    """登记一条出口清点记录（调用方负责事务与全部受理校验）。

    初始版本为 1；submitted_count 与 counted 同置为提交人数，此后
    submitted_count 不再改变（幂等比对用），counted 随更正更新。
    """
    await db.execute(
        """INSERT INTO exit_counts
           (serial, group_id, exit_id, submitted_count, counted,
            counted_minute, version, created_minute)
           VALUES(?,?,?,?,?,?,1,?)""",
        (serial, group_id, exit_id, count, count, counted_minute,
         created_minute))


def _exit_count_row(row) -> dict:
    return {"serial": row[0], "group_id": row[1], "exit_id": row[2],
            "submitted_count": row[3], "counted": row[4],
            "counted_minute": row[5], "version": row[6],
            "created_minute": row[7]}


async def load_exit_count(db: aiosqlite.Connection,
                          serial: str) -> dict | None:
    """按流水号读取一条清点记录；不存在时返回 None。"""
    async with db.execute(
            """SELECT serial, group_id, exit_id, submitted_count, counted,
                      counted_minute, version, created_minute
               FROM exit_counts WHERE serial=?""", (serial,)) as cur:
        row = await cur.fetchone()
    return None if row is None else _exit_count_row(row)


async def load_exit_counts(db: aiosqlite.Connection,
                           group_id: int | None = None) -> list[dict]:
    """按登记先后列出清点记录；可限定单个组。"""
    query = ("SELECT serial, group_id, exit_id, submitted_count, counted,"
             " counted_minute, version, created_minute FROM exit_counts")
    params: tuple = ()
    if group_id is not None:
        query += " WHERE group_id=?"
        params = (group_id,)
    query += " ORDER BY rowid"
    async with db.execute(query, params) as cur:
        rows = await cur.fetchall()
    return [_exit_count_row(r) for r in rows]


async def group_counted_total(db: aiosqlite.Connection,
                              group_id: int) -> int:
    """该组当前生效人数的累计（更正后的口径）。"""
    async with db.execute(
            "SELECT COALESCE(SUM(counted), 0) FROM exit_counts "
            "WHERE group_id=?", (group_id,)) as cur:
        return int((await cur.fetchone())[0])


async def update_exit_count(db: aiosqlite.Connection, *, serial: str,
                            counted: int, version: int) -> None:
    """应用一次更正：更新生效人数与版本（调用方负责事务与全部校验）。"""
    await db.execute(
        "UPDATE exit_counts SET counted=?, version=? WHERE serial=?",
        (counted, version, serial))


async def insert_exit_count_correction(db: aiosqlite.Connection, *,
                                       serial: str, from_version: int,
                                       to_version: int, old_count: int,
                                       new_count: int, reason: str,
                                       minute: int) -> None:
    """追加一条更正历史（保留原值与新值；调用方负责事务）。"""
    await db.execute(
        """INSERT INTO exit_count_corrections
           (serial, from_version, to_version, old_count, new_count,
            reason, minute)
           VALUES(?,?,?,?,?,?,?)""",
        (serial, from_version, to_version, old_count, new_count,
         reason, minute))


async def load_exit_count_corrections(db: aiosqlite.Connection,
                                      serial: str) -> list[dict]:
    """按更正先后列出一条清点记录的更正历史（原值与新值均保留）。"""
    async with db.execute(
            """SELECT from_version, to_version, old_count, new_count,
                      reason, minute
               FROM exit_count_corrections WHERE serial=? ORDER BY id""",
            (serial,)) as cur:
        rows = await cur.fetchall()
    return [{"from_version": r[0], "to_version": r[1], "old_count": r[2],
             "new_count": r[3], "reason": r[4], "minute": r[5]}
            for r in rows]


# --------------------------------------------------------------------------
# 逐段实际通行核报
# --------------------------------------------------------------------------

_TRAVERSAL_COLUMNS = ("report_id", "group_id", "seq", "edge_id",
                      "enter_minute", "exit_minute", "reported_minute",
                      "active", "detached_minute", "detach_reason")


def _traversal_row(row) -> dict:
    return {"report_id": row[0], "group_id": row[1], "seq": row[2],
            "edge_id": row[3], "enter_minute": row[4],
            "exit_minute": row[5], "reported_minute": row[6],
            "active": bool(row[7]), "detached_minute": row[8],
            "detach_reason": row[9]}


_TRAVERSAL_SELECT = (
    "SELECT report_id, group_id, seq, edge_id, enter_minute, exit_minute, "
    "reported_minute, active, detached_minute, detach_reason "
    "FROM traversal_reports")


async def insert_traversal_report(db: aiosqlite.Connection, *,
                                  report_id: str, group_id: int, seq: int,
                                  edge_id: int, enter_minute: int,
                                  exit_minute: int,
                                  reported_minute: int) -> None:
    """登记一条有效的逐段通行核报（调用方负责事务与全部受理校验）。"""
    await db.execute(
        """INSERT INTO traversal_reports
           (report_id, group_id, seq, edge_id, enter_minute, exit_minute,
            reported_minute, active)
           VALUES(?,?,?,?,?,?,?,1)""",
        (report_id, group_id, seq, edge_id, enter_minute, exit_minute,
         reported_minute))


async def load_traversal_report(db: aiosqlite.Connection,
                                report_id: str) -> dict | None:
    """按报送号读取一条核报记录（含已失效留痕）；不存在返回 None。"""
    async with db.execute(
            f"{_TRAVERSAL_SELECT} WHERE report_id=?", (report_id,)) as cur:
        row = await cur.fetchone()
    return None if row is None else _traversal_row(row)


async def load_traversal_reports(db: aiosqlite.Connection,
                                 group_id: int | None = None,
                                 *, active_only: bool = False) -> list[dict]:
    """按受理先后列出核报记录；可限定组，或仅取仍有效（对应当前路线）记录。"""
    clauses = []
    params: list = []
    if group_id is not None:
        clauses.append("group_id=?")
        params.append(group_id)
    if active_only:
        clauses.append("active=1")
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    async with db.execute(
            f"{_TRAVERSAL_SELECT}{where} ORDER BY id", params) as cur:
        rows = await cur.fetchall()
    return [_traversal_row(r) for r in rows]


async def detach_traversal_suffix(db: aiosqlite.Connection, *, group_id: int,
                                  seq_from: int, minute: int) -> None:
    """节点滞留改编时，把该组 seq >= seq_from 的仍有效核报标记为失效留痕。

    被释放的旧余程段不再属于现行路线，对应核报不再参与计划/实绩并列与
    逐分钟实绩汇总，但记录保留可查（active=0，附失效分钟与原因）；
    改编后的新余程可重新对同一 seq 核报。已走前缀（seq < seq_from）的
    核报保持有效。调用方负责事务。
    """
    await db.execute(
        """UPDATE traversal_reports SET active=0, detached_minute=?,
           detach_reason='delay-replan'
           WHERE group_id=? AND seq>=? AND active=1""",
        (minute, group_id, seq_from))


# --------------------------------------------------------------------------
# 修订历史（编排变更的审计追溯）
# --------------------------------------------------------------------------

async def insert_revision(db: aiosqlite.Connection, operation: str,
                          detail: dict, plan_before: dict,
                          plan_after: dict) -> int:
    """在调用方事务内写入一条修订记录，返回全局递增修订号。

    修订号取 MAX(rev)+1：所有变更均由服务层互斥锁串行化，编号顺序
    即实际提交顺序，连续无重复。本函数必须与编排变更处于同一事务，
    保证查询不会只看到其中一方。
    """
    async with db.execute(
            "SELECT COALESCE(MAX(rev), 0) + 1 FROM revisions") as cur:
        rev = int((await cur.fetchone())[0])
    await db.execute(
        """INSERT INTO revisions(rev, operation, detail, minute,
                                 plan_before, plan_after)
           VALUES(?,?,?,?,?,?)""",
        (rev, operation,
         json.dumps(detail, ensure_ascii=False),
         int(plan_after["current_minute"]),
         json.dumps(plan_before, ensure_ascii=False),
         json.dumps(plan_after, ensure_ascii=False)))
    return rev


async def load_revision(db: aiosqlite.Connection, rev: int) -> dict | None:
    """按修订号读取修订（含前后快照）；不存在时返回 None。"""
    async with db.execute(
            """SELECT operation, detail, minute, plan_before, plan_after
               FROM revisions WHERE rev=?""", (rev,)) as cur:
        row = await cur.fetchone()
    if row is None:
        return None
    return {
        "rev": rev,
        "operation": row[0],
        "detail": json.loads(row[1]),
        "minute": int(row[2]),
        "plan_before": json.loads(row[3]),
        "plan_after": json.loads(row[4]),
    }


async def list_revisions(db: aiosqlite.Connection) -> list[dict]:
    """按修订号升序列出全部修订的元信息（不含快照正文）。"""
    async with db.execute(
            "SELECT rev, operation, detail, minute FROM revisions"
            " ORDER BY rev") as cur:
        rows = await cur.fetchall()
    return [{"rev": int(r), "operation": op, "detail": json.loads(d),
             "minute": int(m)} for r, op, d, m in rows]
