"""Tests for archive information and verification component formatting."""

from __future__ import annotations

from repo_archive.inspection import _status_component


def test_submodule_status_uses_bounded_definition_summary() -> None:
    commits = [f"{number:040x}" for number in range(100)]

    component = _status_component(
        "submodules",
        {
            "status": "not-archived",
            "detected": True,
            "repositories": [
                {
                    "path": "vendor/library",
                    "url": "../library.git",
                    "commits": commits,
                    "refs": [],
                }
            ],
        },
    )

    assert component.message is not None
    assert "100 pinned commits" in component.message
    assert commits[0] in component.message
    assert commits[-1] not in component.message
    assert len(component.message) < 260
