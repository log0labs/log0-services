"""Which tests to run for a project, decided from the files in its folder. Pure functions plus fixed tables.

Images and commands are an allowlist in code, never read from the repository: a repository cannot choose what
runs, only provide the code the fixed command tests. The prepare step (fetching dependencies) is chosen so that it
**executes no repository code**: ``npm ci --ignore-scripts``, ``go mod download``, ``cargo fetch`` and wheels-only
``pip install`` do not run install scripts or build backends. Repository code runs only in the test step, which
has no network.
"""

import json
from dataclasses import dataclass, field
from pathlib import Path

CACHE = "/work/.cache"


@dataclass(frozen=True)
class SandboxPlan:
    language: str
    image: str
    name: str  # shown to people: the test command
    prepare: tuple[str, ...] | None  # fetch dependencies (network, no repository code), or None
    test: tuple[str, ...]  # run the tests (no network)
    env: dict[str, str] = field(default_factory=dict)  # fixed, safe environment for both steps


DEFAULT_IMAGES = {
    "go": "golang:1.23-alpine",
    "node": "node:22-alpine",
    "rust": "rust:1-slim",
    "python": "python:3.13-slim",
}


def _go(images: dict[str, str]) -> SandboxPlan:
    env = {"GOMODCACHE": f"{CACHE}/gomod", "GOCACHE": "/tmp/gobuild", "GOTMPDIR": "/tmp", "CGO_ENABLED": "0", "GOFLAGS": "-mod=readonly"}
    return SandboxPlan("go", images["go"], "go test ./...", ("go", "mod", "download"), ("go", "test", "./..."), env)


def _node(folder: Path, images: dict[str, str]) -> SandboxPlan | None:
    try:
        scripts = json.loads((folder / "package.json").read_text(encoding="utf-8")).get("scripts", {})
    except (OSError, ValueError, AttributeError):
        return None
    if not isinstance(scripts, dict) or "test" not in scripts:
        return None
    locked = (folder / "package-lock.json").exists()
    install = ("npm", "ci" if locked else "install", "--ignore-scripts", "--no-audit", "--no-fund")
    env = {"npm_config_cache": f"{CACHE}/npm", "CI": "1"}
    return SandboxPlan("node", images["node"], "npm test", install, ("npm", "test", "--silent"), env)


def _rust(folder: Path, images: dict[str, str]) -> SandboxPlan:
    fetch = ("cargo", "fetch", "--locked") if (folder / "Cargo.lock").exists() else ("cargo", "fetch")
    env = {"CARGO_HOME": f"{CACHE}/cargo", "CARGO_TARGET_DIR": f"{CACHE}/target", "CARGO_NET_OFFLINE": "false"}
    return SandboxPlan("rust", images["rust"], "cargo test", fetch, ("cargo", "test", "--offline"), env)


def _python(folder: Path, images: dict[str, str]) -> SandboxPlan | None:
    if not (folder / "requirements.txt").exists():
        return None  # a pyproject-only project would need its build backend run with network access: not supported
    install = ("pip", "install", "--only-binary=:all:", "--no-input", "--disable-pip-version-check", "--target", f"{CACHE}/site", "-r", "requirements.txt", "pytest")
    env = {"PYTHONPATH": f"{CACHE}/site", "PYTHONDONTWRITEBYTECODE": "1", "PIP_NO_CACHE_DIR": "1"}
    return SandboxPlan("python", images["python"], "pytest", install, ("python", "-m", "pytest", "-q", "-p", "no:cacheprovider"), env)


def plan_for(folder: Path, images: dict[str, str] | None = None) -> SandboxPlan | None:
    """The plan for the project in ``folder``, or ``None`` when its toolchain is unknown (nothing is run)."""
    chosen = {**DEFAULT_IMAGES, **(images or {})}
    if (folder / "go.mod").exists():
        return _go(chosen)
    if (folder / "package.json").exists():
        return _node(folder, chosen)
    if (folder / "Cargo.toml").exists():
        return _rust(folder, chosen)
    return _python(folder, chosen)
