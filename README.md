# OS_Agent

OS_Agent 是一个基于大语言模型的 Linux 运维助手。当前仓库仅保留 CLI 交互链路：用户在终端输入自然语言，系统生成结构化执行计划、进行风险判断、执行命令，并把审计结果落盘到本地。

## 项目来源

- 本项目是在开源项目 LinuxAgent 的基础上进行的二次开发。
- 原项目作者与仓库：`Eilen6316` / `https://github.com/Eilen6316/LinuxAgent`
- 当前仓库在保留原有能力思路的基础上，面向 `OS_Agent` 方向做了命名、交互和工程化调整。

## 当前状态

- 交互入口：CLI
- 核心编排：`src/service/`
- 风险控制：`src/service/risk.py`
- 审计落盘：`~/.os_agent/tasks`
- 不再包含：`src/web/`、`frontend/` 以及对应 Web 依赖

## 主要能力

- 自然语言转 Linux 命令或多步任务计划
- 基于发行版差异自动适配包管理与诊断命令
- `clarify -> answer -> continue` 的澄清续跑闭环
- `safe / confirm / block` 三段式风险分类
- 风险预警、阻断与二次确认
- 显式状态流：`planning -> clarify / confirm -> execute -> analyze -> recover -> finish`
- 失败分析、恢复建议与任务反思
- 审计文件落盘，并自动生成可读任务报告，便于复盘和演示
- 内置知识库、推荐与上下文相关智能能力

## 环境要求

- Linux 环境
- Python 3.8+
- 可访问 LLM 服务的网络
- 可用的 LLM API Key

## 安装

```bash
git clone git@github.com:xiaoben765/OS_Agent.git
cd OS_Agent
pip install -r requirements.txt
```

## 配置

直接编辑根目录下的 `config.yaml`：

```yaml
api:
  provider: "openai"
  api_key: "your_api_key_here"
  base_url: "https://dashscope.aliyuncs.com/compatible-mode/v1"
  model: "qwen3.5-plus"
```

常用配置项：

- `api.provider`：当前支持的提供者
- `api.api_key`：模型服务密钥
- `api.base_url`：OpenAI 兼容服务地址
- `api.model`：默认模型名
- `security.confirm_dangerous_commands`：高风险命令是否强制确认
- `service.audit_dir`：任务审计目录

## 启动

```bash
python3 os_agent.py -c config.yaml
```

调试模式：

```bash
python3 os_agent.py -d -c config.yaml
```

查看版本：

```bash
python3 os_agent.py --version
```

## 使用示例

启动后会进入交互式提示符。可以直接输入自然语言，例如：

- `帮我查看当前磁盘剩余空间，并告诉我哪个分区最需要关注`
- `查看 8080 端口是否被占用，并说明对应进程`
- `创建一个普通用户 demo_user，并验证是否创建成功`
- `检查 nginx 服务状态，如果失败就帮我分析原因`

## 审计文件

每次任务执行后，默认会在 `~/.os_agent/tasks` 下生成一个任务目录，常见文件包括：

- `request.json`
- `plan.json`
- `events.jsonl`
- `result.json`
- `report.md`

这些文件可用于排障、复盘和演示留痕。

## 风险模型

OS_Agent 当前采用显式的三段式风险分类：

- `safe`：默认允许直接执行的观测型或低风险命令
- `confirm`：需要用户二次确认的安装、服务控制、用户管理、权限调整等操作
- `block`：默认拒绝执行的核心路径删除、敏感文件修改、高风险组合命令等操作

风险分类结果会写入计划、执行轨迹、`events.jsonl` 和 `report.md`，便于赛题答辩时展示“为什么执行 / 为什么阻断”。

## 测试与验证

自动化回归：

```bash
python3 -m unittest \
  tests/test_task_service.py \
  tests/test_agent_cli.py \
  tests/test_nlp_enhancer.py \
  tests/test_provider_proxy.py

python3 -m py_compile \
  os_agent.py \
  src/agent.py \
  src/config.py \
  src/service/*.py \
  src/providers/*.py \
  src/intelligence/*.py
```

场景化验证：

```bash
python3 tests/harness/run_scenarios.py
```

当前内置场景覆盖：

- 磁盘查询
- 文件检索
- 端口/进程检查
- 用户创建
- 用户删除
- 高风险删除阻断
- `clarify -> continue`
- `failure -> recovery`

更细的手工验收脚本可参考 [tests/test.md](./tests/test.md) 和 [docs/hackathon/self-test.md](./docs/hackathon/self-test.md)。

## 启动前配置校验

在正式启动 CLI 前，建议先执行一次 fail-fast 配置检查：

```bash
python3 os_agent.py --check -c config.yaml
```

这条命令会完成以下动作：

- 校验 `api.api_key` 是否缺失或仍为占位值
- 校验 `service.audit_dir` 是否可以创建且可写
- 校验日志、历史记录、智能化相关关键路径是否合法
- 输出一份脱敏后的配置摘要，避免在终端泄露密钥

如果校验失败，程序会直接退出并打印明确的错误项；如果校验通过，不会启动交互式 Agent。

## 一键验证入口

仓库提供了本地一键验证脚本：

```bash
bash scripts/self_check.sh
```

脚本会依次执行：

- `unittest discover`
- `py_compile`
- `tests/harness/run_scenarios.py`（仅当该文件存在时）

适合在提交前、联调前和赛前统一跑一遍。

## 赛前自检建议

建议在演示或答辩前按下面顺序执行：

1. 确认 `config.yaml` 中的模型提供者、`base_url`、`model` 和真实 API Key 已配置完成。
2. 运行 `python3 os_agent.py --check -c config.yaml`，确保配置校验通过，且输出中没有泄露任何 secret。
3. 运行 `bash scripts/self_check.sh`，确保单元测试、语法编译和场景验证全部通过。
4. 重点检查审计目录 `~/.os_agent/tasks` 是否可写，避免现场执行后没有审计产物。
5. 如需演示具体任务，再执行 `python3 os_agent.py -c config.yaml` 进入 CLI。

## 目录说明

```text
.
├── os_agent.py          # CLI 入口
├── config.yaml            # 运行配置
├── src/agent.py           # 交互式 CLI agent
├── src/service/           # 任务编排、风险控制、审计
├── src/providers/         # LLM 提供者实现
├── src/intelligence/      # 知识库、推荐、上下文等能力
├── tests/                 # 自动化与手工验证脚本
├── tests/harness/         # YAML 场景驱动的赛题预检
├── scripts/               # 本地自检与辅助脚本
└── docs/hackathon/        # 赛题说明、自测和演示材料
```

## 说明

- 本次整理后，仓库默认维护 CLI 主链，不再维护 Web 入口。
- `config.yaml` 中的密钥占位符需要替换成真实值后再运行。
