"""Swift test assertions must stay active in optimized builds.

Swift removes ``assert(...)`` under ``-O``, which causes tests using ``assert``
to pass vacuously in optimized builds. Tests in ``macos/Voice/Tests/`` must use
an always-on assertion helper (``check(...)``) rather than ``assert``,
``assertionFailure``, or ``precondition``.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TESTS_DIR = ROOT / "macos" / "Voice" / "Tests"
TEST_SCRIPT = ROOT / "macos" / "Voice" / "scripts" / "test.sh"

FORBIDDEN_CALL_PATTERN = re.compile(r"\b(assert|assertionFailure|precondition)\s*\(")


def _strip_line_comments(source: str) -> str:
    """Strip Swift `//` line comments while leaving string literals intact."""
    pattern = r"""(#"(?:.*?)"#|"(?:\\.|[^"\\])*")|(//.*)"""

    def replace(match: re.Match[str]) -> str:
        if match.group(1) is not None:
            return match.group(1)
        return ""

    return re.sub(pattern, replace, source)


def test_no_asserts_or_preconditions_in_swift_tests():
    swift_files = sorted(TESTS_DIR.glob("*.swift"))
    assert swift_files, f"No Swift test files found in {TESTS_DIR}"

    violations: list[str] = []
    for path in swift_files:
        if path.name == "Check.swift":
            continue
        content = path.read_text(encoding="utf-8")
        stripped = _strip_line_comments(content)
        for line_no, line in enumerate(stripped.splitlines(), start=1):
            if FORBIDDEN_CALL_PATTERN.search(line):
                violations.append(f"{path.relative_to(ROOT)}:{line_no}: {line.strip()}")

    assert not violations, "Found forbidden assertions in Swift tests; use check(...) instead:\n" + "\n".join(
        violations
    )


def test_test_sh_compiles_check_helper():
    assert TEST_SCRIPT.exists(), f"Test script not found at {TEST_SCRIPT}"
    check_file = TESTS_DIR / "Check.swift"
    assert check_file.exists(), f"Check helper not found at {check_file}"

    script_content = TEST_SCRIPT.read_text(encoding="utf-8")
    assert "Check.swift" in script_content, "macos/Voice/scripts/test.sh must compile Check.swift"
