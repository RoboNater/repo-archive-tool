import re
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).parents[2]
WORKFLOW_ROOT = REPOSITORY_ROOT / ".github" / "workflows"
USES_LINE = re.compile(
    r"^\s*(?:-\s*)?uses:\s*(?P<target>\S+)(?:\s+#\s*(?P<comment>.*))?$"
)
FULL_COMMIT_SHA = re.compile(r"[0-9a-f]{40}")
RELEASE_TAG = re.compile(r"v\d+(?:\.\d+)+(?:[-+][0-9A-Za-z.-]+)?")


def test_external_workflow_actions_are_immutably_pinned() -> None:
    violations: list[str] = []

    workflows = sorted(WORKFLOW_ROOT.rglob("*.yml")) + sorted(
        WORKFLOW_ROOT.rglob("*.yaml")
    )
    for workflow in workflows:
        for line_number, line in enumerate(
            workflow.read_text(encoding="utf-8").splitlines(), start=1
        ):
            match = USES_LINE.match(line)
            if match is None:
                continue

            target = match.group("target")
            if target.startswith(("./", "docker://")):
                continue

            action, separator, reference = target.rpartition("@")
            location = f"{workflow.relative_to(REPOSITORY_ROOT)}:{line_number}"
            if (
                not separator
                or not action
                or FULL_COMMIT_SHA.fullmatch(reference) is None
            ):
                violations.append(
                    f"{location}: external action must use a full commit SHA"
                )

            comment = match.group("comment") or ""
            if RELEASE_TAG.fullmatch(comment.strip()) is None:
                violations.append(
                    f"{location}: pinned action must include an exact release tag"
                )

    assert violations == []
