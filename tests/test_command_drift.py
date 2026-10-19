"""The check commands are spelled in CI, README.md and CLAUDE.md. They drift apart.

The pi-voice test globs live in ``pi-voice/package.json`` and CI runs them through
``npm test``. Docs that spell the command out must list the same globs. The ruff
targets are one set that CI, README.md and CLAUDE.md must agree on.
"""

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CI = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
DOCS = {name: (ROOT / name).read_text() for name in ("README.md", "CLAUDE.md")}


def _ruff_targets(text: str, subcommand: str) -> set[str]:
    match = re.search(rf"ruff {subcommand}(?: --check)? ([^\n`#]+)", text)
    assert match, f"no `ruff {subcommand}` command found"
    return {target.rstrip("/") for target in match.group(1).split()}


def _package_test_globs() -> list[str]:
    script = json.loads((ROOT / "pi-voice" / "package.json").read_text())["scripts"]["test"]
    return re.findall(r"'([^']+)'", script)


def test_ci_runs_pi_voice_tests_through_package_json() -> None:
    assert "npm test" in CI, (
        "ci.yml must run pi-voice tests with `npm test` so the globs live only in pi-voice/package.json"
    )
    assert "node --test" not in CI, "ci.yml spells a node --test glob; run `npm test` in pi-voice instead"


def test_ci_installs_worker_dependencies_before_tests() -> None:
    assert re.search(r"working-directory: pi-voice/src/worker\s+run: npm ci", CI), (
        "pi-voice/src/worker/config.test.ts imports pi-ai; ci.yml must `npm ci` in pi-voice/src/worker"
    )


def test_docs_list_every_package_json_test_glob() -> None:
    globs = [f"pi-voice/{g}" for g in _package_test_globs()]
    assert len(globs) >= 2, "pi-voice/package.json test script lost a glob"
    for name, text in DOCS.items():
        commands = re.findall(r"node --test ([^\n`#]+)", text)
        assert commands, f"{name} has no `node --test` command"
        for command in commands:
            missing = [g for g in globs if f"'{g}'" not in command]
            assert not missing, (
                f"{name}: `node --test {command.strip()}` is missing {missing}; copy the globs from pi-voice/package.json"
            )


def test_ruff_targets_match_everywhere() -> None:
    for subcommand in ("check", "format"):
        expected = _ruff_targets(CI, subcommand)
        for name, text in DOCS.items():
            actual = _ruff_targets(text, subcommand)
            assert actual == expected, (
                f"{name}: `ruff {subcommand}` targets {sorted(actual)}, ci.yml runs {sorted(expected)}"
            )
