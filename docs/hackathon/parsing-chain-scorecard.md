# 解析链专项对照表

本表用于按赛题语言说明当前 OS_Agent 主链 `process_user_input -> TaskOrchestrator` 的解析能力、缺口和证据。

| 赛题要求 | 当前实现位置 | 当前状态 | 改进后表现 | 演示/证据 |
| --- | --- | --- | --- | --- |
| 输入理解 | `src/service/orchestrator.py` | 半达标 | `intent_label` 与 `input_understanding` 已结构化；当前以模型返回为主、本地规则为兜底，可覆盖赛题样例之外的开放任务语义 | CLI 计划摘要、`plan.json`、`tests/test_task_service.py` |
| 意图归一化 | `TaskPlan` | 半达标 | `TaskPlan.intent_label` 与 `TaskPlan.user_intent` 同时保留开放语义标签与人类可读意图总结，不要求固定枚举 | `docs/hackathon/demo-scenarios.md` 场景 1-6 |
| 任务拆解 | `TaskPlan.steps` | 已达标 | 每个步骤补充 `expected_outcome`、`selection_reason`、`environment_rationale`，从“有命令”提升为“有明确步骤说明” | 多步创建用户、连续任务演示、`tests/test_task_service.py` |
| 环境/工具选择 | `environment_summary` + `TaskStep.environment_rationale` | 半达标 | 让 `df -h`、`find`、`ss`、`useradd` 等命令的环境选择依据显式进入计划和回放 | 端口查询、磁盘查询、`plan.json` |
| 风险判定 | `src/service/risk.py` | 已达标 | 保持阻断/确认逻辑，并在解析说明中关联风险解释 | 高风险删除阻断、普通用户创建确认 |
| 结果组织 | `ExecutionTrace` | 已达标 | 保持执行反馈与失败分析，同时增加 `clarify` 模式，避免信息不足时误执行 | 失败恢复建议、CLI 输出、`result.json` |
| 澄清/拒绝分支 | `TaskPlan.response_mode` | 未达标 -> 已补齐 | 对缺少用户名、缺少端口号、缺少删除路径等请求先进入 `clarify` 分支 | `创建一个普通用户` 场景、`tests/test_task_service.py` |

## 当前主链结论

- 基础要求：已满足自然语言输入、任务执行、风险控制、结果反馈。
- 高分观感：通过开放语义 `intent_label`、`parsing_summary`、`clarify` 分支和步骤级依据说明，主链更接近赛题描述的“先解析，再决策，再执行”。
- 兜底链说明：`agent.py` 旧流程仍保留，用于兼容回退；答辩时建议明确“共享编排器是主链，旧逻辑是稳定性兜底”。

## 建议答辩口径

1. 用户输入进入系统后，先做输入理解和意图归一化，而不是直接把自然语言透传给模型。
2. 当信息不足时，系统会进入 `clarify` 分支，先补充关键参数，再继续执行，体现自主且可控。
3. 当可以执行时，系统会输出显式步骤、命令依据、环境依据、风险级别和最终反馈，形成完整的赛题闭环。
