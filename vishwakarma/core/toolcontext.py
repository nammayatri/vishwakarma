"""
Per-tool-call context — lets a tool know which investigation it's running in
without threading incident_id through every signature.

ToolExecutor.execute() sets `current_incident` right before invoking the
tool handler. Because execute() and the handler run in the SAME thread (the
engine's worker thread calls execute synchronously), the ContextVar value is
visible to the toolset and naturally isolated per worker thread / per
concurrent investigation.
"""
from contextvars import ContextVar

current_incident: ContextVar[str] = ContextVar("current_incident", default="")
