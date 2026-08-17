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


@dataclass(frozen=True)
class ParsedGitConfig:
    """Valid definitions and recoverable problems from Git-native parsing."""

    definitions: tuple[tuple[str, str], ...]
    warnings: tuple[str, ...]
    sections_seen: int


@dataclass(frozen=True)
class TreeListing:
    """Submodule-relevant entries from one unique Git tree."""

    gitmodules_oid: str | None
    gitlinks: dict[str, str]
    warnings: tuple[str, ...]


def inspect_submodules(mirror_path: Path, runner: GitRunner) -> SubmoduleArchiveResult:
    """Find submodule definitions and pinned commits across tree-bearing refs."""
    refs_result = runner.git("for-each-ref", "--format=%(refname)", cwd=mirror_path)
    if not refs_result.succeeded:
        return _inspection_result(
            {}, 0, False, (), ("Could not enumerate refs for submodule inspection.",)
        )
    refs = tuple(sorted(filter(None, refs_result.stdout.splitlines())))
    if not refs:
        return _inspection_result({}, 0, False, (), ())

    resolved = runner.git(
        "cat-file",
        "--batch-check=%(objectname) %(objecttype)",
        cwd=mirror_path,
        input_text="".join(f"{ref}^{{tree}}\n" for ref in refs),
    )
    if not resolved.succeeded:
        return _inspection_result(
            {},
            0,
            False,
            (),
            ("Could not resolve refs to trees for submodule inspection.",),
        )
    tree_refs, resolution_warnings, resolution_failures = _group_refs_by_tree(
        refs, resolved.stdout
    )

    found: dict[tuple[str, str], dict[str, set[str]]] = defaultdict(
        lambda: {"commits": set(), "refs": set()}
    )
    config_cache: dict[str, ParsedGitConfig | None] = {}
    warnings = list(resolution_warnings)
    failures = list(resolution_failures)
    sections_seen = 0
    inspected_refs = 0

    for tree_oid, tree_ref_names in sorted(tree_refs.items()):
        listed = runner.git(
            "-c",
            "core.quotePath=false",
            "ls-tree",
            "-r",
            "-z",
            "--full-tree",
            tree_oid,
            cwd=mirror_path,
        )
        if not listed.succeeded:
            failures.append(
                f"Could not inspect tree {tree_oid} referenced by "
                f"{', '.join(sorted(tree_ref_names))}."
            )
            continue
        inspected_refs += len(tree_ref_names)
        tree = _parse_tree(listed.stdout)
        warnings.extend(tree.warnings)
        if tree.gitmodules_oid is None:
            continue

        if tree.gitmodules_oid not in config_cache:
            configured = runner.git(
                "config",
                "--null",
                "--blob",
                tree.gitmodules_oid,
                "--get-regexp",
                r"^submodule\..*\.(path|url)$",
                cwd=mirror_path,
            )
            if not configured.succeeded and configured.returncode != 1:
                failures.append(
                    f"Could not parse .gitmodules blob {tree.gitmodules_oid}."
                )
                config_cache[tree.gitmodules_oid] = None
            else:
                config_cache[tree.gitmodules_oid] = _parse_git_config(configured.stdout)

        parsed = config_cache[tree.gitmodules_oid]
        if parsed is None:
            continue
        sections_seen += parsed.sections_seen
        warnings.extend(parsed.warnings)
        for path, url in parsed.definitions:
            state = found[(path, url)]
            state["refs"].update(tree_ref_names)
            commit = tree.gitlinks.get(path)
            if commit is None:
                warnings.append(
                    f"Submodule definition {path!r} has no gitlink in tree {tree_oid}."
                )
            else:
                state["commits"].add(commit)

    return _inspection_result(
        found,
        inspected_refs,
        sections_seen > 0,
        tuple(dict.fromkeys(warnings)),
        tuple(dict.fromkeys(failures)),
    )


def _group_refs_by_tree(
    refs: tuple[str, ...], output: str
) -> tuple[dict[str, set[str]], tuple[str, ...], tuple[str, ...]]:
    lines = output.splitlines()
    if len(lines) != len(refs):
        return {}, (), ("Git returned an unexpected number of ref tree results.",)
    trees: dict[str, set[str]] = defaultdict(set)
    warnings: list[str] = []
    for ref, line in zip(refs, lines, strict=True):
        fields = line.split()
        if len(fields) == 2 and fields[1] == "tree":
            trees[fields[0]].add(ref)
        elif not line.endswith(" missing"):
            warnings.append(f"Ref {ref} does not resolve to an inspectable tree.")
    return trees, tuple(warnings), ()


def _parse_tree(output: str) -> TreeListing:
    gitmodules_oid: str | None = None
    gitlinks: dict[str, str] = {}
    warnings: list[str] = []
    for record in output.split("\0"):
        if not record:
            continue
        metadata, separator, path = record.partition("\t")
        fields = metadata.split()
        if not separator or len(fields) != 3:
            warnings.append("Git returned a malformed tree entry.")
            continue
        mode, object_type, oid = fields
        if path == ".gitmodules" and object_type == "blob":
            gitmodules_oid = oid
        elif mode == "160000" and object_type == "commit":
            gitlinks[path] = oid
    return TreeListing(gitmodules_oid, gitlinks, tuple(warnings))


def _parse_git_config(output: str) -> ParsedGitConfig:
    values: dict[str, dict[str, str]] = defaultdict(dict)
    warnings: list[str] = []
    for record in output.split("\0"):
        if not record:
            continue
        key, separator, value = record.partition("\n")
        if not separator:
            warnings.append("Git returned malformed null-delimited config output.")
            continue
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
            warnings.append(
                f"Ignored incomplete {section!r}; both path and url are required."
            )
            continue
        definitions.append((path, url))
    return ParsedGitConfig(tuple(definitions), tuple(warnings), len(values))


def _inspection_result(
    found: dict[tuple[str, str], dict[str, set[str]]],
    refs_inspected: int,
    detected_hint: bool,
    warnings: tuple[str, ...],
    failures: tuple[str, ...],
) -> SubmoduleArchiveResult:
    repositories = [
        {
            "path": path,
            "url": url,
            "commits": sorted(state["commits"]),
            "refs": sorted(state["refs"]),
        }
        for (path, url), state in sorted(found.items())
    ]
    detected: bool | None = bool(repositories) or detected_hint
    if failures and not detected:
        detected = None

    manifest: dict[str, object] = {
        "detected": detected,
        "repositories": repositories,
        "refs_inspected": refs_inspected,
    }
    if warnings:
        manifest["inspection_warnings"] = list(warnings)
    if failures:
        manifest["inspection_failures"] = list(failures)
        manifest["status"] = "partial"
        message = "Submodule inspection was incomplete: " + " ".join(failures)
        if repositories:
            message += " " + _definition_summary(repositories)
        return SubmoduleArchiveResult(
            manifest,
            ComponentResult("submodules", ComponentStatus.PARTIAL, message),
        )

    if repositories:
        manifest["status"] = "not-archived"
        message = (
            f"Detected {len(repositories)} submodule definition(s); submodule "
            "repositories are not recursively archived. "
            + _definition_summary(repositories)
        )
        if warnings:
            message += " " + " ".join(warnings)
        return SubmoduleArchiveResult(
            manifest,
            ComponentResult("submodules", ComponentStatus.WARNING, message),
        )

    if warnings:
        manifest["status"] = "complete-with-warnings"
        message = "Submodule inspection completed with warnings: " + " ".join(warnings)
        return SubmoduleArchiveResult(
            manifest,
            ComponentResult("submodules", ComponentStatus.WARNING, message),
        )

    manifest["status"] = "not-applicable"
    return SubmoduleArchiveResult(
        manifest,
        ComponentResult(
            "submodules", ComponentStatus.COMPLETE, "No submodules detected."
        ),
    )


def _definition_summary(repositories: list[dict[str, object]]) -> str:
    definitions = []
    for item in repositories:
        commits = item["commits"]
        commit_text = (
            ", ".join(str(commit) for commit in commits)
            if isinstance(commits, list)
            else str(commits)
        )
        definitions.append(
            f"{item['path']} -> {item['url']} at {commit_text or 'no gitlink'}"
        )
    return "; ".join(definitions)
