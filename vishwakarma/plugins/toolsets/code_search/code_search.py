"""
Code Search toolset — fast codebase exploration in a single tool call.

Instead of the LLM making 15+ separate bash(grep ...) calls (5-30s each),
this toolset runs multiple grep patterns in parallel and returns combined
results in ONE tool call (~1-2 seconds total).

Supports:
  - code_search: multi-pattern grep + auto-read matching files
  - code_read: read a file with smart truncation
  - code_symbols: find function/class/type definitions
"""
import json
import logging
import os
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from vishwakarma.core.models import ToolOutput, ToolStatus
from vishwakarma.core.tools import Toolset, ToolDef
from vishwakarma.core.toolset_manager import register_toolset

log = logging.getLogger(__name__)

DEFAULT_CODE_PATH = "/data/example-app-src"
MAX_MATCHES_PER_PATTERN = 50
MAX_FILE_LINES = 200
SEARCH_TIMEOUT = 15


def _rg(pattern: str, path: str, extra_args: str = "", timeout: int = SEARCH_TIMEOUT) -> str:
    """Run ripgrep (or grep -r as fallback) and return output."""
    # Try ripgrep first (much faster)
    for cmd in [
        f"rg -n -i --max-count {MAX_MATCHES_PER_PATTERN} {extra_args} '{pattern}' {path}",
        f"grep -rn -i --include='*.hs' --include='*.py' --include='*.yaml' --include='*.json' '{pattern}' {path} | head -{MAX_MATCHES_PER_PATTERN}",
    ]:
        try:
            result = subprocess.run(
                ["bash", "-c", cmd],
                capture_output=True, text=True, timeout=timeout,
            )
            output = (result.stdout or "").strip()
            if output:
                return output
        except (subprocess.TimeoutExpired, Exception):
            continue
    return "(no matches)"


def _read_file(path: str, max_lines: int = MAX_FILE_LINES, context_around: int = 0, line_number: int = 0) -> str:
    """Read a file with smart truncation."""
    try:
        if not os.path.exists(path):
            return f"(file not found: {path})"
        with open(path, "r", errors="replace") as f:
            lines = f.readlines()
        total = len(lines)
        if line_number > 0 and context_around > 0:
            # Read around a specific line
            start = max(0, line_number - context_around - 1)
            end = min(total, line_number + context_around)
            selected = lines[start:end]
            header = f"[{path} lines {start+1}-{end} of {total}]\n"
        elif total > max_lines:
            # Truncate: first half + last half
            half = max_lines // 2
            selected = lines[:half] + [f"\n... ({total - max_lines} lines omitted) ...\n\n"] + lines[-half:]
            header = f"[{path} ({total} lines, showing first/last {half})]\n"
        else:
            selected = lines
            header = f"[{path} ({total} lines)]\n"
        numbered = [f"{i+1:4d} | {line}" for i, line in enumerate(selected)]
        return header + "".join(numbered)
    except Exception as e:
        return f"(error reading {path}: {e})"


@register_toolset
class CodeSearchToolset(Toolset):
    name = "code_search"
    description = (
        "Fast codebase search — grep multiple patterns in parallel and auto-read matching files. "
        "Use this instead of multiple bash(grep ...) calls. Returns combined results in one call."
    )

    def __init__(self, config: dict | None = None):
        super().__init__(config)
        self._code_path = (config or {}).get("code_path", DEFAULT_CODE_PATH)

    _last_pull: float = 0  # class-level: track last git pull time

    def check_prerequisites(self) -> tuple[bool, str]:
        if os.path.isdir(self._code_path):
            return True, ""
        return False, f"Code path not found: {self._code_path}"

    def _ensure_latest(self) -> None:
        """Git pull if last pull was >30 min ago. Non-blocking — runs in background."""
        import time, threading
        now = time.time()
        if now - CodeSearchToolset._last_pull < 1800:  # 30 min cooldown
            return
        CodeSearchToolset._last_pull = now

        def _pull():
            try:
                subprocess.run(
                    ["git", "-C", self._code_path, "pull", "--ff-only"],
                    capture_output=True, timeout=30,
                )
            except Exception:
                pass
        threading.Thread(target=_pull, daemon=True).start()

    def get_tools(self) -> list[ToolDef]:
        return [
            ToolDef(
                name="code_search",
                description=(
                    "Search the codebase for multiple patterns in parallel. "
                    "Returns grep matches + auto-reads the most relevant files. "
                    "Use this for: finding where something is defined, how credentials are used, "
                    "tracing function calls, finding config loading code. "
                    "Much faster than multiple bash(grep ...) calls."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "patterns": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": (
                                "List of grep patterns to search for (regex). "
                                "All patterns run in parallel. Example: "
                                "[\"GupShup\", \"merchant_service_config\", \"show.*configJSON\"]"
                            ),
                        },
                        "file_type": {
                            "type": "string",
                            "description": "File extension filter. Default: all code files. Examples: 'hs', 'py', 'yaml'",
                            "default": "",
                        },
                        "auto_read": {
                            "type": "boolean",
                            "description": "Automatically read files that match all patterns (intersection). Default: true",
                            "default": True,
                        },
                        "path": {
                            "type": "string",
                            "description": "Subdirectory to search in (relative to code root). Default: '' (entire codebase)",
                            "default": "",
                        },
                    },
                    "required": ["patterns"],
                },
            ),
            ToolDef(
                name="code_read",
                description=(
                    "Read a source code file with line numbers. Supports reading around a specific line "
                    "or reading the full file (truncated to 200 lines)."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "File path (relative to code root or absolute)",
                        },
                        "line": {
                            "type": "integer",
                            "description": "Center on this line number (with ±30 lines context). 0 = read from start.",
                            "default": 0,
                        },
                        "max_lines": {
                            "type": "integer",
                            "description": "Max lines to return. Default: 200",
                            "default": 200,
                        },
                    },
                    "required": ["path"],
                },
            ),
            ToolDef(
                name="code_symbols",
                description=(
                    "Find function, class, type, and data definitions in the codebase. "
                    "Searches for definition patterns (def, class, data, type, newtype, module)."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": "Symbol name to find (function, class, type). Example: 'GupShupCfg'",
                        },
                        "file_type": {
                            "type": "string",
                            "description": "File extension. Default: 'hs' (Haskell)",
                            "default": "hs",
                        },
                    },
                    "required": ["name"],
                },
            ),
        ]

    def execute(self, tool_name: str, params: dict) -> ToolOutput:
        self._ensure_latest()  # non-blocking git pull in background
        if tool_name == "code_search":
            return self._code_search(params)
        if tool_name == "code_read":
            return self._code_read(params)
        if tool_name == "code_symbols":
            return self._code_symbols(params)
        return ToolOutput(tool_call_id="", tool_name=tool_name, status=ToolStatus.ERROR,
                          error=f"Unknown tool: {tool_name}")

    def _code_search(self, params: dict) -> ToolOutput:
        patterns = params.get("patterns", [])
        file_type = params.get("file_type", "")
        auto_read = params.get("auto_read", True)
        subpath = params.get("path", "")

        if not patterns:
            return ToolOutput(tool_call_id="", tool_name="code_search",
                              status=ToolStatus.ERROR, error="No patterns provided")

        search_path = os.path.join(self._code_path, subpath) if subpath else self._code_path
        extra_args = f"--type-add 'code:*.{file_type}' --type code" if file_type else ""

        # Run all patterns in parallel
        results: dict[str, str] = {}
        files_per_pattern: dict[str, set[str]] = {}

        with ThreadPoolExecutor(max_workers=min(8, len(patterns))) as pool:
            futures = {}
            for pattern in patterns[:8]:  # max 8 patterns
                futures[pool.submit(_rg, pattern, search_path, extra_args)] = pattern
            for future in as_completed(futures):
                pattern = futures[future]
                try:
                    output = future.result()
                    results[pattern] = output
                    # Extract file paths from grep output
                    files = set()
                    for line in output.split("\n"):
                        if ":" in line and not line.startswith("("):
                            filepath = line.split(":")[0]
                            if os.path.isfile(filepath):
                                files.add(filepath)
                    files_per_pattern[pattern] = files
                except Exception as e:
                    results[pattern] = f"(error: {e})"
                    files_per_pattern[pattern] = set()

        # Auto-read: find files that match ALL patterns (intersection)
        auto_read_content = ""
        if auto_read and len(files_per_pattern) > 1:
            common_files = set.intersection(*files_per_pattern.values()) if files_per_pattern else set()
            if common_files:
                # Read top 3 most relevant files (shortest path = most specific)
                for filepath in sorted(common_files, key=lambda f: len(f))[:3]:
                    auto_read_content += f"\n{'='*60}\n"
                    auto_read_content += _read_file(filepath, max_lines=100)
        elif auto_read and len(files_per_pattern) == 1:
            # Single pattern: read top 3 files
            files = list(files_per_pattern.values())[0]
            for filepath in sorted(files, key=lambda f: len(f))[:3]:
                auto_read_content += f"\n{'='*60}\n"
                auto_read_content += _read_file(filepath, max_lines=100)

        # Build output
        output_parts = []
        for pattern, matches in results.items():
            match_count = len([l for l in matches.split("\n") if l.strip() and not l.startswith("(")])
            output_parts.append(f"## Pattern: {pattern} ({match_count} matches)\n{matches}")

        if auto_read_content:
            output_parts.append(f"\n## Auto-Read Files (matched all patterns){auto_read_content}")

        output = "\n\n".join(output_parts)

        return ToolOutput(
            tool_call_id="", tool_name="code_search",
            status=ToolStatus.SUCCESS, output=output,
            invocation=f"code_search(patterns={patterns})",
        )

    def _code_read(self, params: dict) -> ToolOutput:
        path = params.get("path", "")
        line = params.get("line", 0)
        max_lines = params.get("max_lines", MAX_FILE_LINES)

        # Resolve path
        if not os.path.isabs(path):
            path = os.path.join(self._code_path, path)

        content = _read_file(path, max_lines=max_lines, context_around=30 if line else 0, line_number=line)

        return ToolOutput(
            tool_call_id="", tool_name="code_read",
            status=ToolStatus.SUCCESS, output=content,
            invocation=f"code_read({path}, line={line})",
        )

    def _code_symbols(self, params: dict) -> ToolOutput:
        name = params.get("name", "")
        file_type = params.get("file_type", "hs")

        if not name:
            return ToolOutput(tool_call_id="", tool_name="code_symbols",
                              status=ToolStatus.ERROR, error="No symbol name provided")

        search_path = self._code_path

        # Haskell definition patterns
        if file_type == "hs":
            patterns = [
                f"^(data|type|newtype|class)\\s+{name}",
                f"^{name}\\s*::",
                f"^{name}\\s+",
                f"mk{name}|build{name}|create{name}",
            ]
        else:
            patterns = [
                f"(def|class|function)\\s+{name}",
                f"{name}\\s*=",
            ]

        results = []
        for pattern in patterns:
            output = _rg(pattern, search_path, f"--type-add 'code:*.{file_type}' --type code")
            if output and not output.startswith("(no"):
                results.append(f"## {pattern}\n{output}")

        # Auto-read the most relevant file
        all_files = set()
        for r in results:
            for line in r.split("\n"):
                if ":" in line and not line.startswith("#") and not line.startswith("("):
                    fp = line.split(":")[0]
                    if os.path.isfile(fp):
                        all_files.add(fp)

        file_content = ""
        if all_files:
            # Read the shortest-path file (most specific)
            best_file = sorted(all_files, key=lambda f: len(f))[0]
            file_content = f"\n## Source: {best_file}\n" + _read_file(best_file, max_lines=150)

        output = "\n\n".join(results) + file_content if results else f"No definitions found for '{name}'"

        return ToolOutput(
            tool_call_id="", tool_name="code_symbols",
            status=ToolStatus.SUCCESS, output=output,
            invocation=f"code_symbols({name})",
        )
