"""
Learnings manager — persistent, categorized knowledge built from past incidents.
Stored at /data/learnings/{category}.md on PVC.
"""
import os
import re
import logging
import time
from datetime import datetime

log = logging.getLogger(__name__)

# In-memory cache: category -> (timestamp, content)
_learnings_cache: dict[str, tuple[float, str]] = {}
_CACHE_TTL = 300  # 5 minutes

# Default categories created on first run
_DEFAULT_CATEGORIES = ["rds", "redis", "drainer", "kubernetes", "networking", "general"]

# Alert label/name → category keyword mapping
_ALERT_CATEGORY_KEYWORDS: dict[str, list[str]] = {
    "rds": ["rds", "aurora", "database", "db", "cpu", "sql", "replication", "connection", "query", "performance"],
    "redis": ["redis", "cache", "elasticache", "evict", "memory"],
    "drainer": ["drainer", "drain"],
    "kubernetes": ["pod", "deploy", "node", "oom", "crash", "restart", "evict", "pvc", "k8s", "container", "allocator", "alloc", "producer", "drainer"],
    "networking": ["alb", "5xx", "ingress", "network", "dns", "istio", "latency", "timeout", "cmrl", "cris", "pt", "transit"],
}

_VALID_CATEGORY_RE = re.compile(r'^[a-z0-9][a-z0-9_-]{0,63}$')


def _valid_category_name(name: str) -> bool:
    return bool(_VALID_CATEGORY_RE.match(name))


class LearningsManager:
    """
    Manages persistent, categorized learnings derived from past incidents.
    Each category is stored as a Markdown file under `path/`.
    Categories are dynamic — any valid name can be created at runtime.
    """

    def __init__(self, path: str | None = None):
        # DB-backed now (was /data/learnings/*.md on the PVC). `path` kept for
        # signature compatibility but unused. Seeds default categories in the DB.
        self._init_defaults()

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _init_defaults(self) -> None:
        """Create default categories in the DB if they don't exist yet."""
        from vishwakarma.storage import site_content
        for cat in _DEFAULT_CATEGORIES:
            try:
                if site_content.get_learning(cat) is None:
                    site_content.set_learning(cat, f"# {cat.capitalize()} Learnings\n")
            except Exception as e:
                log.debug(f"learnings default init skipped for {cat}: {e}")

    def _all_categories(self) -> list[str]:
        from vishwakarma.storage import site_content
        try:
            cats = [c["category"] for c in site_content.list_learnings()]
            return cats or list(_DEFAULT_CATEGORIES)
        except Exception:
            return list(_DEFAULT_CATEGORIES)

    # ── Public API ────────────────────────────────────────────────────────────

    def create(self, category: str) -> None:
        """Create a new category. Raises ValueError for invalid names."""
        cat = category.lower().strip()
        if not _valid_category_name(cat):
            raise ValueError(
                f"Invalid category name '{cat}'. "
                "Use lowercase letters, digits, hyphens, or underscores (max 64 chars)."
            )
        from vishwakarma.storage import site_content
        if site_content.get_learning(cat) is None:
            site_content.set_learning(cat, f"# {cat.capitalize()} Learnings\n")

    def get(self, category: str) -> str:
        """Return the full content of a category (cached with TTL)."""
        cat = category.lower().strip()
        now = time.time()
        if cat in _learnings_cache:
            ts, content = _learnings_cache[cat]
            if now - ts < _CACHE_TTL:
                return content
        from vishwakarma.storage import site_content
        content = site_content.get_learning(cat)
        if content is None:
            return f"# {cat.capitalize()} Learnings\n"
        _learnings_cache[cat] = (now, content)
        return content

    def set(self, category: str, content: str) -> None:
        """Overwrite the content of a category."""
        cat = category.lower().strip()
        from vishwakarma.storage import site_content
        site_content.set_learning(cat, content)
        _learnings_cache.pop(cat, None)

    def append(self, category: str, fact: str) -> None:
        """Append a bullet-point fact to a category."""
        cat = category.lower().strip()
        existing = self.get(cat)
        if existing and not existing.endswith("\n"):
            existing += "\n"
        self.set(cat, existing + f"- {fact.strip()}\n")

    def forget(self, category: str, keyword: str) -> int:
        """Remove all lines containing `keyword` (case-insensitive). Returns count removed."""
        cat = category.lower().strip()
        lines = self.get(cat).splitlines(keepends=True)
        kw = keyword.lower()
        kept = [l for l in lines if kw not in l.lower()]
        removed = len(lines) - len(kept)
        if removed:
            self.set(cat, "".join(kept))
        return removed

    def list_categories(self) -> list[dict]:
        """[{category, fact_count, size_bytes, last_modified}] from the DB."""
        from vishwakarma.storage import site_content
        result = []
        for c in site_content.list_learnings():
            result.append({
                "category": c["category"],
                "fact_count": c["fact_count"],
                "size_bytes": c["size_bytes"],
                "last_modified": c.get("last_modified"),
            })
        return result

    def compact(self, category: str, llm_summarize_fn) -> bool:
        """
        If a category has grown large (>30 facts or >4KB), use the LLM to
        consolidate it into the best, non-redundant set of facts.
        Returns True if compaction happened.
        """
        cat = category.lower().strip()
        content = self.get(cat)
        facts = [l.strip() for l in content.splitlines() if l.strip().startswith("- ")]

        if len(facts) <= 50 and len(content) <= 5000:
            return False

        log.info(f"Compacting learnings category '{cat}' ({len(facts)} facts, {len(content)} bytes)")

        prompt = (
            f"The following is a list of learned facts for incident category '{cat}'.\n"
            f"Consolidate them into the best, most actionable set of facts:\n"
            f"- Merge duplicates and near-duplicates into one\n"
            f"- Keep facts specific (service names, error types, fix patterns)\n"
            f"- Remove vague or generic facts\n"
            f"- Output ONLY bullet points starting with '- '\n\n"
            f"{content}"
        )
        try:
            compacted = llm_summarize_fn(prompt)
            # Rebuild file: header + compacted facts
            header = f"# {cat.capitalize()} Learnings\n"
            self.set(cat, header + compacted.strip() + "\n")
            log.info(f"Compacted '{cat}' learnings successfully")
            return True
        except Exception as e:
            log.warning(f"Learnings compaction failed for '{cat}': {e}")
            return False

    def for_alert(self, alert_name: str) -> str:
        """
        Map an alert name to relevant categories using keyword matching,
        then return only the bullet-point fact lines from those categories,
        prefixed with a section header.

        Returns an empty string if no relevant facts are found.
        """
        alert_lower = alert_name.lower()
        matched_categories: list[str] = []

        for cat, keywords in _ALERT_CATEGORY_KEYWORDS.items():
            if any(kw in alert_lower for kw in keywords):
                matched_categories.append(cat)

        # Always include general
        if "general" not in matched_categories:
            matched_categories.append("general")

        parts: list[str] = []
        for cat in matched_categories:
            content = self.get(cat)
            if not content:
                continue

            facts = [l.rstrip() for l in content.splitlines() if l.strip().startswith("- ")]
            if facts:
                header = f"## Learned Facts ({cat})"
                parts.append(header + "\n" + "\n".join(facts))

        return "\n\n".join(parts)
