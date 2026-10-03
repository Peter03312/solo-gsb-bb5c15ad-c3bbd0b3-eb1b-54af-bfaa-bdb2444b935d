#!/usr/bin/env bash
# 场馆疏散通道编排服务 —— curl 调用示例
# 用法： bash examples/curl_examples.sh [BASE_URL]
# 依次演示：初始化、新增组、新增封闭、推进分钟、409 拒绝、容量管制登记/
# 重叠/超载拒绝、有效上限核验、组纠正、现场节点滞留报告（受理/拒绝/待救援）、
# 人工救援队登记/派遣拒绝/派遣受理/结案核销、先导关系登记/替换/拒绝/前置
# 未满足/多级依赖/人工送达锚点、出口清点（幂等登记/拒绝/更正/查询）、
# 逐段实际通行核报（顺序核报/幂等/异常/滞留改编前缀保留/重置）、
# 烟雾暴露时段与累计暴露上限（等待消散/重叠与超限拒绝/已出发保护/重置）、
# 中途节点计划滞留限流（推迟上游/改道/起点等待免费/重叠与已出发超额拒绝/
# 逐分钟停留查询/滞留报告待救援/重置）、修订追溯。
set -euo pipefail

BASE="${1:-${BASE:-http://localhost:8000}}"

say() { printf '\n==== %s ====\n' "$1"; }

say "1) 健康检查"
curl -s "$BASE/health" | python3 -m json.tool

say "2) 全量初始化一个自定义场景（通道/出口/封闭/人群组）"
curl -s -X POST "$BASE/scenario" \
  -H 'Content-Type: application/json' \
  -d '{
    "passages": [
      {"id": 1, "src": 1, "dst": 2, "minutes": 4, "capacity": 30},
      {"id": 2, "src": 2, "dst": 3, "minutes": 3, "capacity": 30},
      {"id": 3, "src": 3, "dst": 5, "minutes": 2, "capacity": 30},
      {"id": 5, "src": 2, "dst": 6, "minutes": 2, "capacity": 40},
      {"id": 7, "src": 7, "dst": 5, "minutes": 5, "capacity": 20}
    ],
    "exits": [5, 6],
    "closures": [
      {"edge_id": 1, "start_minute": 3, "end_minute": 10}
    ],
    "groups": [
      {"id": 1, "size": 30, "origin": 1, "earliest_departure": 0, "priority": 10, "deadline": 30},
      {"id": 3, "size": 10, "origin": 2, "earliest_departure": 0, "priority": 20, "deadline": 20},
      {"id": 4, "size": 15, "origin": 7, "earliest_departure": 0, "priority": 5, "deadline": 15}
    ]
  }' | python3 -m json.tool

say "3) 查询编排结果（按优先级、最早出发、组号排序）"
curl -s "$BASE/plan" | python3 -m json.tool

say "4) 新增人群组（仅重排未出发组）"
curl -s -X POST "$BASE/groups" \
  -H 'Content-Type: application/json' \
  -d '[
    {"id": 7, "size": 25, "origin": 1, "earliest_departure": 2, "priority": 8, "deadline": 40}
  ]' | python3 -m json.tool

say "5) 新增不触及已出发组的封闭时段（登记后重排未出发组）"
curl -s -X POST "$BASE/closures" \
  -H 'Content-Type: application/json' \
  -d '{"edge_id": 2, "start_minute": 20, "end_minute": 30}' \
  | python3 -m json.tool

say "6) 推进当前分钟 1 分钟（t=0 出发的组固定，随后重排未出发组）"
curl -s -X POST "$BASE/tick" \
  -H 'Content-Type: application/json' \
  -d '{"minutes": 1}' | python3 -m json.tool

say "7) 尝试封闭已出发组剩余路线（组3正占用通道5的[1,2)，预期 HTTP 409 整次拒绝）"
curl -s -o /tmp/resp.json -w 'HTTP %{http_code}\n' -X POST "$BASE/closures" \
  -H 'Content-Type: application/json' \
  -d '{"edge_id": 5, "start_minute": 1, "end_minute": 3}'
python3 -m json.tool /tmp/resp.json || true

say "8) 拒绝后再次查询，原编排保持不变"
curl -s "$BASE/plan" | python3 -m json.tool

say "9) 查询通道 1 每分钟容量占用（核验不超容量）"
curl -s "$BASE/passages/1/load" | python3 -m json.tool

say "10) 登记使已出发组超载的容量管制（组3剩余占用通道5的[1,2)共10人，上限5 -> 预期 HTTP 409）"
curl -s -o /tmp/resp.json -w 'HTTP %{http_code}\n' -X POST "$BASE/capacity-controls" \
  -H 'Content-Type: application/json' \
  -d '{"edge_id": 5, "start_minute": 1, "end_minute": 3, "capacity": 5}'
python3 -m json.tool /tmp/resp.json || true

say "11) 登记合法容量管制（通道1第10..14分钟限流至10人/分钟，登记后重排未出发组）"
curl -s -X POST "$BASE/capacity-controls" \
  -H 'Content-Type: application/json' \
  -d '{"edge_id": 1, "start_minute": 10, "end_minute": 14, "capacity": 10}' \
  | python3 -m json.tool

say "12) 查询通道 1 占用：管制时段内每分钟有效上限收紧为 10，时段外仍为 30"
curl -s "$BASE/passages/1/load" | python3 -m json.tool

say "13) 列出当前全部容量管制时段"
curl -s "$BASE/capacity-controls" | python3 -m json.tool

say "14) 登记与已有管制重叠的时段（通道1第12..16分钟，预期 HTTP 409 整次拒绝）"
curl -s -o /tmp/resp.json -w 'HTTP %{http_code}\n' -X POST "$BASE/capacity-controls" \
  -H 'Content-Type: application/json' \
  -d '{"edge_id": 1, "start_minute": 12, "end_minute": 16, "capacity": 20}'
python3 -m json.tool /tmp/resp.json || true

say "15) 纠正未出发组（组7：人数 25->20、优先级 8->12，整批原子生效并重排未出发组）"
curl -s -X POST "$BASE/group-corrections" \
  -H 'Content-Type: application/json' \
  -d '[{"group_id": 7, "size": 20, "priority": 12}]' | python3 -m json.tool

say "16) 纠正已出发的组 3（预期 HTTP 409 整批拒绝，状态与修订历史不变）"
curl -s -o /tmp/resp.json -w 'HTTP %{http_code}\n' -X POST "$BASE/group-corrections" \
  -H 'Content-Type: application/json' \
  -d '[{"group_id": 3, "size": 5}]'
python3 -m json.tool /tmp/resp.json || true

say "17) 含无效项的纠正批（组999不存在，预期 HTTP 422，整批拒绝）"
curl -s -o /tmp/resp.json -w 'HTTP %{http_code}\n' -X POST "$BASE/group-corrections" \
  -H 'Content-Type: application/json' \
  -d '[{"group_id": 7, "size": 18}, {"group_id": 999, "size": 1}]'
python3 -m json.tool /tmp/resp.json || true

say "18) 重置为便于演示节点滞留报告的小场景，并推进到组1抵达节点2（第4分钟）"
curl -s -X POST "$BASE/scenario" \
  -H 'Content-Type: application/json' \
  -d '{
    "passages": [
      {"id": 1, "src": 1, "dst": 2, "minutes": 4, "capacity": 10},
      {"id": 2, "src": 2, "dst": 6, "minutes": 2, "capacity": 10},
      {"id": 3, "src": 2, "dst": 3, "minutes": 2, "capacity": 10},
      {"id": 4, "src": 3, "dst": 5, "minutes": 2, "capacity": 10}
    ],
    "exits": [5, 6],
    "closures": [],
    "groups": [
      {"id": 1, "size": 10, "origin": 1, "earliest_departure": 0, "priority": 10, "deadline": 30},
      {"id": 2, "size": 10, "origin": 1, "earliest_departure": 0, "priority": 5, "deadline": 30},
      {"id": 3, "size": 10, "origin": 2, "earliest_departure": 0, "priority": 1, "deadline": 30}
    ]
  }' > /dev/null
curl -s -X POST "$BASE/tick" -H 'Content-Type: application/json' \
  -d '{"minutes": 4}' | python3 -m json.tool

say "19) 报告未出发的组 2（节点1，预期 HTTP 409 NOT_DEPARTED，不改编排）"
curl -s -o /tmp/resp.json -w 'HTTP %{http_code}\n' -X POST "$BASE/groups/2/delay-report" \
  -H 'Content-Type: application/json' \
  -d '{"node_id": 1, "recovery_minute": 4}'
python3 -m json.tool /tmp/resp.json || true

say "20) 组 1 报告错误节点（实际在节点2，预期 HTTP 409 NODE_MISMATCH）"
curl -s -o /tmp/resp.json -w 'HTTP %{http_code}\n' -X POST "$BASE/groups/1/delay-report" \
  -H 'Content-Type: application/json' \
  -d '{"node_id": 1, "recovery_minute": 4}'
python3 -m json.tool /tmp/resp.json || true

say "21) 恢复分钟早于当前分钟（预期 HTTP 422）"
curl -s -o /tmp/resp.json -w 'HTTP %{http_code}\n' -X POST "$BASE/groups/1/delay-report" \
  -H 'Content-Type: application/json' \
  -d '{"node_id": 2, "recovery_minute": 3}'
python3 -m json.tool /tmp/resp.json || true

say "22) 正式报告：组1在节点2滞留、预计第7分钟恢复（释放旧预约并重找余程，组2随之重排）"
curl -s -X POST "$BASE/groups/1/delay-report" \
  -H 'Content-Type: application/json' \
  -d '{"node_id": 2, "recovery_minute": 7}' | python3 -m json.tool

say "23) 等价的集合式写法：POST /delay-reports（组号置于请求体；此处演示对组2的409拒绝）"
curl -s -o /tmp/resp.json -w 'HTTP %{http_code}\n' -X POST "$BASE/delay-reports" \
  -H 'Content-Type: application/json' \
  -d '{"group_id": 2, "node_id": 1, "recovery_minute": 4}'
python3 -m json.tool /tmp/resp.json || true

say "24) 待救援场景：唯一余程通道自第5分钟封闭，组1报告恢复6 -> awaiting_rescue"
curl -s -X POST "$BASE/scenario" \
  -H 'Content-Type: application/json' \
  -d '{
    "passages": [
      {"id": 1, "src": 1, "dst": 2, "minutes": 4, "capacity": 10},
      {"id": 2, "src": 2, "dst": 6, "minutes": 1, "capacity": 10}
    ],
    "exits": [6],
    "closures": [{"edge_id": 2, "start_minute": 5, "end_minute": 30}],
    "groups": [
      {"id": 1, "size": 10, "origin": 1, "earliest_departure": 0, "priority": 10, "deadline": 30}
    ]
  }' > /dev/null
curl -s -X POST "$BASE/tick" -H 'Content-Type: application/json' \
  -d '{"minutes": 4}' > /dev/null
curl -s -X POST "$BASE/groups/1/delay-report" \
  -H 'Content-Type: application/json' \
  -d '{"node_id": 2, "recovery_minute": 6}' | python3 -m json.tool

say "25) 时间推进不自动重排待救援组（仍为 awaiting_rescue，只保留已走前缀）"
curl -s -X POST "$BASE/tick" -H 'Content-Type: application/json' \
  -d '{"minutes": 6}' | python3 -m json.tool

# ---- 人工救援队：登记 / 派遣 / 结案核销 ----
say "26) 登记人工救援队（队1载员10；重复编号预期 HTTP 409 整次拒绝）"
curl -s -X POST "$BASE/rescue-teams" -H 'Content-Type: application/json' \
  -d '{"team_id": 1, "capacity": 10}' | python3 -m json.tool
curl -s -o /tmp/resp.json -w 'HTTP %{http_code}\n' -X POST "$BASE/rescue-teams" \
  -H 'Content-Type: application/json' \
  -d '{"team_id": 1, "capacity": 20}'
python3 -m json.tool /tmp/resp.json || true
curl -s -X POST "$BASE/rescue-teams" -H 'Content-Type: application/json' \
  -d '{"team_id": 2, "capacity": 5}' > /dev/null

say "27) 列出救援队（两队均空闲，task_id 为 null）"
curl -s "$BASE/rescue-teams" | python3 -m json.tool

say "28) 派遣校验：节点不符 409 NODE_MISMATCH（组1实际滞留节点2）"
curl -s -o /tmp/resp.json -w 'HTTP %{http_code}\n' -X POST "$BASE/rescue-tasks" \
  -H 'Content-Type: application/json' \
  -d '{"group_id": 1, "team_id": 1, "node_id": 1, "observed_minute": 10}'
python3 -m json.tool /tmp/resp.json || true

say "29) 派遣校验：队2载员5小于组人数10 -> 409 TEAM_CAPACITY_EXCEEDED"
curl -s -o /tmp/resp.json -w 'HTTP %{http_code}\n' -X POST "$BASE/rescue-tasks" \
  -H 'Content-Type: application/json' \
  -d '{"group_id": 1, "team_id": 2, "node_id": 2, "observed_minute": 10}'
python3 -m json.tool /tmp/resp.json || true

say "30) 派遣校验：所见分钟10与当前分钟一致；再演示失步（observed 9 -> 409）"
curl -s -o /tmp/resp.json -w 'HTTP %{http_code}\n' -X POST "$BASE/rescue-tasks" \
  -H 'Content-Type: application/json' \
  -d '{"group_id": 1, "team_id": 1, "node_id": 2, "observed_minute": 9}'
python3 -m json.tool /tmp/resp.json || true

say "31) 正式派遣：组1在节点2交给队1（当前分钟10），返回任务号；组置 rescuing"
curl -s -X POST "$BASE/rescue-tasks" -H 'Content-Type: application/json' \
  -d '{"group_id": 1, "team_id": 1, "node_id": 2, "observed_minute": 10}' \
  | python3 -m json.tool

say "32) 同组重复派遣 / 队伍忙：组1再派（即使换队2）-> 409 GROUP_RESCUE_IN_PROGRESS"
curl -s -o /tmp/resp.json -w 'HTTP %{http_code}\n' -X POST "$BASE/rescue-tasks" \
  -H 'Content-Type: application/json' \
  -d '{"group_id": 1, "team_id": 2, "node_id": 2, "observed_minute": 10}'
python3 -m json.tool /tmp/resp.json || true

say "33) 救援中的组拒绝既有滞留报告（409 GROUP_RESCUING）"
curl -s -o /tmp/resp.json -w 'HTTP %{http_code}\n' -X POST "$BASE/groups/1/delay-report" \
  -H 'Content-Type: application/json' \
  -d '{"node_id": 2, "recovery_minute": 12}'
python3 -m json.tool /tmp/resp.json || true

say "34) 查询任务1（in_progress）与任务列表（?status=in_progress 过滤）"
curl -s "$BASE/rescue-tasks/1" | python3 -m json.tool
curl -s "$BASE/rescue-tasks?status=in_progress" | python3 -m json.tool

say "35) 结案时序：送达12晚于当前10 -> 409 DELIVERY_IN_FUTURE；非既有出口7 -> 422"
curl -s -o /tmp/resp.json -w 'HTTP %{http_code}\n' -X POST "$BASE/rescue-tasks/1/completion" \
  -H 'Content-Type: application/json' \
  -d '{"exit_id": 6, "delivery_minute": 12}'
python3 -m json.tool /tmp/resp.json || true
curl -s -o /tmp/resp.json -w 'HTTP %{http_code}\n' -X POST "$BASE/rescue-tasks/1/completion" \
  -H 'Content-Type: application/json' \
  -d '{"exit_id": 7, "delivery_minute": 10}'
python3 -m json.tool /tmp/resp.json || true

say "36) 推进到第12分钟，结案核销：出口6、实际送达12（晚于原截止也如实记录）"
curl -s -X POST "$BASE/tick" -H 'Content-Type: application/json' \
  -d '{"minutes": 2}' > /dev/null
curl -s -X POST "$BASE/rescue-tasks/1/completion" -H 'Content-Type: application/json' \
  -d '{"exit_id": 6, "delivery_minute": 12}' | python3 -m json.tool

say "37) 结案后：组为 rescued（实际出口/时刻单列，规划到达字段为空），队1空闲"
curl -s "$BASE/plan" | python3 -m json.tool
curl -s "$BASE/rescue-teams" | python3 -m json.tool

say "38) 重复结案 -> 409 TASK_ALREADY_COMPLETED；已救援组再派 -> 409"
curl -s -o /tmp/resp.json -w 'HTTP %{http_code}\n' -X POST "$BASE/rescue-tasks/1/completion" \
  -H 'Content-Type: application/json' \
  -d '{"exit_id": 6, "delivery_minute": 12}'
python3 -m json.tool /tmp/resp.json || true

say "39) 列出修订历史（派遣与结案各一条 rescue-* 修订，被拒绝的不产生）"
curl -s "$BASE/revisions" | python3 -m json.tool

say "40) 按修订号查询：取最新一条（rescue-completions）的快照与逐组差异"
LATEST=$(curl -s "$BASE/revisions" | python3 -c \
  'import json,sys; print(json.load(sys.stdin)["revisions"][-1]["rev"])')
curl -s "$BASE/revisions/$LATEST" | python3 -m json.tool

say "41) 查询未知修订号与未知任务号（预期 HTTP 404）"
curl -s -o /tmp/resp.json -w 'HTTP %{http_code}\n' "$BASE/revisions/999999"
python3 -m json.tool /tmp/resp.json || true
curl -s -o /tmp/resp.json -w 'HTTP %{http_code}\n' "$BASE/rescue-tasks/999999"
python3 -m json.tool /tmp/resp.json || true

# ---- 先导组关系与非负放行间隔 ----
say "42) 重置为双路小场景，登记先导关系：组2 以组1 为先导、放行间隔 3"
curl -s -X POST "$BASE/scenario" \
  -H 'Content-Type: application/json' \
  -d '{
    "passages": [
      {"id": 1, "src": 1, "dst": 2, "minutes": 4, "capacity": 10},
      {"id": 2, "src": 2, "dst": 6, "minutes": 2, "capacity": 10},
      {"id": 3, "src": 1, "dst": 4, "minutes": 3, "capacity": 10},
      {"id": 4, "src": 4, "dst": 6, "minutes": 2, "capacity": 10}
    ],
    "exits": [6],
    "closures": [],
    "groups": [
      {"id": 1, "size": 10, "origin": 1, "earliest_departure": 0, "priority": 10, "deadline": 30},
      {"id": 2, "size": 10, "origin": 1, "earliest_departure": 0, "priority": 20, "deadline": 30}
    ]
  }' > /dev/null
curl -s -X POST "$BASE/group-prerequisites" \
  -H 'Content-Type: application/json' \
  -d '{"group_id": 2, "leader_group_id": 1, "release_gap": 3}' \
  | python3 -m json.tool

say "43) 查询全部先导关系及实时放行依据（先导状态/采用的到达分钟/最早放行分钟）"
curl -s "$BASE/group-prerequisites" | python3 -m json.tool

say "44) 同一组再次登记替换旧关系（间隔 3 -> 0；修订 detail.replaced 记录旧关系）"
curl -s -X POST "$BASE/group-prerequisites" \
  -H 'Content-Type: application/json' \
  -d '{"group_id": 2, "leader_group_id": 1, "release_gap": 0}' \
  | python3 -m json.tool

say "45) 登记拒绝：自依赖（预期 409 SELF_DEPENDENCY，整次拒绝）"
curl -s -o /tmp/resp.json -w 'HTTP %{http_code}\n' -X POST "$BASE/group-prerequisites" \
  -H 'Content-Type: application/json' \
  -d '{"group_id": 2, "leader_group_id": 2, "release_gap": 0}'
python3 -m json.tool /tmp/resp.json || true

say "46) 登记拒绝：形成环（1->2 与既有 2->1 成环，预期 409 CYCLE_DETECTED 并给出环路径）"
curl -s -o /tmp/resp.json -w 'HTTP %{http_code}\n' -X POST "$BASE/group-prerequisites" \
  -H 'Content-Type: application/json' \
  -d '{"group_id": 1, "leader_group_id": 2, "release_gap": 0}'
python3 -m json.tool /tmp/resp.json || true

say "47) 登记拒绝：先导组不存在 / 负间隔（预期 422）"
curl -s -o /tmp/resp.json -w 'HTTP %{http_code}\n' -X POST "$BASE/group-prerequisites" \
  -H 'Content-Type: application/json' \
  -d '{"group_id": 2, "leader_group_id": 99, "release_gap": 0}'
python3 -m json.tool /tmp/resp.json || true
curl -s -o /tmp/resp.json -w 'HTTP %{http_code}\n' -X POST "$BASE/group-prerequisites" \
  -H 'Content-Type: application/json' \
  -d '{"group_id": 2, "leader_group_id": 1, "release_gap": -1}'
python3 -m json.tool /tmp/resp.json || true

say "48) 前置未满足：组1 截止收紧到无法安排，后继组2 标记 blocked（不占通道）"
curl -s -X POST "$BASE/group-corrections" \
  -H 'Content-Type: application/json' \
  -d '[{"group_id": 1, "deadline": 3}]' | python3 -m json.tool
curl -s -X POST "$BASE/group-prerequisites" \
  -H 'Content-Type: application/json' \
  -d '{"group_id": 2, "leader_group_id": 1, "release_gap": 0}' \
  | python3 -m json.tool

say "49) 先导恢复可行（截止放回 30），后继随重排自动转正"
curl -s -X POST "$BASE/group-corrections" \
  -H 'Content-Type: application/json' \
  -d '[{"group_id": 1, "deadline": 30}]' | python3 -m json.tool

say "50) 多级关系：新增组3（节点2），链 3->2->1 按依赖顺序安排"
curl -s -X POST "$BASE/groups" -H 'Content-Type: application/json' \
  -d '[{"id": 3, "size": 10, "origin": 2, "earliest_departure": 0, "priority": 1, "deadline": 30}]' \
  | python3 -m json.tool
curl -s -X POST "$BASE/group-prerequisites" \
  -H 'Content-Type: application/json' \
  -d '{"group_id": 3, "leader_group_id": 2, "release_gap": 1}' \
  | python3 -m json.tool

say "51) 先导人工救援送达后以实际送达分钟为锚点：待救援场景 + 派遣 + 结案重排"
curl -s -X POST "$BASE/scenario" \
  -H 'Content-Type: application/json' \
  -d '{
    "passages": [
      {"id": 1, "src": 1, "dst": 2, "minutes": 4, "capacity": 10},
      {"id": 2, "src": 2, "dst": 6, "minutes": 1, "capacity": 10}
    ],
    "exits": [6],
    "closures": [{"edge_id": 2, "start_minute": 5, "end_minute": 30}],
    "groups": [
      {"id": 1, "size": 10, "origin": 1, "earliest_departure": 0, "priority": 10, "deadline": 30},
      {"id": 3, "size": 10, "origin": 6, "earliest_departure": 0, "priority": 5, "deadline": 40}
    ]
  }' > /dev/null
curl -s -X POST "$BASE/group-prerequisites" -H 'Content-Type: application/json' \
  -d '{"group_id": 3, "leader_group_id": 1, "release_gap": 2}' > /dev/null
curl -s -X POST "$BASE/tick" -H 'Content-Type: application/json' \
  -d '{"minutes": 4}' > /dev/null
curl -s -X POST "$BASE/groups/1/delay-report" -H 'Content-Type: application/json' \
  -d '{"node_id": 2, "recovery_minute": 6}' | python3 -m json.tool
curl -s -X POST "$BASE/rescue-teams" -H 'Content-Type: application/json' \
  -d '{"team_id": 1, "capacity": 10}' > /dev/null
curl -s -X POST "$BASE/rescue-tasks" -H 'Content-Type: application/json' \
  -d '{"group_id": 1, "team_id": 1, "node_id": 2, "observed_minute": 4}' > /tmp/dispatch.json
TASK_ID=$(python3 -c 'import json; print(json.load(open("/tmp/dispatch.json"))["task_id"])')
curl -s -X POST "$BASE/tick" -H 'Content-Type: application/json' \
  -d '{"minutes": 5}' > /dev/null
curl -s -X POST "$BASE/rescue-tasks/$TASK_ID/completion" -H 'Content-Type: application/json' \
  -d '{"exit_id": 6, "delivery_minute": 9}' | python3 -m json.tool
curl -s "$BASE/plan" | python3 -m json.tool

# ---- 出口清点：按组清点实际撤离人数（幂等登记 / 版本化更正 / 按组查询）----
say "52) 重置为清点演示场景：组1(10人) t=0 出发、t=2 抵达出口6；组2(5人) 尚未出发；推进到第3分钟"
curl -s -X POST "$BASE/scenario" \
  -H 'Content-Type: application/json' \
  -d '{
    "passages": [
      {"id": 1, "src": 1, "dst": 6, "minutes": 2, "capacity": 20}
    ],
    "exits": [5, 6],
    "closures": [],
    "groups": [
      {"id": 1, "size": 10, "origin": 1, "earliest_departure": 0, "priority": 10, "deadline": 30},
      {"id": 2, "size": 5, "origin": 1, "earliest_departure": 10, "priority": 5, "deadline": 30}
    ]
  }' > /dev/null
curl -s -X POST "$BASE/tick" -H 'Content-Type: application/json' \
  -d '{"minutes": 3}' > /dev/null
curl -s "$BASE/plan" | python3 -m json.tool

say "53) 登记校验：未抵达组 409 GROUP_NOT_ARRIVED；出口不符 409 EXIT_MISMATCH；分钟越窗 409"
curl -s -o /tmp/resp.json -w 'HTTP %{http_code}\n' -X POST "$BASE/exit-counts" \
  -H 'Content-Type: application/json' \
  -d '{"serial": "C-NA", "group_id": 2, "exit_id": 6, "count": 1, "counted_minute": 3}'
python3 -m json.tool /tmp/resp.json || true
curl -s -o /tmp/resp.json -w 'HTTP %{http_code}\n' -X POST "$BASE/exit-counts" \
  -H 'Content-Type: application/json' \
  -d '{"serial": "C-EX", "group_id": 1, "exit_id": 5, "count": 1, "counted_minute": 2}'
python3 -m json.tool /tmp/resp.json || true
curl -s -o /tmp/resp.json -w 'HTTP %{http_code}\n' -X POST "$BASE/exit-counts" \
  -H 'Content-Type: application/json' \
  -d '{"serial": "C-TM", "group_id": 1, "exit_id": 6, "count": 1, "counted_minute": 1}'
python3 -m json.tool /tmp/resp.json || true

say "54) 正式登记：流水号 C-0001，组1 在出口6 第2分钟清点 4 人（HTTP 201）"
curl -s -X POST "$BASE/exit-counts" \
  -H 'Content-Type: application/json' \
  -d '{"serial": "C-0001", "group_id": 1, "exit_id": 6, "count": 4, "counted_minute": 2}' \
  | python3 -m json.tool

say "55) 幂等：同流水号同内容重试 -> HTTP 200 返回原记录；异内容 -> HTTP 409 整次拒绝"
curl -s -o /tmp/resp.json -w 'HTTP %{http_code}\n' -X POST "$BASE/exit-counts" \
  -H 'Content-Type: application/json' \
  -d '{"serial": "C-0001", "group_id": 1, "exit_id": 6, "count": 4, "counted_minute": 2}'
python3 -m json.tool /tmp/resp.json || true
curl -s -o /tmp/resp.json -w 'HTTP %{http_code}\n' -X POST "$BASE/exit-counts" \
  -H 'Content-Type: application/json' \
  -d '{"serial": "C-0001", "group_id": 1, "exit_id": 6, "count": 5, "counted_minute": 2}'
python3 -m json.tool /tmp/resp.json || true

say "56) 累计上限：组1 共10人已清点4，再登记7人 -> 409 COUNT_EXCEEDS_GROUP_SIZE；登记6人成功"
curl -s -o /tmp/resp.json -w 'HTTP %{http_code}\n' -X POST "$BASE/exit-counts" \
  -H 'Content-Type: application/json' \
  -d '{"serial": "C-0002", "group_id": 1, "exit_id": 6, "count": 7, "counted_minute": 3}'
python3 -m json.tool /tmp/resp.json || true
curl -s -X POST "$BASE/exit-counts" \
  -H 'Content-Type: application/json' \
  -d '{"serial": "C-0002", "group_id": 1, "exit_id": 6, "count": 6, "counted_minute": 3}' \
  | python3 -m json.tool

say "57) 更正：凭流水号+版本1+非空原因把 C-0001 更正为 3 人（保留原值与历史）"
curl -s -X POST "$BASE/exit-counts/C-0001/correction" \
  -H 'Content-Type: application/json' \
  -d '{"version": 1, "count": 3, "reason": "复核实为 3 人"}' | python3 -m json.tool

say "58) 更正校验：对同一版本重复更正 -> 409 VERSION_ALREADY_CORRECTED；版本失配 -> 409 VERSION_MISMATCH"
curl -s -o /tmp/resp.json -w 'HTTP %{http_code}\n' -X POST "$BASE/exit-counts/C-0001/correction" \
  -H 'Content-Type: application/json' \
  -d '{"version": 1, "count": 2, "reason": "重复更正"}'
python3 -m json.tool /tmp/resp.json || true
curl -s -o /tmp/resp.json -w 'HTTP %{http_code}\n' -X POST "$BASE/exit-counts/C-0001/correction" \
  -H 'Content-Type: application/json' \
  -d '{"version": 9, "count": 2, "reason": "版本失配"}'
python3 -m json.tool /tmp/resp.json || true

say "59) 按组查询清点台账：应到/已清点/未清点与逐条记录（含更正历史）"
curl -s "$BASE/exit-counts/1" | python3 -m json.tool
curl -s "$BASE/exit-counts" | python3 -m json.tool

say "60) 救援送达组按实际出口与时刻清点：待救援场景 + 派遣 + 结案（出口6、送达4）"
curl -s -X POST "$BASE/scenario" \
  -H 'Content-Type: application/json' \
  -d '{
    "passages": [
      {"id": 1, "src": 1, "dst": 2, "minutes": 4, "capacity": 10},
      {"id": 2, "src": 2, "dst": 6, "minutes": 1, "capacity": 10}
    ],
    "exits": [5, 6],
    "closures": [{"edge_id": 2, "start_minute": 5, "end_minute": 30}],
    "groups": [
      {"id": 1, "size": 10, "origin": 1, "earliest_departure": 0, "priority": 10, "deadline": 30}
    ]
  }' > /dev/null
curl -s -X POST "$BASE/tick" -H 'Content-Type: application/json' \
  -d '{"minutes": 4}' > /dev/null
curl -s -X POST "$BASE/groups/1/delay-report" -H 'Content-Type: application/json' \
  -d '{"node_id": 2, "recovery_minute": 6}' > /dev/null
curl -s -X POST "$BASE/rescue-teams" -H 'Content-Type: application/json' \
  -d '{"team_id": 1, "capacity": 10}' > /dev/null
curl -s -X POST "$BASE/rescue-tasks" -H 'Content-Type: application/json' \
  -d '{"group_id": 1, "team_id": 1, "node_id": 2, "observed_minute": 4}' > /tmp/dispatch.json
TASK_ID=$(python3 -c 'import json; print(json.load(open("/tmp/dispatch.json"))["task_id"])')
curl -s -X POST "$BASE/rescue-tasks/$TASK_ID/completion" -H 'Content-Type: application/json' \
  -d '{"exit_id": 6, "delivery_minute": 4}' > /dev/null
curl -s -X POST "$BASE/exit-counts" -H 'Content-Type: application/json' \
  -d '{"serial": "C-1001", "group_id": 1, "exit_id": 6, "count": 10, "counted_minute": 4}' \
  | python3 -m json.tool
curl -s "$BASE/exit-counts/1" | python3 -m json.tool

say "61) 场景重置清空全部清点记录（组1 台账归零、流水号可复用）"
curl -s -X POST "$BASE/scenario" \
  -H 'Content-Type: application/json' \
  -d '{
    "passages": [{"id": 1, "src": 1, "dst": 6, "minutes": 2, "capacity": 20}],
    "exits": [6],
    "closures": [],
    "groups": [
      {"id": 1, "size": 10, "origin": 1, "earliest_departure": 0, "priority": 10, "deadline": 30}
    ]
  }' > /dev/null
curl -s "$BASE/exit-counts" | python3 -m json.tool

# ---- 逐段实际通行核报（已出发组沿现行路线核报实际进入/离开分钟）----
say "62) 重置为两段路线（1->2 四分钟、2->6 两分钟），推进到第6分钟：组1 fixed"
curl -s -X POST "$BASE/scenario" \
  -H 'Content-Type: application/json' \
  -d '{
    "passages": [
      {"id": 1, "src": 1, "dst": 2, "minutes": 4, "capacity": 10},
      {"id": 2, "src": 2, "dst": 6, "minutes": 2, "capacity": 10}
    ],
    "exits": [5, 6],
    "closures": [],
    "groups": [
      {"id": 1, "size": 10, "origin": 1, "earliest_departure": 0, "priority": 10, "deadline": 30}
    ]
  }' > /dev/null
curl -s -X POST "$BASE/tick" -H 'Content-Type: application/json' \
  -d '{"minutes": 6}' > /dev/null
curl -s "$BASE/plan" | python3 -m json.tool

say "63) 核报校验：未出发不可核报；段不存在/通道不符/越序/离开晚于当前 均 409"
# 重新造一个尚未出发的组 2 演示 NOT_DEPARTED
curl -s -X POST "$BASE/groups" -H 'Content-Type: application/json' \
  -d '[{"id": 2, "size": 5, "origin": 1, "earliest_departure": 10, "priority": 5, "deadline": 30}]' \
  > /dev/null
curl -s -o /tmp/resp.json -w 'HTTP %{http_code}\n' -X POST "$BASE/traversal-reports" \
  -H 'Content-Type: application/json' \
  -d '{"report_id": "V-NA", "group_id": 2, "seq": 0, "edge_id": 1, "enter_minute": 0, "exit_minute": 4}'
python3 -m json.tool /tmp/resp.json || true
# 越序：先报 seq=1
curl -s -o /tmp/resp.json -w 'HTTP %{http_code}\n' -X POST "$BASE/traversal-reports" \
  -H 'Content-Type: application/json' \
  -d '{"report_id": "V-OO", "group_id": 1, "seq": 1, "edge_id": 2, "enter_minute": 4, "exit_minute": 6}'
python3 -m json.tool /tmp/resp.json || true
# 通道不符
curl -s -o /tmp/resp.json -w 'HTTP %{http_code}\n' -X POST "$BASE/traversal-reports" \
  -H 'Content-Type: application/json' \
  -d '{"report_id": "V-EM", "group_id": 1, "seq": 0, "edge_id": 2, "enter_minute": 0, "exit_minute": 4}'
python3 -m json.tool /tmp/resp.json || true
# 段不存在
curl -s -o /tmp/resp.json -w 'HTTP %{http_code}\n' -X POST "$BASE/traversal-reports" \
  -H 'Content-Type: application/json' \
  -d '{"report_id": "V-SN", "group_id": 1, "seq": 9, "edge_id": 1, "enter_minute": 0, "exit_minute": 4}'
python3 -m json.tool /tmp/resp.json || true

say "64) 正式核报：组1 第0段（通道1）实际 [0,4)；偏离计划（计划[0,4)，无偏离）"
curl -s -X POST "$BASE/traversal-reports" -H 'Content-Type: application/json' \
  -d '{"report_id": "T-0001", "group_id": 1, "seq": 0, "edge_id": 1, "enter_minute": 0, "exit_minute": 4}' \
  | python3 -m json.tool

say "65) 核报第1段（通道2）实绩 [4,6)；顺序与时序校验（进入不早于前段离开）"
curl -s -X POST "$BASE/traversal-reports" -H 'Content-Type: application/json' \
  -d '{"report_id": "T-0002", "group_id": 1, "seq": 1, "edge_id": 2, "enter_minute": 4, "exit_minute": 6}' \
  | python3 -m json.tool
# 同段重复报送（换报送号）-> 409 DUPLICATE_SEQ
curl -s -o /tmp/resp.json -w 'HTTP %{http_code}\n' -X POST "$BASE/traversal-reports" \
  -H 'Content-Type: application/json' \
  -d '{"report_id": "T-DUP", "group_id": 1, "seq": 0, "edge_id": 1, "enter_minute": 0, "exit_minute": 4}'
python3 -m json.tool /tmp/resp.json || true

say "66) 幂等：同报送号同内容重试 -> HTTP 200 返回原记录；异内容 -> 409"
curl -s -o /tmp/resp.json -w 'HTTP %{http_code}\n' -X POST "$BASE/traversal-reports" \
  -H 'Content-Type: application/json' \
  -d '{"report_id": "T-0001", "group_id": 1, "seq": 0, "edge_id": 1, "enter_minute": 0, "exit_minute": 4}'
python3 -m json.tool /tmp/resp.json || true
curl -s -o /tmp/resp.json -w 'HTTP %{http_code}\n' -X POST "$BASE/traversal-reports" \
  -H 'Content-Type: application/json' \
  -d '{"report_id": "T-0001", "group_id": 1, "seq": 0, "edge_id": 1, "enter_minute": 1, "exit_minute": 4}'
python3 -m json.tool /tmp/resp.json || true

say "67) 查询：计划/实绩时刻并列、未核报段、通道逐分钟实绩人数与异常"
curl -s "$BASE/traversal-reports/1" | python3 -m json.tool
curl -s "$BASE/traversal-reports" | python3 -m json.tool

say "68) 异常演示：实绩命中封闭（CLOSED）且合计超容量（OVER_CAPACITY 35>30），只留痕不改编排"
curl -s -X POST "$BASE/scenario" \
  -H 'Content-Type: application/json' \
  -d '{
    "passages": [
      {"id": 1, "src": 1, "dst": 2, "minutes": 4, "capacity": 30},
      {"id": 2, "src": 2, "dst": 6, "minutes": 1, "capacity": 30}
    ],
    "exits": [6],
    "closures": [{"edge_id": 1, "start_minute": 3, "end_minute": 10}],
    "groups": [
      {"id": 1, "size": 20, "origin": 1, "earliest_departure": 0, "priority": 10, "deadline": 30},
      {"id": 2, "size": 15, "origin": 1, "earliest_departure": 0, "priority": 5, "deadline": 30}
    ]
  }' > /dev/null
# 计划因封闭把两组排在第10分钟后；推进到第20分钟，两组均 fixed
curl -s -X POST "$BASE/tick" -H 'Content-Type: application/json' \
  -d '{"minutes": 20}' > /dev/null
# 现场如实核报两组同一实绩区间 [2,6)：分钟3..5 命中封闭 CLOSED；
# 分钟2..5 实绩合计 20+15=35 > 当时有效容量 30 -> OVER_CAPACITY
curl -s -X POST "$BASE/traversal-reports" -H 'Content-Type: application/json' \
  -d '{"report_id": "T-1001", "group_id": 1, "seq": 0, "edge_id": 1, "enter_minute": 2, "exit_minute": 6}' \
  > /dev/null
curl -s -X POST "$BASE/traversal-reports" -H 'Content-Type: application/json' \
  -d '{"report_id": "T-1002", "group_id": 2, "seq": 0, "edge_id": 1, "enter_minute": 2, "exit_minute": 6}' \
  > /dev/null
curl -s "$BASE/traversal-reports" | python3 -c \
  'import json,sys; d=json.load(sys.stdin); print(json.dumps([m for m in d["edge_minutes"] if m["anomalies"]], ensure_ascii=False, indent=2))'

say "69) 节点滞留改编：固定前缀实绩保留，旧余程核报失效留痕，新余程重新核报"
curl -s -X POST "$BASE/scenario" \
  -H 'Content-Type: application/json' \
  -d '{
    "passages": [
      {"id": 1, "src": 1, "dst": 2, "minutes": 4, "capacity": 10},
      {"id": 2, "src": 2, "dst": 6, "minutes": 2, "capacity": 10}
    ],
    "exits": [5, 6],
    "closures": [],
    "groups": [
      {"id": 1, "size": 10, "origin": 1, "earliest_departure": 0, "priority": 10, "deadline": 30}
    ]
  }' > /dev/null
curl -s -X POST "$BASE/tick" -H 'Content-Type: application/json' \
  -d '{"minutes": 4}' > /dev/null
# 实绩比计划快：seq0 [0,2)、seq1 [2,4)
curl -s -X POST "$BASE/traversal-reports" -H 'Content-Type: application/json' \
  -d '{"report_id": "T-2001", "group_id": 1, "seq": 0, "edge_id": 1, "enter_minute": 0, "exit_minute": 2}' > /dev/null
curl -s -X POST "$BASE/traversal-reports" -H 'Content-Type: application/json' \
  -d '{"report_id": "T-2002", "group_id": 1, "seq": 1, "edge_id": 2, "enter_minute": 2, "exit_minute": 4}' > /dev/null
# 在节点2报告滞留、第6分钟恢复：旧 seq1 核报被标记失效留痕
curl -s -X POST "$BASE/groups/1/delay-report" -H 'Content-Type: application/json' \
  -d '{"node_id": 2, "recovery_minute": 6}' | python3 -m json.tool
curl -s "$BASE/traversal-reports/1" | python3 -m json.tool

say "70) 场景重置清空本场全部实绩（核报台账归零、报送号可复用）"
curl -s -X POST "$BASE/scenario" \
  -H 'Content-Type: application/json' \
  -d '{
    "passages": [{"id": 1, "src": 1, "dst": 6, "minutes": 2, "capacity": 20}],
    "exits": [6],
    "closures": [],
    "groups": [
      {"id": 1, "size": 10, "origin": 1, "earliest_departure": 0, "priority": 10, "deadline": 30}
    ]
  }' > /dev/null
curl -s "$BASE/traversal-reports" | python3 -m json.tool

say "71) 烟雾暴露：重置双路场景，为组3（节点2）登记累计暴露上限 3"
curl -s -X POST "$BASE/scenario" \
  -H 'Content-Type: application/json' \
  -d '{
    "passages": [
      {"id": 1, "src": 1, "dst": 2, "minutes": 4, "capacity": 30},
      {"id": 2, "src": 2, "dst": 3, "minutes": 3, "capacity": 30},
      {"id": 3, "src": 3, "dst": 5, "minutes": 2, "capacity": 30},
      {"id": 5, "src": 2, "dst": 6, "minutes": 2, "capacity": 40}
    ],
    "exits": [5, 6],
    "closures": [],
    "groups": [
      {"id": 1, "size": 30, "origin": 1, "earliest_departure": 0, "priority": 10, "deadline": 30},
      {"id": 3, "size": 10, "origin": 2, "earliest_departure": 0, "priority": 20, "deadline": 20}
    ]
  }' > /dev/null
curl -s -X POST "$BASE/smoke-limits" -H 'Content-Type: application/json' \
  -d '{"group_id": 3, "smoke_limit": 3}' | python3 -m json.tool

say "72) 通道5 第0..2分钟每分钟暴露5：立即通过累计10超上限，组3 等烟雾消散后第2分钟进入"
curl -s -X POST "$BASE/smoke-windows" -H 'Content-Type: application/json' \
  -d '{"edge_id": 5, "start_minute": 0, "end_minute": 2, "smoke_value": 5}' \
  | python3 -m json.tool
curl -s "$BASE/plan" | python3 -m json.tool

say "73) 列出烟雾暴露时段与各组上限"
curl -s "$BASE/smoke-windows" | python3 -m json.tool
curl -s "$BASE/smoke-limits" | python3 -m json.tool

say "74) 登记校验：同通道时段重叠 409；通道不存在/起止无效/负值 422；起点早于当前 422"
curl -s -o /tmp/resp.json -w 'HTTP %{http_code}\n' -X POST "$BASE/smoke-windows" \
  -H 'Content-Type: application/json' \
  -d '{"edge_id": 5, "start_minute": 1, "end_minute": 3, "smoke_value": 1}'
python3 -m json.tool /tmp/resp.json || true
curl -s -o /dev/null -w '未知通道 HTTP %{http_code}\n' -X POST "$BASE/smoke-windows" \
  -H 'Content-Type: application/json' \
  -d '{"edge_id": 999, "start_minute": 0, "end_minute": 2, "smoke_value": 1}'

say "75) 推进1分钟后组3 fixed（通道5 占用[1,2)）：窗口[1,5)值4使其预计暴露4>上限3 -> 409 整次拒绝"
curl -s -X POST "$BASE/tick" -H 'Content-Type: application/json' -d '{"minutes": 1}' > /dev/null
curl -s -o /tmp/resp.json -w 'HTTP %{http_code}\n' -X POST "$BASE/smoke-windows" \
  -H 'Content-Type: application/json' \
  -d '{"edge_id": 5, "start_minute": 1, "end_minute": 5, "smoke_value": 4}'
python3 -m json.tool /tmp/resp.json || true
# 值3恰好不超限 -> 受理；/plan 中前缀已用0、余程预计3
curl -s -X POST "$BASE/smoke-windows" -H 'Content-Type: application/json' \
  -d '{"edge_id": 5, "start_minute": 1, "end_minute": 5, "smoke_value": 3}' \
  | python3 -m json.tool

say "76) 已出发组再登记暴露上限 -> 409 SMOKE_LIMIT_TARGET_DEPARTED（整次拒绝）"
curl -s -o /tmp/resp.json -w 'HTTP %{http_code}\n' -X POST "$BASE/smoke-limits" \
  -H 'Content-Type: application/json' \
  -d '{"group_id": 3, "smoke_limit": 99}'
python3 -m json.tool /tmp/resp.json || true

say "77) 场景重置清空全部烟雾暴露时段与各组上限（旧修订保留）"
curl -s -X POST "$BASE/scenario" \
  -H 'Content-Type: application/json' \
  -d '{
    "passages": [{"id": 1, "src": 1, "dst": 6, "minutes": 2, "capacity": 20}],
    "exits": [6],
    "closures": [],
    "groups": [
      {"id": 1, "size": 10, "origin": 1, "earliest_departure": 0, "priority": 10, "deadline": 30}
    ]
  }' > /dev/null
curl -s "$BASE/smoke-windows" | python3 -m json.tool
curl -s "$BASE/smoke-limits" | python3 -m json.tool

# ---- 中途节点计划滞留限流 ----

say "78) 重置节点限流演示场景：两组各10人 1->2(4)，通道5(2->6,1分钟) 封闭[4,6)"
curl -s -X POST "$BASE/scenario" \
  -H 'Content-Type: application/json' \
  -d '{
    "passages": [
      {"id": 1, "src": 1, "dst": 2, "minutes": 4, "capacity": 100},
      {"id": 2, "src": 2, "dst": 3, "minutes": 9, "capacity": 100},
      {"id": 3, "src": 3, "dst": 5, "minutes": 9, "capacity": 100},
      {"id": 5, "src": 2, "dst": 6, "minutes": 1, "capacity": 100}
    ],
    "exits": [5, 6],
    "closures": [{"edge_id": 5, "start_minute": 4, "end_minute": 6}],
    "groups": [
      {"id": 1, "size": 10, "origin": 1, "earliest_departure": 0, "priority": 10, "deadline": 40},
      {"id": 2, "size": 10, "origin": 1, "earliest_departure": 0, "priority": 5, "deadline": 40}
    ]
  }' > /dev/null

say "79) 登记节点2第4..6分钟每分钟最多15人：组1在节点2等待[4,6)，组2错峰（推迟通道1到第2分钟进入）"
curl -s -X POST "$BASE/node-controls" \
  -H 'Content-Type: application/json' \
  -d '{"node_id": 2, "start_minute": 4, "end_minute": 6, "capacity": 15}' \
  | python3 -m json.tool

say "80) 查询节点2逐分钟计划停留人数（两组错峰，第4/5分钟各10人）"
curl -s "$BASE/nodes/2/stays" | python3 -m json.tool

say "81) 无效登记：节点不存在(422)、出口节点6(422)、上限0(422)、时段无效(422)"
for body in \
  '{"node_id": 99, "start_minute": 0, "end_minute": 5, "capacity": 3}' \
  '{"node_id": 6, "start_minute": 0, "end_minute": 5, "capacity": 3}' \
  '{"node_id": 2, "start_minute": 0, "end_minute": 5, "capacity": 0}' \
  '{"node_id": 2, "start_minute": 5, "end_minute": 5, "capacity": 3}'; do
  curl -s -o /tmp/resp.json -w 'HTTP %{http_code}\n' -X POST "$BASE/node-controls" \
    -H 'Content-Type: application/json' -d "$body"
done

say "82) 同节点时段重叠 -> 409 NODE_CONTROL_OVERLAP（整次拒绝）"
curl -s -o /tmp/resp.json -w 'HTTP %{http_code}\n' -X POST "$BASE/node-controls" \
  -H 'Content-Type: application/json' \
  -d '{"node_id": 2, "start_minute": 5, "end_minute": 9, "capacity": 15}'
python3 -m json.tool /tmp/resp.json || true
# 端点相接（起点=已有终点6）允许
curl -s -o /dev/null -w '端点相接 HTTP %{http_code}\n' -X POST "$BASE/node-controls" \
  -H 'Content-Type: application/json' \
  -d '{"node_id": 2, "start_minute": 6, "end_minute": 9, "capacity": 15}'

say "83) 推进到第4分钟两组均已固定后，登记使固定等待超额的限流(每分钟5人) -> 409"
curl -s -X POST "$BASE/tick" -H 'Content-Type: application/json' -d '{"minutes": 4}' > /dev/null
curl -s -o /tmp/resp.json -w 'HTTP %{http_code}\n' -X POST "$BASE/node-controls" \
  -H 'Content-Type: application/json' \
  -d '{"node_id": 2, "start_minute": 4, "end_minute": 6, "capacity": 5}'
python3 -m json.tool /tmp/resp.json || true
# 名额能容纳（每分钟20人）时受理，已出发组路线不变
curl -s -o /dev/null -w '容量20可受理 HTTP %{http_code}\n' -X POST "$BASE/node-controls" \
  -H 'Content-Type: application/json' \
  -d '{"node_id": 2, "start_minute": 4, "end_minute": 6, "capacity": 20}'

say "84) 唯一路线场景：节点限流9人、报告滞留至第8分钟（固定等待10人超额）-> 待救援"
curl -s -X POST "$BASE/scenario" \
  -H 'Content-Type: application/json' \
  -d '{
    "passages": [
      {"id": 1, "src": 1, "dst": 2, "minutes": 2, "capacity": 30},
      {"id": 5, "src": 2, "dst": 6, "minutes": 1, "capacity": 30}
    ],
    "exits": [6],
    "closures": [],
    "groups": [
      {"id": 1, "size": 10, "origin": 1, "earliest_departure": 0, "priority": 10, "deadline": 40}
    ]
  }' > /dev/null
curl -s -X POST "$BASE/tick" -H 'Content-Type: application/json' -d '{"minutes": 2}' > /dev/null
curl -s -X POST "$BASE/node-controls" -H 'Content-Type: application/json' \
  -d '{"node_id": 2, "start_minute": 2, "end_minute": 8, "capacity": 9}' > /dev/null
curl -s -X POST "$BASE/groups/1/delay-report" -H 'Content-Type: application/json' \
  -d '{"node_id": 2, "recovery_minute": 8}' | python3 -m json.tool

say "85) 场景重置清空全部节点限流（旧修订保留）"
curl -s -X POST "$BASE/scenario" \
  -H 'Content-Type: application/json' \
  -d '{
    "passages": [{"id": 1, "src": 1, "dst": 6, "minutes": 2, "capacity": 20}],
    "exits": [6],
    "closures": [],
    "groups": [
      {"id": 1, "size": 10, "origin": 1, "earliest_departure": 0, "priority": 10, "deadline": 30}
    ]
  }' > /dev/null
curl -s "$BASE/node-controls" | python3 -m json.tool

say "完成。交互文档： $BASE/docs"