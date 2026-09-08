"""The entrypoints must generate an opencode config that resolves.

``docker-entrypoint.sh`` writes ``opencode.json`` from ``PR_AF_MODEL``. opencode
reads the top-level ``model`` as ``"<provider>/<key>"``, and the only provider
the generated config declares is ``openrouter`` — so a bare OpenRouter slug like
``deepseek/deepseek-v4-flash-0731`` (the repo default) named an *undeclared*
provider ``deepseek``, and the model key under ``openrouter`` did not match the
model being requested either.

These run the real scripts, so they catch a shell-syntax or quoting mistake as
well as the routing itself. Skipped where ``sh`` is unavailable.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
ENTRYPOINTS = ("docker-entrypoint.sh", "go/docker-entrypoint.sh")
BARE_SLUG = "deepseek/deepseek-v4-flash-0731"

pytestmark = pytest.mark.skipif(
    shutil.which("sh") is None, reason="POSIX sh not available"
)


def _generate(entrypoint: str, tmp_path: Path, model: str | None) -> dict:
    """Run an entrypoint with a no-op command; return the opencode config."""
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(tmp_path),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "OPENROUTER_API_KEY": "sk-or-test",
    }
    if model is not None:
        env["PR_AF_MODEL"] = model

    result = subprocess.run(
        ["sh", str(REPO_ROOT / entrypoint), "true"],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, (
        f"{entrypoint} exited {result.returncode}: {result.stderr}"
    )
    config_path = tmp_path / "config" / "opencode" / "opencode.json"
    assert config_path.is_file(), f"{entrypoint} wrote no config"
    return json.loads(config_path.read_text(encoding="utf-8"))


@pytest.mark.parametrize("entrypoint", ENTRYPOINTS)
@pytest.mark.parametrize(
    "model", [BARE_SLUG, "openrouter/" + BARE_SLUG, None], ids=["bare", "prefixed", "default"]
)
def test_generated_config_resolves_through_openrouter(
    entrypoint: str, model: str | None, tmp_path: Path
) -> None:
    """Whatever form PR_AF_MODEL takes, the config must be self-consistent."""
    config = _generate(entrypoint, tmp_path, model)

    providers = config["provider"]
    assert list(providers) == ["openrouter"], (
        f"config declares {list(providers)}; only 'openrouter' is configured "
        f"with an API key, so anything else cannot authenticate"
    )

    # The requested model must name the declared provider...
    assert config["model"].startswith("openrouter/")
    assert config["small_model"] == config["model"]

    # ...and the key under that provider must be the slug the model resolves to,
    # or opencode has no entry for the model it is being asked to run.
    model_key = config["model"].removeprefix("openrouter/")
    assert model_key in providers["openrouter"]["models"], (
        f"model key {model_key!r} is not declared under the openrouter provider "
        f"(declared: {list(providers['openrouter']['models'])})"
    )


@pytest.mark.parametrize("entrypoint", ENTRYPOINTS)
def test_api_key_is_referenced_not_baked(entrypoint: str, tmp_path: Path) -> None:
    """The key must be an {env:...} reference, never the literal secret."""
    config = _generate(entrypoint, tmp_path, BARE_SLUG)
    api_key = config["provider"]["openrouter"]["options"]["apiKey"]
    assert api_key == "{env:OPENROUTER_API_KEY}"
    assert "sk-or-test" not in json.dumps(config)


@pytest.mark.parametrize("entrypoint", ENTRYPOINTS)
def test_prefix_is_not_doubled(entrypoint: str, tmp_path: Path) -> None:
    config = _generate(entrypoint, tmp_path, "openrouter/" + BARE_SLUG)
    assert config["model"].count("openrouter/") == 1
