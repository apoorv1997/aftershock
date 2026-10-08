"""Cross-repo consumer reach: GitHub code search as a second consumer tree.

For each finding with a clean query (a removed parameter or a removed object) we search public code,
fetch the matching files into a local tree (`<owner>__<repo>/<path>`, cached), and hand that tree to
the same candidate scan and verification the local path uses. GitHub's code-search API allows 10
requests per minute; pages are throttled to that. File contents use the ordinary API (5,000/hour).
Requires `gh` to be authenticated."""
from __future__ import annotations

import base64
import json
import pathlib
import subprocess
import time

from .contracts import Finding

CACHE = pathlib.Path.home() / ".cache" / "aftershock" / "github"
SEARCH_INTERVAL_S = 6.5          # 10 searches/minute, with margin


def query_for(finding: Finding, package: str) -> str | None:
    """A code-search query that over-selects; the AST scan does the precise matching afterwards."""
    if finding.parameter and "removed" in finding.message.lower():
        head = finding.object_name.split(".")[0]
        head = package if head in ("request", "get", "post", "put", "patch", "delete", "head", "options", "stream") else head
        return f'"{head}" "{finding.parameter}=" {package} language:python'
    if "Public object was removed" in finding.message:
        return f'"{finding.object_name.split(".")[-1]}" {package} language:python'
    return None


def _gh(args: list[str], retries: int = 3) -> str:
    for attempt in range(retries):
        r = subprocess.run(["gh", *args], capture_output=True, text=True)
        if r.returncode == 0:
            return r.stdout
        if "rate limit" in (r.stderr or "").lower() or "403" in (r.stderr or ""):
            time.sleep(60 * (attempt + 1))
            continue
        return ""
    return ""


def search(query: str, limit: int) -> list[dict]:
    """[{repo, path, sha}] — gh pages internally; we throttle between calls, not pages."""
    out = _gh(["search", "code", query, "--limit", str(limit), "--json", "repository,path,sha"])
    time.sleep(SEARCH_INTERVAL_S)
    try:
        rows = json.loads(out) if out else []
    except json.JSONDecodeError:
        rows = []
    return [{"repo": r["repository"]["nameWithOwner"], "path": r["path"], "sha": r.get("sha", "")} for r in rows]


def fetch(repo: str, path: str, sha: str, tree: pathlib.Path) -> pathlib.Path | None:
    """Write the file into `tree/<owner>__<repo>/<path>`; cached by content sha."""
    target = tree / repo.replace("/", "__") / path
    cache = CACHE / (sha or "nosha") / path.replace("/", "__")
    if cache.exists():
        text = cache.read_text(errors="replace")
    else:
        out = _gh(["api", f"repos/{repo}/contents/{path}", "--jq", ".content"])
        if not out:
            return None
        try:
            text = base64.b64decode(out.strip()).decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            return None
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(text)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text)
    return target


def build_consumer_tree(findings: list[Finding], package: str, tree: pathlib.Path,
                        per_query: int = 100, max_queries: int = 12, log=print) -> dict:
    """Search + fetch for every finding with a query. Returns {query: n_results}."""
    tree.mkdir(parents=True, exist_ok=True)
    stats, seen_queries, fetched = {}, set(), set()
    for f in findings:
        q = query_for(f, package)
        if not q or q in seen_queries or len(seen_queries) >= max_queries:
            continue
        seen_queries.add(q)
        rows = search(q, per_query)
        stats[q] = len(rows)
        log(f"  search {q!r}: {len(rows)} files")
        for r in rows:
            key = (r["repo"], r["path"])
            if key in fetched:
                continue
            fetched.add(key)
            fetch(r["repo"], r["path"], r["sha"], tree)
    stats["_files"] = len(fetched)
    return stats
