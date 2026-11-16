"""The test sandbox: the plan for each toolchain, the exact ``docker run`` flags, and (with Docker) the real isolation.

The probe tests start throwaway containers from ``alpine`` and try to do what a hostile test could: reach the network,
write to the filesystem, read the host's environment, grab memory or processes, run forever. They are skipped when
Docker is not available (or ``SKIP_DOCKER_TESTS=1``); everything else runs anywhere.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from app.sandbox.docker import (
    MAX_OUTPUT_TAIL,
    NAME_PREFIX,
    DockerSandbox,
    SandboxConfig,
    SandboxLimits,
    run_args,
    safe_subdir,
)
from app.sandbox.plan import DEFAULT_IMAGES, plan_for

# --------------------------------------------------------------------------- plans


def test_each_toolchain_gets_a_fixed_plan(tmp_path: Path) -> None:
    (tmp_path / "go.mod").write_text("module x\n")
    plan = plan_for(tmp_path)
    assert plan is not None and plan.language == "go" and plan.image == DEFAULT_IMAGES["go"]
    assert plan.test == ("go", "test", "./...") and plan.prepare == ("go", "mod", "download")


def test_node_needs_a_test_script_and_never_runs_install_scripts(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text(json.dumps({"scripts": {"build": "x"}}))
    assert plan_for(tmp_path) is None  # nothing to run

    (tmp_path / "package.json").write_text(json.dumps({"scripts": {"test": "vitest run"}}))
    plan = plan_for(tmp_path)
    assert plan is not None and plan.prepare is not None and "--ignore-scripts" in plan.prepare and plan.prepare[1] == "install"
    (tmp_path / "package-lock.json").write_text("{}")
    assert plan_for(tmp_path).prepare[1] == "ci"  # type: ignore[union-attr]


def test_rust_and_python_plans_fetch_without_running_repository_code(tmp_path: Path) -> None:
    (tmp_path / "Cargo.toml").write_text("[package]\n")
    rust = plan_for(tmp_path)
    assert rust is not None and rust.prepare == ("cargo", "fetch") and rust.test == ("cargo", "test", "--offline")
    (tmp_path / "Cargo.lock").write_text("")
    assert plan_for(tmp_path).prepare == ("cargo", "fetch", "--locked")  # type: ignore[union-attr]

    other = tmp_path / "py"
    other.mkdir()
    (other / "requirements.txt").write_text("requests\n")
    python = plan_for(other)
    assert python is not None and "--only-binary=:all:" in (python.prepare or ())


def test_unknown_projects_and_pyproject_only_python_are_not_run(tmp_path: Path) -> None:
    assert plan_for(tmp_path) is None
    (tmp_path / "pyproject.toml").write_text("[project]\n")
    assert plan_for(tmp_path) is None  # building it would run the repository's build backend with network access


def test_images_can_be_replaced_by_the_operator_but_not_by_the_repository(tmp_path: Path) -> None:
    (tmp_path / "go.mod").write_text("module x\n")

    assert plan_for(tmp_path, {"go": "registry.example/go:1.24"}).image == "registry.example/go:1.24"  # type: ignore[union-attr]
    (tmp_path / "log0.yml").write_text("image: evil\n")  # nothing in the repository is read for this
    assert plan_for(tmp_path).image == DEFAULT_IMAGES["go"]  # type: ignore[union-attr]


@pytest.mark.parametrize("bad", ["../etc", "a/../../b", "/abs", "C:\\x\\..\\..\\y"])
def test_a_project_folder_cannot_leave_the_repository(bad: str) -> None:
    with pytest.raises(ValueError):
        safe_subdir(bad)


def test_ordinary_folders_are_normalised() -> None:
    assert safe_subdir("") == "" and safe_subdir(".") == "" and safe_subdir("services/payments/") == "services/payments"
    assert safe_subdir("services\\payments") == "services/payments"


# ------------------------------------------------------------------- the docker flags


def _args(**over: object) -> list[str]:
    base = {
        "name": "log0-sbx-1",
        "image": "golang:1.23-alpine",
        "command": ("go", "test", "./..."),
        "env": {"B": "2", "A": "1"},
        "host_dir": "/tmp/work",
        "subdir": "services/payments",
        "network": "none",
        "limits": SandboxLimits(),
    }
    return run_args(**(base | over))  # type: ignore[arg-type]


def _flag(args: list[str], name: str) -> list[str]:
    return [args[i + 1] for i, a in enumerate(args) if a == name]


def test_every_hardening_flag_is_present() -> None:
    args = _args()

    assert _flag(args, "--network") == ["none"] and "--read-only" in args and "--init" in args
    assert _flag(args, "--cap-drop") == ["ALL"] and _flag(args, "--security-opt") == ["no-new-privileges"]
    assert _flag(args, "--user") == ["65534:65534"]
    assert _flag(args, "--memory") == ["1g"] and _flag(args, "--memory-swap") == ["1g"]  # no swap: the limit is the limit
    assert _flag(args, "--pids-limit") == ["256"] and _flag(args, "--cpus") == ["1.0"]
    assert "nofile=1024:1024" in _flag(args, "--ulimit") and "core=0" in _flag(args, "--ulimit")
    assert _flag(args, "--volume") == ["/tmp/work:/work:rw"] and _flag(args, "--workdir") == ["/work/services/payments"]


def test_nothing_from_the_host_is_passed_in_and_the_image_and_command_come_last() -> None:
    args = _args()

    assert "--privileged" not in args and "--env-file" not in args and "--pid" not in args and "--ipc" not in args
    assert not any("docker.sock" in a for a in args) and len(_flag(args, "--volume")) == 1
    assert _flag(args, "--env") == ["HOME=/tmp", "LANG=C.UTF-8", "A=1", "B=2"]  # only fixed values, sorted
    assert args[-4:] == ["golang:1.23-alpine", "go", "test", "./..."]


def test_a_stronger_runtime_is_used_when_the_operator_sets_one() -> None:
    assert _flag(_args(runtime="runsc"), "--runtime") == ["runsc"]
    assert "--runtime" not in _args()


# --------------------------------------------------------- real isolation (needs Docker)


def _docker_works() -> bool:
    if os.getenv("SKIP_DOCKER_TESTS", "").lower() in ("1", "true", "yes") or shutil.which("docker") is None:
        return False
    try:
        return subprocess.run(["docker", "image", "inspect", "alpine:latest"], capture_output=True, timeout=30, check=False).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


needs_docker = pytest.mark.skipif(not _docker_works(), reason="Docker with the alpine image is not available")


async def probe(command: str, *, limits: SandboxLimits | None = None, timeout: float = 30, tmp: Path, env: dict[str, str] | None = None):
    sandbox = DockerSandbox(SandboxConfig(limits=limits or SandboxLimits()))
    work = tmp / "work"
    work.mkdir(exist_ok=True)
    return await sandbox._exec("alpine:latest", env or {}, ("sh", "-c", command), work, "", network="none", timeout=timeout)


def _leftover_containers() -> list[str]:
    out = subprocess.run(["docker", "ps", "-a", "--filter", f"name={NAME_PREFIX}", "--format", "{{.Names}}"], capture_output=True, text=True, timeout=30, check=False)
    return out.stdout.split()


@needs_docker
@pytest.mark.asyncio
async def test_the_code_cannot_reach_the_network(tmp_path: Path) -> None:
    result = await probe("wget -T 3 -q -O- http://1.1.1.1 >/dev/null 2>&1 && echo REACHED || echo BLOCKED; ls /sys/class/net", tmp=tmp_path)

    assert "BLOCKED" in result.output and "REACHED" not in result.output
    assert "eth0" not in result.output  # not even an interface to try


@needs_docker
@pytest.mark.asyncio
async def test_the_root_filesystem_is_read_only_but_tmp_and_work_are_writable(tmp_path: Path) -> None:
    result = await probe("touch /etc/x 2>/dev/null && echo ROOT_WRITABLE; touch /tmp/ok && echo TMP_OK; touch /work/ok && echo WORK_OK", tmp=tmp_path)

    assert "ROOT_WRITABLE" not in result.output and "TMP_OK" in result.output and "WORK_OK" in result.output


@needs_docker
@pytest.mark.asyncio
async def test_it_runs_unprivileged_with_no_capabilities(tmp_path: Path) -> None:
    result = await probe("id -u; grep CapEff /proc/self/status; grep NoNewPrivs /proc/self/status", tmp=tmp_path)

    assert "65534" in result.output
    assert "CapEff:\t0000000000000000" in result.output
    assert "NoNewPrivs:\t1" in result.output


@needs_docker
@pytest.mark.asyncio
async def test_the_hosts_environment_and_files_are_not_visible(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOG0_HOST_SECRET", "super-secret-value")
    result = await probe("env; ls /var/run/docker.sock /host_mnt /c /Users 2>&1 | head -5", tmp=tmp_path)

    assert "super-secret-value" not in result.output and "LOG0_HOST_SECRET" not in result.output
    assert "docker.sock" not in result.output.replace("No such file or directory", "").replace("ls: /var/run/docker.sock", "")


@needs_docker
@pytest.mark.asyncio
async def test_memory_is_capped_and_an_out_of_memory_kill_is_reported(tmp_path: Path) -> None:
    result = await probe('x=$(head -c 300000000 /dev/zero | tr "\\0" a); echo ALLOCATED', limits=SandboxLimits(memory="64m"), tmp=tmp_path)

    assert "ALLOCATED" not in result.output and result.exit_code not in (0, None)


@needs_docker
@pytest.mark.asyncio
async def test_a_test_that_never_ends_is_killed_and_its_container_removed(tmp_path: Path) -> None:
    result = await probe("sleep 60", timeout=3, tmp=tmp_path)

    assert result.timed_out and result.exit_code is None
    assert _leftover_containers() == []


@needs_docker
@pytest.mark.asyncio
async def test_a_fork_bomb_is_stopped_by_the_process_limit(tmp_path: Path) -> None:
    result = await probe("i=0; while [ $i -lt 300 ]; do sleep 20 & i=$((i+1)); done 2>/dev/null; echo spawned=$(ps | grep -c 'sleep 20')", limits=SandboxLimits(pids=40), timeout=40, tmp=tmp_path)

    # the shell either dies when fork fails (non-zero exit) or finishes with far fewer processes than the 300 it asked for
    finished = "spawned=" in result.output
    assert result.exit_code != 0 or (finished and int(result.output.rsplit("spawned=", 1)[1].split()[0]) < 60)


@needs_docker
@pytest.mark.asyncio
async def test_a_noisy_test_cannot_fill_memory_with_output(tmp_path: Path) -> None:
    result = await probe('head -c 20000000 /dev/zero | tr "\\0" a', timeout=60, tmp=tmp_path)

    assert result.exit_code == 0 and len(result.output) <= MAX_OUTPUT_TAIL * 2 + 4096


def _go_project(root: Path, *, passing: bool) -> None:
    (root / "go.mod").write_text("module example.com/x\n\ngo 1.23\n")
    (root / "x.go").write_text("package x\n\nfunc Add(a, b int) int { return a + b }\n")
    expected = "3" if passing else "4"
    (root / "x_test.go").write_text(f'package x\n\nimport "testing"\n\nfunc TestAdd(t *testing.T) {{\n\tif Add(1, 2) != {expected} {{\n\t\tt.Fatal("wrong sum")\n\t}}\n}}\n')


def _go_image_present() -> bool:
    return _docker_works() and subprocess.run(["docker", "image", "inspect", DEFAULT_IMAGES["go"]], capture_output=True, check=False).returncode == 0


needs_go_image = pytest.mark.skipif(not _go_image_present(), reason="the golang image is not available")


@needs_go_image
@pytest.mark.asyncio
async def test_a_passing_and_a_failing_go_project_end_to_end(tmp_path: Path) -> None:
    sandbox = DockerSandbox()
    good, bad = tmp_path / "good", tmp_path / "bad"
    for folder, passing in ((good, True), (bad, False)):
        folder.mkdir()
        _go_project(folder, passing=passing)

    ok = await sandbox.run(source=good, subdir="")
    broken = await sandbox.run(source=bad, subdir="")

    assert ok is not None and ok.status == "passed" and ok.exit_code == 0 and ok.name == "go test ./..."
    assert broken is not None and broken.status == "failed" and "wrong sum" in broken.output_tail
    assert not (good / ".cache").exists() and sorted(p.name for p in good.iterdir()) == ["go.mod", "x.go", "x_test.go"]  # the source was only copied
    assert _leftover_containers() == []


@needs_go_image
@pytest.mark.asyncio
async def test_a_test_that_tries_the_network_or_reads_the_hosts_secrets_finds_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOG0_HOST_SECRET", "super-secret-value")
    _go_project(tmp_path, passing=True)
    (tmp_path / "evil_test.go").write_text(
        'package x\n\nimport (\n\t"net/http"\n\t"os"\n\t"testing"\n\t"time"\n)\n\nfunc TestEvil(t *testing.T) {\n'
        '\tc := http.Client{Timeout: 2 * time.Second}\n\tif _, err := c.Get("http://1.1.1.1"); err == nil {\n\t\tt.Fatal("NETWORK_REACHED")\n\t}\n'
        '\tif os.Getenv("LOG0_HOST_SECRET") != "" {\n\t\tt.Fatal("SECRET_VISIBLE")\n\t}\n'
        '\tif err := os.WriteFile("/etc/pwned", []byte("x"), 0o644); err == nil {\n\t\tt.Fatal("ROOT_WRITABLE")\n\t}\n}\n'
    )

    run = await DockerSandbox().run(source=tmp_path, subdir="")

    assert run is not None and run.status == "passed", run.output_tail  # every attack failed, so the "evil" test passes


@pytest.mark.asyncio
async def test_an_unknown_toolchain_runs_nothing(tmp_path: Path) -> None:
    assert await DockerSandbox().run(source=tmp_path, subdir="") is None


@pytest.mark.asyncio
async def test_a_folder_that_escapes_the_repository_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        await DockerSandbox().run(source=tmp_path, subdir="../outside")
