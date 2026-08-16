"""Submodule discovery across archived refs without a worktree."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from repo_archive.git import GitRunner
from repo_archive.results import ComponentResult, ComponentStatus


@dataclass(frozen=True)
class SubmoduleArchiveResult:
    """Manifest state and operation component for submodule discovery."""

    manifest: dict[str, object]
    component: ComponentResult


def inspect_submodules(mirror_path: Path, runner: GitRunner) -> SubmoduleArchiveResult:
    """Find submodule definitions and pinned commits across tree-bearing refs."""
    refs = runner.git("for-each-ref", "--format=%(refname)", cwd=mirror_path)
    if not refs.succeeded:
        return _partial("Could not enumerate refs for submodule inspection.")

    found: dict[tuple[str, str], dict[str, set[str]]] = defaultdict(
        lambda: {"commits": set(), "refs": set()}
    )
    inspected_refs = 0
    for ref in sorted(filter(None, refs.stdout.splitlines())):
        tree = runner.git("cat-file", "-e", f"{ref}^{{tree}}", cwd=mirror_path)
        if not tree.succeeded:
            continue
        inspected_refs += 1
        present = runner.git(
            "ls-tree", "--name-only", ref, "--", ".gitmodules", cwd=mirror_path
        )
        if not present.succeeded:
            return _partial(f"Could not inspect .gitmodules at {ref}.")
        if not present.stdout.strip():
            continue
        configured = runner.git(
            "config",
            "--null",
            "--blob",
            f"{ref}:.gitmodules",
            "--get-regexp",
            r"^submodule\..*\.(path|url)$",
            cwd=mirror_path,
        )
        if not configured.succeeded and configured.returncode != 1:
            return _partial(f"Could not parse .gitmodules at {ref}.")
        try:
            definitions = _parse_git_config(configured.stdout)
        except ValueError as error:
            return _partial(f"Could not parse .gitmodules at {ref}: {error}")
        for path, url in definitions:
            state = found[(path, url)]
            state["refs"].add(ref)
            entry = runner.git("ls-tree", ref, "--", path, cwd=mirror_path)
            if not entry.succeeded:
                return _partial(f"Could not inspect submodule path {path!r} at {ref}.")
            commit = _gitlink_commit(entry.stdout)
            if commit is not None:
                state["commits"].add(commit)

    repositories = [
        {
            "path": path,
            "url": url,
            "commits": sorted(state["commits"]),
            "refs": sorted(state["refs"]),
        }
        for (path, url), state in sorted(found.items())
    ]
    if not repositories:
        return SubmoduleArchiveResult(
            {
                "detected": False,
                "status": "not-applicable",
                "repositories": [],
                "refs_inspected": inspected_refs,
            },
            ComponentResult(
                "submodules", ComponentStatus.COMPLETE, "No submodules detected."
            ),
        )

    definitions = "; ".join(
        f"{item['path']} -> {item['url']} at "
        + (", ".join(item["commits"]) or "no gitlink")
        for item in repositories
    )
    return SubmoduleArchiveResult(
        {
            "detected": True,
            "status": "not-archived",
            "repositories": repositories,
            "refs_inspected": inspected_refs,
        },
        ComponentResult(
            "submodules",
            ComponentStatus.WARNING,
            f"Detected {len(repositories)} submodule definition(s); submodule "
            f"repositories are not recursively archived. {definitions}",
        ),
    )


def _parse_git_config(output: str) -> list[tuple[str, str]]:
    values: dict[str, dict[str, str]] = defaultdict(dict)
    for record in output.split("\0"):
        if not record:
            continue
        key, separator, value = record.partition("\n")
        if not separator:
            raise ValueError("Git returned malformed null-delimited config output")
        section, dot, field = key.rpartition(".")
        field = field.lower()
        if not dot or field not in {"path", "url"}:
            continue
        values[section][field] = value

    definitions: list[tuple[str, str]] = []
    for section, fields in sorted(values.items()):
        path = fields.get("path", "").strip()
        url = fields.get("url", "").strip()
        if not path or not url:
            raise ValueError(f"{section!r} must define both path and url")
        definitions.append((path, url))
    return definitions


def _gitlink_commit(output: str) -> str | None:
    for line in output.splitlines():
        metadata, separator, _path = line.partition("\t")
        fields = metadata.split()
        if separator and len(fields) == 3 and fields[0] == "160000":
            return fields[2]
    return None


def _partial(message: str) -> SubmoduleArchiveResult:
    return SubmoduleArchiveResult(
        {
            "detected": False,
            "status": "partial",
            "repositories": [],
            "reason": message,
        },
        ComponentResult("submodules", ComponentStatus.PARTIAL, message),
    )
