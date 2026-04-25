#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
共享任务编排器
"""

from __future__ import annotations

import logging
import json
import re
import time
import uuid
from queue import Empty, Queue
from threading import Thread
from typing import Any, Callable, Dict, List, Optional

from .audit import AuditStore
from .models import ExecutionTrace, StepExecution, TaskPlan, TaskRequest, TaskStep, WorkflowState, utcnow_iso
from .risk import RiskEvaluator


class TaskOrchestrator:
    """复用现有 provider 和 executor 的共享任务执行内核"""

    def __init__(
        self,
        provider,
        executor,
        security_config,
        audit_store: Optional[AuditStore] = None,
        logger=None,
        analysis_mode: str = "smart",
        allow_template_fallback: bool = False,
    ):
        self.provider = provider
        self.executor = executor
        self.logger = logger or logging.getLogger("task_orchestrator")
        self.risk_evaluator = RiskEvaluator(security_config)
        self.audit_store = audit_store
        self.analysis_mode = (analysis_mode or "smart").lower()
        self.allow_template_fallback = bool(allow_template_fallback)

    def build_plan(
        self,
        user_input: str,
        source: str = "cli",
        session_id: Optional[str] = None,
        task_id: Optional[str] = None,
        event_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> TaskPlan:
        request = TaskRequest(
            task_id=task_id or self._new_task_id(),
            user_input=user_input,
            source=source,
            session_id=session_id,
        )
        system_info = self.executor.get_system_info()
        environment_summary = self._summarize_environment(system_info)
        intent = self._classify_intent(user_input)
        response = self._generate_command_response(
            request.task_id,
            user_input,
            system_info,
            event_callback=event_callback,
        )
        response = self._normalize_command_response(user_input, response)
        intent = self._merge_intent_with_response(intent, response)
        response_mode = str(response.get("response_mode", "execute") or "execute")
        clarification_prompt = str(response.get("clarification_prompt", "") or "")
        local_safety_clarification = self._detect_local_safety_clarification(user_input, intent, response)
        if local_safety_clarification and not clarification_prompt:
            clarification_prompt = local_safety_clarification
            response_mode = "clarify"
        if response.get("needs_clarification") and clarification_prompt:
            response_mode = "clarify"
        if response_mode == "clarify":
            parsing_summary = self._build_parsing_summary(
                intent=intent,
                environment_summary=environment_summary,
                steps=[],
                response_mode="clarify",
                total_risk="low",
                clarification_prompt=clarification_prompt,
            )
            plan = TaskPlan(
                task_id=request.task_id,
                source=source,
                original_input=user_input,
                user_intent=intent["summary"],
                environment_summary=environment_summary,
                steps=[],
                total_risk="low",
                requires_confirmation=False,
                response_mode="clarify",
                intent_label=intent["label"],
                intent_confidence=float(intent["confidence"]),
                clarification_prompt=clarification_prompt or "需要补充更多任务信息后才能继续执行。",
                parsing_summary=parsing_summary,
            )
            if self.audit_store:
                self.audit_store.save_request(request)
                self.audit_store.save_plan(plan)
            return plan

        steps = self._build_steps_from_response(response, user_input, environment_summary, intent)
        if not steps:
            raise ValueError("LLM未返回可执行命令")

        for step in steps:
            step.risk = self.risk_evaluator.assess_command(step.command, environment_summary)

        total_risk = self.risk_evaluator.combine_levels(step.risk.level for step in steps if step.risk)
        requires_confirmation = any(step.risk and step.risk.requires_confirmation for step in steps)
        parsing_summary = self._build_parsing_summary(
            intent=intent,
            environment_summary=environment_summary,
            steps=steps,
            response_mode="execute",
            total_risk=total_risk,
        )
        plan = TaskPlan(
            task_id=request.task_id,
            source=source,
            original_input=user_input,
            user_intent=intent["summary"],
            environment_summary=environment_summary,
            steps=steps,
            total_risk=total_risk,
            requires_confirmation=requires_confirmation,
            response_mode="execute",
            intent_label=intent["label"],
            intent_confidence=float(intent["confidence"]),
            clarification_prompt="",
            parsing_summary=parsing_summary,
        )

        if self.audit_store:
            self.audit_store.save_request(request)
            self.audit_store.save_plan(plan)

        return plan

    def execute_task(
        self,
        user_input: str,
        source: str = "cli",
        session_id: Optional[str] = None,
        approval_callback: Optional[Callable[[str, str, TaskStep], bool]] = None,
        credential_callback: Optional[Callable[[str, TaskStep, int, str], Dict[str, Any]]] = None,
        event_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> ExecutionTrace:
        planning_started = time.perf_counter()
        plan = self.build_plan(
            user_input,
            source=source,
            session_id=session_id,
            event_callback=event_callback,
        )
        planning_ms = int((time.perf_counter() - planning_started) * 1000)
        trace = self.execute_plan(
            plan,
            approval_callback=approval_callback,
            credential_callback=credential_callback,
            event_callback=event_callback,
            initial_trace_summary={"planning_ms": planning_ms},
        )
        self._persist_result(trace)
        return trace

    def execute_plan(
        self,
        plan: TaskPlan,
        approval_callback: Optional[Callable[[str, str, TaskStep], bool]] = None,
        credential_callback: Optional[Callable[[str, TaskStep, int, str], Dict[str, Any]]] = None,
        event_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
        emit_plan_ready: bool = True,
        initial_trace_summary: Optional[Dict[str, Any]] = None,
    ) -> ExecutionTrace:
        base_trace_summary = {
            "task_id": plan.task_id,
            "status": WorkflowState.PLANNING,
            "response_mode": plan.response_mode,
            "intent_label": plan.intent_label,
            "environment_summary": plan.environment_summary,
            "selected_commands": [step.command for step in plan.steps],
            "parsing_summary": plan.parsing_summary,
            "planning_ms": 0,
            "execution_ms": 0,
            "analysis_ms": 0,
            "total_ms": 0,
            "reflection_count": 0,
            "recovery_used": False,
            "plan_adjustments": 0,
            "state_transitions": [],
        }
        if initial_trace_summary:
            base_trace_summary.update(initial_trace_summary)
        trace = ExecutionTrace(
            task_id=plan.task_id,
            source=plan.source,
            original_input=plan.original_input,
            status=WorkflowState.PLANNING,
            state=WorkflowState.PLANNING,
            total_risk=plan.total_risk,
            trace_summary=base_trace_summary,
        )
        self._transition_trace_state(trace, WorkflowState.PLANNING, event_callback, {"response_mode": plan.response_mode})

        if plan.response_mode == "clarify":
            self._transition_trace_state(
                trace,
                WorkflowState.NEEDS_CLARIFICATION,
                event_callback,
                {"clarification_prompt": plan.clarification_prompt},
            )
            trace.updated_at = utcnow_iso()
            trace.final_feedback = plan.clarification_prompt or "需要补充更多任务信息后才能继续执行。"
            self._finalize_trace_summary(trace, execution_ms=0, analysis_ms=0)
            self._persist_result(trace)
            self._emit(
                event_callback,
                plan.task_id,
                "task_needs_clarification",
                {
                    "intent_label": plan.intent_label,
                    "clarification_prompt": plan.clarification_prompt,
                },
            )
            return trace

        if emit_plan_ready:
            self._emit(event_callback, plan.task_id, "plan_ready", {"plan": plan.to_dict()})

        blocked_steps = [step for step in plan.steps if step.risk and not step.risk.allowed]
        if blocked_steps:
            self._transition_trace_state(
                trace,
                WorkflowState.BLOCKED,
                event_callback,
                {"reason": self._compose_blocked_feedback(blocked_steps)},
            )
            trace.updated_at = utcnow_iso()
            trace.final_feedback = self._compose_blocked_feedback(blocked_steps)
            self._finalize_trace_summary(trace, execution_ms=0, analysis_ms=0)
            self._persist_result(trace)
            self._emit(event_callback, plan.task_id, "task_blocked", {"reason": trace.final_feedback})
            return trace

        execution_total_ms = 0
        analysis_total_ms = 0
        reflection_count = 0
        recovery_used = False
        plan_adjustments = 0
        pending_steps = list(plan.steps)
        next_dynamic_index = max((step.index for step in plan.steps), default=0) + 1
        intent_context = {
            "label": plan.intent_label,
            "summary": plan.user_intent,
            "input_understanding": plan.parsing_summary.get("input_understanding", "execute"),
            "confidence": plan.intent_confidence,
        }
        while pending_steps:
            step = pending_steps.pop(0)
            if step.risk and step.risk.requires_confirmation:
                self._transition_trace_state(
                    trace,
                    WorkflowState.AWAITING_CONFIRMATION,
                    event_callback,
                    {"step_index": step.index, "command": step.command, "classification": step.risk.classification},
                )
                prompt = (
                    f"步骤 {step.index} 存在 {step.risk.level} 风险。\n"
                    f"命令: {step.command}\n"
                    f"{step.risk.explanation}\n"
                    f"是否继续执行?"
                )
                approved = approval_callback(prompt, step.risk.level, step) if approval_callback else False
                trace.confirmations.append(
                    {
                        "step_index": step.index,
                        "command": step.command,
                        "risk_level": step.risk.level,
                        "approved": approved,
                        "timestamp": utcnow_iso(),
                    }
                )
                self._emit(
                    event_callback,
                    plan.task_id,
                    "confirmation_recorded",
                    {
                        **trace.confirmations[-1],
                        "classification": step.risk.classification,
                    },
                )
                if not approved:
                    self._transition_trace_state(trace, WorkflowState.CANCELLED, event_callback, {"step": step.index})
                    trace.updated_at = utcnow_iso()
                    trace.final_feedback = f"步骤 {step.index} 未获确认，任务已取消。"
                    self._finalize_trace_summary(trace, execution_ms=execution_total_ms, analysis_ms=analysis_total_ms)
                    self._persist_result(trace)
                    self._emit(event_callback, plan.task_id, "task_cancelled", {"step": step.index})
                    return trace

            sudo_password = None
            credential_attempt = 0
            last_credential_error = ""
            while True:
                if credential_callback and self._command_requires_sudo_password(step.command):
                    credential_attempt += 1
                    prompt = f"步骤 {step.index} 需要 sudo 密码才能继续执行。"
                    try:
                        credential_result = credential_callback(
                            prompt,
                            step,
                            credential_attempt,
                            last_credential_error,
                        )
                    except TimeoutError as exc:
                        self._transition_trace_state(trace, WorkflowState.FAILED, event_callback, {"step": step.index})
                        trace.updated_at = utcnow_iso()
                        trace.final_feedback = str(exc)
                        self._finalize_trace_summary(trace, execution_ms=execution_total_ms, analysis_ms=analysis_total_ms)
                        self._persist_result(trace)
                        self._emit(event_callback, plan.task_id, "task_failed", {"step": step.index})
                        return trace

                    if credential_result.get("action") != "submit":
                        self._transition_trace_state(trace, WorkflowState.CANCELLED, event_callback, {"step": step.index})
                        trace.updated_at = utcnow_iso()
                        trace.final_feedback = f"步骤 {step.index} 的 sudo 密码输入已取消。"
                        self._finalize_trace_summary(trace, execution_ms=execution_total_ms, analysis_ms=analysis_total_ms)
                        self._persist_result(trace)
                        self._emit(event_callback, plan.task_id, "task_cancelled", {"step": step.index})
                        return trace

                    sudo_password = str(credential_result.get("password", ""))

                step_started_at = utcnow_iso()
                self._transition_trace_state(
                    trace,
                    WorkflowState.EXECUTING,
                    event_callback,
                    {"step_index": step.index, "command": step.command},
                )
                self._emit(
                    event_callback,
                    plan.task_id,
                    "step_started",
                    {"step_index": step.index, "command": step.command, "goal": step.goal},
                )
                command_started = time.perf_counter()
                stdout, stderr, return_code = self.executor.execute_command(
                    step.command,
                    sudo_password=sudo_password,
                )
                command_finished = time.perf_counter()
                step_execution_ms = int((command_finished - command_started) * 1000)
                execution_total_ms += step_execution_ms

                if credential_callback and self._is_sudo_password_error(step.command, stderr):
                    last_credential_error = "sudo 密码错误，请重试。"
                    self._emit(
                        event_callback,
                        plan.task_id,
                        "credential_rejected",
                        {
                            "step_index": step.index,
                            "command": step.command,
                            "attempt": credential_attempt,
                            "last_error": last_credential_error,
                        },
                    )
                    continue

                if credential_callback and self._command_requires_sudo_password(step.command) and credential_attempt > 0:
                    self._emit(
                        event_callback,
                        plan.task_id,
                        "credential_accepted",
                        {
                            "step_index": step.index,
                            "command": step.command,
                            "attempt": credential_attempt,
                        },
                    )
                break

            return_code = self._normalize_delete_return_code(
                step.command,
                stdout,
                stderr,
                return_code,
            )
            return_code = self._normalize_observation_return_code(
                step.command,
                stdout,
                stderr,
                return_code,
            )

            should_analyze = self._should_run_analysis(plan, step, return_code)
            if should_analyze:
                self._transition_trace_state(
                    trace,
                    WorkflowState.ANALYZING,
                    event_callback,
                    {"step_index": step.index, "command": step.command},
                )
                self._emit(
                    event_callback,
                    plan.task_id,
                    "analysis_started",
                    {
                        "step_index": step.index,
                        "command": step.command,
                    },
                )
                analysis_started = time.perf_counter()
                analysis = self._normalize_analysis(self.provider.analyze_output(step.command, stdout, stderr))
                step_analysis_ms = int((time.perf_counter() - analysis_started) * 1000)
                analysis_total_ms += step_analysis_ms
                self._emit(
                    event_callback,
                    plan.task_id,
                    "analysis_finished",
                    {
                        "step_index": step.index,
                        "command": step.command,
                        "analysis_preview": str(analysis.get("explanation", ""))[:300],
                        "analysis": analysis,
                        "analysis_mode": self.analysis_mode,
                    },
                )
            else:
                analysis = self._build_local_analysis(step.command, stdout, stderr, return_code)

            self._emit(
                event_callback,
                plan.task_id,
                "reflection_started",
                {
                    "step_index": step.index,
                    "command": step.command,
                    "remaining_step_count": len(pending_steps),
                },
            )
            reflection = self._reflect_step(
                step=step,
                analysis=analysis,
                return_code=return_code,
                stdout=stdout,
                stderr=stderr,
                executed_steps=trace.steps,
                pending_steps=pending_steps,
                environment_summary=plan.environment_summary,
                next_step_index=next_dynamic_index,
                intent=intent_context,
            )
            reflection_count += 1
            next_dynamic_index += len(reflection["next_steps"])
            analysis["reflection"] = {
                "action": reflection["action"],
                "reason": reflection["reason"],
                "generated_step_count": len(reflection["next_steps"]),
            }
            analysis["next_action"] = reflection["action"]
            trace.trace_summary["reflection_count"] = reflection_count
            self._emit(
                event_callback,
                plan.task_id,
                "reflection_finished",
                {
                    "step_index": step.index,
                    "command": step.command,
                    "action": reflection["action"],
                    "reason": reflection["reason"],
                    "generated_step_count": len(reflection["next_steps"]),
                },
            )

            status = "completed" if return_code == 0 else "failed"
            step_execution = StepExecution(
                index=step.index,
                goal=step.goal,
                command=step.command,
                status=status,
                return_code=return_code,
                stdout=stdout,
                stderr=stderr,
                expected_outcome=step.expected_outcome,
                selection_reason=step.selection_reason,
                environment_rationale=step.environment_rationale,
                success_criteria=step.success_criteria,
                fallback_hint=step.fallback_hint,
                risk=step.risk,
                analysis=analysis or {},
                started_at=step_started_at,
                finished_at=utcnow_iso(),
            )
            trace.steps.append(step_execution)
            self._emit(
                event_callback,
                plan.task_id,
                "step_finished",
                {
                    "step_index": step.index,
                    "status": status,
                    "return_code": return_code,
                    "stdout_preview": stdout[:500],
                    "stderr_preview": stderr[:500],
                    "step": step_execution.to_dict(),
                    "analysis_mode": "llm" if should_analyze else "local",
                },
            )

            if reflection["action"] == "revise_remaining_steps" and reflection["next_steps"]:
                for generated_step in reflection["next_steps"]:
                    generated_step.risk = self.risk_evaluator.assess_command(
                        generated_step.command,
                        plan.environment_summary,
                    )
                pending_steps = reflection["next_steps"]
                plan_adjustments += 1
                trace.trace_summary["plan_adjustments"] = plan_adjustments
                self._emit(
                    event_callback,
                    plan.task_id,
                    "plan_adjusted",
                    {
                        "step_index": step.index,
                        "reason": reflection["reason"],
                        "replacement_commands": [item.command for item in reflection["next_steps"]],
                    },
                )

            if reflection["action"] == "recover" and reflection["next_steps"]:
                self._transition_trace_state(
                    trace,
                    WorkflowState.RECOVERING,
                    event_callback,
                    {"step_index": step.index, "reason": reflection["reason"]},
                )
                for generated_step in reflection["next_steps"]:
                    generated_step.risk = self.risk_evaluator.assess_command(
                        generated_step.command,
                        plan.environment_summary,
                    )
                pending_steps = reflection["next_steps"]
                recovery_used = True
                plan_adjustments += 1
                trace.trace_summary["recovery_used"] = True
                trace.trace_summary["plan_adjustments"] = plan_adjustments
                self._emit(
                    event_callback,
                    plan.task_id,
                    "recovery_suggested",
                    {
                        "step_index": step.index,
                        "reason": reflection["reason"],
                        "recovery_commands": [item.command for item in reflection["next_steps"]],
                    },
                )

            if return_code != 0 and step.stop_on_failure:
                if reflection["action"] == "recover" and pending_steps:
                    continue
                if reflection["action"] == "complete":
                    continue
                trace.status = "failed"
                self._transition_trace_state(trace, WorkflowState.FAILED, event_callback, {"step": step.index})
                trace.updated_at = utcnow_iso()
                trace.final_feedback = self._compose_failure_feedback(step_execution)
                self._finalize_trace_summary(trace, execution_ms=execution_total_ms, analysis_ms=analysis_total_ms)
                self._persist_result(trace)
                self._emit(event_callback, plan.task_id, "task_failed", {"step": step.index})
                return trace

        self._transition_trace_state(trace, WorkflowState.COMPLETED, event_callback, {"step_count": len(trace.steps)})
        trace.updated_at = utcnow_iso()
        trace.trace_summary["reflection_count"] = reflection_count
        trace.trace_summary["recovery_used"] = recovery_used
        trace.trace_summary["plan_adjustments"] = plan_adjustments
        trace.final_feedback = self._compose_success_feedback(trace)
        self._finalize_trace_summary(trace, execution_ms=execution_total_ms, analysis_ms=analysis_total_ms)
        self.logger.info(
            "任务 %s 耗时统计: planning=%sms execution=%sms analysis=%sms total=%sms",
            plan.task_id,
            trace.trace_summary.get("planning_ms", 0),
            trace.trace_summary.get("execution_ms", 0),
            trace.trace_summary.get("analysis_ms", 0),
            trace.trace_summary.get("total_ms", 0),
        )
        self._persist_result(trace)
        self._emit(event_callback, plan.task_id, "task_completed", {"step_count": len(trace.steps)})
        return trace

    def _build_steps_from_response(
        self,
        response: Dict[str, Any],
        user_input: str,
        environment_summary: Dict[str, Any],
        intent: Dict[str, Any],
    ) -> List[TaskStep]:
        steps: List[TaskStep] = []
        commands = response.get("commands")
        if isinstance(commands, list):
            for idx, item in enumerate(commands, start=1):
                command = self._prepare_command_for_execution(
                    (item or {}).get("command", "").strip(),
                    environment_summary,
                    intent,
                    user_input,
                )
                if command and not self._has_unresolved_placeholder(command):
                    selection_reason, environment_rationale = self._infer_step_rationale(
                        command,
                        environment_summary,
                        intent,
                    )
                    steps.append(
                        TaskStep(
                            index=idx,
                            goal=(item or {}).get("explanation", f"步骤 {idx}"),
                            command=command,
                            expected_outcome=(item or {}).get("expected_outcome") or (item or {}).get("explanation", "命令执行成功"),
                            selection_reason=(item or {}).get("selection_reason", selection_reason),
                            environment_rationale=(item or {}).get("environment_rationale", environment_rationale),
                            success_criteria=(item or {}).get("success_criteria", (item or {}).get("expected_outcome", "")),
                            fallback_hint=(item or {}).get("fallback_hint", ""),
                        )
                    )
            return steps

        command = self._prepare_command_for_execution(
            str(response.get("command", "")).strip(),
            environment_summary,
            intent,
            user_input,
        )
        explanation = str(response.get("explanation", user_input)).strip() or user_input
        expected_outcome = str(response.get("expected_outcome", explanation)).strip() or explanation
        if not command or self._has_unresolved_placeholder(command):
            return []

        split_commands = self._split_commands(command)
        for idx, item in enumerate(split_commands, start=1):
            item = self._prepare_command_for_execution(item, environment_summary, intent, user_input)
            selection_reason, environment_rationale = self._infer_step_rationale(
                item,
                environment_summary,
                intent,
            )
            steps.append(
                TaskStep(
                    index=idx,
                    goal=explanation if len(split_commands) == 1 else f"{explanation} - 步骤 {idx}",
                    command=item,
                    expected_outcome=expected_outcome if len(split_commands) == 1 else f"{expected_outcome} - 步骤 {idx}",
                    selection_reason=selection_reason,
                    environment_rationale=environment_rationale,
                    success_criteria=str(response.get("success_criteria", expected_outcome)).strip() if len(split_commands) == 1 else "",
                    fallback_hint=str(response.get("fallback_hint", "")).strip(),
                )
            )
        return steps

    def _split_commands(self, command: str) -> List[str]:
        fragments: List[str] = []
        current: List[str] = []
        in_single_quote = False
        in_double_quote = False
        escaped = False
        index = 0

        while index < len(command):
            char = command[index]

            if escaped:
                current.append(char)
                escaped = False
                index += 1
                continue

            if char == "\\":
                current.append(char)
                escaped = True
                index += 1
                continue

            if char == "'" and not in_double_quote:
                in_single_quote = not in_single_quote
                current.append(char)
                index += 1
                continue

            if char == '"' and not in_single_quote:
                in_double_quote = not in_double_quote
                current.append(char)
                index += 1
                continue

            if not in_single_quote and not in_double_quote:
                if char == ";":
                    fragment = "".join(current).strip()
                    if fragment:
                        fragments.append(fragment)
                    current = []
                    index += 1
                    continue
                if char == "&" and index + 1 < len(command) and command[index + 1] == "&":
                    fragment = "".join(current).strip()
                    if fragment:
                        fragments.append(fragment)
                    current = []
                    index += 2
                    continue

            current.append(char)
            index += 1

        trailing = "".join(current).strip()
        if trailing:
            fragments.append(trailing)
        return fragments or [command]

    def _command_requires_sudo_password(self, command: str) -> bool:
        detector = getattr(self.executor, "command_requires_sudo_password", None)
        if callable(detector):
            return bool(detector(command))
        return str(command).strip().startswith("sudo ")

    def _is_sudo_password_error(self, command: str, stderr: str) -> bool:
        detector = getattr(self.executor, "is_sudo_password_error", None)
        if callable(detector):
            return bool(detector(command, stderr))
        lowered = (stderr or "").lower()
        return "password" in lowered or "try again" in lowered

    def _generate_command_response(
        self,
        task_id: str,
        user_input: str,
        system_info: Dict[str, Any],
        event_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> Dict[str, Any]:
        stream_method = getattr(self.provider, "stream_response", None)
        if not callable(stream_method):
            return self.provider.generate_command(user_input, system_info) or {}

        messages = self._build_command_messages(user_input, system_info)
        model_name = getattr(self.provider, "model", "unknown") or "unknown"
        planning_started = time.perf_counter()
        self._emit(
            event_callback,
            task_id,
            "planning_stream_started",
            {
                "planning_model": model_name,
                "planning_elapsed_ms": 0,
            },
        )

        collected = self._collect_stream_output(
            stream_method(messages),
            task_id,
            planning_started,
            event_callback,
        )
        if not collected.strip():
            self._emit(
                event_callback,
                task_id,
                "planning_timeout",
                {
                    "planning_elapsed_ms": int((time.perf_counter() - planning_started) * 1000),
                    "planning_timeout": True,
                    "reason": "规划阶段等待模型响应超时",
                },
            )
            raise TimeoutError("规划阶段等待模型响应超时")

        stream_error = self._detect_stream_error(collected)
        if stream_error:
            event_type = "planning_timeout" if stream_error["timeout"] else "plan_failed"
            self._emit(
                event_callback,
                task_id,
                event_type,
                {
                    "planning_elapsed_ms": int((time.perf_counter() - planning_started) * 1000),
                    "planning_timeout": stream_error["timeout"],
                    "reason": stream_error["message"],
                    "planning_stream_preview": self._truncate_stream_preview(collected),
                },
            )
            if stream_error["timeout"]:
                raise TimeoutError(stream_error["message"])
            raise ValueError(stream_error["message"])

        self._emit(
            event_callback,
            task_id,
            "planning_parse_started",
            {
                "planning_elapsed_ms": int((time.perf_counter() - planning_started) * 1000),
                "planning_stream_preview": self._truncate_stream_preview(collected),
            },
        )
        parsed = self._parse_streamed_command_response(collected)
        self._emit(
            event_callback,
            task_id,
            "planning_parse_finished",
            {
                "planning_elapsed_ms": int((time.perf_counter() - planning_started) * 1000),
                "planning_stream_preview": self._truncate_stream_preview(collected),
            },
        )
        return parsed

    def _build_command_messages(self, user_input: str, system_info: Dict[str, Any]) -> List[Dict[str, str]]:
        schema_message = {
            "role": "system",
            "content": (
                "请以结构化 JSON 返回规划结果。"
                "默认 response_mode 为 execute；若信息不足，请返回 "
                "{\"response_mode\":\"clarify\",\"needs_clarification\":true,"
                "\"clarification_prompt\":\"需要补充的信息\"}。"
                "intent_label 可以是开放语义标签，不限制在固定枚举内。"
                "执行模式下优先返回 {\"intent_label\":\"...\",\"intent_summary\":\"...\",\"command\":\"...\","
                "\"explanation\":\"...\",\"expected_outcome\":\"...\"}，"
                "多步任务返回 {\"intent_label\":\"...\",\"intent_summary\":\"...\",\"commands\":[{\"command\":\"...\","
                "\"explanation\":\"...\",\"expected_outcome\":\"...\",\"selection_reason\":\"...\","
                "\"environment_rationale\":\"...\"}]}"
                "。不要输出 JSON 之外的说明文字。"
            ),
        }
        builder = getattr(self.provider, "build_command_messages", None)
        if callable(builder):
            messages = builder(user_input, system_info)
            return [schema_message, *messages]

        prompt_builder = getattr(self.provider, "_build_command_prompt", None)
        if callable(prompt_builder):
            prompt = prompt_builder(user_input, system_info)
            messages: List[Dict[str, str]] = []
            messages.append(schema_message)
            messages.append({"role": "user", "content": prompt})
            return messages

        return [schema_message, {"role": "user", "content": user_input}]

    def _collect_stream_output(
        self,
        stream,
        task_id: str,
        planning_started: float,
        event_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> str:
        queue: Queue = Queue()

        def consume_stream() -> None:
            try:
                for chunk in stream:
                    queue.put(("chunk", chunk))
            except Exception as exc:  # pragma: no cover - defensive path
                queue.put(("error", exc))
            finally:
                queue.put(("done", None))

        worker = Thread(target=consume_stream, daemon=True)
        worker.start()

        collected = ""
        first_chunk = True
        finished = False
        while not finished:
            try:
                item_type, payload = queue.get(timeout=1.0)
            except Empty:
                self._emit(
                    event_callback,
                    task_id,
                    "planning_heartbeat",
                    {
                        "planning_elapsed_ms": int((time.perf_counter() - planning_started) * 1000),
                        "planning_stream_preview": self._truncate_stream_preview(collected),
                    },
                )
                continue

            if item_type == "chunk":
                if not payload:
                    continue
                collected += str(payload)
                event_type = "planning_first_chunk" if first_chunk else "planning_chunk"
                payload_data = {
                    "planning_elapsed_ms": int((time.perf_counter() - planning_started) * 1000),
                    "planning_stream_preview": self._truncate_stream_preview(collected),
                    "chunk": str(payload),
                }
                if first_chunk:
                    payload_data["first_token_ms"] = payload_data["planning_elapsed_ms"]
                    first_chunk = False
                self._emit(event_callback, task_id, event_type, payload_data)
            elif item_type == "error":
                raise payload
            elif item_type == "done":
                finished = True

        return collected

    def _parse_streamed_command_response(self, content: str) -> Dict[str, Any]:
        parsed = self._parse_markdown_json(content)
        if isinstance(parsed, dict):
            return parsed

        text = content.strip()
        if text.startswith("{") and text.endswith("}"):
            try:
                candidate = json.loads(text)
            except json.JSONDecodeError:
                candidate = None
            if isinstance(candidate, dict):
                return candidate

        parser = getattr(self.provider, "_parse_text_response", None)
        if callable(parser):
            parsed = parser(content)
            if isinstance(parsed, dict):
                return parsed

        return {
            "command": "",
            "explanation": text,
        }

    def _detect_stream_error(self, content: str) -> Optional[Dict[str, Any]]:
        lowered = (content or "").lower()
        if not lowered.strip():
            return {
                "timeout": True,
                "message": "规划阶段等待模型响应超时",
            }
        if "timed out" in lowered or "timeout" in lowered or "超时" in content:
            return {
                "timeout": True,
                "message": "规划阶段等待模型响应超时",
            }
        if "错误:" in content or "无法获取流式响应" in content or "api请求失败" in content.lower():
            return {
                "timeout": False,
                "message": self._preview_text(content, limit=200),
            }
        return None

    def _truncate_stream_preview(self, content: str, limit: int = 800) -> str:
        if len(content) <= limit:
            return content
        return content[-limit:]

    def _normalize_command_response(self, user_input: str, response: Dict[str, Any]) -> Dict[str, Any]:
        normalized = dict(response or {})
        command = str(normalized.get("command", "")).strip()
        explanation = str(normalized.get("explanation", "")).strip()

        upgraded = self._maybe_upgrade_cpu_process_permission_query(user_input, normalized)
        if upgraded is not None:
            return upgraded

        if self.allow_template_fallback and self._is_unusable_command(command, explanation):
            fallback = self._fallback_response(user_input)
            if fallback:
                self.logger.warning(f"使用回退命令处理请求: {user_input} -> {fallback['command']}")
                return fallback

        if self.allow_template_fallback and command == "help":
            fallback = self._fallback_response(user_input)
            if fallback:
                return fallback

        return normalized

    def _maybe_upgrade_cpu_process_permission_query(self, user_input: str, response: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        text = str(user_input or "").lower()
        command = str((response or {}).get("command", "")).strip()
        if not command:
            return None

        asks_cpu_top_process = all(keyword in text for keyword in ["cpu", "最高"]) and any(
            keyword in text for keyword in ["程序", "进程"]
        )
        asks_path_and_permissions = all(keyword in text for keyword in ["路径", "权限"])
        if not (asks_cpu_top_process and asks_path_and_permissions):
            return None

        normalized_command = self._normalize_command_text(command).lower()
        already_complete = "stat -c" in normalized_command and ("path:" in normalized_command or "readlink" in normalized_command)
        if already_complete:
            return None

        if "/proc/" not in normalized_command and "sort=-%cpu" not in normalized_command:
            return None

        upgraded = dict(response)
        upgraded["command"] = (
            "sh -c 'pid=$(ps -eo pid=,%cpu=,comm= --sort=-%cpu | awk \"NR==1 {print \\$1}\"); "
            "exe=$(readlink -f /proc/$pid/exe 2>/dev/null); "
            "ps -p \"$pid\" -o pid=,comm=,%cpu= --no-headers; "
            "printf \"PATH: %s\\n\" \"${exe:-N/A}\"; "
            "[ -n \"$exe\" ] && stat -c \"PERMISSIONS: %A (%a) OWNER: %U GROUP: %G FILE: %n\" \"$exe\"'"
        )
        upgraded["explanation"] = str(
            upgraded.get("explanation", "获取 CPU 占用最高进程的 PID、路径和权限信息")
        ).strip() or "获取 CPU 占用最高进程的 PID、路径和权限信息"
        upgraded["expected_outcome"] = str(
            upgraded.get("expected_outcome", "显示 CPU 占用最高进程的 PID、路径和权限信息")
        ).strip() or "显示 CPU 占用最高进程的 PID、路径和权限信息"
        upgraded["success_criteria"] = "输出 CPU 占用最高进程的 PID、可执行文件路径、权限和属主属组"
        return upgraded

    def _classify_intent(self, user_input: str) -> Dict[str, Any]:
        text = (user_input or "").strip()
        lowered = text.lower()
        rules = [
            ("diagnose_request", "diagnose", "诊断与排障类 Linux 操作请求", [r"失败", r"异常", r"原因", r"诊断", r"恢复", r"修复"]),
            ("query_request", "query", "查询类 Linux 操作请求", [r"查看", r"看看", r"确认", r"检查", r"显示", r"列出", r"查询", r"统计", r"查找", r"找出", r"搜索"]),
            ("create_request", "create", "创建类 Linux 操作请求", [r"创建", r"新建", r"生成", r"useradd", r"mkdir", r"touch"]),
            ("delete_request", "delete", "删除或清理类 Linux 操作请求", [r"删除", r"移除", r"清理", r"\brm\b"]),
            ("modify_request", "modify", "修改或配置类 Linux 操作请求", [r"修改", r"更改", r"配置", r"调整", r"chmod", r"chown"]),
        ]
        for label, input_understanding, summary, patterns in rules:
            if any(re.search(pattern, lowered, re.IGNORECASE) for pattern in patterns):
                return {
                    "label": label,
                    "input_understanding": input_understanding,
                    "summary": summary,
                    "confidence": 0.9,
                }
        return {
            "label": "general_task",
            "input_understanding": "execute",
            "summary": "通用系统操作任务",
            "confidence": 0.4,
        }

    def _merge_intent_with_response(self, inferred_intent: Dict[str, Any], response: Dict[str, Any]) -> Dict[str, Any]:
        merged = dict(inferred_intent)
        response_label = str((response or {}).get("intent_label", "")).strip()
        response_summary = str((response or {}).get("intent_summary", "")).strip()
        response_understanding = str((response or {}).get("input_understanding", "")).strip()
        response_confidence = (response or {}).get("intent_confidence")

        if response_label:
            merged["label"] = response_label
        if response_summary:
            merged["summary"] = response_summary
        if response_understanding:
            merged["input_understanding"] = response_understanding
        if isinstance(response_confidence, (int, float)):
            merged["confidence"] = float(response_confidence)
        elif response_label or response_summary:
            merged["confidence"] = max(float(merged.get("confidence", 0.0)), 0.85)
        return merged

    def _detect_local_safety_clarification(self, user_input: str, intent: Dict[str, Any], response: Dict[str, Any]) -> str:
        input_understanding = intent.get("input_understanding", "")
        lowered = (user_input or "").lower()
        candidate_commands = []
        command = str((response or {}).get("command", "")).strip()
        if command:
            candidate_commands.append(command)
        commands = (response or {}).get("commands")
        if isinstance(commands, list):
            for item in commands:
                item_command = str((item or {}).get("command", "")).strip()
                if item_command:
                    candidate_commands.append(item_command)

        has_user_management_delete = any(
            "userdel" in cmd or "deluser" in cmd or "gpasswd" in cmd or "usermod" in cmd
            for cmd in candidate_commands
        )
        if input_understanding == "delete" and not has_user_management_delete and "/" not in lowered and "~/" not in lowered and "../" not in lowered and "./" not in lowered:
            return "请明确要删除或清理的路径范围，避免误删系统文件。"
        return ""

    def _extract_candidate_username(self, user_input: str) -> str:
        for candidate in re.findall(r"\b[a-z_][a-z0-9_-]{2,31}\b", (user_input or "").lower()):
            if candidate not in {"sudo", "user", "useradd", "usermod", "passwd", "normal", "linux"}:
                return candidate
        return ""

    def _build_parsing_summary(
        self,
        intent: Dict[str, Any],
        environment_summary: Dict[str, Any],
        steps: List[TaskStep],
        response_mode: str,
        total_risk: str,
        clarification_prompt: str = "",
    ) -> Dict[str, Any]:
        decomposition = "clarify"
        if response_mode == "execute":
            decomposition = "multi_step" if len(steps) > 1 else "single_step"
        return {
            "input_understanding": intent.get("input_understanding", "execute"),
            "intent_label": intent.get("label", "general_task"),
            "intent_summary": intent.get("summary", ""),
            "task_decomposition": decomposition,
            "environment_decision": self._summarize_environment_decision(steps, environment_summary, intent, response_mode),
            "risk_decision": total_risk,
            "result_mode": response_mode,
            "clarification_prompt": clarification_prompt,
        }

    def _summarize_environment_decision(
        self,
        steps: List[TaskStep],
        environment_summary: Dict[str, Any],
        intent: Dict[str, Any],
        response_mode: str,
    ) -> str:
        distribution = environment_summary.get("distribution", "linux")
        if response_mode == "clarify":
            return f"{distribution} 环境已识别，但当前缺少关键参数，需先澄清后再选择具体工具。"
        if not steps:
            return f"{distribution} 环境已识别，等待生成可执行步骤。"
        return steps[0].environment_rationale or f"已结合 {distribution} 环境生成执行方案。"

    def _infer_step_rationale(
        self,
        command: str,
        environment_summary: Dict[str, Any],
        intent: Dict[str, Any],
    ) -> tuple[str, str]:
        distribution = environment_summary.get("distribution", "linux")
        package_manager = environment_summary.get("package_manager", "system package manager")
        network_tool = environment_summary.get("network_tool", "ss")
        lowered = (command or "").lower()
        label = intent.get("label", "general_task")
        if re.search(r"\b(dnf|yum|apt|apt-get)\s+install\b", lowered):
            return (
                f"软件安装任务按当前环境选择 {package_manager} 执行安装，避免沿用不匹配的发行版命令。",
                f"{distribution} 环境使用 {package_manager} 作为包管理器，因此已将安装步骤适配为 {package_manager} 命令。",
            )
        if re.search(r"\b(dpkg\s+-l|rpm\s+-qa|dnf\s+list\s+installed|yum\s+list\s+installed|apt(-get)?\s+list\s+--installed)\b", lowered):
            return (
                "软件包存在性校验会优先使用当前发行版原生命令，保证诊断链路在不同系统下都能直接执行。",
                f"{distribution} 环境已按 {package_manager} 生态切换软件包查询命令。",
            )
        if " ss " in f" {lowered} " or lowered.startswith("ss ") or "ss -tunap" in lowered:
            return (
                f"端口与套接字查询任务优先使用 {network_tool} 提取监听端口和关联进程。",
                f"{distribution} 环境默认具备 {network_tool}，适合直接检查端口占用与进程归属。",
            )
        if lowered.startswith("df "):
            return (
                "磁盘空间查询任务优先使用 df -h 获取分区级容量与使用率。",
                f"{distribution} 环境下 df -h 输出稳定，便于快速解释各分区剩余空间。",
            )
        if lowered.startswith("find "):
            return (
                "文件检索任务优先使用 find 组合路径、时间和大小条件进行筛选。",
                f"{distribution} 环境下 find 能直接覆盖目录范围、时间窗口和文件大小约束。",
            )
        if "--sort=-%cpu" in lowered and ("/proc/" in lowered or "readlink" in lowered or "stat -c" in lowered):
            return (
                "CPU热点进程排查优先使用 ps 锁定占用最高的进程，再结合 /proc 和可执行文件信息补充路径与权限。",
                f"{distribution} 环境提供 /proc 进程视图，可直接读取可执行文件路径并检查权限属性。",
            )
        if "useradd" in lowered:
            return (
                "用户创建任务使用 useradd 建立普通账号，再配合校验命令确认结果。",
                f"{distribution} 环境按 Linux 用户管理最佳实践使用 useradd 创建本地用户。",
            )
        if "userdel" in lowered:
            return (
                "用户删除任务使用 userdel 移除本地账号，再配合校验命令确认结果。",
                f"{distribution} 环境按 Linux 用户管理最佳实践使用 userdel 删除本地用户。",
            )
        if lowered.startswith("id "):
            return (
                "用户创建后的验证步骤使用 id 检查用户是否已成功写入系统账户信息。",
                f"{distribution} 环境下 id 可直接返回用户的 uid/gid 作为创建成功证据。",
            )
        if "systemctl" in lowered:
            return (
                "服务状态诊断任务优先使用 systemctl 获取服务健康状态。",
                f"{distribution} 环境使用 systemd，适合通过 systemctl 查询服务状态和故障原因。",
            )
        return (
            f"该步骤由解析链为 {label} 任务选择的可执行命令。",
            f"已结合 {distribution} 环境摘要生成该步骤，并保留后续风险校验。",
        )

    def _build_step_from_next_step(
        self,
        step_index: int,
        item: Dict[str, Any],
        environment_summary: Dict[str, Any],
        intent: Dict[str, Any],
        default_goal: str,
    ) -> Optional[TaskStep]:
        command = self._prepare_command_for_execution(
            str((item or {}).get("command", "")).strip(),
            environment_summary,
            intent,
            str((item or {}).get("explanation", "")),
        )
        if not command or self._has_unresolved_placeholder(command):
            return None
        selection_reason, environment_rationale = self._infer_step_rationale(
            command,
            environment_summary,
            intent,
        )
        explanation = str((item or {}).get("explanation", default_goal)).strip() or default_goal
        expected_outcome = str((item or {}).get("expected_outcome", explanation)).strip() or explanation
        return TaskStep(
            index=step_index,
            goal=explanation,
            command=command,
            expected_outcome=expected_outcome,
            selection_reason=str((item or {}).get("selection_reason", selection_reason)),
            environment_rationale=str((item or {}).get("environment_rationale", environment_rationale)),
            success_criteria=str((item or {}).get("success_criteria", expected_outcome)),
            fallback_hint=str((item or {}).get("fallback_hint", "")),
        )

    def _reflect_step(
        self,
        step: TaskStep,
        analysis: Dict[str, Any],
        return_code: int,
        stdout: str,
        stderr: str,
        executed_steps: List[StepExecution],
        pending_steps: List[TaskStep],
        environment_summary: Dict[str, Any],
        next_step_index: int,
        intent: Dict[str, Any],
    ) -> Dict[str, Any]:
        next_steps_payload = analysis.get("next_steps", []) if isinstance(analysis, dict) else []
        built_next_steps: List[TaskStep] = []
        if isinstance(next_steps_payload, list):
            for offset, item in enumerate(next_steps_payload):
                built = self._build_step_from_next_step(
                    step_index=next_step_index + offset,
                    item=item if isinstance(item, dict) else {},
                    environment_summary=environment_summary,
                    intent=intent,
                    default_goal=f"{step.goal} 的后续动作",
                )
                if built:
                    built_next_steps.append(built)

        package_lookup_step = self._build_package_lookup_step_for_missing_binary(
            step=step,
            return_code=return_code,
            analysis=analysis,
            executed_steps=executed_steps,
            environment_summary=environment_summary,
            intent=intent,
            step_index=next_step_index + len(built_next_steps),
        )
        if package_lookup_step:
            built_next_steps = [package_lookup_step]

        built_next_steps = self._filter_next_steps_for_context(
            current_step=step,
            candidate_steps=built_next_steps,
            executed_steps=executed_steps,
            pending_steps=pending_steps,
            intent=intent,
        )

        terminal_diagnostic_evidence = return_code != 0 and self._has_terminal_diagnostic_evidence(
            step=step,
            analysis=analysis,
            stdout=stdout,
            stderr=stderr,
            executed_steps=executed_steps,
            intent=intent,
        )

        if terminal_diagnostic_evidence and self._is_package_lookup_command(self._normalize_command_text(step.command)):
            return {
                "action": "complete",
                "reason": str(analysis.get("explanation", "已完成故障诊断")).strip() or "已完成故障诊断",
                "next_steps": [],
            }
        if return_code != 0 and built_next_steps:
            return {
                "action": "recover",
                "reason": str(analysis.get("explanation", "执行失败，进入恢复流程")).strip() or "执行失败，进入恢复流程",
                "next_steps": built_next_steps,
            }
        if terminal_diagnostic_evidence:
            return {
                "action": "complete",
                "reason": str(analysis.get("explanation", "已完成故障诊断")).strip() or "已完成故障诊断",
                "next_steps": [],
            }
        if (
            return_code == 0
            and built_next_steps
            and pending_steps
            and self._allows_success_plan_revision(intent)
        ):
            return {
                "action": "revise_remaining_steps",
                "reason": str(analysis.get("explanation", "根据执行结果调整剩余步骤")).strip() or "根据执行结果调整剩余步骤",
                "next_steps": built_next_steps,
            }
        if return_code == 0:
            if pending_steps:
                return {
                    "action": "continue",
                    "reason": "当前步骤已满足预期，继续执行剩余步骤。",
                    "next_steps": [],
                }
            return {
                "action": "complete",
                "reason": "当前步骤已满足预期，任务可结束。",
                "next_steps": [],
            }
        return {
            "action": "stop",
            "reason": str(analysis.get("explanation", "步骤执行失败，且没有可执行恢复方案。")).strip() or "步骤执行失败，且没有可执行恢复方案。",
            "next_steps": [],
        }

    def _filter_next_steps_for_context(
        self,
        current_step: TaskStep,
        candidate_steps: List[TaskStep],
        executed_steps: List[StepExecution],
        pending_steps: List[TaskStep],
        intent: Dict[str, Any],
    ) -> List[TaskStep]:
        seen_commands = {
            self._normalize_command_text(current_step.command),
            *(self._normalize_command_text(step.command) for step in executed_steps),
            *(self._normalize_command_text(step.command) for step in pending_steps),
        }
        filtered_steps: List[TaskStep] = []
        for step in candidate_steps:
            normalized = self._normalize_command_text(step.command)
            if not normalized or normalized in seen_commands:
                continue
            if self._has_unresolved_placeholder(step.command):
                continue
            if self._is_diagnostic_intent(intent) and not self._is_observation_only_command(step.command):
                continue
            filtered_steps.append(step)
            seen_commands.add(normalized)
        return filtered_steps

    def _has_terminal_diagnostic_evidence(
        self,
        step: TaskStep,
        analysis: Dict[str, Any],
        stdout: str,
        stderr: str,
        executed_steps: List[StepExecution],
        intent: Dict[str, Any],
    ) -> bool:
        if not self._is_diagnostic_intent(intent):
            return False

        current_text = self._collect_diagnostic_text(
            command=step.command,
            stdout=stdout,
            stderr=stderr,
            analysis=analysis,
        )
        if not self._contains_missing_target_signal(current_text):
            return False

        command = self._normalize_command_text(step.command)
        if self._is_package_lookup_command(command):
            return True

        if self._is_binary_lookup_command(command):
            history_text = " ".join(
                self._collect_diagnostic_text(
                    command=item.command,
                    stdout=item.stdout,
                    stderr=item.stderr,
                    analysis=item.analysis,
                )
                for item in executed_steps
            )
            return self._contains_missing_service_signal(history_text)

        return False

    def _build_package_lookup_step_for_missing_binary(
        self,
        step: TaskStep,
        return_code: int,
        analysis: Dict[str, Any],
        executed_steps: List[StepExecution],
        environment_summary: Dict[str, Any],
        intent: Dict[str, Any],
        step_index: int,
    ) -> Optional[TaskStep]:
        if not self._is_diagnostic_intent(intent):
            return None

        normalized_command = self._normalize_command_text(step.command)
        if not self._is_binary_lookup_command(normalized_command):
            return None

        if return_code == 0:
            return None

        history_text = " ".join(
            self._collect_diagnostic_text(
                command=item.command,
                stdout=item.stdout,
                stderr=item.stderr,
                analysis=item.analysis,
            )
            for item in executed_steps
        )
        if not self._contains_missing_service_signal(history_text):
            return None

        target = self._extract_binary_lookup_target(normalized_command)
        if not target:
            return None

        package_lookup_command = self._build_package_lookup_command(target, environment_summary)
        return self._build_step_from_next_step(
            step_index=step_index,
            item={
                "command": package_lookup_command,
                "explanation": f"检查 {target} 软件包",
                "expected_outcome": f"返回已安装的 {target} 软件包列表",
            },
            environment_summary=environment_summary,
            intent=intent,
            default_goal=f"检查 {target} 软件包",
        )

    def _is_diagnostic_intent(self, intent: Dict[str, Any]) -> bool:
        combined = " ".join(
            [
                str(intent.get("label", "")),
                str(intent.get("summary", "")),
                str(intent.get("input_understanding", "")),
            ]
        ).lower()
        keywords = ["diagnose", "diagnostic", "诊断", "排障", "故障", "原因", "恢复", "分析"]
        return any(keyword in combined for keyword in keywords)

    def _allows_success_plan_revision(self, intent: Dict[str, Any]) -> bool:
        input_understanding = str(intent.get("input_understanding", "")).lower()
        if input_understanding == "query":
            return True
        return self._is_diagnostic_intent(intent)

    def _is_observation_only_command(self, command: str) -> bool:
        normalized = self._normalize_command_text(command)
        mutating_patterns = [
            r"\b(apt|apt-get|yum|dnf)\s+(install|remove|purge|upgrade|dist-upgrade|autoremove)\b",
            r"\b(systemctl|service)\s+(start|stop|restart|reload|enable|disable)\b",
            r"\b(useradd|userdel|usermod|passwd|gpasswd|chmod|chown|rm|mv|cp)\b",
            r"\btee\b",
            r"\bsed\s+-i\b",
        ]
        return not any(re.search(pattern, normalized) for pattern in mutating_patterns)

    def _normalize_command_text(self, command: str) -> str:
        return " ".join(str(command or "").strip().split())

    def _normalize_intent_specific_command(
        self,
        command: str,
        intent: Dict[str, Any],
        hint_text: str = "",
    ) -> str:
        normalized = self._normalize_command_text(command)
        if not normalized:
            return normalized

        input_understanding = str(intent.get("input_understanding", "")).lower()
        if input_understanding == "delete":
            username = self._extract_username_from_command(normalized) or self._extract_candidate_username(hint_text)
            if username and not self._explicitly_requests_home_removal(hint_text):
                normalized = re.sub(
                    rf"^(sudo\s+)?deluser\s+{re.escape(username)}\b",
                    rf"\1userdel {username}",
                    normalized,
                    flags=re.IGNORECASE,
                )
                normalized = re.sub(
                    rf"^(sudo\s+)?userdel\s+-r\s+{re.escape(username)}\b",
                    rf"\1userdel {username}",
                    normalized,
                    flags=re.IGNORECASE,
                )
            if username and self._looks_like_delete_verification_command(normalized, username):
                return f"getent passwd {username} || echo deleted"

        return normalized

    def _prepare_command_for_execution(
        self,
        command: str,
        environment_summary: Dict[str, Any],
        intent: Dict[str, Any],
        hint_text: str = "",
    ) -> str:
        normalized = self._resolve_natural_language_alternative_command(command, environment_summary)
        normalized = self._adapt_command_for_environment(normalized, environment_summary)
        normalized = self._normalize_intent_specific_command(normalized, intent, hint_text)
        return self._normalize_command_text(normalized)

    def _resolve_natural_language_alternative_command(
        self,
        command: str,
        environment_summary: Dict[str, Any],
    ) -> str:
        normalized = self._normalize_command_text(command)
        if not normalized:
            return normalized

        fragments = self._split_natural_language_alternatives(normalized)
        if len(fragments) <= 1:
            return normalized

        resolved: List[str] = []
        for fragment in fragments:
            adapted = self._normalize_command_text(self._adapt_command_for_environment(fragment, environment_summary))
            if adapted and adapted not in resolved:
                resolved.append(adapted)

        return resolved[0] if resolved else normalized

    def _split_natural_language_alternatives(self, command: str) -> List[str]:
        fragments: List[str] = []
        current: List[str] = []
        in_single_quote = False
        in_double_quote = False
        escaped = False
        index = 0

        while index < len(command):
            char = command[index]

            if escaped:
                current.append(char)
                escaped = False
                index += 1
                continue

            if char == "\\":
                current.append(char)
                escaped = True
                index += 1
                continue

            if char == "'" and not in_double_quote:
                in_single_quote = not in_single_quote
                current.append(char)
                index += 1
                continue

            if char == '"' and not in_single_quote:
                in_double_quote = not in_double_quote
                current.append(char)
                index += 1
                continue

            if not in_single_quote and not in_double_quote:
                connector = self._match_natural_language_connector(command, index)
                if connector:
                    fragment = "".join(current).strip()
                    if fragment:
                        fragments.append(fragment)
                    current = []
                    index += len(connector)
                    continue

            current.append(char)
            index += 1

        trailing = "".join(current).strip()
        if trailing:
            fragments.append(trailing)
        return fragments or [command]

    def _match_natural_language_connector(self, command: str, index: int) -> str:
        tail = command[index:]
        connectors = [" 或者 ", " 或 ", " or ", " OR "]
        for connector in connectors:
            if tail.startswith(connector):
                return connector
        return ""

    def _extract_binary_lookup_target(self, command: str) -> str:
        match = re.match(r"^(?:sudo\s+)?(?:which|command\s+-v|type)\s+([A-Za-z0-9._+-]+)\b", command)
        if not match:
            return ""
        return match.group(1)

    def _build_package_lookup_command(self, package_name: str, environment_summary: Dict[str, Any]) -> str:
        package_manager = str(environment_summary.get("package_manager", "apt")).lower()
        if package_manager == "apt":
            return f"dpkg -l | grep {package_name}"
        return f"rpm -qa | grep {package_name}"

    def _extract_username_from_command(self, command: str) -> str:
        patterns = [
            r"\buserdel\s+(?:-r\s+)?([a-z_][a-z0-9_-]{2,31})\b",
            r"\bid\s+([a-z_][a-z0-9_-]{2,31})\b",
            r"\bgetent\s+passwd\s+([a-z_][a-z0-9_-]{2,31})\b",
            r"\bgrep\s+([a-z_][a-z0-9_-]{2,31})\s+/etc/passwd\b",
        ]
        lowered = str(command or "").lower()
        for pattern in patterns:
            match = re.search(pattern, lowered)
            if match:
                return match.group(1)
        return ""

    def _looks_like_delete_verification_command(self, command: str, username: str) -> bool:
        lowered = str(command or "").lower()
        if f"getent passwd {username}" in lowered:
            return False
        if re.search(rf"\bid\s+{re.escape(username)}\b", lowered):
            return True
        if re.search(rf"\bgrep\s+{re.escape(username)}\b.*?/etc/passwd", lowered):
            return True
        return False

    def _explicitly_requests_home_removal(self, text: str) -> bool:
        lowered = str(text or "").lower()
        keywords = ["主目录", "家目录", "home", "目录也删除", "同时删除目录", "彻底删除", "删除用户及其主目录"]
        return any(keyword in lowered for keyword in keywords)

    def _has_unresolved_placeholder(self, command: str) -> bool:
        normalized = self._normalize_command_text(command)
        if not normalized:
            return False
        return bool(re.search(r"<[^>\n]+>", normalized))

    def _is_binary_lookup_command(self, command: str) -> bool:
        return bool(re.search(r"^(sudo\s+)?(which|command\s+-v|type)\s+\S+", command))

    def _is_package_lookup_command(self, command: str) -> bool:
        return bool(
            re.search(r"^(sudo\s+)?dpkg\s+-l\b", command)
            or re.search(r"^(sudo\s+)?rpm\s+-qa\b", command)
            or re.search(r"^(sudo\s+)?dnf\s+list\s+installed\b", command)
            or re.search(r"^(sudo\s+)?apt(-get)?\s+list\s+--installed\b", command)
        )

    def _collect_diagnostic_text(self, command: str, stdout: str, stderr: str, analysis: Dict[str, Any]) -> str:
        analysis_text = ""
        if isinstance(analysis, dict):
            analysis_text = str(analysis.get("explanation", ""))
        return " ".join(
            [
                self._normalize_command_text(command),
                str(stdout or "").strip(),
                str(stderr or "").strip(),
                analysis_text.strip(),
            ]
        ).lower()

    def _contains_missing_target_signal(self, text: str) -> bool:
        patterns = [
            r"could not be found",
            r"not found",
            r"no package",
            r"未找到",
            r"不存在",
            r"未安装",
            r"未检测到已安装",
            r"无匹配",
        ]
        return any(re.search(pattern, text) for pattern in patterns)

    def _contains_missing_service_signal(self, text: str) -> bool:
        patterns = [
            r"unit .* could not be found",
            r"service .* could not be found",
            r"服务单元不存在",
            r"服务不存在",
        ]
        return any(re.search(pattern, text) for pattern in patterns)

    def _is_unusable_command(self, command: str, explanation: str) -> bool:
        if not command:
            return True
        lowered = command.lower()
        if "unable to generate a proper command" in lowered:
            return True
        if command.startswith("```") or explanation.startswith("```json"):
            return True
        return False

    def _fallback_response(self, user_input: str) -> Dict[str, Any]:
        text = user_input.lower()
        if "系统基本信息" in user_input or "系统信息" in user_input:
            return {
                "command": "hostnamectl",
                "explanation": "显示系统基本信息，包括主机名、内核和发行版信息",
                "dangerous": False,
            }
        if "帮助" in user_input or text == "help":
            return {
                "command": "printf '%s\\n' 'OS_Agent CLI 支持：自然语言任务提交、风险确认、执行回放和审计查看。'",
                "explanation": "显示 OS_Agent CLI 的帮助信息",
                "dangerous": False,
            }
        if "磁盘" in user_input or "空间" in user_input:
            return {
                "command": "df -h",
                "explanation": "查看当前磁盘空间使用情况",
                "dangerous": False,
            }
        if "内存" in user_input:
            return {
                "command": "free -h",
                "explanation": "查看当前内存使用情况",
                "dangerous": False,
            }
        return {}

    def _normalize_analysis(self, analysis: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(analysis, dict):
            return {"explanation": str(analysis)}
        explanation = analysis.get("explanation")
        if isinstance(explanation, str):
            parsed = self._parse_markdown_json(explanation)
            if isinstance(parsed, dict):
                merged = dict(analysis)
                merged.update(parsed)
                return merged
        return analysis

    def _parse_markdown_json(self, text: str) -> Optional[Dict[str, Any]]:
        match = re.search(r"```json\s*(\{.*\})\s*```", text, re.DOTALL)
        candidate = match.group(1) if match else None
        if candidate is None:
            stripped = text.strip()
            if stripped.startswith("{") and stripped.endswith("}"):
                candidate = stripped
        if not candidate:
            return None
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, dict) else None

    def _summarize_environment(self, system_info: Dict[str, Any]) -> Dict[str, Any]:
        distribution = (
            system_info.get("ID")
            or system_info.get("NAME")
            or system_info.get("OS")
            or "linux"
        )
        distribution = str(distribution).lower()
        if "ubuntu" in distribution or "debian" in distribution:
            package_manager = "apt"
        elif "centos" in distribution:
            package_manager = "yum"
        else:
            package_manager = "dnf"
        service_manager = "systemctl"
        network_tool = "ss"
        return {
            "distribution": distribution,
            "hostname": system_info.get("HOSTNAME") or system_info.get("hostname", ""),
            "package_manager": package_manager,
            "service_manager": service_manager,
            "network_tool": network_tool,
        }

    def _adapt_command_for_environment(self, command: str, environment_summary: Dict[str, Any]) -> str:
        normalized = " ".join(str(command or "").strip().split())
        if not normalized:
            return normalized

        package_manager = environment_summary.get("package_manager", "apt")
        network_tool = environment_summary.get("network_tool", "ss")

        if package_manager in {"dnf", "yum"}:
            normalized = re.sub(r"^(sudo\s+)?apt(-get)?\s+install\s+", rf"\1{package_manager} install -y ", normalized)
            normalized = re.sub(r"^(sudo\s+)?apt(-get)?\s+remove\s+", rf"\1{package_manager} remove -y ", normalized)
            normalized = re.sub(r"^(sudo\s+)?dpkg\s+-l\s+\|\s+grep\s+", "rpm -qa | grep ", normalized)
            normalized = re.sub(r"^(sudo\s+)?apt(-get)?\s+list\s+--installed\b", f"{package_manager} list installed", normalized)
        elif package_manager == "apt":
            normalized = re.sub(r"^(sudo\s+)?yum\s+install\s+", r"\1apt install ", normalized)
            normalized = re.sub(r"^(sudo\s+)?dnf\s+install\s+", r"\1apt install ", normalized)
            normalized = re.sub(r"^(sudo\s+)?rpm\s+-qa\s+\|\s+grep\s+", "dpkg -l | grep ", normalized)

        if network_tool == "ss" and normalized.startswith("netstat "):
            normalized = "ss -tunap"

        return normalized

    def _compose_blocked_feedback(self, blocked_steps: List[TaskStep]) -> str:
        first = blocked_steps[0]
        return f"任务已阻断。步骤 {first.index} 的命令 `{first.command}` 被判定为不可执行。原因: {first.risk.explanation}"

    def _compose_failure_feedback(self, step: StepExecution) -> str:
        explanation = step.analysis.get("explanation", "") if step.analysis else ""
        next_action = step.analysis.get("next_action", "") if step.analysis else ""
        next_action_text = f" 反思决策: {next_action}。" if next_action else ""
        return f"任务在步骤 {step.index} 失败。命令: {step.command}。错误: {step.stderr or '无标准错误输出'}。{explanation}{next_action_text}"

    def _compose_success_feedback(self, trace: ExecutionTrace) -> str:
        if not trace.steps:
            return "任务未执行任何步骤。"
        last_analysis = trace.steps[-1].analysis.get("explanation", "") if trace.steps[-1].analysis else ""
        adjustment_text = ""
        if trace.trace_summary.get("recovery_used"):
            adjustment_text = " 执行过程中已触发失败恢复。"
        elif trace.trace_summary.get("plan_adjustments"):
            adjustment_text = " 执行过程中已根据观察结果调整后续步骤。"
        return f"任务执行完成，共执行 {len(trace.steps)} 个步骤。{last_analysis}{adjustment_text}"

    def _should_run_analysis(self, plan: TaskPlan, step: TaskStep, return_code: int) -> bool:
        if self._prefers_local_analysis(step.command):
            return False
        if self.analysis_mode == "full":
            return True
        if self.analysis_mode == "off":
            return False
        if return_code != 0:
            return True
        if len(plan.steps) > 1:
            return True
        return (step.risk.level if step.risk else "low") != "low"

    def _build_local_analysis(self, command: str, stdout: str, stderr: str, return_code: int) -> Dict[str, Any]:
        account_verification_analysis = self._build_account_verification_analysis(command, stdout, stderr, return_code)
        if account_verification_analysis:
            return account_verification_analysis
        empty_observation_analysis = self._build_empty_observation_analysis(command, stdout, stderr, return_code)
        if empty_observation_analysis:
            return empty_observation_analysis
        if return_code == 0:
            process_analysis = self._build_process_observation_analysis(command, stdout)
            if process_analysis:
                return process_analysis
            if stdout.strip():
                explanation = f"命令 `{command}` 已成功执行。输出摘要: {self._preview_text(stdout)}"
            else:
                explanation = f"命令 `{command}` 已成功执行，未返回标准输出。"
            return {
                "explanation": explanation,
                "recommendations": [],
                "next_steps": [],
                "analysis_mode": "local",
            }
        return {
            "explanation": f"命令 `{command}` 执行失败。错误摘要: {self._preview_text(stderr or stdout)}",
            "recommendations": [],
            "next_steps": [],
            "analysis_mode": "local",
        }

    def _prefers_local_analysis(self, command: str) -> bool:
        normalized = self._normalize_command_text(command)
        if not normalized:
            return False
        if re.fullmatch(r"id\s+[a-z_][a-z0-9_-]{2,31}", normalized, re.IGNORECASE):
            return True
        if re.fullmatch(r"getent\s+passwd\s+[a-z_][a-z0-9_-]{2,31}\s+\|\|\s+echo\s+deleted", normalized, re.IGNORECASE):
            return True
        return False

    def _build_account_verification_analysis(
        self,
        command: str,
        stdout: str,
        stderr: str,
        return_code: int,
    ) -> Optional[Dict[str, Any]]:
        normalized = self._normalize_command_text(command)
        id_match = re.fullmatch(r"id\s+([a-z_][a-z0-9_-]{2,31})", normalized, re.IGNORECASE)
        if id_match:
            username = id_match.group(1)
            if return_code == 0:
                summary = self._preview_text(stdout)
                return {
                    "explanation": f"已确认用户 {username} 存在。系统返回: {summary}",
                    "recommendations": [],
                    "next_steps": [],
                    "analysis_mode": "local",
                }
            return {
                "explanation": f"已确认用户 {username} 不存在。错误摘要: {self._preview_text(stderr or stdout)}",
                "recommendations": [],
                "next_steps": [],
                "analysis_mode": "local",
            }

        getent_match = re.fullmatch(
            r"getent\s+passwd\s+([a-z_][a-z0-9_-]{2,31})\s+\|\|\s+echo\s+deleted",
            normalized,
            re.IGNORECASE,
        )
        if getent_match:
            username = getent_match.group(1)
            output = (stdout or "").strip()
            if output == "deleted":
                return {
                    "explanation": f"已确认用户 {username} 不存在，说明删除验证通过。",
                    "recommendations": [],
                    "next_steps": [],
                    "analysis_mode": "local",
                }
            if output:
                return {
                    "explanation": f"检测到用户 {username} 仍存在。系统返回: {self._preview_text(output)}",
                    "recommendations": [],
                    "next_steps": [],
                    "analysis_mode": "local",
                }
            return {
                "explanation": f"用户 {username} 验证已完成，但未返回明确输出。",
                "recommendations": [],
                "next_steps": [],
                "analysis_mode": "local",
            }

        return None

    def _normalize_observation_return_code(
        self,
        command: str,
        stdout: str,
        stderr: str,
        return_code: int,
    ) -> int:
        normalized = self._normalize_command_text(command).lower()
        if return_code != 1:
            return return_code
        if stdout.strip() or stderr.strip():
            return return_code
        if not self._is_observation_only_command(command):
            return return_code
        if "| grep" in normalized or normalized.startswith("grep "):
            return 0
        return return_code

    def _normalize_delete_return_code(
        self,
        command: str,
        stdout: str,
        stderr: str,
        return_code: int,
    ) -> int:
        normalized = self._normalize_command_text(command).lower()
        if return_code == 0:
            return return_code
        if not re.search(r"^(sudo\s+)?(userdel|deluser)\b", normalized):
            return return_code

        lowered_error = str(stderr or "").lower()
        missing_patterns = [
            "does not exist",
            "doesn't exist",
            "not exist",
            "不存在",
            "未找到",
            "no such user",
        ]
        if any(pattern in lowered_error for pattern in missing_patterns):
            return 0
        return return_code

    def _build_empty_observation_analysis(
        self,
        command: str,
        stdout: str,
        stderr: str,
        return_code: int,
    ) -> Optional[Dict[str, Any]]:
        normalized = self._normalize_command_text(command)
        if return_code != 0 or stdout.strip() or stderr.strip():
            return None
        lowered = normalized.lower()
        if "| grep" not in lowered and not lowered.startswith("grep "):
            return None

        port_match = re.search(r":(\d+)", normalized)
        if port_match and "ss " in f" {lowered} ":
            port = port_match.group(1)
            explanation = f"未发现监听 {port} 端口的进程，说明当前 {port} 端口未被占用。"
        else:
            explanation = f"命令 `{normalized}` 已执行完成，但未发现匹配结果。"

        return {
            "explanation": explanation,
            "recommendations": [],
            "next_steps": [],
            "analysis_mode": "local",
        }

    def _build_process_observation_analysis(self, command: str, stdout: str) -> Optional[Dict[str, Any]]:
        normalized = self._normalize_command_text(command).lower()
        if "--sort=-%cpu" not in normalized or ("/proc/" not in normalized and "readlink" not in normalized):
            return None

        pid_match = re.search(r"PID:\s*(\d+)", stdout or "")
        if not pid_match:
            pid_match = re.search(r"^\s*(\d+)\s+\S+\s+\d+(?:\.\d+)?", stdout or "", re.MULTILINE)
        path_match = re.search(r"/proc/\d+/exe\s*->\s*(/.+)", stdout or "")
        explicit_path_match = re.search(r"PATH:\s*(/.+)", stdout or "")
        executable_path = ""
        if explicit_path_match:
            executable_path = explicit_path_match.group(1).strip()
        elif path_match:
            executable_path = path_match.group(1).strip()

        permissions = owner = group = ""
        explicit_perm_match = re.search(
            r"PERMISSIONS:\s*([\-dlpscb][rwx-]{9}).*?OWNER:\s*(\S+)\s+GROUP:\s*(\S+)",
            stdout or "",
        )
        if explicit_perm_match:
            permissions, owner, group = explicit_perm_match.groups()
        else:
            for line in (stdout or "").splitlines():
                parts = line.split()
                if len(parts) >= 9 and re.match(r"^[\-dlpscb][rwx-]{9}$", parts[0]) and parts[-1].startswith("/"):
                    permissions = parts[0]
                    owner = parts[2]
                    group = parts[3]
                    if not executable_path:
                        executable_path = parts[-1]

        details = []
        if pid_match:
            details.append(f"CPU 占用最高的进程 PID 为 {pid_match.group(1)}")
        if executable_path:
            details.append(f"可执行文件路径为 {executable_path}")
        if permissions:
            owner_group = f"{owner}:{group}" if owner or group else "未知属主"
            details.append(f"文件权限为 {permissions}，属主属组为 {owner_group}")

        if not details:
            return None

        return {
            "explanation": "；".join(details) + "。",
            "recommendations": [],
            "next_steps": [],
            "analysis_mode": "local",
        }

    def _preview_text(self, value: str, limit: int = 240) -> str:
        text = " ".join((value or "").strip().split())
        if not text:
            return "无输出"
        if len(text) <= limit:
            return text
        return f"{text[:limit]}..."

    def _finalize_trace_summary(self, trace: ExecutionTrace, execution_ms: int, analysis_ms: int) -> None:
        planning_ms = int(trace.trace_summary.get("planning_ms", 0))
        trace.trace_summary["status"] = trace.status
        trace.trace_summary["state"] = trace.state
        trace.trace_summary["step_count"] = len(trace.steps)
        trace.trace_summary["execution_ms"] = int(execution_ms)
        trace.trace_summary["analysis_ms"] = int(analysis_ms)
        trace.trace_summary["total_ms"] = planning_ms + int(execution_ms) + int(analysis_ms)

    def _persist_result(self, trace: ExecutionTrace) -> None:
        if self.audit_store:
            self.audit_store.save_result(trace)

    def _emit(
        self,
        callback: Optional[Callable[[Dict[str, Any]], None]],
        task_id: str,
        event_type: str,
        payload: Dict[str, Any],
    ) -> None:
        event = {
            "task_id": task_id,
            "type": event_type,
            "timestamp": utcnow_iso(),
            **payload,
        }
        if self.audit_store:
            self.audit_store.append_event(task_id, event)
        if callback:
            callback(event)

    def _transition_trace_state(
        self,
        trace: ExecutionTrace,
        new_state: str,
        event_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
        payload: Optional[Dict[str, Any]] = None,
    ) -> None:
        payload = payload or {}
        if trace.state == new_state and trace.trace_summary.get("state_transitions"):
            return
        trace.state = new_state
        trace.status = new_state
        transition = {
            "state": new_state,
            "timestamp": utcnow_iso(),
            **payload,
        }
        transitions = trace.trace_summary.setdefault("state_transitions", [])
        transitions.append(transition)
        self._emit(
            event_callback,
            trace.task_id,
            "state_changed",
            transition,
        )

    def _new_task_id(self) -> str:
        return f"task-{uuid.uuid4().hex[:12]}"
