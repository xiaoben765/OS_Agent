#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
命令风险评估
"""

from __future__ import annotations

import os
import re
import shlex
from typing import Any, Dict, Iterable, List

from .models import RiskAssessment


RISK_ORDER = {
    "low": 0,
    "medium": 1,
    "high": 2,
    "blocked": 3,
}


class RiskEvaluator:
    """基于规则、路径和操作范围判断命令风险"""

    def __init__(self, security_config):
        self.blocked_commands = list(getattr(security_config, "blocked_commands", []))
        self.confirm_patterns = list(getattr(security_config, "confirm_patterns", []))
        self.protected_paths = list(
            getattr(
                security_config,
                "protected_paths",
                ["/", "/etc", "/boot", "/usr", "/root", "/var/lib"],
            )
        )
        self.sensitive_files = list(
            getattr(
                security_config,
                "sensitive_files",
                ["/etc/passwd", "/etc/shadow", "/etc/sudoers"],
            )
        )

    def assess_command(self, command: str, environment_summary: Dict[str, Any] | None = None) -> RiskAssessment:
        environment_summary = environment_summary or {}
        normalized = command.strip()
        reasons: List[str] = []
        scope: List[str] = []
        level = "low"
        allowed = True
        requires_confirmation = False

        for blocked in self.blocked_commands:
            if normalized == blocked or normalized.startswith(f"{blocked} "):
                reasons.append(f"命中禁用命令规则: {blocked}")
                level = "blocked"
                allowed = False

        for pattern in self.confirm_patterns:
            if re.search(pattern, normalized):
                level = self._raise_level(level, "medium")
                reasons.append(f"命中确认规则: {pattern}")
                requires_confirmation = True

        paths = self._extract_paths(normalized)
        scope.extend(paths)

        if self._matches_deletion(normalized):
            deletion_level = self._assess_deletion(paths)
            level = self._raise_level(level, deletion_level)
            reasons.append("检测到删除类操作")

        if self._touches_sensitive_file_mutation(normalized):
            level = self._raise_level(level, "blocked")
            reasons.append("检测到敏感系统文件修改")

        if re.search(r"\bchmod\b", normalized) and re.search(r"-R\s+777", normalized):
            level = self._raise_level(level, "blocked")
            reasons.append("检测到递归开放 777 权限")
            allowed = False

        if re.search(r"\b(chown|chmod)\b", normalized):
            level = self._raise_level(level, "medium")
            reasons.append("检测到权限或属主变更操作")
            requires_confirmation = True

        if re.search(r"\b(chown|chmod)\b", normalized) and any(self._is_protected_path(path) for path in paths):
            level = self._raise_level(level, "blocked")
            reasons.append("检测到对关键目录的权限变更")
            allowed = False

        if re.search(r"^(sudo\s+)?(useradd|userdel|usermod|passwd|gpasswd)\b", normalized):
            level = self._raise_level(level, "high")
            reasons.append("检测到用户与权限管理操作")
            requires_confirmation = True

        if re.search(r"\b(apt|apt-get|yum|dnf)\b", normalized):
            level = self._raise_level(level, "medium")
            reasons.append("检测到系统级安装或服务管理操作")
            requires_confirmation = True

        if re.search(r"\b(systemctl|service)\s+(start|stop|restart|reload|enable|disable)\b", normalized):
            level = self._raise_level(level, "medium")
            reasons.append("检测到服务状态变更操作")
            requires_confirmation = True

        if re.search(r"\|\s*(sh|bash)\b", normalized) and re.search(r"\b(curl|wget)\b", normalized):
            level = self._raise_level(level, "blocked")
            reasons.append("检测到远程下载后直接执行的高风险组合命令")
            allowed = False

        if level == "blocked":
            allowed = False
            requires_confirmation = False
        elif level in {"medium", "high"}:
            requires_confirmation = True

        classification = self._classify_level(level)
        explanation = self._build_explanation(level, reasons, scope, environment_summary)
        return RiskAssessment(
            level=level,
            allowed=allowed,
            requires_confirmation=requires_confirmation,
            classification=classification,
            rule_sources=list(reasons or ["未发现明显风险特征"]),
            reasons=reasons or ["未发现明显风险特征"],
            scope=sorted(set(scope)),
            explanation=explanation,
        )

    def combine_levels(self, levels: Iterable[str]) -> str:
        highest = "low"
        for level in levels:
            highest = self._raise_level(highest, level)
        return highest

    def _raise_level(self, current: str, incoming: str) -> str:
        return incoming if RISK_ORDER[incoming] > RISK_ORDER[current] else current

    def _extract_paths(self, command: str) -> List[str]:
        try:
            tokens = shlex.split(command)
        except ValueError:
            tokens = command.split()
        paths = []
        for token in tokens:
            if token.startswith("/"):
                paths.append(token.rstrip(";"))
        return paths

    def _matches_deletion(self, command: str) -> bool:
        return bool(re.search(r"\brm\b", command) or re.search(r"\b(find|xargs)\b.*\bdelete\b", command))

    def _assess_deletion(self, paths: List[str]) -> str:
        if not paths:
            return "high"
        for path in paths:
            if self._is_protected_path(path):
                return "blocked"
        return "high"

    def _is_protected_path(self, path: str) -> bool:
        normalized = os.path.normpath(path)
        for protected in self.protected_paths:
            protected_norm = os.path.normpath(protected)
            if protected_norm == "/":
                if normalized == "/":
                    return True
                continue
            if normalized == protected_norm or normalized.startswith(protected_norm.rstrip("/") + "/"):
                return True
        return False

    def _touches_sensitive_file_mutation(self, command: str) -> bool:
        if not any(file_path in command for file_path in self.sensitive_files):
            return False
        mutation_patterns = [
            r"(^|\s)(rm|mv|cp|tee|sed\s+-i|chmod|chown|echo|printf)\b",
            r">",
            r">>",
        ]
        return any(re.search(pattern, command) for pattern in mutation_patterns)

    def _classify_level(self, level: str) -> str:
        if level == "blocked":
            return "block"
        if level in {"medium", "high"}:
            return "confirm"
        return "safe"

    def _build_explanation(
        self,
        level: str,
        reasons: List[str],
        scope: List[str],
        environment_summary: Dict[str, Any],
    ) -> str:
        distro = environment_summary.get("distribution", "unknown")
        scope_text = "、".join(scope) if scope else "当前会话范围"
        reason_text = "；".join(reasons) if reasons else "未发现明显风险"
        classification = self._classify_level(level)
        return f"风险等级: {level}（{classification}）。影响范围: {scope_text}。判定依据: {reason_text}。目标环境: {distro}。"
