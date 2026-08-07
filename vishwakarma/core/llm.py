"""
LLM abstraction layer — wraps LiteLLM for multi-provider support.

Supports:
  - OpenAI (GPT-4, GPT-4o, etc.)
  - Anthropic (Claude)
  - Azure OpenAI
  - Acme AI (OpenAI-compatible custom endpoint)
  - Any OpenAI-compatible provider via api_base
"""
import json
import logging
import os
import time
from typing import Any, Generator

import litellm
from litellm import completion, completion_cost
from pydantic import BaseModel

from vishwakarma.core.models import InvestigationMeta

log = logging.getLogger(__name__)

# Suppress LiteLLM's verbose logging
litellm.suppress_debug_info = True
os.environ.setdefault("LITELLM_LOG", "ERROR")

# Cap LiteLLM's internal retry backoff — don't let it wait 60s between retries
litellm.num_retries = 2              # max 2 retries per call (not infinite)
litellm.request_timeout = 30         # 30s per attempt


class LLMConfig(BaseModel):
    model: str
    fast_model: str | None = None  # cheap/fast model for summarization + compaction
    # Fallback chains — tried in order, first success wins
    fast_fallbacks: list[str] = []  # e.g. ["openai/kimi-latest", "openai/glm-flash-experimental"]
    model_fallbacks: list[str] = []  # e.g. ["openai/glm-latest"]
    api_key: str | None = None
    api_keys: list[str] = []   # optional pool — round-robined, 429-aware
    api_base: str | None = None
    api_version: str | None = None
    max_tokens: int = 65536
    temperature: float = 0.0
    timeout: int = 300


class LLMResponse(BaseModel):
    content: str
    tool_calls: list[dict] = []
    raw: dict = {}
    cost: float = 0.0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0


class VishwakarmaLLM:
    """
    Main LLM client for Vishwakarma.
    Wraps LiteLLM to support all providers with one interface.
    """

    def __init__(self, cfg: LLMConfig):
        self.cfg = cfg
        self._total_cost = 0.0
        self._total_prompt_tokens = 0
        self._total_completion_tokens = 0

    def _pick_key(self) -> str | None:
        """Next key from the pool if configured, else the single configured key."""
        try:
            from vishwakarma.core.keypool import get_pool
            pool = get_pool()
            if pool and pool.size:
                return pool.get()
        except Exception:
            pass
        return self.cfg.api_key

    @staticmethod
    def _penalize_key(key: str | None) -> None:
        if not key:
            return
        try:
            from vishwakarma.core.keypool import get_pool
            pool = get_pool()
            if pool:
                pool.penalize(key)
        except Exception:
            pass

    def complete(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        response_format: dict | None = None,
        stream: bool = False,
    ) -> LLMResponse:
        """
        Call the LLM with messages and optional tools.
        Returns structured LLMResponse.
        """
        kwargs: dict[str, Any] = {
            "model": self.cfg.model,
            "messages": messages,
            "temperature": self.cfg.temperature,
            "timeout": self.cfg.timeout,
        }

        _key = self._pick_key()
        if _key:
            kwargs["api_key"] = _key
        if self.cfg.api_base:
            kwargs["api_base"] = self.cfg.api_base
        if self.cfg.api_version:
            kwargs["api_version"] = self.cfg.api_version
        if self.cfg.max_tokens:
            kwargs["max_tokens"] = self.cfg.max_tokens
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"
        if response_format:
            kwargs["response_format"] = response_format

        # Env var overrides for custom providers (e.g. Acme)
        max_content = os.environ.get("OVERRIDE_MAX_CONTENT_SIZE")
        max_output = os.environ.get("OVERRIDE_MAX_OUTPUT_TOKEN")
        if max_content:
            litellm.max_input_tokens = int(max_content)  # type: ignore
        if max_output:
            kwargs["max_tokens"] = int(max_output)

        # Use fallback chain: try main model first, then fallbacks
        chain = self._get_main_chain()
        if len(chain) > 1:
            try:
                response = self._call_with_fallback(
                    models=chain,
                    messages=messages,
                    max_tokens=kwargs.get("max_tokens", self.cfg.max_tokens),
                    temperature=self.cfg.temperature,
                    timeout=90,  # 90s per model before trying next
                    tools=tools,
                    total_budget=self.cfg.timeout,  # total budget across all fallbacks
                )
                return self._parse_response(response)
            except Exception as e:
                log.error(f"LLM call failed (all fallbacks exhausted): {e}", exc_info=True)
                raise
        else:
            try:
                response = completion(**kwargs)
                return self._parse_response(response)
            except litellm.exceptions.RateLimitError:
                raise
            except litellm.exceptions.AuthenticationError:
                raise
            except Exception as e:
                log.error(f"LLM call failed: {e}", exc_info=True)
                raise

    def stream(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
    ) -> Generator[dict, None, None]:
        """
        Stream LLM response events.
        Yields dicts with type: text_delta | tool_call_complete | tool_calls | analysis_done

        tool_call_complete is emitted as soon as an individual tool call's
        arguments form valid JSON, allowing the caller to start execution
        before the full LLM response is finished (streaming tool overlap).
        """
        # Apply env var overrides (same as complete())
        max_content = os.environ.get("OVERRIDE_MAX_CONTENT_SIZE")
        max_output = os.environ.get("OVERRIDE_MAX_OUTPUT_TOKEN")
        if max_content:
            litellm.max_input_tokens = int(max_content)  # type: ignore

        # Try main + model_fallbacks for stream initialization.
        # Once chunks flow we commit to that model; mid-stream failures bubble up
        # and get retried by the engine's outer retry loop.
        chain = self._get_main_chain()
        last_err: Exception | None = None
        response = None
        for i, model in enumerate(chain):
            kwargs: dict[str, Any] = {
                "model": model,
                "messages": messages,
                "temperature": self.cfg.temperature,
                "timeout": self.cfg.timeout,
                "stream": True,
                "num_retries": 0,
            }
            _key = self._pick_key()
            if _key:
                kwargs["api_key"] = _key
            if self.cfg.api_base:
                kwargs["api_base"] = self.cfg.api_base
            if self.cfg.api_version:
                kwargs["api_version"] = self.cfg.api_version
            if self.cfg.max_tokens:
                kwargs["max_tokens"] = self.cfg.max_tokens
            if max_output:
                kwargs["max_tokens"] = int(max_output)
            if tools:
                kwargs["tools"] = tools
                kwargs["tool_choice"] = "auto"
            # Same reasoning policy as complete(): thinking off by default —
            # it adds 10-30s/step on GLM-class models and reduces parallel
            # tool-call batching. VK_THINK_ON_TOOL_STEPS=true re-enables.
            _think = os.environ.get("VK_THINK_ON_TOOL_STEPS", "").lower() in ("1", "true", "yes")
            if not tools or not _think:
                kwargs["extra_body"] = {
                    "chat_template_kwargs": {"enable_thinking": False, "thinking": False}
                }

            try:
                response = completion(**kwargs)
                if i > 0:
                    log.info(f"Stream fallback to {model} succeeded (primary failed)")
                break
            except Exception as e:
                last_err = e
                log.warning(
                    f"Stream init failed on {model} ({type(e).__name__}: {str(e)[:80]}); "
                    f"{'falling back to next' if i < len(chain) - 1 else 'no more fallbacks'}"
                )

        if response is None:
            log.error(f"LLM stream call failed (all fallbacks exhausted): {last_err}", exc_info=True)
            raise last_err  # type: ignore

        collected_content = ""
        collected_tool_calls: dict[int, dict] = {}
        emitted_complete: set[int] = set()  # indices already yielded as tool_call_complete

        # Wall-clock guard for the stream consumption. litellm's `timeout` only
        # covers initial connection — once chunks start flowing, a gateway that
        # stalls mid-stream would hang the loop forever. Cap total stream time
        # at cfg.timeout (default 300s).
        stream_deadline = time.time() + max(60, self.cfg.timeout)
        for chunk in response:
            if time.time() > stream_deadline:
                raise TimeoutError(
                    f"Stream consumption exceeded {self.cfg.timeout}s — upstream stalled mid-response"
                )
            delta = chunk.choices[0].delta if chunk.choices else None
            if not delta:
                continue

            # Text delta
            if delta.content:
                collected_content += delta.content
                yield {"type": "text_delta", "content": delta.content}

            # Tool call delta
            if delta.tool_calls:
                for tc in delta.tool_calls:
                    idx = tc.index
                    if idx not in collected_tool_calls:
                        collected_tool_calls[idx] = {
                            "id": tc.id or "",
                            "type": "function",
                            "function": {"name": "", "arguments": ""},
                        }
                    if tc.function:
                        if tc.function.name:
                            collected_tool_calls[idx]["function"]["name"] += tc.function.name
                        if tc.function.arguments:
                            collected_tool_calls[idx]["function"]["arguments"] += tc.function.arguments

                    # Early completion detection: if this tool call has a
                    # name, an id, and its arguments parse as valid JSON,
                    # emit it immediately so the engine can start execution
                    # while the LLM is still generating.
                    if idx not in emitted_complete:
                        entry = collected_tool_calls[idx]
                        fn = entry["function"]
                        if fn["name"] and entry["id"] and fn["arguments"]:
                            try:
                                json.loads(fn["arguments"])
                                # Valid JSON — this tool call is complete
                                emitted_complete.add(idx)
                                yield {
                                    "type": "tool_call_complete",
                                    "id": entry["id"],
                                    "name": fn["name"],
                                    "arguments": fn["arguments"],
                                    "index": idx,
                                }
                            except (json.JSONDecodeError, ValueError):
                                pass  # arguments still incomplete, keep accumulating

        # Emit the final batch event (backward compatibility + catch-all)
        tool_calls = list(collected_tool_calls.values())
        if tool_calls:
            yield {"type": "tool_calls", "tool_calls": tool_calls, "content": collected_content}
        else:
            yield {"type": "analysis_done", "content": collected_content}

    def _parse_response(self, response) -> LLMResponse:
        choice = response.choices[0]
        message = choice.message

        content = message.content or ""
        tool_calls = []

        if message.tool_calls:
            for tc in message.tool_calls:
                try:
                    args = json.loads(tc.function.arguments) if tc.function.arguments else {}
                except json.JSONDecodeError:
                    args = {"raw": tc.function.arguments}
                tool_calls.append({
                    "id": tc.id,
                    "name": tc.function.name,
                    "params": args,
                })

        # Cost tracking
        cost = 0.0
        prompt_tokens = 0
        completion_tokens = 0
        cached_tokens = 0

        if hasattr(response, "usage") and response.usage:
            usage = response.usage
            prompt_tokens = getattr(usage, "prompt_tokens", 0) or 0
            completion_tokens = getattr(usage, "completion_tokens", 0) or 0
            cached_tokens = getattr(
                getattr(usage, "prompt_tokens_details", None),
                "cached_tokens", 0
            ) or 0
            try:
                cost = completion_cost(completion_response=response)
            except Exception:
                cost = 0.0

        self._total_cost += cost
        self._total_prompt_tokens += prompt_tokens
        self._total_completion_tokens += completion_tokens

        return LLMResponse(
            content=content,
            tool_calls=tool_calls,
            cost=cost,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cached_tokens=cached_tokens,
        )

    def _call_with_fallback(
        self,
        models: list[str],
        messages: list[dict],
        max_tokens: int = 4096,
        temperature: float = 0.0,
        timeout: int = 30,
        tools: list | None = None,
        total_budget: int = 60,
    ):
        """Try models in order, return first successful response.

        Each model gets `timeout` seconds. Total time across all models
        capped at `total_budget` seconds. Raises the last exception if all fail.
        """
        start = time.time()
        last_error = None
        # Index-based walk so a rate-limit wait can retry the SAME model
        # (the old for-loop's `continue` silently advanced to the next one,
        # wasting the sleep).
        i = 0
        retried_same: set[int] = set()
        while i < len(models):
            model = models[i]
            # Check total time budget
            elapsed = time.time() - start
            if elapsed > total_budget:
                log.warning(f"Fallback chain exhausted time budget ({total_budget}s)")
                break
            remaining = min(timeout, int(total_budget - elapsed))
            if remaining < 5:
                break  # not enough time for another attempt

            try:
                kwargs: dict[str, Any] = {
                    "model": model,
                    "messages": messages,
                    "temperature": temperature,
                    "max_tokens": max_tokens,
                    "timeout": remaining,
                    "num_retries": 0,  # no per-model retry — fall through to next model fast
                }
                _key = self._pick_key()
                if _key:
                    kwargs["api_key"] = _key
                if self.cfg.api_base:
                    kwargs["api_base"] = self.cfg.api_base
                if tools:
                    kwargs["tools"] = tools
                    kwargs["tool_choice"] = "auto"
                # Disable reasoning by default — on GLM-class models thinking
                # adds 10-30s per step and empirically yields FEWER parallel
                # tool calls per step (deeper progress without it). Set
                # VK_THINK_ON_TOOL_STEPS=true to re-enable for tool steps.
                # Works for GLM-5 (enable_thinking) and Kimi-K2.5 (thinking).
                _think = os.environ.get("VK_THINK_ON_TOOL_STEPS", "").lower() in ("1", "true", "yes")
                if not tools or not _think:
                    kwargs["extra_body"] = {
                        "chat_template_kwargs": {"enable_thinking": False, "thinking": False}
                    }
                # Apply env var overrides (custom providers)
                max_output = os.environ.get("OVERRIDE_MAX_OUTPUT_TOKEN")
                if max_output:
                    kwargs["max_tokens"] = int(max_output)
                response = completion(**kwargs)
                if i > 0:
                    log.info(f"Fallback to {model} succeeded (primary failed)")
                return response
            except Exception as e:
                last_error = e
                error_type = type(e).__name__
                # Rate limit: extract reset time and wait briefly before next attempt
                if "RateLimit" in error_type or "429" in str(e):
                    self._penalize_key(_key)   # bench this key in the pool
                    import re
                    reset_match = re.search(r'resets at: (\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})', str(e))
                    if reset_match:
                        from datetime import datetime, timezone
                        try:
                            reset_time = datetime.strptime(reset_match.group(1), "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                            wait_secs = (reset_time - datetime.now(timezone.utc)).total_seconds()
                            if 0 < wait_secs <= 10 and i not in retried_same:
                                log.info(f"Rate limit resets in {wait_secs:.0f}s — waiting")
                                time.sleep(min(wait_secs + 0.5, 10))
                                retried_same.add(i)
                                continue  # retry the SAME model (i unchanged)
                        except Exception:
                            pass
                log.warning(f"Model {model} failed ({error_type}: {str(e)[:80]}), "
                           f"{'trying next' if i < len(models) - 1 else 'no more fallbacks'} "
                           f"[{time.time() - start:.1f}s elapsed]")
                i += 1
        raise last_error  # type: ignore

    def _get_fast_chain(self) -> list[str]:
        """Get ordered list of fast models to try."""
        primary = self.cfg.fast_model or self.cfg.model
        fallbacks = self.cfg.fast_fallbacks or []
        chain = [primary] + [f for f in fallbacks if f != primary]
        # Always include main model as last resort
        if self.cfg.model not in chain:
            chain.append(self.cfg.model)
        return chain

    def _get_main_chain(self) -> list[str]:
        """Get ordered list of main models to try."""
        chain = [self.cfg.model] + (self.cfg.model_fallbacks or [])
        return chain

    def summarize(self, prompt: str) -> str:
        """
        Fast, cheap LLM call to compress a long tool output.
        Uses fast model chain with fallbacks.
        """
        try:
            response = self._call_with_fallback(
                models=self._get_fast_chain(),
                messages=[{"role": "user", "content": prompt}],
                max_tokens=4096,
                timeout=30,  # fast calls should be fast — 30s timeout per model
            )
            msg = response.choices[0].message
            content = msg.content or ""
            # Reasoning models may put content in reasoning_content
            if not content.strip():
                content = getattr(msg, "reasoning_content", "") or ""
            # Strip reasoning preamble if present
            import re
            content = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL).strip()
            return content or prompt[:2000]
        except Exception as e:
            log.warning(f"All summarization models failed: {e} — truncating instead")
            return prompt[:4000] + "\n... [truncated]"

    def build_meta(self, steps: int, compactions: int, start_time: float) -> InvestigationMeta:
        return InvestigationMeta(
            model=self.cfg.model,
            total_cost=round(self._total_cost, 6),
            prompt_tokens=self._total_prompt_tokens,
            completion_tokens=self._total_completion_tokens,
            steps_taken=steps,
            compactions=compactions,
            duration_seconds=round(time.time() - start_time, 2),
        )
