#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
任务审计持久化
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict


class AuditStore:
    """将任务请求、计划、事件和结果保存到文件系统"""

    def __init__(self, base_dir: str):
        self.base_dir = Path(base_dir).expanduser()
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def _task_dir(self, task_id: str) -> Path:
        task_dir = self.base_dir / task_id
        task_dir.mkdir(parents=True, exist_ok=True)
        return task_dir

    def _write_json(self, path: Path, payload: Dict[str, Any]) -> None:
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def save_request(self, request) -> None:
        self._write_json(self._task_dir(request.task_id) / "request.json", request.to_dict())

    def save_plan(self, plan) -> None:
        self._write_json(self._task_dir(plan.task_id) / "plan.json", plan.to_dict())

    def append_event(self, task_id: str, event: Dict[str, Any]) -> None:
        event_file = self._task_dir(task_id) / "events.jsonl"
        with event_file.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")

    def save_result(self, trace) -> None:
        task_dir = self._task_dir(trace.task_id)
        self._write_json(task_dir / "result.json", trace.to_dict())
        self._write_report(task_dir / "report.md", self._build_report(task_dir, trace))

    def _write_report(self, path: Path, content: str) -> None:
        path.write_text(content, encoding="utf-8")

    def _load_json_if_exists(self, path: Path) -> Dict[str, Any]:
        if not path.exists():
            return {}
        return json.loads(path.read_text(encoding="utf-8"))

    def _build_report(self, task_dir: Path, trace) -> str:
        request = self._load_json_if_exists(task_dir / "request.json")
        plan = self._load_json_if_exists(task_dir / "plan.json")
        trace_dict = trace.to_dict()
        event_lines = (task_dir / "events.jsonl").read_text(encoding="utf-8").splitlines() if (task_dir / "events.jsonl").exists() else []
        event_payloads = [json.loads(line) for line in event_lines if line.strip()]

        lines = [
            "# 任务报告",
            "",
            "## 基本信息",
            "",
            f"- 任务 ID: `{trace.task_id}`",
            f"- 状态: `{trace.status}`",
            f"- 原始输入: {request.get('user_input') or trace.original_input}",
            f"- 总风险: `{trace.total_risk}`",
            "",
            "## 环境摘要",
            "",
        ]

        environment_summary = plan.get("environment_summary", {})
        if environment_summary:
            for key, value in environment_summary.items():
                lines.append(f"- {key}: `{value}`")
        else:
            lines.append("- 无环境摘要")

        lines.extend(["", "## 执行计划", ""])
        for step in plan.get("steps", []):
            lines.append(f"### 步骤 {step.get('index', '?')}")
            lines.append(f"- 目标: {step.get('goal', '')}")
            lines.append(f"- 命令: `{step.get('command', '')}`")
            if step.get("selection_reason"):
                lines.append(f"- 命令依据: {step.get('selection_reason')}")
            if step.get("environment_rationale"):
                lines.append(f"- 环境依据: {step.get('environment_rationale')}")
            risk = step.get("risk") or {}
            if risk.get("classification"):
                lines.append(f"- 风险分类: `{risk.get('classification')}` / `{risk.get('level', '')}`")
            if risk.get("explanation"):
                lines.append(f"- 风险说明: {risk.get('explanation')}")
            lines.append("")
        if not plan.get("steps"):
            lines.extend(["- 无执行步骤", ""])

        lines.extend(["## 执行结果", ""])
        for step in trace_dict.get("steps", []):
            lines.append(f"### 步骤 {step.get('index', '?')} 结果")
            lines.append(f"- 状态: `{step.get('status', '')}`")
            lines.append(f"- 返回码: `{step.get('return_code', '')}`")
            lines.append(f"- 命令: `{step.get('command', '')}`")
            if step.get("stdout"):
                lines.append(f"- stdout 摘要: `{str(step.get('stdout', ''))[:500]}`")
            if step.get("stderr"):
                lines.append(f"- stderr 摘要: `{str(step.get('stderr', ''))[:500]}`")
            analysis = step.get("analysis", {}) or {}
            if analysis.get("explanation"):
                lines.append(f"- 分析: {analysis.get('explanation')}")
            lines.append("")
        if not trace_dict.get("steps"):
            lines.extend(["- 无执行结果", ""])

        lines.extend(
            [
                "## 最终反馈",
                "",
                trace.final_feedback or "无最终反馈",
                "",
                "## 追踪摘要",
                "",
            ]
        )
        for key, value in (trace_dict.get("trace_summary", {}) or {}).items():
            if key == "state_transitions":
                continue
            lines.append(f"- {key}: `{value}`")
        lines.extend(["", "## 状态时间线", ""])
        transitions = [
            {"timestamp": item.get("timestamp", ""), "state": item.get("state", "")}
            for item in event_payloads
            if item.get("type") == "state_changed"
        ]
        if not transitions:
            transitions = (trace_dict.get("trace_summary", {}) or {}).get("state_transitions", [])
        if transitions:
            for item in transitions:
                lines.append(f"- `{item.get('timestamp', '')}` -> `{item.get('state', '')}`")
        else:
            lines.append("- 无状态迁移记录")
        return "\n".join(lines).strip() + "\n"
