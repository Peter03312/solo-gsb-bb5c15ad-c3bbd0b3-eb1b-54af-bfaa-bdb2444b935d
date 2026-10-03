"""HTTP 请求/响应模型（字段 snake_case，原因码附中文说明）。"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field, model_validator


class PassageIn(BaseModel):
    id: int = Field(..., ge=1, description="通道编号（正整数，全图唯一）")
    src: int = Field(..., ge=0, description="起点节点编号")
    dst: int = Field(..., ge=0, description="终点节点编号")
    minutes: int = Field(..., gt=0, description="通行分钟数（正整数）")
    capacity: int = Field(..., gt=0, description="每分钟通行人数（正整数）")

    @model_validator(mode="after")
    def _no_self_loop(self):
        if self.src == self.dst:
            raise ValueError("通道起点与终点不能相同")
        return self


class ClosureIn(BaseModel):
    edge_id: int = Field(..., description="被封闭的通道编号")
    start_minute: int = Field(..., ge=0, description="封闭开始分钟（含）")
    end_minute: int = Field(..., ge=0, description="封闭结束分钟（不含）")

    @model_validator(mode="after")
    def _half_open(self):
        if self.end_minute <= self.start_minute:
            raise ValueError("封闭时段需为左闭右开：end_minute 必须大于 start_minute")
        return self


class CapacityControlIn(BaseModel):
    edge_id: int = Field(..., description="被管制的通道编号")
    start_minute: int = Field(..., ge=0, description="管制开始分钟（含，须不早于当前分钟）")
    end_minute: int = Field(..., ge=0, description="管制结束分钟（不含）")
    capacity: int = Field(..., gt=0,
                          description="管制时段内每分钟人数上限（正整数）")

    @model_validator(mode="after")
    def _half_open(self):
        if self.end_minute <= self.start_minute:
            raise ValueError(
                "管制时段需为左闭右开：end_minute 必须大于 start_minute")
        return self


class CapacityControlOut(BaseModel):
    edge_id: int
    start_minute: int
    end_minute: int
    capacity: int


class CapacityControlListOut(BaseModel):
    current_minute: int
    controls: list[CapacityControlOut]


# --------------------------------------------------------------------------
# 烟雾暴露时段与累计暴露上限
# --------------------------------------------------------------------------

class SmokeWindowIn(BaseModel):
    edge_id: int = Field(..., description="被登记烟雾暴露的通道编号")
    start_minute: int = Field(..., ge=0, description="暴露开始分钟（含，须不早于当前分钟）")
    end_minute: int = Field(..., ge=0, description="暴露结束分钟（不含）")
    smoke_value: int = Field(
        ..., ge=0, description="时段内每分钟暴露值（非负整数；未登记分钟为零）")

    @model_validator(mode="after")
    def _half_open(self):
        if self.end_minute <= self.start_minute:
            raise ValueError(
                "烟雾暴露时段需为左闭右开：end_minute 必须大于 start_minute")
        return self


class SmokeWindowOut(BaseModel):
    edge_id: int
    start_minute: int
    end_minute: int
    smoke_value: int = Field(description="时段内每分钟暴露值")


class SmokeWindowListOut(BaseModel):
    current_minute: int
    windows: list[SmokeWindowOut]


class SmokeLimitIn(BaseModel):
    """为尚未出发的人群组登记非负累计暴露上限（每组至多一项，再登记替换）。"""
    group_id: int = Field(..., ge=1, description="人群组编号，须尚未出发")
    smoke_limit: int = Field(
        ..., ge=0,
        description="非负累计暴露上限：沿路线逐分钟暴露值之和不得超过该值")


class SmokeLimitOut(BaseModel):
    group_id: int
    smoke_limit: int = Field(description="非负累计暴露上限")


class SmokeLimitListOut(BaseModel):
    current_minute: int
    groups: list[SmokeLimitOut]


class DelayReportIn(BaseModel):
    """现场指挥对单个已出发组的节点滞留报告（组号取自路径参数）。"""
    node_id: int = Field(..., ge=0, description="组按现行计划已到达、当前滞留的节点编号")
    recovery_minute: int = Field(
        ..., ge=0, description="预计恢复分钟（不得早于当前分钟）")


class DelayReportAliasIn(DelayReportIn):
    """节点滞留报告的集合式写法（组号放在请求体内）。"""
    group_id: int = Field(..., ge=1, description="被报告的已出发人群组编号")


# --------------------------------------------------------------------------
# 先导关系（放行前置）
# --------------------------------------------------------------------------

class PrerequisiteIn(BaseModel):
    """为尚未出发的人群组登记先导组与非负放行间隔（每组至多一项）。"""
    group_id: int = Field(..., ge=1, description="后继（目标）人群组编号，须尚未出发")
    leader_group_id: int = Field(..., ge=1, description="先导组编号（须存在）")
    release_gap: int = Field(
        ..., ge=0, description="非负放行间隔：后继不早于先导到达分钟 + 该间隔")


class PrerequisiteBasisOut(BaseModel):
    """后继组当前的放行依据（实时由先导组状态计算）。"""
    satisfied: bool = Field(
        description="先导是否已有可用的到达锚点（规划到达或人工实际送达）")
    # planned_arrival=先导规划到达分钟；actual_delivery=先导人工救援实际送达
    # 分钟；前置未满足时为 null
    basis_type: Optional[str] = None
    leader_status: str = Field(description="计算放行依据时先导组的状态")
    leader_arrival_minute: Optional[int] = Field(
        None, description="放行依据采用的先导到达/实际送达分钟")
    earliest_release_minute: Optional[int] = Field(
        None, description="先导到达分钟 + 放行间隔（未满足时为 null）")
    # 未满足时给出原因：LEADER_PENDING / LEADER_AWAITING_RESCUE /
    # LEADER_RESCUING / LEADER_FAILED / LEADER_BLOCKED
    reason: Optional[str] = None


class PrerequisiteOut(BaseModel):
    group_id: int
    leader_group_id: int
    release_gap: int
    release_basis: PrerequisiteBasisOut = Field(
        description="该关系当前的放行依据（先导状态、采用的到达分钟与最早放行分钟）")


class PrerequisiteListOut(BaseModel):
    current_minute: int
    relations: list[PrerequisiteOut]


# --------------------------------------------------------------------------
# 人工救援队与救援任务
# --------------------------------------------------------------------------

class RescueTeamIn(BaseModel):
    """人工救援队登记：唯一编号 + 正整数载员量。"""
    team_id: int = Field(..., ge=1, description="救援队唯一编号（正整数）")
    capacity: int = Field(..., gt=0, description="载员量（正整数）")


class RescueTeamOut(BaseModel):
    team_id: int
    capacity: int
    # 当前进行中任务编号；空闲为 null
    task_id: Optional[int] = None


class RescueTeamListOut(BaseModel):
    current_minute: int
    teams: list[RescueTeamOut]


class RescueDispatchIn(BaseModel):
    """人工救援派遣：组号、队号、滞留节点、所见当前分钟。"""
    group_id: int = Field(..., ge=1, description="待救援人群组编号")
    team_id: int = Field(..., ge=1, description="人工救援队编号")
    node_id: int = Field(..., ge=0, description="该组当前滞留的节点编号")
    observed_minute: int = Field(
        ..., ge=0, description="提交时所见的当前分钟（须与实际当前分钟一致）")


class RescueCompletionIn(BaseModel):
    """救援结案：既有出口编号与实际送达分钟。"""
    exit_id: int = Field(..., ge=0, description="实际送达的既有出口编号")
    delivery_minute: int = Field(
        ..., ge=0, description="实际送达分钟（不早于派遣、不晚于当前分钟）")


class RescueTaskOut(BaseModel):
    task_id: int
    group_id: int
    team_id: int
    node_id: int = Field(description="派遣时组的滞留节点")
    observed_minute: int = Field(description="派遣提交时所见当前分钟")
    dispatch_minute: int = Field(description="实际受理派遣的当前分钟")
    status: str = Field(description="in_progress=救援中 | completed=已核销")
    # 结案后给出的实际出口与实际送达分钟；进行中为 null
    exit_id: Optional[int] = Field(None, description="实际送达出口（结案后）")
    delivery_minute: Optional[int] = Field(
        None, description="实际送达分钟（结案后）")


class RescueTaskListOut(BaseModel):
    current_minute: int
    tasks: list[RescueTaskOut]


# --------------------------------------------------------------------------
# 出口清点（按组清点实际撤离人数）
# --------------------------------------------------------------------------

class ExitCountIn(BaseModel):
    """出口清点提交：非空唯一流水号 + 组号 + 出口号 + 正整数人数 + 非负分钟。"""
    serial: str = Field(
        ..., min_length=1,
        description="非空唯一流水号（幂等键：同内容重试返回原记录，异内容拒绝）")
    group_id: int = Field(..., ge=1, description="人群组编号")
    exit_id: int = Field(..., ge=0,
                         description="清点出口编号（须为当前场景既有出口）")
    count: int = Field(..., gt=0, description="本次清点人数（正整数）")
    counted_minute: int = Field(
        ..., ge=0,
        description="清点分钟（不早于该组到达/送达分钟、不晚于当前分钟）")


class ExitCountCorrectionIn(BaseModel):
    """清点更正：凭原流水号、原记录版本与非空原因更正为非负人数。"""
    version: int = Field(..., ge=1,
                         description="被更正的记录版本（须为记录当前版本）")
    count: int = Field(..., ge=0, description="更正后人数（非负整数）")
    reason: str = Field(..., min_length=1, description="非空更正原因")


class ExitCountCorrectionOut(BaseModel):
    from_version: int = Field(description="被更正的记录版本")
    to_version: int = Field(description="更正后的记录版本")
    old_count: int = Field(description="更正前人数（原值保留）")
    new_count: int = Field(description="更正后人数")
    reason: str = Field(description="更正原因")
    minute: int = Field(description="受理更正时的当前分钟")


class ExitCountRecordOut(BaseModel):
    serial: str
    group_id: int
    exit_id: int
    count: int = Field(description="当前生效人数（更正后口径）")
    submitted_count: int = Field(
        description="首次提交人数（原值保留，不随更正改变）")
    counted_minute: int = Field(description="清点分钟")
    version: int = Field(description="记录版本：首次登记为 1，每次更正 +1")
    created_minute: int = Field(description="首次登记时的当前分钟")
    corrections: list[ExitCountCorrectionOut] = Field(
        default_factory=list, description="更正历史（按更正先后）")


class ExitCountArrivalOut(BaseModel):
    """组的可清点到达锚点：fixed 取规划到达，rescued 取实际送达。"""
    basis: Optional[str] = Field(
        None, description="planned_arrival=按固定路线抵达 | "
                          "actual_delivery=人工救援实际送达；未抵达为 null")
    exit_id: Optional[int] = Field(None, description="抵达/送达出口编号")
    minute: Optional[int] = Field(None, description="抵达/送达分钟")


class GroupTallyOut(BaseModel):
    group_id: int
    expected: int = Field(description="应到人数（组人数）")
    counted: int = Field(description="已清点人数（全部记录当前生效人数之和）")
    remaining: int = Field(description="未清点人数（应到 - 已清点）")
    arrival: ExitCountArrivalOut = Field(
        description="可清点到达锚点（救援送达组采用实际出口与时刻）")
    records: list[ExitCountRecordOut] = Field(
        default_factory=list, description="逐条清点记录（含更正历史）")


class ExitCountListOut(BaseModel):
    current_minute: int
    groups: list[GroupTallyOut]


class ExitCountGroupOut(GroupTallyOut):
    current_minute: int


# --------------------------------------------------------------------------
# 逐段实际通行核报（已出发组沿现行路线逐段上报实际进入/离开分钟）
# --------------------------------------------------------------------------

class TraversalReportIn(BaseModel):
    """逐段实际通行核报：唯一报送号 + 组号 + 现行路线段序号 + 通道号 + 时刻。"""
    report_id: str = Field(
        ..., min_length=1,
        description="唯一报送号（幂等键：同号同内容重试返回原记录，"
                    "同号异内容整次拒绝）")
    group_id: int = Field(..., ge=1, description="已出发人群组编号")
    seq: int = Field(..., ge=0,
                     description="现行路线段序号（从 0 起，须沿路线顺序核报）")
    edge_id: int = Field(..., ge=1, description="该段实际通行的通道编号（须与现行路线一致）")
    enter_minute: int = Field(..., ge=0, description="实际进入分钟（非负，早于离开分钟）")
    exit_minute: int = Field(..., ge=0,
                             description="实际离开分钟（不晚于当前分钟；不早于前段离开）")

    @model_validator(mode="after")
    def _enter_before_exit(self):
        if self.exit_minute <= self.enter_minute:
            raise ValueError("实际离开分钟必须晚于实际进入分钟")
        return self


class TraversalActualStepOut(BaseModel):
    """现行路线中一段的计划时刻与实绩时刻并列。"""
    seq: int = Field(description="现行路线段序号（从 0 起）")
    edge_id: int
    from_node: int
    to_node: int
    planned_enter_minute: Optional[int] = Field(
        None, description="计划进入分钟（现行编排）")
    planned_exit_minute: Optional[int] = Field(
        None, description="计划离开分钟（现行编排）")
    actual_enter_minute: Optional[int] = Field(
        None, description="实际进入分钟（已核报给出；未核报为 null）")
    actual_exit_minute: Optional[int] = Field(
        None, description="实际离开分钟（已核报给出；未核报为 null）")
    report_id: Optional[str] = Field(
        None, description="该段实绩对应的报送号（未核报为 null）")
    deviates: bool = Field(
        False, description="实绩时刻是否偏离计划时刻（任一时刻不同即偏离）")
    anomalies: list[str] = Field(
        default_factory=list,
        description="该段实绩异常标记：CLOSED=实绩区间命中通道封闭；"
                    "OVER_CAPACITY=实绩区间某分钟实绩人数超当时有效容量")


class TraversalRecordOut(BaseModel):
    """一条逐段通行核报记录（幂等登记，永不因编排改动而删除）。"""
    report_id: str
    group_id: int
    seq: int
    edge_id: int
    enter_minute: int = Field(description="实际进入分钟")
    exit_minute: int = Field(description="实际离开分钟")
    reported_minute: int = Field(description="受理核报时的当前分钟")
    active: bool = Field(
        True,
        description="是否仍对应当前现行路线的段；节点滞留改编释放该段后"
                    "置为 false（历史留痕，不参与逐分钟汇总与并列展示）")
    detached_minute: Optional[int] = Field(
        None, description="被节点滞留改编标记为失效时的当前分钟（active 时为 null）")
    detach_reason: Optional[str] = Field(
        None, description="失效原因（delay-replan：节点滞留改编释放该段）")


class TraversalGroupOut(BaseModel):
    """单组逐段计划/实绩并列、未核报段与核报记录。"""
    group_id: int
    status: str
    size: int
    prefix_step_count: Optional[int] = Field(
        None, description="已走固定前缀步数（被节点滞留改编过的组给出）")
    steps: list[TraversalActualStepOut] = Field(
        default_factory=list,
        description="现行路线各段：计划与实绩时刻并列（未核报段实绩为 null）")
    unreported_seqs: list[int] = Field(
        default_factory=list, description="尚未核报的现行路线段序号（按序号升序）")
    next_seq: Optional[int] = Field(
        None, description="下一个应核报的段序号（全部核报完为 null）")
    reports: list[TraversalRecordOut] = Field(
        default_factory=list,
        description="该组全部核报记录（按受理先后，含已失效留痕）")
    detached_reports: list[TraversalRecordOut] = Field(
        default_factory=list,
        description="因节点滞留改编而不再对应现行路线的历史核报记录")


class TraversalEdgeMinuteOut(BaseModel):
    """某通道某分钟按已核报实绩汇总的人数与异常标记。"""
    edge_id: int
    minute: int
    used: int = Field(description="该分钟在该通道上的实绩总人数（仅已核报实绩）")
    effective_capacity: int = Field(
        description="当时有效容量（容量管制时段内为 min(原容量, 管制上限)）")
    groups: list[dict] = Field(
        default_factory=list,
        description="贡献实绩人数的组：group_id/size")
    anomalies: list[str] = Field(
        default_factory=list,
        description="异常标记：OVER_CAPACITY=实绩人数超当时有效容量；"
                    "CLOSED=该分钟命中通道封闭")


class TraversalGroupDetailOut(TraversalGroupOut):
    current_minute: int
    edge_minutes: list[TraversalEdgeMinuteOut] = Field(
        default_factory=list,
        description="与该组实绩相关的通道逐分钟实绩人数汇总")


class TraversalReportListOut(BaseModel):
    current_minute: int
    groups: list[TraversalGroupOut]
    edge_minutes: list[TraversalEdgeMinuteOut] = Field(
        default_factory=list,
        description="全部通道按已核报实绩汇总的逐分钟人数（标出超容量/封闭异常）")


class GroupIn(BaseModel):
    id: int = Field(..., ge=1, description="人群组编号（正整数，唯一）")
    size: int = Field(..., gt=0, description="人数（正整数，整组不可拆分）")
    origin: int = Field(..., ge=0, description="起点节点编号")
    earliest_departure: int = Field(..., ge=0, description="最早出发分钟")
    priority: int = Field(..., ge=0, description="优先级，数值越大越优先")
    deadline: int = Field(..., ge=0, description="规划截止分钟（到达须不晚于此）")

    @model_validator(mode="after")
    def _deadline_ok(self):
        if self.deadline < self.earliest_departure:
            raise ValueError("规划截止分钟不能早于最早出发分钟")
        return self


class ScenarioIn(BaseModel):
    passages: list[PassageIn] = Field(default_factory=list)
    exits: list[int] = Field(default_factory=list, description="出口节点编号列表")
    closures: list[ClosureIn] = Field(default_factory=list)
    groups: list[GroupIn] = Field(default_factory=list)


class GroupCorrectionIn(BaseModel):
    """单条人群组纠正：指定已有组编号及至少一个待纠正字段。

    未指定的字段沿用原值；合并后的最终字段须通过 GroupIn 的全部校验
    （在服务层按合并结果复核，含"截止不早于最早出发"的跨字段约束）。
    """
    group_id: int = Field(..., ge=1, description="要纠正的已有人群组编号")
    size: Optional[int] = Field(None, gt=0, description="新人数（正整数）")
    origin: Optional[int] = Field(None, ge=0,
                                  description="新起点节点编号（须为场馆节点）")
    earliest_departure: Optional[int] = Field(None, ge=0,
                                              description="新最早出发分钟")
    priority: Optional[int] = Field(None, ge=0,
                                    description="新优先级，数值越大越优先")
    deadline: Optional[int] = Field(None, ge=0,
                                    description="新规划截止分钟（到达须不晚于此）")

    @model_validator(mode="after")
    def _at_least_one_field(self):
        if all(v is None for v in (self.size, self.origin,
                                   self.earliest_departure, self.priority,
                                   self.deadline)):
            raise ValueError(
                "每项纠正至少指定一个字段：size/origin/earliest_departure/"
                "priority/deadline")
        return self


class StepOut(BaseModel):
    edge_id: int
    from_node: int
    to_node: int
    enter_minute: int
    exit_minute: int


class GroupPlanOut(BaseModel):
    group_id: int
    status: str
    exit_id: Optional[int] = None
    departure_minute: Optional[int] = None
    arrival_minute: Optional[int] = None
    route: list[int] = Field(default_factory=list, description="通道编号序列")
    steps: list[StepOut] = Field(default_factory=list)
    # 仅被现场滞留报告影响过的组（已恢复或待救援）给出：已走前缀步数，
    # steps[0:prefix_step_count] 为按现行计划已走过、固定保留的前缀；
    # 其后为报告恢复后重找的新余程。未报告组恒为 null，字段语义保持兼容。
    prefix_step_count: Optional[int] = None
    reason: Optional[str] = None
    detail: Optional[str] = None
    # ---- 人工救援队交接（仅被派遣/已核销的组给出，其余恒为 null）----
    # rescuing=已派遣、人工救援中；rescued=已结案核销送达
    rescue_task_id: Optional[int] = Field(
        None, description="进行中或已核销的人工救援任务编号")
    rescue_team_id: Optional[int] = Field(None, description="接手的救援队编号")
    rescued_exit_id: Optional[int] = Field(
        None, description="结案单列的实际送达出口（非规划到达，rescued 时给出）")
    delivery_minute: Optional[int] = Field(
        None, description="结案单列的实际送达分钟（可晚于原截止，rescued 时给出）")
    # ---- 先导关系（仅登记过先导的组给出，其余恒为 null，字段语义兼容）----
    leader_group_id: Optional[int] = Field(
        None, description="先导组编号（后继组最早放行分钟的前置）")
    release_gap: Optional[int] = Field(
        None, description="非负放行间隔：最早放行=先导到达分钟+该间隔")
    release_basis: Optional[PrerequisiteBasisOut] = Field(
        None, description="当前放行依据：先导状态、采用的到达分钟与最早放行分钟")
    # ---- 烟雾累计暴露（仅登记过非负暴露上限的组给出，其余恒为 null）----
    smoke_exposure_limit: Optional[int] = Field(
        None, description="该组登记的非负累计暴露上限（未设上限的组为 null）")
    smoke_exposure_used: Optional[int] = Field(
        None,
        description="已走前缀按原计划时刻累计的烟雾暴露（未出发组为 0）；"
                    "节点滞留改编时该部分保留、不计入余程额度")
    smoke_exposure_remaining: Optional[int] = Field(
        None,
        description="余程（scheduled/fixed 含未来段；blocked/awaiting_rescue/"
                    "rescuing/rescued 无余程）按现行计划时刻预计的累计烟雾暴露；"
                    "无可行路线或无余程时为 null")


class PlanResponse(BaseModel):
    current_minute: int
    groups: list[GroupPlanOut]


class ConflictOut(BaseModel):
    group_id: int
    edge_id: int
    enter_minute: int
    exit_minute: int
    closure: list[int] = Field(description="与之相交的封闭时段 [start,end)")


# --------------------------------------------------------------------------
# 修订历史（编排变更追溯）
# --------------------------------------------------------------------------

class GroupChangeOut(BaseModel):
    """单个组在一次修订前后的差异。"""
    group_id: int
    change: str = Field(description="added=新增 | removed=移除 | changed=编排变化")
    changed_fields: list[str] = Field(
        default_factory=list,
        description="change=changed 时发生变化的字段，如 status/route/steps/reason")
    before: Optional[GroupPlanOut] = Field(
        None, description="提交前该组编排（added 时为 null）")
    after: Optional[GroupPlanOut] = Field(
        None, description="提交后该组编排（removed 时为 null）")


class RevisionSummaryOut(BaseModel):
    rev: int = Field(description="全局递增修订号（按实际提交顺序编号）")
    operation: str = Field(
        description="触发操作：scenario=场景重置 | groups=新增人群组 | "
                    "group-corrections=纠正人群组 | closures=登记封闭 | "
                    "capacity-controls=登记容量管制 | "
                    "group-prerequisites=登记先导关系 | "
                    "delay-reports=现场报告节点滞留 | "
                    "rescue-dispatches=派遣人工救援 | "
                    "rescue-completions=救援结案核销 | tick=时间推进")
    minute: int = Field(description="该修订提交后的当前分钟")
    detail: dict = Field(default_factory=dict, description="触发操作的参数摘要")


class RevisionListOut(BaseModel):
    revisions: list[RevisionSummaryOut]


class RevisionDetailOut(RevisionSummaryOut):
    snapshot: PlanResponse = Field(
        description="该修订提交后的完整编排快照（当时状态，含固定组逐段"
                    "通行时刻；落库保存，不按当前场景重算）")
    changes: list[GroupChangeOut] = Field(
        description="各组提交前后差异（新增/移除/状态、出发与到达分钟、"
                    "路线、失败原因）")
