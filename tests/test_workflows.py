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
LISTENER = "telegram-listener.yml"
EXPECTED = {"post.yml", "publish-approved.yml", "analyze.yml", "report.yml", "refresh-token.yml", LISTENER}
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


def assert_valid_tgnews_line(where: str, line: str, valid: set[str]) -> None:
    tokens = line.split()
    assert tokens[0] == "tgnews" and len(tokens) >= 2 and tokens[1] in valid, \
        f"{where}: unknown subcommand in {line!r}"
    rest = tokens[2:]
    if tokens[1] == "run":
        allowed = [[], ["--ignore-slots"]]
    elif tokens[1] == "dry-run":
        allowed = [["--out", rest[1]]] if len(rest) == 2 and rest[0] == "--out" else []
    elif tokens[1] == "listen":
        allowed = [["--post"]]  # the listener also runs the posting pipeline
    else:
        allowed = [[]]
    assert rest in allowed, f"{where}: unexpected arguments in {line!r}"


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
        if path.name == LISTENER:  # long-running; own group so it never blocks posting runs
            assert doc["concurrency"] == {"group": "tgnews-listener", "cancel-in-progress": False}
            assert doc["permissions"] == {"contents": "read", "actions": "write"}
            return
        assert doc["concurrency"] == {"group": "tgnews", "cancel-in-progress": False}
        assert doc["permissions"] == {"contents": "read"}

    def test_actions_pinned_to_major(self, path):
        for job in load(path)["jobs"].values():
            for step in job["steps"]:
                if "uses" in step:
                    assert re.fullmatch(r"actions/[\w-]+@v\d+", step["uses"]), step["uses"]

    def test_env_values_are_expressions_only(self, path):
        for name, value in env_values(load(path)):
            if path.name == LISTENER and (name, value) == ("GH_TOKEN", "${{ github.token }}"):
                continue  # the built-in token, used only to start the next listener run
            assert re.fullmatch(r"\$\{\{ (secrets|vars)\.[A-Z_]+( \|\| '[a-z]+')? \}\}", str(value)), \
                f"{path.name}: {name} must be a secrets./vars. expression, not a literal"

    def test_secret_names_match_their_variable(self, path):
        for name, value in env_values(load(path)):
            if name == "GH_TOKEN":
                continue
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
                assert_valid_tgnews_line(path.name, line, valid)
            elif path.name == LISTENER and line.startswith("gh workflow run telegram-listener.yml "):
                continue
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


def test_listener_runs_listen_then_restarts_itself():
    doc = load(WF_DIR / LISTENER)
    lines = [l for l in run_lines(doc) if l.startswith(("tgnews ", "gh "))]
    assert lines[0] == "tgnews listen --post"
    assert lines[1].startswith("gh workflow run telegram-listener.yml ")
    job = doc["jobs"]["listen"]
    assert job["timeout-minutes"] > 340 / 1 * 1  # longer than `tgnews listen --minutes` default (340)
    assert job["timeout-minutes"] < 360  # GitHub hard limit is 6 hours
    restart = next(st for st in job["steps"] if st.get("name") == "Start the next listener")
    assert restart["if"] == "${{ !cancelled() }}"


def test_post_runs_then_publishes_approved():
    lines = [l for l in run_lines(load(WF_DIR / "post.yml")) if l.startswith("tgnews ")]
    assert lines == ["tgnews run", "tgnews publish-approved"]


def test_publish_approved_covers_0115_to_1745_every_30_min_off_the_slot_marks():
    fired = set()
    for c in triggers(load(WF_DIR / "publish-approved.yml"))["schedule"]:
        minute, hour, *_ = c["cron"].split()
        hours = range(int(hour.split("-")[0]), int(hour.split("-")[-1]) + 1)
        for h in hours:
            for m in minute.split(","):
                fired.add(h * 60 + int(m))
    assert fired == set(range(75, 17 * 60 + 46, 30))
    assert not any(t % 30 == 0 for t in fired)  # never on :00 / :30, where post slots run


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
            if name == "GH_TOKEN":
                continue  # the runner's built-in token, not a project setting
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


def test_run_strings_contain_no_expressions():
    for path in WORKFLOWS:
        for line in run_lines(load(path)):
            assert "${{" not in line, f"{path.name}: expression inside run: {line!r}"


def test_tgnews_line_validator_rejects_extra_tokens():
    valid = subcommands()
    assert_valid_tgnews_line("t", "tgnews run", valid)
    assert_valid_tgnews_line("t", "tgnews run --ignore-slots", valid)
    assert_valid_tgnews_line("t", "tgnews dry-run --out ./out", valid)
    for bad in ("tgnews run extra", "tgnews run --force", "tgnews publish-approved --ignore-slots",
                "tgnews dry-run", "tgnews dry-run --out", "tgnews bogus", "tgnews report; curl x"):
        with pytest.raises(AssertionError):
            assert_valid_tgnews_line("t", bad, valid)


IG_ENV = {"IG_ACCESS_TOKEN", "IG_USER_ID", "IG_API_VERSION"}
STORAGE_ENV = {"STORAGE_ENDPOINT", "STORAGE_ACCESS_KEY_ID", "STORAGE_SECRET_ACCESS_KEY",
               "STORAGE_BUCKET", "STORAGE_PUBLIC_BASE"}  # s3 backend only; mapped but may be unset
GITHUB_IMAGES_ENV = {"STORAGE_PROVIDER", "IMAGES_GH_TOKEN", "IMAGES_GH_REPO", "IMAGES_GH_BRANCH",
                     "IMAGES_PUBLIC_BASE"}
PRUNE_ENV = {"STORAGE_PROVIDER", "IMAGES_GH_TOKEN", "IMAGES_GH_REPO", "IMAGES_GH_BRANCH", "IMAGES_KEEP_DAYS"}


def required_env(cmd: str) -> set[str]:
    """Required env names per subcommand, derived from cli.py's own _NEEDS_* sets plus the
    unconditional requirements in build_deps (DATABASE_URL for everything but dry-run;
    storage for `run`; prune-images needs only the images repo). Telegram is optional in the code, but the workflows must pass it."""
    if cmd == "prune-images":  # no database, LLM or Instagram: only the images repo (and Telegram alerts)
        return PRUNE_ENV | {"TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"}
    need = {"DATABASE_URL"} if cmd != "dry-run" else set()
    if cmd in cli._NEEDS_LLM:
        # build_llm reads the key of whichever provider is selected, so a step must pass both
        # provider keys (the unused one may be an empty secret) plus the provider/model variables.
        need |= {"GEMINI_API_KEY", "ANTHROPIC_API_KEY", "LLM_PROVIDER", "LLM_MODEL"}
    if cmd in cli._NEEDS_IG:
        need |= IG_ENV
    if cmd == "run":
        need |= STORAGE_ENV | GITHUB_IMAGES_ENV | IG_ENV  # IG is needed whenever MODE=auto, and the workflow must support it
    if cmd == "publish-approved":
        need |= IG_ENV  # purpose name is "publish" in build_deps
    return need | {"TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"}


def test_every_step_passes_the_env_its_subcommand_needs():
    # sanity: the cli constants this test relies on still exist
    assert cli._NEEDS_LLM and cli._NEEDS_IG
    checked = set()
    for path in WORKFLOWS:
        for job in load(path)["jobs"].values():
            for step in job["steps"]:
                line = step.get("run", "").strip()
                if not line.startswith("tgnews "):
                    continue
                cmd = line.split()[1]
                missing = required_env(cmd) - set(step.get("env") or {})
                assert not missing, f"{path.name}: `{line}` is missing env {sorted(missing)}"
                checked.add(cmd)
    assert {"run", "publish-approved", "analyze", "report", "refresh-token", "prune-images"} <= checked


def test_dropping_storage_from_run_step_would_fail():
    assert STORAGE_ENV <= required_env("run")
    assert not STORAGE_ENV & required_env("report")


def test_normalised_database_url_documented():
    assert "postgresql+psycopg://" in (ROOT / "README.md").read_text()
    assert "postgres://" in (ROOT / ".env.example").read_text()


def test_llm_env_only_passed_where_the_command_needs_it():
    llm_env = {"GEMINI_API_KEY", "ANTHROPIC_API_KEY", "LLM_PROVIDER", "LLM_MODEL"}
    for path in WORKFLOWS:
        for job in load(path)["jobs"].values():
            for step in job["steps"]:
                line = step.get("run", "").strip()
                if line.startswith("tgnews "):
                    has = llm_env & set(step.get("env") or {})
                    assert (line.split()[1] in cli._NEEDS_LLM or line == "tgnews listen --post") == bool(has), \
                        f"{path.name}: {line}"


def test_llm_secrets_vs_variables_mapping():
    for path in WORKFLOWS:
        for name, value in env_values(load(path)):
            if name in ("GEMINI_API_KEY", "ANTHROPIC_API_KEY"):
                assert str(value).startswith("${{ secrets."), (path.name, name)
            if name in ("LLM_PROVIDER", "LLM_MODEL"):
                assert str(value).startswith("${{ vars."), (path.name, name)


def test_anthropic_is_an_optional_extra_not_a_dependency():
    import tomllib
    proj = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    assert not any(d.lower().startswith("anthropic") for d in proj["dependencies"])
    assert proj["optional-dependencies"]["anthropic"] == ["anthropic"]


def test_image_storage_env_only_where_needed():
    images = (GITHUB_IMAGES_ENV | STORAGE_ENV | {"IMAGES_KEEP_DAYS"}) - {"TELEGRAM_BOT_TOKEN"}
    for path in WORKFLOWS:
        for job in load(path)["jobs"].values():
            for step in job["steps"]:
                line = step.get("run", "").strip()
                if line.startswith("tgnews "):
                    has = images & set(step.get("env") or {})
                    assert bool(has) == (line.split()[1] in {"run", "prune-images"} or line == "tgnews listen --post"), \
                        f"{path.name}: {line}"
    assert not STORAGE_ENV & required_env("prune-images")


def test_images_secret_and_variable_mapping():
    seen = set()
    for path in WORKFLOWS:
        for name, value in env_values(load(path)):
            if name == "IMAGES_GH_TOKEN":
                assert str(value) == "${{ secrets.IMAGES_GH_TOKEN }}"  # never GITHUB_*-named
            if name in ("STORAGE_PROVIDER", "IMAGES_GH_REPO", "IMAGES_GH_BRANCH", "IMAGES_PUBLIC_BASE",
                        "IMAGES_KEEP_DAYS"):
                assert str(value).startswith("${{ vars."), (path.name, name)
            if name.startswith(("STORAGE_E", "STORAGE_A", "STORAGE_S", "STORAGE_B")):
                assert str(value).startswith("${{ secrets."), (path.name, name)
            seen.add((path.name, name))
    assert ("analyze.yml", "IMAGES_KEEP_DAYS") in seen and ("post.yml", "IMAGES_GH_TOKEN") in seen
    for path in WORKFLOWS:
        for name, _ in env_values(load(path)):
            assert not name.startswith("GITHUB_"), name


def test_analyze_prunes_images_after_analysis_even_if_it_failed():
    steps = next(iter(load(WF_DIR / "analyze.yml")["jobs"].values()))["steps"]
    cmds = [st.get("run", "").strip() for st in steps]
    assert cmds.index("tgnews prune-images") > cmds.index("tgnews analyze")
    prune = steps[cmds.index("tgnews prune-images")]
    assert prune["if"] == "${{ !cancelled() }}"


def test_prune_images_and_check_images_are_subcommands():
    assert {"prune-images", "check-images"} <= subcommands()
