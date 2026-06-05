"""
Runbook toolset — agentic runbook retrieval mid-investigation.

The orchestrator injects the best upfront runbook match, but the agent knows
far more after a few recon steps. runbook_search lets it re-query with a
recon-enriched description ("RDS CPU high + deploy at 10:34 + seqscan on
driver_offers") — post-recon queries are where most retrieval precision
comes from, since one alert name can map to many root causes.

Always active (like todo) — it only reads the runbook tables.
"""
import logging

from vishwakarma.core.tools import Toolset, ToolDef, ToolOutput, ToolStatus
from vishwakarma.core.toolset_manager import register_toolset

log = logging.getLogger(__name__)


@register_toolset
class RunbookToolset(Toolset):
    name = "runbooks"
    description = (
        "Search the runbook library mid-investigation. After initial recon, "
        "search with what you've learned (symptoms + findings, not just the "
        "alert name) to pull a more specific investigation runbook."
    )

    def __init__(self, config: dict | None = None):
        super().__init__(config)
        self.cloud = (config or {}).get("cloud", "")

    def check_prerequisites(self) -> tuple[bool, str]:
        return True, ""

    def get_tools(self) -> list[ToolDef]:
        return [
            ToolDef(
                name="runbook_search",
                description=(
                    "Find runbooks matching a description of the problem. Use a "
                    "rich query including symptoms and evidence found so far, "
                    "e.g. 'RDS CPU spike correlated with deploy, seqscan on large "
                    "table'. Returns the top matching runbooks' content."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Problem description enriched with recon findings",
                        },
                        "max_results": {"type": "integer", "description": "Default 2"},
                    },
                    "required": ["query"],
                },
            ),
        ]

    def execute(self, tool_name: str, params: dict) -> ToolOutput:
        if tool_name != "runbook_search":
            return ToolOutput(tool_name=tool_name, status=ToolStatus.ERROR,
                              error=f"Unknown tool: {tool_name}")
        query = params.get("query", "").strip()
        if not query:
            return ToolOutput(tool_name=tool_name, status=ToolStatus.ERROR,
                              error="query required")
        top_k = min(int(params.get("max_results") or 2), 3)
        try:
            from vishwakarma.core.runbook_match import match_runbooks
            matched = match_runbooks(query, cloud=self.cloud, top_k=top_k)
        except Exception as e:
            return ToolOutput(tool_name=tool_name, status=ToolStatus.ERROR,
                              error=f"runbook search failed: {e}")
        if not matched:
            return ToolOutput(tool_name=tool_name, status=ToolStatus.NO_DATA,
                              invocation=f"runbook_search({query[:60]})")
        out = "\n\n---\n\n".join(
            f"# Runbook: {m['title']} (id: {m['id']})\n\n{m['content_md']}"
            for m in matched
        )
        return ToolOutput(tool_name=tool_name, status=ToolStatus.SUCCESS, output=out,
                          invocation=f"runbook_search({query[:60]})")
