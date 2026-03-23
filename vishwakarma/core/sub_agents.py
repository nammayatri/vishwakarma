"""
Sub-agent architecture for parallel domain investigation.

When the main investigation engine encounters a broad alert that spans multiple
domains (e.g., service degradation affecting DB + Redis + pods + logs), it can
spawn domain-specific sub-agents that investigate in parallel.

Each sub-agent:
- Gets a focused system prompt (domain-specific, no full runbook)
- Gets a limited tool set (only tools relevant to its domain)
- Runs max 3-5 steps (domain-dependent)
- Returns a structured finding summary

The main engine then synthesizes all sub-agent findings into a unified RCA.
"""
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError
from typing import Any, Callable

from vishwakarma.core.llm import LLMConfig, VishwakarmaLLM
from vishwakarma.core.models import ToolOutput, ToolStatus
from vishwakarma.core.tools import ToolExecutor

log = logging.getLogger(__name__)

# Total wall-clock budget for all sub-agents combined.
# Individual sub-agents may finish earlier; this is the hard cap.
SUB_AGENT_TIMEOUT = 60  # seconds


# ── Domain Definitions ────────────────────────────────────────────────────────

SUB_AGENT_DOMAINS: dict[str, dict[str, Any]] = {
    "rds": {
        "description": "Database health -- RDS CPU, connections, slow queries, replication, Performance Insights",
        "tools": ["bash"],
        "prompt_template": (
            "You are a focused sub-agent investigating DATABASE HEALTH for this alert:\n"
            "{alert_context}\n\n"
            "Namespace: {namespace}\n\n"
            "Check RDS instances: CPU utilization, active connections, IOPS, slow queries via Performance Insights.\n"
            "Use `aws cloudwatch get-metric-statistics`, `aws pi get-resource-metrics`, "
            "`aws rds describe-db-instances`.\n\n"
            "You have a maximum of {max_steps} tool calls. Be efficient -- run the most diagnostic commands first.\n\n"
            "When done, return your findings in this exact format:\n"
            "STATUS: healthy | degraded | critical\n"
            "KEY_METRICS: (list the important numbers you found)\n"
            "ROOT_CAUSE: (if you found one, describe it; otherwise say 'not determined from DB metrics alone')\n"
            "EVIDENCE: (the specific data points supporting your conclusion)"
        ),
        "max_steps": 5,
    },
    "redis": {
        "description": "Redis/ElastiCache health -- CPU, memory, evictions, connections, bandwidth",
        "tools": ["bash"],
        "prompt_template": (
            "You are a focused sub-agent investigating REDIS/ELASTICACHE HEALTH for this alert:\n"
            "{alert_context}\n\n"
            "Namespace: {namespace}\n\n"
            "Check ElastiCache clusters: CPU utilization, memory usage, evictions, "
            "current connections, network bandwidth.\n"
            "Use `aws cloudwatch get-metric-statistics` and `aws elasticache describe-cache-clusters`.\n\n"
            "You have a maximum of {max_steps} tool calls. Be efficient.\n\n"
            "When done, return your findings in this exact format:\n"
            "STATUS: healthy | degraded | critical\n"
            "KEY_METRICS: (list the important numbers you found)\n"
            "ROOT_CAUSE: (if you found one, describe it; otherwise say 'not determined from Redis metrics alone')\n"
            "EVIDENCE: (the specific data points supporting your conclusion)"
        ),
        "max_steps": 4,
    },
    "kubernetes": {
        "description": "K8s pod health -- status, restarts, events, OOMKills, HPA scaling",
        "tools": ["bash"],
        "prompt_template": (
            "You are a focused sub-agent investigating KUBERNETES POD HEALTH for this alert:\n"
            "{alert_context}\n\n"
            "Namespace: {namespace}\n\n"
            "Check pods: status, restart counts, OOMKilled events, recent deployments, HPA status.\n"
            "Use `kubectl get pods -n {namespace}`, `kubectl describe pod`, "
            "`kubectl get events -n {namespace} --sort-by=.lastTimestamp`, `kubectl get hpa -n {namespace}`.\n\n"
            "You have a maximum of {max_steps} tool calls. Be efficient.\n\n"
            "When done, return your findings in this exact format:\n"
            "STATUS: healthy | degraded | critical\n"
            "UNHEALTHY_PODS: (list any pods with issues)\n"
            "RECENT_EVENTS: (notable events in the last 30 minutes)\n"
            "ROOT_CAUSE: (if you found one, describe it; otherwise say 'not determined from K8s data alone')\n"
            "EVIDENCE: (the specific data points supporting your conclusion)"
        ),
        "max_steps": 4,
    },
    "logs": {
        "description": "Application logs -- errors, exceptions, 5xx patterns",
        "tools": ["bash", "elasticsearch_search"],
        "prompt_template": (
            "You are a focused sub-agent investigating APPLICATION LOGS for this alert:\n"
            "{alert_context}\n\n"
            "Namespace: {namespace}\n\n"
            "Search for errors, exceptions, and 5xx responses in the last 30 minutes.\n"
            "Use Elasticsearch queries on protocol-lp-logs-* and istio-proxy-* indices.\n"
            "Focus on error frequency, top error messages, and any sudden spikes.\n\n"
            "You have a maximum of {max_steps} tool calls. Be efficient.\n\n"
            "When done, return your findings in this exact format:\n"
            "STATUS: healthy | degraded | critical\n"
            "ERROR_PATTERNS: (top error types and frequencies)\n"
            "TOP_ERRORS: (the most common error messages)\n"
            "ROOT_CAUSE: (if you found one, describe it; otherwise say 'not determined from logs alone')\n"
            "EVIDENCE: (the specific data points supporting your conclusion)"
        ),
        "max_steps": 3,
    },
    "metrics": {
        "description": "Application metrics -- 5xx rates, latency, business metrics",
        "tools": ["bash", "prometheus_query"],
        "prompt_template": (
            "You are a focused sub-agent investigating APPLICATION METRICS for this alert:\n"
            "{alert_context}\n\n"
            "Namespace: {namespace}\n\n"
            "Check Prometheus/VictoriaMetrics for: HTTP 5xx rates by service, latency p99, "
            "request rates, and any business metric anomalies.\n"
            "Use prometheus_query or bash with curl to the VictoriaMetrics endpoint.\n\n"
            "You have a maximum of {max_steps} tool calls. Be efficient.\n\n"
            "When done, return your findings in this exact format:\n"
            "STATUS: healthy | degraded | critical\n"
            "ANOMALOUS_METRICS: (list any metrics with unusual values)\n"
            "TREND: (are things getting worse, stable, or improving?)\n"
            "ROOT_CAUSE: (if you found one, describe it; otherwise say 'not determined from metrics alone')\n"
            "EVIDENCE: (the specific data points supporting your conclusion)"
        ),
        "max_steps": 3,
    },
}


# ── Domain Selection ──────────────────────────────────────────────────────────

# Alert name patterns mapped to relevant investigation domains.
# Order matters -- first match wins. Patterns are checked case-insensitively.
_ALERT_DOMAIN_MAP: list[tuple[list[str], list[str]]] = [
    # RDS-related alerts
    (["rds", "database", "db_", "postgres", "mysql", "aurora"],
     ["rds", "kubernetes", "metrics"]),

    # Redis/ElastiCache alerts
    (["redis", "elasticache", "cache_", "eviction"],
     ["redis", "kubernetes", "metrics"]),

    # ALB/Load balancer 5xx
    (["alb", "elb", "5xx", "target_response", "http_error"],
     ["kubernetes", "logs", "metrics", "rds"]),

    # Drainer/node lifecycle
    (["drainer", "node_", "cordoned", "drain"],
     ["kubernetes", "logs", "rds"]),

    # Business metrics
    (["booking", "ride", "search", "allocation", "business", "revenue"],
     ["metrics", "logs", "kubernetes"]),

    # Pod/container health
    (["pod", "container", "oom", "crashloop", "restart"],
     ["kubernetes", "logs", "metrics"]),

    # Memory/CPU pressure
    (["memory", "cpu_", "resource_pressure"],
     ["kubernetes", "metrics", "rds"]),

    # Network issues
    (["network", "connectivity", "dns", "timeout", "connection_refused"],
     ["kubernetes", "metrics", "logs"]),
]

# Default domains when no specific pattern matches
_DEFAULT_DOMAINS = ["kubernetes", "metrics", "logs"]


def select_domains(alert_name: str, alert_labels: dict | None = None) -> list[str]:
    """
    Select which sub-agent domains to investigate based on alert type.

    Checks alert name against known patterns. Falls back to a general-purpose
    set of domains for unknown alerts.
    """
    name_lower = (alert_name or "").lower()
    labels = alert_labels or {}

    # Also check alertname label if different
    label_alert = labels.get("alertname", "").lower()
    combined = f"{name_lower} {label_alert}"

    for keywords, domains in _ALERT_DOMAIN_MAP:
        if any(kw in combined for kw in keywords):
            log.info(f"Sub-agent domain match for '{alert_name}': {domains} (matched keywords: {[k for k in keywords if k in combined]})")
            return domains

    log.info(f"No specific domain match for '{alert_name}' -- using defaults: {_DEFAULT_DOMAINS}")
    return _DEFAULT_DOMAINS


# ── Sub-Agent Runner ──────────────────────────────────────────────────────────

def _create_sub_agent_llm(llm_config: LLMConfig) -> VishwakarmaLLM:
    """
    Create a lightweight LLM instance for a sub-agent.
    Uses fast_model for speed -- sub-agents don't need the expensive main model.
    """
    sub_cfg = LLMConfig(
        model=llm_config.fast_model or llm_config.model,
        api_key=llm_config.api_key,
        api_base=llm_config.api_base,
        api_version=llm_config.api_version,
        max_tokens=4096,  # sub-agents produce short summaries
        temperature=0.0,
        timeout=55,  # slightly under the total budget
        # Use fast_fallbacks for sub-agents too
        fast_model=llm_config.fast_model,
        fast_fallbacks=llm_config.fast_fallbacks,
    )
    return VishwakarmaLLM(sub_cfg)


def _filter_tools(executor: ToolExecutor, allowed_tool_names: list[str]) -> list[dict]:
    """
    Return OpenAI tool specs filtered to only the tools a sub-agent is allowed to use.
    If a tool name doesn't exist in the executor, it's silently skipped.
    """
    all_tools = executor.all_tool_defs()
    filtered = []
    for tool_def in all_tools:
        # Match by tool name or by toolset prefix (e.g., "bash" matches the bash tool)
        if tool_def.name in allowed_tool_names:
            filtered.append(tool_def.to_openai_spec())
            continue
        # Also match tools whose name starts with an allowed prefix
        for allowed in allowed_tool_names:
            if tool_def.name.startswith(allowed):
                filtered.append(tool_def.to_openai_spec())
                break
    return filtered


def _run_single_sub_agent(
    domain: str,
    domain_config: dict,
    alert_context: str,
    namespace: str,
    llm_config: LLMConfig,
    executor: ToolExecutor,
) -> tuple[str, str]:
    """
    Run a single sub-agent for one domain. Returns (domain, findings_summary).

    This function runs synchronously -- designed to be called from ThreadPoolExecutor.
    Each sub-agent runs a mini agentic loop: LLM call -> tool execution -> repeat.
    """
    from vishwakarma.core.safeguards import LoopGuard

    domain_name = domain.upper()
    max_steps = domain_config["max_steps"]
    allowed_tools = domain_config["tools"]

    # Build the focused prompt
    prompt = domain_config["prompt_template"].format(
        alert_context=alert_context,
        namespace=namespace,
        max_steps=max_steps,
    )

    # Create sub-agent LLM (uses fast_model)
    llm = _create_sub_agent_llm(llm_config)

    # Filter available tools to only what this domain needs
    tools = _filter_tools(executor, allowed_tools)
    if not tools:
        return domain, f"STATUS: unknown\nNo tools available for {domain_name} investigation."

    # Build minimal messages -- no knowledge base, no runbooks, just the task
    messages: list[dict] = [
        {
            "role": "system",
            "content": (
                f"You are a focused investigation sub-agent for the {domain_name} domain. "
                "You are part of a parallel investigation team. "
                "Be concise and efficient -- gather key data quickly and report findings. "
                "Do NOT investigate outside your domain. "
                "Do NOT ask for clarification -- just investigate with what you have."
            ),
        },
        {"role": "user", "content": prompt},
    ]

    guard = LoopGuard()
    all_outputs: list[ToolOutput] = []
    start_time = time.time()

    import json
    for step in range(max_steps):
        elapsed = time.time() - start_time
        if elapsed > SUB_AGENT_TIMEOUT - 5:  # leave 5s buffer
            log.warning(f"Sub-agent [{domain_name}] approaching timeout at step {step + 1} ({elapsed:.0f}s)")
            break

        try:
            response = llm.complete(messages=messages, tools=tools)
        except Exception as e:
            log.warning(f"Sub-agent [{domain_name}] LLM call failed at step {step + 1}: {e}")
            break

        # If LLM returned text with no tool calls, it's done
        if not response.tool_calls:
            elapsed = time.time() - start_time
            log.info(f"Sub-agent [{domain_name}] complete: {step + 1} steps, {elapsed:.1f}s")
            return domain, response.content or f"STATUS: unknown\nSub-agent produced no findings for {domain_name}."

        # Add assistant message with tool calls
        messages.append({
            "role": "assistant",
            "content": response.content or "",
            "tool_calls": [
                {
                    "id": tc["id"],
                    "type": "function",
                    "function": {
                        "name": tc["name"],
                        "arguments": json.dumps(tc["params"]),
                    },
                }
                for tc in response.tool_calls
            ],
        })

        # Execute tool calls (sequentially within sub-agent to keep it simple)
        for tc in response.tool_calls:
            tool_name = tc["name"]
            params = tc["params"]
            call_id = tc["id"]

            # Loop guard check
            allowed, reason = guard.is_allowed(tool_name, params, all_outputs)
            if not allowed:
                messages.append({
                    "role": "tool",
                    "tool_call_id": call_id,
                    "content": reason,
                })
                continue

            try:
                output = executor.execute(tool_name, params)
                output.tool_call_id = call_id
                output.params = params
                all_outputs.append(output)

                if output.status == ToolStatus.ERROR:
                    content = f"Error: {output.error}"
                elif output.status == ToolStatus.NO_DATA:
                    content = "No data returned."
                else:
                    raw_content = str(output.output) if output.output is not None else ""
                    # Truncate very large outputs for sub-agents (they have small context)
                    content = raw_content[:4000] if len(raw_content) > 4000 else raw_content
            except Exception as e:
                content = f"Tool execution error: {e}"

            messages.append({
                "role": "tool",
                "tool_call_id": call_id,
                "content": content,
            })

    # If we exhausted steps without a final text response, force one
    elapsed = time.time() - start_time
    log.info(f"Sub-agent [{domain_name}] max steps reached ({max_steps}), forcing synthesis ({elapsed:.1f}s)")
    messages.append({
        "role": "user",
        "content": (
            "You have used all your investigation steps. "
            "Based on what you found so far, provide your findings NOW in the required format: "
            "STATUS / KEY_METRICS / ROOT_CAUSE / EVIDENCE."
        ),
    })
    try:
        final = llm.complete(messages=messages, tools=None)
        return domain, final.content or f"STATUS: unknown\nSub-agent ran out of steps for {domain_name}."
    except Exception as e:
        log.warning(f"Sub-agent [{domain_name}] final synthesis failed: {e}")
        return domain, f"STATUS: unknown\nSub-agent failed to synthesize {domain_name} findings: {e}"


def run_sub_agents(
    alert_context: str,
    namespace: str,
    domains: list[str],
    llm_config: LLMConfig,
    toolset_manager: ToolExecutor,
    on_progress: Callable[[dict], None] | None = None,
) -> dict[str, str]:
    """
    Run domain-specific sub-agents in parallel.

    Each sub-agent investigates one domain (RDS, Redis, K8s, logs, metrics)
    using a lightweight LLM and limited tool set. All run concurrently.

    Args:
        alert_context: The alert description / investigation question.
        namespace: K8s namespace for the affected service.
        domains: List of domain keys to investigate (e.g., ["rds", "kubernetes", "logs"]).
        llm_config: LLM configuration (sub-agents will use fast_model from this).
        toolset_manager: The shared ToolExecutor for running tools.
        on_progress: Optional callback for progress events.

    Returns:
        Dict mapping domain name to findings summary string.
        Domains that timed out or failed will have partial or error results.
    """
    if not domains:
        return {}

    # Filter to valid domains only
    valid_domains = [d for d in domains if d in SUB_AGENT_DOMAINS]
    if not valid_domains:
        log.warning(f"No valid sub-agent domains in {domains}")
        return {}

    log.info(f"Launching {len(valid_domains)} sub-agents: {valid_domains}")
    if on_progress:
        try:
            on_progress({
                "type": "sub_agents_start",
                "domains": valid_domains,
                "count": len(valid_domains),
            })
        except Exception:
            pass

    results: dict[str, str] = {}
    start_time = time.time()

    with ThreadPoolExecutor(max_workers=len(valid_domains)) as pool:
        futures = {}
        for domain in valid_domains:
            domain_config = SUB_AGENT_DOMAINS[domain]
            future = pool.submit(
                _run_single_sub_agent,
                domain=domain,
                domain_config=domain_config,
                alert_context=alert_context,
                namespace=namespace,
                llm_config=llm_config,
                executor=toolset_manager,
            )
            futures[future] = domain

        # Collect results with overall timeout
        remaining = max(5, SUB_AGENT_TIMEOUT - (time.time() - start_time))
        done_futures = set()

        for future in as_completed(futures, timeout=remaining):
            done_futures.add(future)
            domain = futures[future]
            try:
                domain_name, findings = future.result(timeout=5)
                results[domain_name] = findings
                elapsed = time.time() - start_time
                log.info(f"Sub-agent [{domain_name.upper()}] returned ({elapsed:.1f}s total)")
                if on_progress:
                    try:
                        on_progress({
                            "type": "sub_agent_done",
                            "domain": domain_name,
                            "elapsed": round(elapsed, 1),
                        })
                    except Exception:
                        pass
            except TimeoutError:
                results[domain] = f"STATUS: unknown\nSub-agent timed out for {domain.upper()} (>{SUB_AGENT_TIMEOUT}s)."
                log.warning(f"Sub-agent [{domain.upper()}] timed out")
            except Exception as e:
                results[domain] = f"STATUS: unknown\nSub-agent error for {domain.upper()}: {e}"
                log.warning(f"Sub-agent [{domain.upper()}] failed: {e}")

        # Handle any futures that didn't complete within the timeout
        for future, domain in futures.items():
            if future not in done_futures:
                results[domain] = f"STATUS: unknown\nSub-agent timed out for {domain.upper()} (exceeded {SUB_AGENT_TIMEOUT}s budget)."
                future.cancel()
                log.warning(f"Sub-agent [{domain.upper()}] cancelled (overall timeout)")

    total_elapsed = time.time() - start_time
    log.info(f"All sub-agents complete: {len(results)} results in {total_elapsed:.1f}s")
    if on_progress:
        try:
            on_progress({
                "type": "sub_agents_complete",
                "domains": list(results.keys()),
                "elapsed": round(total_elapsed, 1),
            })
        except Exception:
            pass

    return results


def format_sub_agent_findings(findings: dict[str, str]) -> str:
    """
    Format sub-agent findings into a structured text block for injection
    into the main investigation as a user message.
    """
    if not findings:
        return ""

    parts = ["## Sub-Agent Investigation Findings\n"]
    parts.append(
        "The following domains were investigated in parallel by focused sub-agents. "
        "Synthesize these findings — look for correlations across domains and identify the root cause chain.\n"
    )
    for domain, summary in findings.items():
        parts.append(f"### {domain.upper()}")
        parts.append(summary)
        parts.append("")  # blank line

    parts.append(
        "---\n"
        "Based on the above parallel investigation results, synthesize a unified root cause analysis. "
        "Cross-reference findings across domains — e.g., if DB shows high CPU AND K8s shows pod restarts, "
        "they may be connected. Focus on the PRIMARY root cause, not just symptoms."
    )
    return "\n".join(parts)
