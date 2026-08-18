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
class TreeResolution:
    """Unique trees resolved from refs plus any inspection diagnostics."""

    refs_by_tree: dict[str, set[str]]
    warnings: tuple[str, ...]
    failures: tuple[str, ...]


@dataclass(frozen=True)
class GitmodulesResolution:
    """Root .gitmodules blobs and trees successfully checked for them."""

    blobs_by_tree: dict[str, str]
    inspected_trees: frozenset[str]
    failures: tuple[str, ...]


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
    resolution = _group_refs_by_tree(refs, resolved.stdout)

    found: dict[tuple[str, str], dict[str, set[str]]] = defaultdict(
        lambda: {"commits": set(), "refs": set()}
    )
    config_cache: dict[str, ParsedGitConfig | None] = {}
    warnings = list(resolution.warnings)
    failures = list(resolution.failures)
    sections_seen = 0
    inspected_refs = 0

    location = GitmodulesResolution({}, frozenset(), ())
    if resolution.refs_by_tree:
        located = runner.git(
            "cat-file",
            "--batch-check=%(objectname) %(objecttype)",
            cwd=mirror_path,
            input_text="".join(
                f"{tree_oid}:.gitmodules\n"
                for tree_oid in sorted(resolution.refs_by_tree)
            ),
        )
        if not located.succeeded:
            failures.append("Could not locate .gitmodules blobs in archived trees.")
        else:
            location = _map_gitmodules_blobs(
                tuple(sorted(resolution.refs_by_tree)), located.stdout
            )
            failures.extend(location.failures)

    fully_inspected_trees = set(location.inspected_trees)
    for gitmodules_oid in sorted(set(location.blobs_by_tree.values())):
        configured = runner.git(
            "config",
            "--null",
            "--blob",
            gitmodules_oid,
            "--get-regexp",
            r"^submodule\..*\.(path|url)$",
            cwd=mirror_path,
        )
        if not configured.succeeded and configured.returncode != 1:
            failures.append(f"Could not parse .gitmodules blob {gitmodules_oid}.")
            config_cache[gitmodules_oid] = None
        else:
            config_cache[gitmodules_oid] = _parse_git_config(configured.stdout)

    for tree_oid, gitmodules_oid in sorted(location.blobs_by_tree.items()):
        tree_ref_names = resolution.refs_by_tree[tree_oid]
        parsed = config_cache[gitmodules_oid]
        if parsed is None:
            fully_inspected_trees.discard(tree_oid)
            continue
        sections_seen += parsed.sections_seen
        warnings.extend(parsed.warnings)
        for path, url in parsed.definitions:
            state = found[(path, url)]
            state["refs"].update(tree_ref_names)
        if not parsed.definitions:
            continue
        listed = runner.git(
            "-c",
            "core.quotePath=false",
            "ls-tree",
            "-z",
            "--full-tree",
            tree_oid,
            "--",
            *(f":(top,literal){path}" for path, _ in parsed.definitions),
            cwd=mirror_path,
        )
        if not listed.succeeded:
            failures.append(
                f"Could not resolve declared submodule gitlinks in tree {tree_oid}."
            )
            fully_inspected_trees.discard(tree_oid)
            continue
        gitlinks, tree_warnings = _parse_gitlinks(listed.stdout)
        warnings.extend(tree_warnings)
        for path, url in parsed.definitions:
            commit = gitlinks.get(path)
            if commit is None:
                warnings.append(
                    f"Submodule definition {path!r} has no gitlink in tree {tree_oid}."
                )
            else:
                found[(path, url)]["commits"].add(commit)

    inspected_refs = sum(
        len(resolution.refs_by_tree[tree_oid]) for tree_oid in fully_inspected_trees
    )

    return _inspection_result(
        found,
        inspected_refs,
        sections_seen > 0,
        tuple(dict.fromkeys(warnings)),
        tuple(dict.fromkeys(failures)),
    )


def _group_refs_by_tree(refs: tuple[str, ...], output: str) -> TreeResolution:
    lines = output.splitlines()
    if len(lines) != len(refs):
        return TreeResolution(
            {}, (), ("Git returned an unexpected number of ref tree results.",)
        )
    trees: dict[str, set[str]] = defaultdict(set)
    warnings: list[str] = []
    for ref, line in zip(refs, lines, strict=True):
        fields = line.split()
        if len(fields) == 2 and fields[1] == "tree":
            trees[fields[0]].add(ref)
        elif not line.endswith(" missing"):
            warnings.append(f"Ref {ref} does not resolve to an inspectable tree.")
    return TreeResolution(trees, tuple(warnings), ())


def _map_gitmodules_blobs(
    tree_oids: tuple[str, ...], output: str
) -> GitmodulesResolution:
    lines = output.splitlines()
    if len(lines) != len(tree_oids):
        return GitmodulesResolution(
            {},
            frozenset(),
            ("Git returned an unexpected number of .gitmodules results.",),
        )
    blobs: dict[str, str] = {}
    inspected_trees: set[str] = set()
    failures: list[str] = []
    for tree_oid, line in zip(tree_oids, lines, strict=True):
        fields = line.split()
        if len(fields) == 2 and fields[1] == "blob":
            blobs[tree_oid] = fields[0]
            inspected_trees.add(tree_oid)
        elif line.endswith(" missing"):
            inspected_trees.add(tree_oid)
        else:
            failures.append(f"Tree {tree_oid} has a non-blob .gitmodules entry.")
    return GitmodulesResolution(blobs, frozenset(inspected_trees), tuple(failures))


def _parse_gitlinks(output: str) -> tuple[dict[str, str], tuple[str, ...]]:
    gitlinks: dict[str, str] = {}
    warnings: list[str] = []
    for record in output.split("\0"):
        if not record:
            continue
        metadata, separator, path = record.partition("\t")
        fields = metadata.split()
        if not separator or len(fields) != 3:
            warnings.append("Git returned a malformed gitlink entry.")
            continue
        mode, object_type, oid = fields
        if mode == "160000" and object_type == "commit":
            gitlinks[path] = oid
    return gitlinks, tuple(warnings)


def _parse_git_config(output: str) -> ParsedGitConfig:
    values: dict[str, dict[str, str]] = defaultdict(dict)
    warnings: list[str] = []
    for record in output.split("\0"):
        if not record:
            continue
        key, separator, value = record.partition("\n")
        if not separator:
            value = ""
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
            message += " " + summarize_submodule_definitions(repositories)
        return SubmoduleArchiveResult(
            manifest,
            ComponentResult("submodules", ComponentStatus.PARTIAL, message),
        )

    if repositories:
        manifest["status"] = "not-archived"
        message = (
            f"Detected {len(repositories)} submodule definition(s); submodule "
            "repositories are not recursively archived. "
            + summarize_submodule_definitions(repositories)
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


def summarize_submodule_definitions(repositories: object) -> str:
    """Return a bounded human-readable summary of manifest definitions."""
    if not isinstance(repositories, list):
        return ""
    definitions = []
    for item in repositories:
        if not isinstance(item, dict):
            continue
        commits = item.get("commits", [])
        if isinstance(commits, list) and len(commits) == 1:
            commit_text = f"at {commits[0]}"
        elif isinstance(commits, list) and commits:
            commit_text = f"({len(commits)} pinned commits; e.g. {commits[0]})"
        else:
            commit_text = "at no gitlink"
        definitions.append(f"{item.get('path')} -> {item.get('url')} {commit_text}")
    return "; ".join(definitions)
