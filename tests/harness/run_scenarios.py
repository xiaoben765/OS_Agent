#!/usr/bin/env python3

from __future__ import annotations

import argparse
import io
import json
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Iterable, List, Optional, Tuple

import yaml


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.service.audit import AuditStore
from src.service.orchestrator import TaskOrchestrator
from tests.test_task_service import FakeExecutor, FakeProvider, GeneralizationProvider


SCENARIO_DIR = Path(__file__).resolve().parent / "scenarios"


@dataclass
class ScenarioResult:
    suite: str
    scenario_id: str
    title: str
    passed: bool
    status: str
    commands: List[str] = field(default_factory=list)
    events: List[str] = field(default_factory=list)
    state_flow: List[str] = field(default_factory=list)
    intent_label: str = ""
    total_risk: str = ""
    audit_files: List[str] = field(default_factory=list)
    final_feedback: str = ""
    failures: List[str] = field(default_factory=list)


@dataclass
class RunSummary:
    total: int = 0
    passed: int = 0
    failed: int = 0
    results: List[ScenarioResult] = field(default_factory=list)


def build_default_security() -> SimpleNamespace:
    return SimpleNamespace(
        blocked_commands=[],
        confirm_patterns=[r"rm\s+-rf\s+", r"chmod\s+-R\s+777"],
        protected_paths=["/", "/etc", "/boot", "/usr", "/var/lib", "/root"],
        sensitive_files=["/etc/passwd", "/etc/shadow", "/etc/sudoers"],
    )


def load_scenario_file(path: str | Path) -> Dict[str, Any]:
    scenario_path = Path(path)
    data = yaml.safe_load(scenario_path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{scenario_path} 必须是 YAML 对象")
    scenarios = data.get("scenarios")
    if not isinstance(scenarios, list) or not scenarios:
        raise ValueError(f"{scenario_path} 缺少 scenarios 列表")
    data["suite"] = str(data.get("suite") or scenario_path.stem)
    return data


def default_scenario_paths() -> List[Path]:
    return sorted(SCENARIO_DIR.glob("*.yaml"))


def build_provider(provider_spec: Dict[str, Any]):
    provider_kind = str((provider_spec or {}).get("kind", "scripted")).strip().lower()
    if provider_kind == "generalization":
        return GeneralizationProvider()
    if provider_kind == "scripted":
        analysis = (provider_spec or {}).get("analysis")
        if isinstance(analysis, dict):
            analysis = {
                command: {
                    "explanation": str((item or {}).get("explanation", "命令执行完成")),
                    "recommendations": list((item or {}).get("recommendations", [])),
                    "next_steps": list((item or {}).get("next_steps", [])),
                }
                for command, item in analysis.items()
            }
        return FakeProvider((provider_spec or {}).get("response", {}), analysis_response=analysis)
    raise ValueError(f"不支持的 provider.kind: {provider_kind}")


def build_executor(executor_spec: Dict[str, Any]) -> FakeExecutor:
    raw_results = (executor_spec or {}).get("results", {})
    results: Dict[str, Any] = {}
    for command, item in raw_results.items():
        if isinstance(item, dict):
            results[command] = (
                str(item.get("stdout", "")),
                str(item.get("stderr", "")),
                int(item.get("return_code", 0)),
            )
        else:
            raise ValueError(f"命令 {command} 的结果必须是对象")
    return FakeExecutor(results=results)


def build_approval_callback(approvals: Dict[str, Any]):
    approvals = approvals or {}

    def approval_callback(prompt, level, step):
        command = getattr(step, "command", "")
        if command in approvals:
            return bool(approvals[command])
        if level in approvals:
            return bool(approvals[level])
        return bool(approvals.get("default", True))

    return approval_callback


def list_task_artifacts(task_dir: Path) -> List[str]:
    if not task_dir.exists():
        return []
    return sorted(path.name for path in task_dir.iterdir() if path.is_file())


def state_flow_from_events(events: List[Dict[str, Any]], trace) -> List[str]:
    transitions = [str(item.get("state", "")) for item in events if item.get("type") == "state_changed" and item.get("state")]
    if transitions:
        return transitions
    trace_transitions = (trace.trace_summary or {}).get("state_transitions", []) or []
    return [str(item.get("state", "")) for item in trace_transitions if item.get("state")]


def evaluate_expectations(
    scenario: Dict[str, Any],
    plan,
    trace,
    events: List[Dict[str, Any]],
    executor: FakeExecutor,
    task_dir: Path,
) -> List[str]:
    expect = scenario.get("expect", {})
    failures: List[str] = []
    if not isinstance(expect, dict):
        return ["expect 必须是对象"]

    expected_status = expect.get("status")
    if expected_status and trace.status != expected_status:
        failures.append(f"期望状态 {expected_status}，实际为 {trace.status}")

    expected_intent = expect.get("intent_label")
    if expected_intent and getattr(plan, "intent_label", "") != expected_intent:
        failures.append(f"期望意图标签 {expected_intent}，实际为 {getattr(plan, 'intent_label', '')}")

    expected_total_risk = expect.get("total_risk")
    if expected_total_risk and getattr(plan, "total_risk", "") != expected_total_risk:
        failures.append(f"期望总风险 {expected_total_risk}，实际为 {getattr(plan, 'total_risk', '')}")

    expected_risk_classes = expect.get("risk_classifications")
    if expected_risk_classes is not None:
        actual_classes = [
            getattr(getattr(step, "risk", None), "classification", "")
            for step in getattr(plan, "steps", [])
        ]
        if actual_classes != list(expected_risk_classes):
            failures.append(f"期望风险分类 {list(expected_risk_classes)}，实际为 {actual_classes}")

    expected_commands = expect.get("commands")
    if expected_commands is not None and executor.commands != list(expected_commands):
        failures.append(f"期望命令序列 {list(expected_commands)}，实际为 {executor.commands}")

    for command in list(expect.get("commands_contain", [])):
        if command not in executor.commands:
            failures.append(f"缺少期望命令 {command}")

    event_types = [event.get("type", "") for event in events]
    for event_type in expect.get("events", []):
        if event_type not in event_types:
            failures.append(f"缺少期望事件 {event_type}")

    expected_state_flow = expect.get("state_flow")
    if expected_state_flow is not None:
        actual_state_flow = state_flow_from_events(events, trace)
        if actual_state_flow != list(expected_state_flow):
            failures.append(f"期望状态流 {list(expected_state_flow)}，实际为 {actual_state_flow}")

    expected_audit_files = expect.get("audit_files")
    if expected_audit_files is not None:
        actual_files = list_task_artifacts(task_dir)
        for filename in expected_audit_files:
            if filename not in actual_files:
                failures.append(f"缺少期望审计产物 {filename}")

    for fragment in expect.get("final_feedback_contains", []):
        if fragment not in trace.final_feedback:
            failures.append(f"最终反馈未包含 {fragment}")

    expected_trace_summary = expect.get("trace_summary", {})
    for key, expected_value in expected_trace_summary.items():
        actual_value = trace.trace_summary.get(key)
        if actual_value != expected_value:
            failures.append(f"trace_summary[{key}] 期望 {expected_value}，实际为 {actual_value}")

    return failures


def run_single_turn_scenario(suite_name: str, scenario: Dict[str, Any], temp_dir: str) -> Tuple[Any, Any, List[Dict[str, Any]], FakeExecutor, Path]:
    audit_store = AuditStore(temp_dir)
    provider = build_provider(scenario.get("provider", {}))
    executor = build_executor(scenario.get("executor", {}))
    orchestrator = TaskOrchestrator(
        provider,
        executor,
        build_default_security(),
        audit_store=audit_store,
        analysis_mode=str(scenario.get("analysis_mode", "smart")),
    )
    events: List[Dict[str, Any]] = []
    session_id = f"{suite_name}:{scenario.get('id', 'scenario')}"
    plan = orchestrator.build_plan(
        str(scenario.get("input", "")),
        source="harness",
        session_id=session_id,
        event_callback=events.append,
    )
    trace = orchestrator.execute_plan(
        plan,
        approval_callback=build_approval_callback(scenario.get("approvals", {})),
        event_callback=events.append,
    )
    task_dir = Path(temp_dir) / plan.task_id
    return plan, trace, events, executor, task_dir


def merge_clarification_input(pending: Dict[str, str], user_input: str) -> str:
    return (
        f"{pending.get('original_input', '')}\n"
        f"补充说明: {user_input}\n"
        f"请基于以上补充继续完成原任务。缺失信息提示: {pending.get('clarification_prompt', '请补充任务缺失信息。')}"
    )


def run_multi_turn_scenario(suite_name: str, scenario: Dict[str, Any], temp_dir: str) -> Tuple[Any, Any, List[Dict[str, Any]], FakeExecutor, Path]:
    turns = list(scenario.get("turns", []))
    if not turns:
        raise ValueError(f"场景 {scenario.get('id', 'scenario')} 缺少 turns")

    audit_store = AuditStore(temp_dir)
    events: List[Dict[str, Any]] = []
    combined_commands: List[str] = []
    last_executor: Optional[FakeExecutor] = None
    pending: Optional[Dict[str, str]] = None
    plan = trace = None
    session_id = f"{suite_name}:{scenario.get('id', 'scenario')}"

    for turn in turns:
        provider = build_provider(turn.get("provider", {}))
        executor = build_executor(turn.get("executor", {}))
        orchestrator = TaskOrchestrator(
            provider,
            executor,
            build_default_security(),
            audit_store=audit_store,
            analysis_mode=str(turn.get("analysis_mode", scenario.get("analysis_mode", "smart"))),
        )
        user_input = str(turn.get("input", ""))
        if pending:
            user_input = merge_clarification_input(pending, user_input)

        plan = orchestrator.build_plan(
            user_input,
            source="harness",
            session_id=session_id,
            task_id=pending.get("task_id") if pending else None,
            event_callback=events.append,
        )
        trace = orchestrator.execute_plan(
            plan,
            approval_callback=build_approval_callback(turn.get("approvals", scenario.get("approvals", {}))),
            event_callback=events.append,
        )
        combined_commands.extend(executor.commands)
        last_executor = executor

        if trace.status == "needs_clarification":
            pending = {
                "task_id": plan.task_id,
                "original_input": plan.original_input,
                "clarification_prompt": plan.clarification_prompt,
            }
        else:
            pending = None

    assert plan is not None and trace is not None and last_executor is not None
    last_executor.commands = combined_commands
    task_dir = Path(temp_dir) / plan.task_id
    return plan, trace, events, last_executor, task_dir


def run_scenario(suite_name: str, scenario: Dict[str, Any]) -> ScenarioResult:
    with tempfile.TemporaryDirectory() as temp_dir:
        if scenario.get("turns"):
            plan, trace, events, executor, task_dir = run_multi_turn_scenario(suite_name, scenario, temp_dir)
        else:
            plan, trace, events, executor, task_dir = run_single_turn_scenario(suite_name, scenario, temp_dir)

        failures = evaluate_expectations(scenario, plan, trace, events, executor, task_dir)
        return ScenarioResult(
            suite=suite_name,
            scenario_id=str(scenario.get("id", "scenario")),
            title=str(scenario.get("title", "")),
            passed=not failures,
            status=trace.status,
            commands=list(executor.commands),
            events=[event.get("type", "") for event in events],
            state_flow=state_flow_from_events(events, trace),
            intent_label=getattr(plan, "intent_label", ""),
            total_risk=getattr(plan, "total_risk", ""),
            audit_files=list_task_artifacts(task_dir),
            final_feedback=trace.final_feedback,
            failures=failures,
        )


def iter_scenarios(paths: Iterable[str | Path]) -> Iterable[tuple[str, Dict[str, Any]]]:
    for path in paths:
        payload = load_scenario_file(path)
        suite_name = str(payload.get("suite", Path(path).stem))
        for scenario in payload.get("scenarios", []):
            yield suite_name, scenario


def render_result(stream: io.TextIOBase, result: ScenarioResult) -> None:
    label = "PASS" if result.passed else "FAIL"
    title = f" | {result.title}" if result.title else ""
    stream.write(
        f"[{label}] {result.suite}/{result.scenario_id}{title} | status={result.status} | intent={result.intent_label} | risk={result.total_risk} | commands={json.dumps(result.commands, ensure_ascii=False)}\n"
    )
    if result.failures:
        for failure in result.failures:
            stream.write(f"  - {failure}\n")


def run_suite(paths: Iterable[str | Path], stream: Optional[io.TextIOBase] = None) -> RunSummary:
    stream = stream or sys.stdout
    summary = RunSummary()
    for suite_name, scenario in iter_scenarios(paths):
        result = run_scenario(suite_name, scenario)
        summary.results.append(result)
        summary.total += 1
        if result.passed:
            summary.passed += 1
        else:
            summary.failed += 1
        render_result(stream, result)
    stream.write(f"SUMMARY TOTAL {summary.total} | PASS {summary.passed} | FAIL {summary.failed}\n")
    return summary


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run hackathon scenario harness.")
    parser.add_argument(
        "paths",
        nargs="*",
        help="YAML scenario files. Defaults to tests/harness/scenarios/*.yaml",
    )
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    paths = [Path(path) for path in args.paths] if args.paths else default_scenario_paths()
    if not paths:
        print("未找到任何场景文件。", file=sys.stderr)
        return 1
    summary = run_suite(paths)
    return 0 if summary.failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
