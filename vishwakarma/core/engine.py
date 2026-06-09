"""
Investigation engine — the main agentic loop.

Flow:
  1. Build messages (system prompt + user question + history)
  2. Call LLM with available tools
  3. LLM returns tool calls → execute them → add results → goto 2
  4. LLM returns text (no tool calls) → investigation complete
  5. Return LLMResult with analysis + all tool outputs

Features:
  - Tool approval workflow (pause before executing)
  - Bash allow/deny enforcement
  - Loop detection (safeguards)
  - Context compaction (handle long investigations)
  - Streaming support
  - Multi-turn conversation
"""
import json
import logging
import time
from collections.abc import Generator
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable

from vishwakarma.core.compaction import compact_messages
from vishwakarma.core.llm import VishwakarmaLLM
from vishwakarma.core.models import (
    ApprovalDecision,
    InvestigationMeta,
    LLMResult,
    PendingApproval,
    ToolOutput,
    ToolStatus,
)
from vishwakarma.core.prompt import Section, build_messages, build_system_prompt
from vishwakarma.core.safeguards import LoopGuard
from vishwakarma.core.tools import ToolExecutor

log = logging.getLogger(__name__)

DEFAULT_MAX_STEPS = 40
CHECKPOINT_STEP = 20  # inject a reflection prompt at this step to force RCA-or-continue decision
MAX_TOOL_OUTPUT = 8000  # chars — outputs above this get compressed via fast_model


class InvestigationEngine:
    """
    Main agentic investigation engine.
    One instance per investigation request.
    """

    def __init__(
        self,
        llm: VishwakarmaLLM,
        executor: ToolExecutor,
        max_steps: int = DEFAULT_MAX_STEPS,
        cluster_name: str = "",
        all_toolsets: list | None = None,
        knowledge: str = "",
    ):
        self.llm = llm
        self.executor = executor
        self.max_steps = max_steps
        self.cluster_name = cluster_name
        self.all_toolsets = all_toolsets  # includes disabled ones — shown to LLM
        self.knowledge = knowledge        # site-specific knowledge base (from /data/knowledge.md)

    def _compress_tool_outputs(
        self, executed: dict[str, tuple[ToolOutput, str]]
    ) -> dict[str, tuple[ToolOutput, str]]:
        """
        Batch-compress large tool outputs to save LLM calls.

        - 0 large outputs → no-op
        - 1 large output  → single summarize call (same as before)
        - 2+ large outputs → ONE batched summarize call, parsed back per tool

        Falls back to individual compression if batch parsing fails.
        """
        # Identify which outputs need compression
        large: list[tuple[str, str]] = []  # (call_id, tool_name)
        for cid, (output, content) in executed.items():
            if len(content) > MAX_TOOL_OUTPUT:
                large.append((cid, output.tool_name))

        if not large:
            return executed

        # Single large output — direct summarize (no batching overhead)
        if len(large) == 1:
            cid, tool_name = large[0]
            output, content = executed[cid]
            compressed = self.llm.summarize(
                f"You are helping investigate an infrastructure incident. "
                f"Compress the following {tool_name} output to the 20 most relevant lines. "
                f"Preserve: error messages, stack traces, anomalous values, lines that differ from baseline, "
                f"the LAST 5 lines of the output (errors often appear at the end), and exact timestamps. "
                f"Remove: repetitive healthy/normal entries only.\n\n"
                f"{content}"
            )
            executed[cid] = (output, compressed)
            return executed

        # 2+ large outputs — batch into one LLM call
        log.info(f"Batch-compressing {len(large)} large tool outputs in one LLM call")

        batch_prompt = (
            "You are helping investigate an infrastructure incident. "
            "Compress each of the following tool outputs to the 20 most relevant lines each.\n"
            "Preserve: error messages, stack traces, anomalous values, lines that differ from baseline, "
            "the LAST 5 lines of each output (errors often appear at the end), and exact timestamps.\n"
            "Remove: repetitive healthy/normal entries only.\n\n"
            "Return compressed output for each tool, separated by '### Tool N:' headers "
            "(matching the numbers below).\n\n"
        )
        for i, (cid, tool_name) in enumerate(large, 1):
            _, content = executed[cid]
            # Cap each tool's raw output to avoid blowing up the summarize prompt
            batch_prompt += f"### Tool {i}: {tool_name}\n```\n{content[:12000]}\n```\n\n"

        try:
            batch_response = self.llm.summarize(batch_prompt)
            parsed = self._parse_batch_compression(batch_response, len(large))

            if parsed and len(parsed) == len(large):
                for i, (cid, _) in enumerate(large):
                    output, _ = executed[cid]
                    executed[cid] = (output, parsed[i])
                return executed
            else:
                log.warning(
                    "Batch compression returned %d sections (expected %d) — "
                    "falling back to individual compression",
                    len(parsed) if parsed else 0, len(large),
                )
        except Exception as e:
            log.warning(f"Batch compression failed: {e} — falling back to individual compression")

        # Fallback: compress each individually, in parallel.
        # Sequential compression of N large outputs would block the engine
        # for N × ~8s; parallelizing keeps the step short.
        def _compress_one(cid: str, tool_name: str, content: str) -> tuple[str, str]:
            compressed = self.llm.summarize(
                f"You are helping investigate an infrastructure incident. "
                f"Compress the following {tool_name} output to the 20 most relevant lines. "
                f"Preserve: error messages, stack traces, anomalous values, lines that differ from baseline, "
                f"the LAST 5 lines of the output (errors often appear at the end), and exact timestamps. "
                f"Remove: repetitive healthy/normal entries only.\n\n"
                f"{content}"
            )
            return cid, compressed

        with ThreadPoolExecutor(max_workers=min(len(large), 8)) as ex:
            futures = [
                ex.submit(_compress_one, cid, tool_name, executed[cid][1])
                for cid, tool_name in large
            ]
            for f in as_completed(futures):
                try:
                    cid, compressed = f.result()
                    output, _ = executed[cid]
                    executed[cid] = (output, compressed)
                except Exception as e:
                    log.warning(f"Individual compression failed: {e}")

        return executed

    @staticmethod
    def _parse_batch_compression(response: str, expected: int) -> list[str] | None:
        """
        Parse a batched compression response into individual tool outputs.
        Expects sections delimited by '### Tool N:' headers.
        Returns a list of compressed strings, or None on parse failure.
        """
        import re
        # Split on ### Tool N: headers (with optional tool name after the colon)
        parts = re.split(r"###\s*Tool\s+\d+\s*:[^\n]*\n", response)
        # First element is preamble (before first header) — discard it
        sections = [p.strip() for p in parts[1:] if p.strip()]

        if len(sections) != expected:
            return None

        # Strip code fences if the LLM wrapped its output in them
        cleaned = []
        for s in sections:
            s = re.sub(r"^```[^\n]*\n?", "", s)
            s = re.sub(r"\n?```\s*$", "", s)
            cleaned.append(s.strip())

        return cleaned

    def investigate(
        self,
        question: str,
        history: list[dict] | None = None,
        extra_system_prompt: str | None = None,
        images: list[dict] | None = None,
        files: list[str] | None = None,
        runbooks: list[str] | None = None,
        require_approval: bool = False,
        approval_decisions: list[ApprovalDecision] | None = None,
        bash_always_allow: bool = False,
        bash_always_deny: bool = False,
        sections_off: set[Section] | None = None,
        response_schema: dict | None = None,
        on_progress: Callable[[dict], None] | None = None,
        pre_investigation_findings: str | None = None,
    ) -> LLMResult:
        """
        Run a full investigation and return the result.
        Synchronous — blocks until complete.

        on_progress: optional callback fired at investigation milestones.
          Events: step_start, tool_calls, compaction, checkpoint, hypothesis, complete.

        pre_investigation_findings: optional text from sub-agent parallel investigation.
          Injected as a user message before the main loop so the LLM starts with data.
        """
        start_time = time.time()
        guard = LoopGuard()
        compactions = 0
        all_tool_outputs: list[ToolOutput] = []
        pending_approvals: list[PendingApproval] = []
        tool_call_counter = 0
        checkpoint_injected = False

        # Decisions index for approval workflow
        decisions = {d.tool_call_id: d for d in (approval_decisions or [])}

        # Build bash approval session state
        approved_prefixes: set[str] = set()
        if approval_decisions:
            for d in approval_decisions:
                for prefix in d.remember_prefix:
                    approved_prefixes.add(prefix)

        # Build initial messages
        system = build_system_prompt(
            toolsets=self.executor.toolsets,
            cluster_name=self.cluster_name,
            runbooks=runbooks,
            knowledge=self.knowledge or None,
            extra_prompt=extra_system_prompt,
            sections_off=sections_off,
            all_toolsets=self.all_toolsets,
        )
        messages = build_messages(
            question=question,
            history=history or [],
            system_prompt=system,
            images=images,
            files=files,
        )

        # Inject sub-agent findings before the main loop starts.
        # This gives the main LLM a head start with parallel investigation data.
        if pre_investigation_findings:
            messages.append({
                "role": "user",
                "content": pre_investigation_findings,
            })
            log.info("Injected sub-agent pre-investigation findings into messages")

        tools = self.executor.openai_tools()
        _MAX_LLM_RETRIES = 3

        def _emit(event: dict) -> None:
            """Fire progress callback safely — never let it kill the investigation."""
            if on_progress:
                try:
                    on_progress(event)
                except Exception as e:
                    log.debug(f"Progress callback error (non-fatal): {e}")

        for step in range(self.max_steps):
            log.debug(f"Investigation step {step + 1}/{self.max_steps}")
            _emit({"type": "step_start", "step": step + 1, "max_steps": self.max_steps})

            # Checkpoint: at step 20, force the LLM to decide RCA-or-continue
            if step == CHECKPOINT_STEP and not checkpoint_injected:
                checkpoint_injected = True
                _emit({"type": "checkpoint", "step": step + 1})
                messages.append({
                    "role": "user",
                    "content": (
                        f"**Investigation Checkpoint (step {step}):** "
                        "You have gathered significant data. Pause and evaluate:\n"
                        "1. What is your current best hypothesis for the root cause?\n"
                        "2. Do you have enough evidence to write the final RCA now?\n"
                        "   - If YES → write the complete RCA immediately (Root Cause / Confidence / Evidence Chain / Immediate Fix / Prevention).\n"
                        "   - If NO → state in one sentence exactly what is still missing, then continue investigating.\n\n"
                        "Be decisive. Do not re-run tools you have already run."
                    ),
                })

            # Compact if needed (pass llm for LLM-based compaction)
            messages, did_compact = compact_messages(messages, llm=self.llm)
            if did_compact:
                compactions += 1
                guard.reset()  # Allow retries after compaction — history is gone
                _emit({"type": "compaction", "step": step + 1, "compactions": compactions})

            # Call LLM with retry — retries do NOT consume step budget
            response = None
            for _attempt in range(_MAX_LLM_RETRIES):
                try:
                    response = self.llm.complete(
                        messages=messages,
                        tools=tools if tools else None,
                        response_format=response_schema,
                    )
                    break  # success
                except Exception as llm_err:
                    log.warning("LLM call error on step %d (attempt %d/%d): %s",
                                step, _attempt + 1, _MAX_LLM_RETRIES, llm_err)
                    if _attempt + 1 >= _MAX_LLM_RETRIES:
                        raise  # give up after 3 consecutive failures

            # Log intermediate AI reasoning text (mirrors Holmes "AI: ..." output)
            if response.content and response.content.strip():
                log.info(f"[bold #00FFFF]AI:[/bold #00FFFF] {response.content}")
                _emit({"type": "hypothesis", "step": step + 1, "content": response.content[:200]})

            # No tool calls → LLM is done
            if not response.tool_calls:
                _emit({"type": "complete", "step": step + 1})
                meta = self.llm.build_meta(step + 1, compactions, start_time)
                return LLMResult(
                    answer=response.content,
                    tool_outputs=all_tool_outputs,
                    messages=messages,
                    meta=meta,
                    pending_approvals=pending_approvals,
                )

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

            # Log tool call summary (mirrors Holmes-style progress output)
            n_calls = len(response.tool_calls)
            log.info(f"The AI requested {n_calls} tool call(s).")
            tool_names = [tc["name"] for tc in response.tool_calls]
            _emit({"type": "tool_calls", "step": step + 1, "tools": tool_names, "count": n_calls})

            # Pre-check all tool calls (guards + approvals) then execute in parallel
            to_execute: list[tuple[str, str, dict]] = []  # (call_id, tool_name, params)
            blocked: dict[str, str | None] = {}  # call_id -> reply content (None = pending)

            for tc in response.tool_calls:
                tool_name = tc["name"]
                params = tc["params"]
                call_id = tc["id"]

                # Loop guard (pass tool_outputs for history-based check)
                allowed, reason = guard.is_allowed(tool_name, params, all_tool_outputs)
                if not allowed:
                    blocked[call_id] = reason
                    continue

                # Bash allow/deny
                if tool_name in ("bash", "run_command", "execute_command"):
                    cmd = params.get("command", "")
                    if bash_always_deny:
                        blocked[call_id] = f"Bash command denied by policy: {cmd}"
                        continue
                    if not bash_always_allow:
                        auto_approved = any(cmd.startswith(p) for p in approved_prefixes)
                        if not auto_approved and require_approval:
                            decision = decisions.get(call_id)
                            if decision is None:
                                pending_approvals.append(PendingApproval(
                                    tool_call_id=call_id,
                                    tool_name=tool_name,
                                    description=f"Run bash: {cmd}",
                                    params=params,
                                ))
                                blocked[call_id] = None
                                continue
                            if not decision.approved:
                                blocked[call_id] = f"User denied bash command: {cmd}"
                                continue
                            for prefix in decision.remember_prefix:
                                approved_prefixes.add(prefix)

                # Tool approval for non-bash tools
                elif require_approval:
                    decision = decisions.get(call_id)
                    if decision is None:
                        pending_approvals.append(PendingApproval(
                            tool_call_id=call_id,
                            tool_name=tool_name,
                            description=f"Call {tool_name}",
                            params=params,
                        ))
                        blocked[call_id] = None
                        continue
                    if not decision.approved:
                        blocked[call_id] = f"User denied tool call: {tool_name}"
                        continue

                to_execute.append((call_id, tool_name, params))

            # Execute approved tools in parallel (compression happens AFTER, in batch)
            def _run_tool(call_id: str, tool_name: str, params: dict, tool_idx: int = 0) -> tuple[str, str, ToolOutput, str]:
                # Describe the call briefly (first param value, truncated)
                desc = next(iter(params.values()), "") if params else ""
                desc = str(desc)[:60].replace("\n", " ")
                log.info(f"Running tool #{tool_idx} [bold]{tool_name}[/bold]: {desc}")
                output = self.executor.execute(tool_name, params)
                output.tool_call_id = call_id
                output.params = params  # store for LoopGuard history check
                if output.status == ToolStatus.ERROR:
                    content = f"Error: {output.error}\nCommand: {output.invocation}"
                elif output.status == ToolStatus.NO_DATA:
                    content = f"No data returned. Command: {output.invocation}"
                else:
                    content = str(output.output) if output.output is not None else ""
                return call_id, tool_name, output, content

            executed: dict[str, tuple[ToolOutput, str]] = {}
            if to_execute:
                workers = min(16, len(to_execute))
                with ThreadPoolExecutor(max_workers=workers) as pool:
                    futures = {}
                    for cid, tname, tparams in to_execute:
                        tool_call_counter += 1
                        futures[pool.submit(_run_tool, cid, tname, tparams, tool_call_counter)] = cid
                    for future in as_completed(futures):
                        try:
                            cid, tname, output, content = future.result(timeout=150)
                            executed[cid] = (output, content)
                        except Exception as tool_err:
                            cid = futures[future]
                            err_repr = f"{type(tool_err).__name__}: {tool_err}" if str(tool_err) else type(tool_err).__name__
                            log.error(f"Tool execution failed for {cid}: {err_repr}")
                            blocked[cid] = f"Tool execution error: {err_repr}"

                # Batch-compress large tool outputs (saves LLM calls when multiple are large)
                executed = self._compress_tool_outputs(executed)

            # Append messages in original tool-call order
            for tc in response.tool_calls:
                call_id = tc["id"]
                if call_id in blocked:
                    if blocked[call_id] is not None:
                        messages.append({
                            "role": "tool",
                            "tool_call_id": call_id,
                            "content": blocked[call_id],
                        })
                elif call_id in executed:
                    output, content = executed[call_id]
                    all_tool_outputs.append(output)
                    messages.append({
                        "role": "tool",
                        "tool_call_id": call_id,
                        "content": content,
                    })

            # If we have pending approvals, stop and return them
            if pending_approvals:
                meta = self.llm.build_meta(step + 1, compactions, start_time)
                return LLMResult(
                    answer="",
                    tool_outputs=all_tool_outputs,
                    messages=messages,
                    meta=meta,
                    pending_approvals=pending_approvals,
                )

        # Max steps reached — force a final synthesis call
        log.warning(f"Max steps ({self.max_steps}) reached — forcing final synthesis")
        messages.append({
            "role": "user",
            "content": (
                f"**Max investigation steps ({self.max_steps}) reached.** "
                "You must now write the final RCA based on everything gathered so far. "
                "Do NOT call any more tools. Synthesize your best assessment:\n\n"
                "## Root Cause\n## Confidence\n## Evidence Chain\n## Immediate Fix\n## Prevention\n## Needs More Investigation\n\n"
                "If root cause is still unclear, state what was checked, what was found, and what would be needed to confirm."
            ),
        })
        try:
            final_response = self.llm.complete(messages=messages, tools=None)
            answer = final_response.content or "Investigation incomplete: max steps reached. Review tool outputs above."
        except Exception as e:
            log.error(f"Final synthesis call failed: {e}")
            answer = "Investigation incomplete: max steps reached. Review the tool outputs above for findings."

        meta = self.llm.build_meta(self.max_steps, compactions, start_time)
        return LLMResult(
            answer=answer,
            tool_outputs=all_tool_outputs,
            messages=messages,
            meta=meta,
        )

    def stream_investigate(
        self,
        question: str,
        history: list[dict] | None = None,
        extra_system_prompt: str | None = None,
        images: list[dict] | None = None,
        runbooks: list[str] | None = None,
        require_approval: bool = False,
        approval_decisions: list[ApprovalDecision] | None = None,
        bash_always_allow: bool = False,
        bash_always_deny: bool = False,
        pre_investigation_findings: str | None = None,
        incident_id: str | None = None,
        tool_subset: set[str] | None = None,
    ) -> Generator[dict, None, None]:
        """
        Stream investigation events as they happen.
        Yields dicts suitable for SSE streaming.

        Uses streaming tool overlap: tools are dispatched for execution as
        soon as their arguments are fully received from the LLM stream,
        while the LLM continues generating remaining tool calls or text.
        This reduces wall-clock time when the LLM emits multiple tool calls.

        When incident_id is provided, the conversation is checkpointed to the
        investigations table at each step boundary so a crashed/killed run can
        be resumed from the last step by another worker (durable jobs).
        """
        guard = LoopGuard()
        self.executor.incident_id = incident_id or ""

        def _checkpoint(step_no: int) -> None:
            if not incident_id:
                return
            try:
                from vishwakarma.storage.investigations import checkpoint_investigation
                checkpoint_investigation(incident_id, messages=messages, step=step_no)
            except Exception as cp_err:
                # Checkpointing must never break a live investigation.
                log.warning(f"Checkpoint failed for {incident_id} at step {step_no}: {cp_err}")
        compactions = 0
        tool_call_counter = 0
        all_stream_tool_outputs: list[ToolOutput] = []
        decisions = {d.tool_call_id: d for d in (approval_decisions or [])}

        system = build_system_prompt(
            toolsets=self.executor.toolsets,
            cluster_name=self.cluster_name,
            runbooks=runbooks,
            knowledge=self.knowledge or None,
            extra_prompt=extra_system_prompt,
            all_toolsets=self.all_toolsets,
        )
        messages = build_messages(
            question=question,
            history=history or [],
            system_prompt=system,
            images=images,
        )

        # Inject sub-agent findings before the main loop starts.
        if pre_investigation_findings:
            messages.append({
                "role": "user",
                "content": pre_investigation_findings,
            })
            log.info("Injected sub-agent pre-investigation findings into streaming messages")

        # Curated tool subset (stable across the investigation → prompt-cache
        # friendly). None = all enabled tools.
        if tool_subset:
            from vishwakarma.core.tool_selection import filter_openai_tools
            tools = filter_openai_tools(self.executor, tool_subset)
            if not tools:                       # never strand the agent
                tools = self.executor.openai_tools()
            else:
                log.info(f"Curated tool subset: {len(tools)} tools from {sorted(tool_subset)}")
        else:
            tools = self.executor.openai_tools()
        _MAX_LLM_RETRIES = 3

        checkpoint_injected_stream = False

        # Build approval prefixes once (used across all steps)
        approved_stream_prefixes: set[str] = set()
        if approval_decisions:
            for d in approval_decisions:
                for prefix in d.remember_prefix:
                    approved_stream_prefixes.add(prefix)

        def _check_tool_allowed(
            call_id: str, tool_name: str, params: dict
        ) -> tuple[bool, str | None]:
            """
            Run LoopGuard + bash/approval checks for a single tool call.
            Returns (allowed, block_reason). block_reason is None if allowed.
            """
            allowed, reason = guard.is_allowed(tool_name, params, all_stream_tool_outputs)
            if not allowed:
                return False, reason

            if tool_name in ("bash", "run_command", "execute_command"):
                cmd = params.get("command", "")
                if bash_always_deny:
                    return False, f"Bash command denied by policy: {cmd}"
                if not bash_always_allow:
                    auto_approved = any(cmd.startswith(p) for p in approved_stream_prefixes)
                    if not auto_approved and require_approval:
                        decision = decisions.get(call_id)
                        if decision is None or not decision.approved:
                            return False, f"Bash command requires approval: {cmd}"
                        for prefix in decision.remember_prefix:
                            approved_stream_prefixes.add(prefix)
            elif require_approval:
                decision = decisions.get(call_id)
                if decision is None or not decision.approved:
                    return False, f"Tool call requires approval: {tool_name}"

            return True, None

        def _run_stream_tool(call_id: str, tool_name: str, params: dict):
            output = self.executor.execute(tool_name, params)
            output.tool_call_id = call_id
            output.params = params
            if output.status == ToolStatus.ERROR:
                content = f"Error: {output.error}\nCommand: {output.invocation}"
            elif output.status == ToolStatus.NO_DATA:
                content = f"No data. Command: {output.invocation}"
            else:
                content = str(output.output) if output.output is not None else ""
            return call_id, tool_name, output, content

        # Persistent executor for streaming tool overlap — tools start while
        # the LLM is still generating subsequent tool calls or text.
        overlap_pool = ThreadPoolExecutor(max_workers=16)

        try:
            for step in range(self.max_steps):
                # Checkpoint: at step 20, force the LLM to decide RCA-or-continue
                if step == CHECKPOINT_STEP and not checkpoint_injected_stream:
                    checkpoint_injected_stream = True
                    messages.append({
                        "role": "user",
                        "content": (
                            f"**Investigation Checkpoint (step {step}):** "
                            "You have gathered significant data. Pause and evaluate:\n"
                            "1. What is your current best hypothesis for the root cause?\n"
                            "2. Do you have enough evidence to write the final RCA now?\n"
                            "   - If YES → write the complete RCA immediately (Root Cause / Confidence / Evidence Chain / Immediate Fix / Prevention).\n"
                            "   - If NO → state in one sentence exactly what is still missing, then continue investigating.\n\n"
                            "Be decisive. Do not re-run tools you have already run."
                        ),
                    })

                messages, did_compact = compact_messages(messages, llm=self.llm)
                if did_compact:
                    compactions += 1
                    guard.reset()  # Allow retries after compaction — history is gone
                    yield {"type": "compaction", "step": step}

                # Stream from LLM with retry — retries do NOT consume step budget
                collected_content = ""
                collected_tool_calls = []
                # Overlap state: tools dispatched during streaming
                overlap_futures: dict[str, Any] = {}     # call_id -> Future
                overlap_dispatched: set[str] = set()      # call_ids already submitted
                overlap_blocked: dict[str, str] = {}      # call_id -> block reason (from early check)

                llm_ok = False
                last_llm_err: Exception | None = None
                for _attempt in range(_MAX_LLM_RETRIES):
                    collected_content = ""
                    collected_tool_calls = []
                    overlap_futures = {}
                    overlap_dispatched = set()
                    overlap_blocked = {}
                    try:
                        for chunk in self.llm.stream(messages, tools=tools or None):
                            chunk_type = chunk.get("type")

                            if chunk_type == "tool_call_complete":
                                # A tool call's arguments are fully received — start
                                # execution immediately while stream continues
                                tc_id = chunk["id"]
                                tc_name = chunk["name"]
                                try:
                                    tc_params = json.loads(chunk["arguments"])
                                except Exception:
                                    tc_params = {}

                                allowed, block_reason = _check_tool_allowed(tc_id, tc_name, tc_params)
                                if not allowed:
                                    overlap_blocked[tc_id] = block_reason
                                    log.debug("Overlap: blocked %s (%s) — %s", tc_name, tc_id, block_reason)
                                else:
                                    tool_call_counter += 1
                                    desc = next(iter(tc_params.values()), "") if tc_params else ""
                                    desc = str(desc)[:60].replace("\n", " ")
                                    log.info(f"Overlap: starting tool #{tool_call_counter} [bold]{tc_name}[/bold]: {desc}")
                                    overlap_dispatched.add(tc_id)
                                    future = overlap_pool.submit(_run_stream_tool, tc_id, tc_name, tc_params)
                                    overlap_futures[tc_id] = future
                                    yield {"type": "tool_call_start", "tool": tc_name, "params": tc_params, "overlap": True}
                                # Don't forward tool_call_complete to caller — it's internal
                                continue

                            # Forward other events to the caller
                            yield chunk

                            if chunk_type == "text_delta":
                                collected_content += chunk.get("content", "")
                            elif chunk_type in ("tool_calls", "analysis_done"):
                                collected_content = chunk.get("content", collected_content)
                                collected_tool_calls = chunk.get("tool_calls", [])

                        llm_ok = True
                        break  # success
                    except Exception as e:
                        last_llm_err = e  # Python clears `e` after except; capture it
                        # Cancel any overlap futures from this failed attempt
                        for f in overlap_futures.values():
                            f.cancel()
                        overlap_futures.clear()
                        overlap_dispatched.clear()
                        overlap_blocked.clear()
                        log.warning("LLM stream error on step %d (attempt %d/%d): %s",
                                    step, _attempt + 1, _MAX_LLM_RETRIES, e)
                        yield {"type": "status", "message": f"LLM error (attempt {_attempt + 1}/{_MAX_LLM_RETRIES}): {type(e).__name__}"}

                if not llm_ok:
                    yield {"type": "done", "content": f"Investigation failed after {_MAX_LLM_RETRIES} LLM retries: {last_llm_err}", "messages": messages}
                    return

                if not collected_tool_calls:
                    # No tool calls — LLM is done. Cancel any stray overlap futures
                    # (shouldn't happen, but defensive).
                    for f in overlap_futures.values():
                        f.cancel()
                    messages.append({"role": "assistant", "content": collected_content})
                    _checkpoint(step + 1)
                    yield {"type": "done", "content": collected_content, "messages": messages}
                    return

                # Sanitise tool call arguments — the LLM may truncate JSON if it
                # hits max_output_tokens mid-tool-call.  LiteLLM's Gemini fallback
                # will crash if invalid JSON is stored in conversation history.
                for tc in collected_tool_calls:
                    raw_args = tc.get("function", {}).get("arguments", "{}")
                    try:
                        json.loads(raw_args)  # validate
                    except Exception:
                        log.warning(
                            "Truncated tool-call arguments for %s — replacing with {}",
                            tc.get("function", {}).get("name", "?"),
                        )
                        tc["function"]["arguments"] = "{}"

                # Add assistant turn
                messages.append({
                    "role": "assistant",
                    "content": collected_content,
                    "tool_calls": collected_tool_calls,
                })

                # Dispatch any tool calls that weren't started during overlap
                # (e.g., arguments were incomplete at stream time, or tool_call_complete
                # event wasn't emitted for some provider reason).
                stream_blocked: dict[str, str] = dict(overlap_blocked)  # start with overlap blocks
                n_early = len(overlap_dispatched)  # count before fallback adds more

                for tc in collected_tool_calls:
                    tool_name = tc["function"]["name"]
                    try:
                        params = json.loads(tc["function"]["arguments"])
                    except Exception:
                        params = {}
                    call_id = tc["id"]

                    # Skip if already dispatched or already blocked during overlap
                    if call_id in overlap_dispatched or call_id in overlap_blocked:
                        continue

                    # Fallback: run safety checks and dispatch now
                    allowed, block_reason = _check_tool_allowed(call_id, tool_name, params)
                    if not allowed:
                        stream_blocked[call_id] = block_reason
                        continue

                    tool_call_counter += 1
                    desc = next(iter(params.values()), "") if params else ""
                    desc = str(desc)[:60].replace("\n", " ")
                    log.info(f"Fallback dispatch: tool #{tool_call_counter} [bold]{tool_name}[/bold]: {desc}")
                    overlap_dispatched.add(call_id)
                    future = overlap_pool.submit(_run_stream_tool, call_id, tool_name, params)
                    overlap_futures[call_id] = future
                    yield {"type": "tool_call_start", "tool": tool_name, "params": params}

                n_total = len(collected_tool_calls)
                n_blocked = len(stream_blocked)
                n_dispatched = len(overlap_futures)
                n_fallback = n_dispatched - n_early
                log.info(
                    f"Step {step + 1}: {n_total} tool call(s), "
                    f"{n_dispatched} dispatched ({n_early} via overlap, {n_fallback} fallback), "
                    f"{n_blocked} blocked"
                )

                # Wait for all dispatched tools to complete
                stream_results: dict[str, tuple[str, Any, str]] = {}
                for call_id, future in overlap_futures.items():
                    try:
                        cid, tool_name, output, content = future.result(timeout=150)
                        stream_results[cid] = (tool_name, output, content)
                        all_stream_tool_outputs.append(output)
                        yield {
                            "type": "tool_call_result",
                            "tool": tool_name,
                            "status": output.status,
                            "invocation": output.invocation,
                        }
                    except Exception as tool_err:
                        err_repr = f"{type(tool_err).__name__}: {tool_err}" if str(tool_err) else type(tool_err).__name__
                        log.error(f"Tool execution failed for {call_id}: {err_repr}")
                        stream_blocked[call_id] = f"Tool execution error: {err_repr}"

                # Batch-compress large tool outputs
                if stream_results:
                    as_executed = {cid: (output, content) for cid, (_, output, content) in stream_results.items()}
                    as_executed = self._compress_tool_outputs(as_executed)
                    for cid in stream_results:
                        tname_orig = stream_results[cid][0]
                        output_orig = as_executed[cid][0]
                        content_new = as_executed[cid][1]
                        stream_results[cid] = (tname_orig, output_orig, content_new)

                # Append messages in original order
                for tc in collected_tool_calls:
                    call_id = tc["id"]
                    if call_id in stream_blocked:
                        messages.append({"role": "tool", "tool_call_id": call_id, "content": stream_blocked[call_id]})
                    elif call_id in stream_results:
                        _, output, content = stream_results[call_id]
                        messages.append({"role": "tool", "tool_call_id": call_id, "content": content})

                # Durable-job checkpoint: conversation + step persisted so a
                # crashed run resumes from here instead of restarting.
                _checkpoint(step + 1)

            yield {"type": "max_steps_reached", "steps": self.max_steps, "messages": messages}
        finally:
            overlap_pool.shutdown(wait=False)
