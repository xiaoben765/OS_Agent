# 架构说明

## 总体方案

OS_Agent 采用“CLI 单入口 + 共享编排内核”设计：

- `CLI` 是唯一交互入口，负责终端交互、风险确认和实际命令执行。
- `src/service/` 作为共享任务编排层，统一处理意图到命令计划、风险判定、步骤执行、结果分析和审计落盘。

## 关键模块

### 1. 任务编排层

位于 `src/service/`，核心对象包括：

- `TaskRequest`：记录原始输入、来源和任务编号。
- `TaskPlan`：结构化执行计划，包含环境摘要、步骤列表、总风险和确认要求。
- `TaskStep`：每一步的目标、命令、预期结果和失败即停策略。
- `RiskAssessment`：对单步命令给出 `low / medium / high / blocked` 分级和解释。
- `ExecutionTrace`：记录执行轨迹、确认信息、输出摘要和最终反馈。

当前 CLI 主链已从一次性顺序执行演进为保守版 agent 执行链：

- `plan`：模型给出初始结构化计划
- `adapt`：根据发行版差异修正包管理、网络诊断等命令
- `act`：执行当前步骤
- `observe`：采集 stdout / stderr / return_code
- `reflect`：根据观察结果决定 `continue / revise_remaining_steps / recover / stop`
- `continue`：在保持现有审计结构兼容的前提下调整剩余步骤或进入恢复链
- `resume`：若任务进入 `clarify`，用户补充参数后可直接续跑原任务，而不是重新完整描述

### 2. 风险控制

风控逻辑位于 `src/service/risk.py`，判断依据包含：

- 禁用命令白名单/黑名单规则
- 正则风险模式
- 关键路径删除
- 敏感系统文件修改
- 大范围权限变更
- 用户与权限管理操作
- 服务管理与系统级安装操作

处理规则：

- `blocked`：直接拒绝执行
- `high`：强制确认
- `medium`：要求确认
- `low`：直接执行

### 3. 审计与回放

审计数据由 `src/service/audit.py` 写入 `service.audit_dir`：

- `request.json`
- `plan.json`
- `events.jsonl`
- `result.json`
- `report.md`

这套文件既是问题排查与结果复盘的数据源，也是视频录制、截图和答辩的证据材料，其中 `report.md` 适合直接向评委展示任务输入、环境依据、执行步骤和最终反馈。

## 设计取舍

- 没有引入数据库，避免增加部署复杂度。
- 没有再维护第二套交互入口，避免 CLI 与其他入口之间出现行为漂移。
- 仍保留原有 `agent.py` 逻辑作为兜底，降低改造风险。
- 本地意图规则仅保留为安全和观测兜底，不再主导具体 Linux 任务语义。
