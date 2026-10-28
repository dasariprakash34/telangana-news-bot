"""Static checks on the GitHub Actions workflows, .env.example and README."""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from tgnews import cli, config

ROOT = Path(__file__).resolve().parent.parent
WF_DIR = ROOT / ".github" / "workflows"
WORKFLOWS = sorted(WF_DIR.glob("*.yml"))
EXPECTED = {"post.yml", "publish-approved.yml", "analyze.yml", "report.yml", "refresh-token.yml"}
SETUP_PREFIXES = ("sudo apt-get ", "python -m pip install ")


def load(path: Path) -> dict:
    return yaml.safe_load(path.read_text())


def triggers(doc: dict) -> dict:
    # YAML 1.1 parses the bare key `on` as boolean True.
    return doc.get("on", doc.get(True))


def subcommands() -> set[str]:
    sub = next(a for a in cli._parser()._actions if a.dest == "command")
    return set(sub.choices)


def run_lines(doc: dict):
    for job in doc["jobs"].values():
        for step in job["steps"]:
            if "run" in step:
                for line in step["run"].splitlines():
                    if line.strip():
                        yield line.strip()


def env_values(doc: dict):
    for job in doc["jobs"].values():
        for step in job["steps"]:
            yield from (step.get("env") or {}).items()


def test_expected_workflows_exist():
    assert {p.name for p in WORKFLOWS} == EXPECTED


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
class TestEachWorkflow:
    def test_dispatch_and_concurrency_and_permissions(self, path):
        doc = load(path)
        assert "workflow_dispatch" in triggers(doc)
        assert doc["concurrency"] == {"group": "tgnews", "cancel-in-progress": False}
        assert doc["permissions"] == {"contents": "read"}

    def test_actions_pinned_to_major(self, path):
        for job in load(path)["jobs"].values():
            for step in job["steps"]:
                if "uses" in step:
                    assert re.fullmatch(r"actions/[\w-]+@v\d+", step["uses"]), step["uses"]

    def test_env_values_are_expressions_only(self, path):
        for name, value in env_values(load(path)):
            assert re.fullmatch(r"\$\{\{ (secrets|vars)\.[A-Z_]+( \|\| '[a-z]+')? \}\}", str(value)), \
                f"{path.name}: {name} must be a secrets./vars. expression, not a literal"

    def test_secret_names_match_their_variable(self, path):
        for name, value in env_values(load(path)):
            assert f".{name}" in str(value), f"{path.name}: {name} mapped to {value}"

    def test_no_credential_looking_text(self, path):
        text = path.read_text()
        for pattern in (r"sk-ant", r"ghp_", r"\bEAA[A-Za-z0-9]{10,}", r"\d{8,}:[A-Za-z0-9_-]{30,}",
                        r"AKIA[A-Z0-9]{12,}", r"postgres(ql)?(\+\w+)?://\S+:\S+@"):
            assert not re.search(pattern, text), f"{path.name} contains text matching {pattern}"

    def test_secrets_are_not_echoed(self, path):
        for line in run_lines(load(path)):
            assert "echo" not in line and "secrets." not in line

    def test_run_lines_use_real_subcommands(self, path):
        valid = subcommands()
        saw_tgnews = False
        for line in run_lines(load(path)):
            if line.startswith("tgnews "):
                saw_tgnews = True
                assert line.split()[1] in valid, f"{path.name}: unknown subcommand in {line!r}"
            else:
                assert line.startswith(SETUP_PREFIXES), f"{path.name}: unexpected run line {line!r}"
        assert saw_tgnews

    def test_installs_postgres_extra_and_shaping_libs(self, path):
        lines = "\n".join(run_lines(load(path)))
        assert '-e ".[postgres]"' in lines
        for pkg in ("libraqm0", "libfribidi0", "libharfbuzz0b"):
            assert pkg in lines


def test_mode_defaults_to_review():
    for path in WORKFLOWS:
        for name, value in env_values(load(path)):
            if name == "MODE":
                assert "|| 'review'" in value


def test_post_cron_matches_default_time_slots():
    crons = [c["cron"] for c in triggers(load(WF_DIR / "post.yml"))["schedule"]]
    got = []
    for expr in crons:
        minute, hour, *rest = expr.split()
        assert rest == ["*", "*", "*"], expr
        got.append(f"{int(hour):02d}:{int(minute):02d}")
    assert sorted(got) == sorted(config.DEFAULT_TIME_SLOTS)
    assert "config.py" in (WF_DIR / "post.yml").read_text()  # the sync comment


def test_post_runs_then_publishes_approved():
    lines = [l for l in run_lines(load(WF_DIR / "post.yml")) if l.startswith("tgnews ")]
    assert lines == ["tgnews run", "tgnews publish-approved"]


def test_publish_approved_covers_0100_to_1730_every_30_min():
    fired = set()
    for c in triggers(load(WF_DIR / "publish-approved.yml"))["schedule"]:
        minute, hour, *_ = c["cron"].split()
        hours = range(int(hour.split("-")[0]), int(hour.split("-")[-1]) + 1)
        for h in hours:
            for m in minute.split(","):
                fired.add(h * 60 + int(m))
    assert fired == set(range(60, 17 * 60 + 31, 30))


def _read_env_names() -> set[str]:
    pat = re.compile(r"""(?:env\.get|need)\(\s*["']([A-Z][A-Z0-9_]+)["']|environ\[\s*["']([A-Z][A-Z0-9_]+)["']""")
    names: set[str] = set()
    for py in (ROOT / "src" / "tgnews").glob("*.py"):
        for m in pat.finditer(py.read_text()):
            names.add(m.group(1) or m.group(2))
    return names


def test_env_example_lists_every_env_var_the_code_reads():
    read = _read_env_names()
    assert {"MODE", "IG_API_VERSION", "TELEGRAM_CHAT_ID", "STORAGE_BUCKET"} <= read, "env scan is broken"
    listed = set(re.findall(r"^([A-Z][A-Z0-9_]+)=", (ROOT / ".env.example").read_text(), re.M))
    missing = read - listed
    assert not missing, f".env.example is missing env vars the code reads: {sorted(missing)}"
    assert re.search(r"^MODE=review$", (ROOT / ".env.example").read_text(), re.M)


def test_workflow_env_names_are_documented():
    listed = set(re.findall(r"^([A-Z][A-Z0-9_]+)=", (ROOT / ".env.example").read_text(), re.M))
    readme = (ROOT / "README.md").read_text()
    for path in WORKFLOWS:
        for name, _ in env_values(load(path)):
            assert name in listed, f"{name} used in {path.name} but not in .env.example"
            assert name in readme, f"{name} used in {path.name} but not in README.md"


def test_env_example_has_no_real_looking_values():
    for line in (ROOT / ".env.example").read_text().splitlines():
        if "=" in line and not line.startswith("#"):
            value = line.split("=", 1)[1]
            assert not re.search(r"sk-ant|ghp_|\d{8,}:[A-Za-z0-9_-]{30,}", value), line


def test_readme_mentions_every_workflow_file():
    readme = (ROOT / "README.md").read_text()
    for name in EXPECTED:
        assert name in readme, f"README.md does not mention {name}"


def test_pyproject_has_postgres_extra():
    text = (ROOT / "pyproject.toml").read_text()
    assert 'postgres = ["psycopg[binary]"]' in text
