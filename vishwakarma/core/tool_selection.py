"""
Curated tool-subset selection per investigation.

Handing the LLM all ~20 toolsets every step hurts tool-selection accuracy and
busts the prompt cache. Instead the orchestrator picks a relevant subset from
the alert type + cloud, and the engine keeps that set STABLE across the whole
investigation (so the tool+prompt prefix is a cache hit after turn 1). The
agent can still reach the full catalog — selection only trims the default.

Conservative by design: a core set is ALWAYS included, and if selection is
empty or uncertain we fall back to ALL tools (never strand the agent).
"""
import logging

log = logging.getLogger(__name__)

# Always available — investigation plumbing + cheap universal tools.
CORE_TOOLSETS = {"bash", "todo", "runbooks", "learnings", "code_analyst", "code_session"}

# Domain → toolsets. An alert matching a domain's keywords pulls its toolsets.
_DOMAIN_TOOLSETS: dict[str, set[str]] = {
    "metrics": {"prometheus", "grafana", "datadog", "newrelic"},
    "logs": {"elasticsearch", "coralogix", "datadog"},
    "database": {"database", "mongodb"},
    "streaming": {"kafka"},
    "cloud": {"aws"},
    "network": {"internet", "http"},
    "tickets": {"servicenow_tables", "cloud_alerts"},
}

_DOMAIN_KEYWORDS: dict[str, tuple[str, ...]] = {
    "metrics": ("cpu", "memory", "latency", "5xx", "error rate", "throughput",
                "saturation", "spike", "p99", "request count", "qps"),
    "logs": ("error", "exception", "stacktrace", "stack trace", "log", "500",
             "timeout", "panic", "fatal"),
    "database": ("rds", "alloydb", "postgres", "mysql", "clickhouse", "db",
                 "query", "connection", "replication", "index", "deadlock",
                 "pgbouncer", "mongo"),
    "streaming": ("kafka", "consumer", "lag", "partition", "drainer", "queue"),
    "cloud": ("rds", "ec2", "alb", "elb", "s3", "sqs", "cloudwatch", "lambda",
              "asg", "eks", "gke"),
    "network": ("dns", "5xx", "alb", "ingress", "gateway", "connection refused",
                "unreachable", "endpoint"),
    "tickets": ("incident", "ticket", "jira", "servicenow"),
}


def select_toolset_names(alert_text: str, available: set[str]) -> set[str]:
    """
    Return the curated toolset names for an alert, intersected with what's
    actually enabled. Always includes the core set. Falls back to ALL enabled
    toolsets when nothing domain-specific matches (uncertain → don't trim).
    """
    text = (alert_text or "").lower()
    selected: set[str] = set(CORE_TOOLSETS)

    matched_domain = False
    for domain, keywords in _DOMAIN_KEYWORDS.items():
        if any(kw in text for kw in keywords):
            selected |= _DOMAIN_TOOLSETS.get(domain, set())
            matched_domain = True

    selected &= available  # only what's enabled

    if not matched_domain:
        # No clear domain — give everything (conservative).
        return set(available)

    # If the trim removed too much (e.g. core toolsets disabled), bail to all.
    if not selected:
        return set(available)
    return selected


def filter_openai_tools(executor, toolset_names: set[str]) -> list[dict]:
    """
    OpenAI tool specs restricted to the given toolsets. A tool belongs to a
    toolset if the toolset's `execute` produces it — we map via the executor's
    toolsets so naming differences (tool name vs toolset name) don't matter.
    """
    allowed_tool_names: set[str] = set()
    for ts in executor.toolsets:
        if not ts.enabled or ts.name not in toolset_names:
            continue
        for tool in ts.get_tools():
            allowed_tool_names.add(tool.name)

    specs = [t.to_openai_spec() for t in executor.all_tool_defs()
             if t.name in allowed_tool_names]
    return specs
