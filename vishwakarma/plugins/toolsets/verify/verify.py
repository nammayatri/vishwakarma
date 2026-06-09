"""
Active verification — turn a hypothesis into a confirmed/refuted verdict.

Most RCAs fail by stopping at correlation ("CPU is high AND a deploy happened"
≠ "the deploy caused it"). verify_hypothesis makes the agent run a SPECIFIC
read-only check whose result would confirm or refute the hypothesis, and
returns a structured verdict — which both raises real confidence and is
recorded as hard evidence.

The check runs through the bash toolset's safety (read-only allow/block,
destructive-SQL blocking), so this can't mutate anything.
"""
import logging
import re

from vishwakarma.core.tools import Toolset, ToolDef, ToolOutput, ToolStatus
from vishwakarma.core.toolset_manager import register_toolset

log = logging.getLogger(__name__)


@register_toolset
class VerifyToolset(Toolset):
    name = "verify"
    description = (
        "Actively verify a root-cause hypothesis instead of inferring from "
        "correlation. Provide the hypothesis, a read-only check whose output "
        "would CONFIRM it, and the signal to look for. Returns a "
        "CONFIRMED/REFUTED verdict with the matching evidence."
    )

    def __init__(self, config: dict | None = None):
        super().__init__(config)
        self._bash = None

    def _bash_toolset(self):
        if self._bash is None:
            from vishwakarma.plugins.toolsets.bash.bash import BashToolset
            # Inherit the deployment's bash safety config when provided.
            self._bash = BashToolset(self.config.get("bash_config", {}))
        return self._bash

    def check_prerequisites(self) -> tuple[bool, str]:
        return True, ""

    def get_tools(self) -> list[ToolDef]:
        return [
            ToolDef(
                name="verify_hypothesis",
                description=(
                    "Run a read-only check to confirm or refute a root-cause "
                    "hypothesis. e.g. hypothesis='missing index on driver_offers "
                    "causes the CPU spike', check='EXPLAIN ... ' via a db query or "
                    "a kubectl/grep command, expect='Seq Scan'. Verdict is "
                    "CONFIRMED if the expected signal appears, REFUTED otherwise."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "hypothesis": {"type": "string"},
                        "check": {"type": "string",
                                  "description": "A read-only shell command that tests the hypothesis"},
                        "expect": {"type": "string",
                                   "description": "Regex/substring that appears in the output IF the hypothesis is true"},
                        "expect_absent": {"type": "boolean",
                                          "description": "Set true if the hypothesis is CONFIRMED by the signal being ABSENT"},
                    },
                    "required": ["hypothesis", "check", "expect"],
                },
            ),
        ]

    def execute(self, tool_name: str, params: dict) -> ToolOutput:
        if tool_name != "verify_hypothesis":
            return ToolOutput(tool_name=tool_name, status=ToolStatus.ERROR,
                              error=f"Unknown tool: {tool_name}")
        hypothesis = params.get("hypothesis", "")
        check = params.get("check", "").strip()
        expect = params.get("expect", "")
        expect_absent = bool(params.get("expect_absent"))
        if not check or not expect:
            return ToolOutput(tool_name=tool_name, status=ToolStatus.ERROR,
                              error="check and expect are required")

        # Run the check through bash safety.
        result = self._bash_toolset().execute("bash", {"command": check})
        if result.status == ToolStatus.ERROR:
            return ToolOutput(
                tool_name=tool_name, status=ToolStatus.ERROR,
                error=f"check could not run: {result.error}",
                invocation=f"verify_hypothesis({check[:60]})")

        output = str(result.output or "")
        try:
            present = re.search(expect, output) is not None
        except re.error:
            present = expect in output   # fall back to literal substring

        confirmed = (not present) if expect_absent else present
        verdict = "CONFIRMED" if confirmed else "REFUTED"

        # Evidence: the matching line(s), capped.
        evidence_lines = [ln for ln in output.splitlines()
                          if (re.search(expect, ln) if _safe_re(expect) else expect in ln)]
        evidence = "\n".join(evidence_lines[:8]) or output[:500]

        body = (
            f"VERDICT: {verdict}\n"
            f"Hypothesis: {hypothesis}\n"
            f"Check: {check}\n"
            f"Expected {'ABSENCE of ' if expect_absent else ''}signal: {expect!r} "
            f"→ {'found' if present else 'not found'}\n\n"
            f"Evidence:\n{evidence}"
        )
        return ToolOutput(tool_name=tool_name, status=ToolStatus.SUCCESS, output=body,
                          invocation=f"verify_hypothesis({hypothesis[:50]}) → {verdict}")


def _safe_re(pattern: str) -> bool:
    try:
        re.compile(pattern)
        return True
    except re.error:
        return False
