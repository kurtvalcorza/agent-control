from __future__ import annotations

import json
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from .capabilities import CapabilityInvocationError
from .models import CapabilityDescriptor, CapabilityResult


@dataclass(slots=True)
class SubprocessCapability:
    """JSON-over-stdin/stdout capability boundary with no shell invocation."""

    descriptor: CapabilityDescriptor
    command: Sequence[str]
    timeout_seconds: float = 60.0

    def invoke(self, arguments: dict[str, Any]) -> CapabilityResult:
        try:
            completed = subprocess.run(
                list(self.command),
                input=json.dumps(arguments),
                text=True,
                capture_output=True,
                timeout=self.timeout_seconds,
                check=False,
                shell=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise CapabilityInvocationError(str(exc)) from exc
        if completed.returncode != 0:
            raise CapabilityInvocationError(completed.stderr.strip() or "subprocess failed")
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise CapabilityInvocationError("subprocess returned invalid JSON") from exc
        if isinstance(payload, dict) and "output" in payload:
            return CapabilityResult(
                output=payload["output"],
                actual_cost_usd=float(payload.get("actual_cost_usd", 0.0)),
                metadata=dict(payload.get("metadata", {})),
            )
        return CapabilityResult(output=payload)


MCPInvoker = Callable[[str, dict[str, Any]], dict[str, Any]]


@dataclass(slots=True)
class MCPCapability:
    """SDK-independent MCP tool adapter supplied with a host transport callback."""

    descriptor: CapabilityDescriptor
    tool_name: str
    invoke_tool: MCPInvoker

    def invoke(self, arguments: dict[str, Any]) -> CapabilityResult:
        try:
            payload = self.invoke_tool(self.tool_name, arguments)
        except Exception as exc:
            raise CapabilityInvocationError(f"MCP invocation failed: {exc}") from exc
        if "output" not in payload:
            raise CapabilityInvocationError("MCP response must contain output")
        return CapabilityResult(
            output=payload["output"],
            actual_cost_usd=float(payload.get("actual_cost_usd", 0.0)),
            metadata=dict(payload.get("metadata", {})),
        )


@dataclass(slots=True)
class AgentRouterCapability(SubprocessCapability):
    """Reference subprocess boundary for agent-router without importing its policy code."""

    @classmethod
    def from_cli(
        cls,
        descriptor: CapabilityDescriptor,
        *,
        catalog: str,
        execute: bool = True,
        timeout_seconds: float = 120.0,
    ) -> AgentRouterCapability:
        command = ["agent-router", "route", "-", "--catalog", catalog, "--json"]
        if execute:
            command.append("--execute")
        return cls(descriptor=descriptor, command=command, timeout_seconds=timeout_seconds)
