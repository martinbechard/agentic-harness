"""Portable CLI adapter contracts; native process details stay inside adapters."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from .contracts import AgentBinding, ConfigSnapshot
from .telemetry import TelemetryDestination


@dataclass(frozen=True)
class SessionHandle:
    session_id: str
    native_session_id: str
    binding: AgentBinding


@dataclass(frozen=True)
class AgentRequest:
    operation_id: str
    invocation_id: str
    snapshot: ConfigSnapshot
    binding: AgentBinding
    prompt: str
    evidence_path: Path
    telemetry: TelemetryDestination
    timeout_seconds: float = 90
    capability_probe: bool = False
    read_only: bool = True
    purpose: str = "implementation"
    provider_operation: str | None = None


@dataclass
class InvocationHandle:
    invocation_id: str
    evidence_path: Path
    session: SessionHandle | None = None
    outcome: str = "requested"
    events: list[dict] = field(default_factory=list)


class AgentCliAdapter(Protocol):
    def validate_profile(self, request: AgentRequest) -> dict: ...
    async def prepare_telemetry(self, request: AgentRequest) -> dict: ...
    async def start_session(self, request: AgentRequest) -> InvocationHandle: ...
    async def resume_session(
        self, session: SessionHandle, request: AgentRequest
    ) -> InvocationHandle: ...
    def observe_events(self, invocation: InvocationHandle) -> AsyncIterator[dict]: ...
    async def reconcile(self, invocation: InvocationHandle) -> dict: ...
    async def request_interrupt(self, invocation: InvocationHandle) -> dict: ...
