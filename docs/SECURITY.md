# Security model & threat considerations

Vishwakarma investigates production systems with an LLM driving real tools.
This document states what it can and cannot do, and the mitigations for the
risks that design creates.

## Posture: read-only by default

- The agent's stance is **READ-ONLY**. The `bash` toolset enforces a
  hardcoded block list (`rm`, `shutdown`, `eval`, interpreters, etc. — not
  overridable by config) plus configurable allow/block prefixes and an
  optional safe-mode whitelist. Destructive SQL (DROP/DELETE/TRUNCATE/ALTER,
  FLUSHALL…) is regex-blocked including inside subshells.
- The `database` toolset accepts SELECT only and sets
  `default_transaction_read_only=on` (PostgreSQL).
- `code_analyst` git operations are restricted to read commands
  (clone/fetch/log/blame/show/diff); push/commit/reset are refused in code.
  Repo access is allow-listed in config; all paths resolve inside the repo
  root (traversal rejected); refs are validated against injection.

## The two write surfaces (both gated)

1. **OpenCode edit sessions** (`code_session` with `mode=edit`):
   - disabled unless `toolsets.code_session.config.allow_edit: true`;
   - run only in an isolated `git worktree` on an `argus/fix-*` branch — the
     main clone and default branch are never touched;
   - read sessions use OpenCode's `plan` agent, which disallows edit tools
     server-side, not just by prompt;
   - every session has a per-message timeout and a wall-clock budget.
2. **Draft PR creation** (lands with the GitHub App): PRs are always draft,
   on a bot branch, with human review + merge on GitHub. Never auto-merge.
   Idempotent per incident (a resumed job won't open a duplicate PR).

## Prompt injection

Alert payloads, log lines, and Slack messages are attacker-influenceable
inputs that flow into the LLM context. Mitigations:

- the agent cannot mutate infrastructure regardless of what the text says
  (tool-layer enforcement above — the model has no write tools to be talked
  into using);
- the edit path is config-gated, worktree-sandboxed, and human-reviewed;
- the Argus noise filter only decides investigate-vs-quiet — a hostile
  message can at worst trigger a read-only investigation;
- secrets are never part of the repo/code context handed to coding agents
  (provider keys are env references, not files).

Residual risk: a crafted input could steer an investigation's *conclusions*.
The ✅/❌ human feedback loop and draft-PR review are the backstops.

## Secrets

- No secrets in the repo or image: LLM keys, Slack tokens, DSNs come from
  env / k8s Secrets (`k8s/argus/secrets.example.yaml` is a template).
- OpenCode provider configs reference keys as `{env:VAR}` — never written
  to disk.
- The console runs **internal-only** (private ingress + SSO/token); it is
  not hardened for public internet exposure.

## Data & PII

Investigations persist tool outputs (logs, DB rows) that may contain
personal data. Deployers should:
- treat the control-plane Postgres as containing production data (same
  access controls as your databases);
- set a retention policy for `incidents`/`investigations`;
- scrub or avoid embedding PII if an embeddings provider is configured.

## RBAC (console)

Two roles: `admin` (edit runbooks/knowledge/settings, approve fixes) and
`reader` (view-only). Interim token auth via `X-VK-Token`; replace with your
SSO at the same dependency point. All write endpoints require admin; runbook
edits and fix approvals are attributable via the author field.

## Reporting

Security issues: open a private report to the maintainers (see repository
contacts). Please do not file public issues for exploitable problems.
