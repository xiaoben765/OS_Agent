#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
任务编排共享数据模型
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional


def utcnow_iso() -> str:
    """返回UTC ISO时间字符串"""
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


class WorkflowState:
    PLANNING = "planning"
    NEEDS_CLARIFICATION = "needs_clarification"
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    EXECUTING = "executing"
    ANALYZING = "analyzing"
    RECOVERING = "recovering"
    COMPLETED = "completed"
    FAILED = "failed"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"


@dataclass
class TaskRequest:
    task_id: str
    user_input: str
    source: str = "cli"
    session_id: Optional[str] = None
    created_at: str = field(default_factory=utcnow_iso)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class RiskAssessment:
    level: str
    allowed: bool
    requires_confirmation: bool
    classification: str = "safe"
    rule_sources: List[str] = field(default_factory=list)
    reasons: List[str] = field(default_factory=list)
    scope: List[str] = field(default_factory=list)
    explanation: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class TaskStep:
    index: int
    goal: str
    command: str
    expected_outcome: str
    stop_on_failure: bool = True
    selection_reason: str = ""
    environment_rationale: str = ""
    success_criteria: str = ""
    fallback_hint: str = ""
    risk: Optional[RiskAssessment] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class TaskPlan:
    task_id: str
    source: str
    original_input: str
    user_intent: str
    environment_summary: Dict[str, Any]
    steps: List[TaskStep]
    total_risk: str
    requires_confirmation: bool
    state: str = WorkflowState.PLANNING
    response_mode: str = "execute"
    intent_label: str = "general_task"
    intent_confidence: float = 0.0
    clarification_prompt: str = ""
    parsing_summary: Dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=utcnow_iso)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class StepExecution:
    index: int
    goal: str
    command: str
    status: str
    return_code: int
    stdout: str
    stderr: str
    expected_outcome: str
    selection_reason: str = ""
    environment_rationale: str = ""
    success_criteria: str = ""
    fallback_hint: str = ""
    risk: Optional[RiskAssessment] = None
    analysis: Dict[str, Any] = field(default_factory=dict)
    started_at: str = field(default_factory=utcnow_iso)
    finished_at: str = field(default_factory=utcnow_iso)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ExecutionTrace:
    task_id: str
    source: str
    original_input: str
    status: str
    total_risk: str
    state: str = WorkflowState.PLANNING
    created_at: str = field(default_factory=utcnow_iso)
    updated_at: str = field(default_factory=utcnow_iso)
    confirmations: List[Dict[str, Any]] = field(default_factory=list)
    steps: List[StepExecution] = field(default_factory=list)
    final_feedback: str = ""
    trace_summary: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)
