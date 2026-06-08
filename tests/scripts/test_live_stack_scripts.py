from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_live_stack_env_exports_required_defaults() -> None:
    env = (ROOT / "scripts/live-stack/env.sh").read_text()
    required = [
        "KDIVE_DATABASE_URL",
        "KDIVE_OIDC_ISSUER",
        "KDIVE_OIDC_JWKS_URI",
        "KDIVE_OIDC_AUDIENCE",
        "KDIVE_S3_ENDPOINT_URL",
        "KDIVE_S3_BUCKET",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "KDIVE_BUILD_WORKSPACE",
        "KDIVE_BUILD_COMPONENT_ROOTS",
        "KDIVE_INSTALL_STAGING",
        "KDIVE_STACK_BASE_URL",
    ]
    for name in required:
        assert f"export {name}=" in env


def test_live_stack_scripts_are_strict_bash() -> None:
    for name in ("env.sh", "apply-migrations.sh", "start.sh", "stop.sh"):
        text = (ROOT / "scripts/live-stack" / name).read_text()
        assert text.startswith("#!/usr/bin/env bash\nset -euo pipefail\n")


def test_stack_start_runs_all_three_kdive_processes() -> None:
    text = (ROOT / "scripts/live-stack/start.sh").read_text()
    assert "python -m kdive server" in text
    assert "python -m kdive worker" in text
    assert "python -m kdive reconciler" in text
    assert "trap cleanup EXIT INT TERM" in text


def test_stack_stop_uses_pid_file_not_process_name_patterns() -> None:
    text = (ROOT / "scripts/live-stack/stop.sh").read_text()
    assert "KDIVE_STACK_PID_FILE" in text
    assert "pkill" not in text
