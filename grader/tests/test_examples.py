"""The two example graders against their example workers, end to end in this process."""

import importlib.util
import json
import shlex
import socket
import subprocess
import sys
import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

EXAMPLES = Path(__file__).parents[1] / "examples"


def load(example: str, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(f"{example}_{name}", EXAMPLES / example / f"{name}.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def worker(monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[[str], ModuleType]]:
    """Start an example's worker on a free port and point WORKER_URL at it; its module."""
    servers: list[Any] = []

    def start(example: str) -> ModuleType:
        module = load(example, "worker")
        server = module.serve(0)
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        monkeypatch.setenv("WORKER_URL", f"http://127.0.0.1:{server.server_address[1]}")
        return module

    yield start
    for server in servers:
        server.shutdown()
        server.server_close()


def test_the_accuracy_example_scores_labels(worker: Callable[[str], None], tmp_path: Path) -> None:
    worker("accuracy")
    result = tmp_path / "result.json"
    assert load("accuracy", "grade").main(EXAMPLES / "accuracy" / "labels.jsonl", result) == 0
    written = json.loads(result.read_text())
    assert written["primary"] == {"name": "accuracy", "value": 17 / 20}
    assert (written["n_items"], written["failures"], written["secondary"]) == (20, 0, {"wrong": 3.0})


def test_the_accuracy_example_invalidates_a_broken_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scripted_http: Any, worker_scripts: Any
) -> None:
    """A worker that answers every other batch with garbage: half the items fail, over the 5%
    the author allows, so the run is invalid rather than merely inaccurate."""
    seen: list[int] = []

    def script(request: bytes) -> list[tuple[float, bytes]]:
        if request.startswith(b"GET /healthz "):
            return [(0, worker_scripts.answer(b"200 OK", b"{}"))]
        seen.append(1)
        if len(seen) % 2:
            return [(0, b"garbage\r\n\r\n")]
        items = json.loads(request.partition(b"\r\n\r\n")[2])["items"]
        return [
            (0, worker_scripts.answer(b"200 OK", json.dumps({"outputs": ["positive"] * len(items)}).encode()))
        ]

    monkeypatch.setenv("WORKER_URL", scripted_http(script).url)
    labels = tmp_path / "labels.jsonl"
    labels.write_text("".join(json.dumps({"input": f"t{i}", "label": "positive"}) + "\n" for i in range(40)))
    result = tmp_path / "result.json"
    assert load("accuracy", "grade").main(labels, result) == 0
    written = json.loads(result.read_text())
    assert (written["primary"]["value"], written["n_items"], written["failures"]) == (None, 40, 20)
    assert written["failure_kinds"] == {"connection": 20}  # garbage instead of HTTP: the batches' items


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def binary_search_probes(hidden: int, bits: int) -> int:
    low, high, probes = 0, 2**bits - 1, 0
    while low < high:
        probe, probes = (low + high) // 2, probes + 1
        if probe == hidden:
            break
        low, high = (probe + 1, high) if hidden > probe else (low, probe - 1)
    return probes


def test_the_oracle_example_scores_the_probes_the_grader_counted(
    worker: Callable[[str], ModuleType], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The worker's binary search, counted by the grader's oracle: exactly the probes it needs for
    each hidden number, which only the grader knows (drawn from the public seed here)."""
    port = free_port()
    monkeypatch.setenv("AR_ORACLE_URL", f"http://127.0.0.1:{port}")
    worker("oracle")
    grade = load("oracle", "grade")
    result = tmp_path / "result.json"
    assert grade.main(result, port) == 0
    numbers = grade.rng("numbers")
    expected = [
        binary_search_probes(numbers.randrange(2**grade.BITS), grade.BITS) for _ in range(grade.N_ITEMS)
    ]
    written = json.loads(result.read_text())
    assert (written["n_items"], written["failures"], written["failure_kinds"]) == (50, 0, {})
    assert written["primary"] == {"name": "probes", "value": sum(expected) / 50}
    assert json.loads((tmp_path / "items.json").read_text())["item_scores"] == expected
    assert written["secondary"]["query_bytes"] > 0
    assert written["secondary"]["answer_bytes"] > 0


def test_the_oracle_example_invalidates_a_worker_that_skips_its_calls(
    worker: Callable[[str], ModuleType], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A worker can't lower the count by not asking: without the oracle's answers it can only
    guess, the grader finds the guesses wrong, and the run has no value."""
    port = free_port()
    monkeypatch.setenv("AR_ORACLE_URL", f"http://127.0.0.1:{port}")

    def guess(item: dict[str, int]) -> int:  # the middle of the range, never asking
        return 2 ** (item["bits"] - 1)

    monkeypatch.setattr(worker("oracle"), "find", guess)
    result = tmp_path / "result.json"
    assert load("oracle", "grade").main(result, port) == 0
    written = json.loads(result.read_text())
    assert (written["primary"]["value"], written["failures"], written["failure_kinds"]) == (
        None,
        50,
        {"wrong": 50},
    )
    assert written["secondary"] == {"query_bytes": 0.0, "answer_bytes": 0.0}


def test_the_readmes_accuracy_commands_run_as_written(tmp_path: Path) -> None:
    """The author README's example block, run by bash with a free port, this test's result path
    and this interpreter: the worker starts in the background, the grader writes a valid result."""
    readme = (EXAMPLES.parent / "README.md").read_text()
    block = readme.split("## Examples", 1)[1].split("```sh\n", 1)[1].split("```", 1)[0]
    assert block.splitlines()[0] == "cd grader/examples/accuracy"
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = str(sock.getsockname()[1])
    script = (
        block.replace("8080", port)
        .replace("/tmp/result.json", str(tmp_path / "result.json"))
        .replace("uv run python", shlex.quote(sys.executable))
        .replace("python worker.py", f"{shlex.quote(sys.executable)} worker.py")
    )
    done = subprocess.run(
        ["bash", "-c", f"set -e\n{script}\nkill %1"],
        cwd=EXAMPLES.parents[1], capture_output=True, text=True, timeout=120, check=False,
    )  # fmt: skip
    assert done.returncode == 0, done.stderr
    written = json.loads((tmp_path / "result.json").read_text())
    assert (written["n_items"], written["failures"]) == (20, 0)
