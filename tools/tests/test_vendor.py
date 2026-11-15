"""tools/vendor.py: `copy` writes what `check` accepts, and `check` names each way a copy can
differ, a deleted copy or kit marker included. (The templates and the projects run `check` on
their own copies in their CI, each naming its release and kits.)"""

import importlib.util
import shutil
import subprocess
from pathlib import Path
from types import ModuleType

import pytest

REPO = Path(__file__).parents[2]


def _vendor() -> ModuleType:
    spec = importlib.util.spec_from_file_location("sdk_vendor", REPO / "tools" / "vendor.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


vendor = _vendor()
CURRENT: int = vendor.CURRENT


def test_the_sdk_knows_its_current_release_and_its_kits() -> None:
    assert CURRENT not in vendor.RELEASES
    assert all(earlier < CURRENT for earlier in vendor.RELEASES)
    for source, marker in vendor.KITS.values():
        assert (source / marker).is_file()


@pytest.mark.parametrize(
    ("drift", "said"),
    [
        ("grader/native/insncap.c", "no grader/native/insncap.c (the insncap kit's)"),
        ("grader/arh_grader", f"no grader/arh_grader (release {CURRENT} expected)"),
    ],
)
def test_a_deleted_copy_or_kit_marker_fails_the_check(tmp_path: Path, drift: str, said: str) -> None:
    """What a project should have is given, so deleting its copy, or a kit's marker, is a
    difference too."""
    root = tmp_path / "project"
    vendor.copy(root, kits=("insncap",))
    assert vendor.problems(root, CURRENT, ("insncap",)) == []
    path = root / drift
    if path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink()
    assert said in vendor.problems(root, CURRENT, ("insncap",))


def project(tmp_path: Path, kit: str | None = None) -> Path:
    """A new project's directory, using `kit` if given (its marker file, an old copy)."""
    root = tmp_path / "project"
    (root / "grader" / "native").mkdir(parents=True)
    if kit is not None:
        (root / vendor.KITS[kit][1]).write_text("an old copy\n")
    return root


@pytest.mark.parametrize("kit", [None, "insncap", "cycles"])
def test_copy_writes_what_check_accepts(tmp_path: Path, kit: str | None) -> None:
    root = project(tmp_path)
    kits = () if kit is None else (kit,)
    assert vendor.problems(root, CURRENT, kits)[0] == f"no grader/arh_grader (release {CURRENT} expected)"

    assert vendor.main(["copy", *(f"--kit={k}" for k in kits), str(root)]) == 0

    assert vendor.main(["check", f"--release={CURRENT}", *(f"--kit={k}" for k in kits), str(root)]) == 0
    assert vendor.files(root / "grader" / "arh_grader") == vendor.files(vendor.SDK)


def test_copy_finds_the_kits_by_their_markers_too(tmp_path: Path) -> None:
    root = project(tmp_path, "insncap")
    vendor.copy(root)
    assert vendor.problems(root, CURRENT, ("insncap",)) == []


def test_copy_of_the_kits_alone_leaves_the_sdk_the_project_pins(tmp_path: Path) -> None:
    root = project(tmp_path, "cycles")
    pinned = root / "grader" / "arh_grader"
    pinned.mkdir()
    (pinned / "__init__.py").write_text("RELEASE = 4\n")

    assert vendor.main(["copy", "--kits-only", str(root)]) == 0

    assert vendor.files(pinned) == {"__init__.py": b"RELEASE = 4\n"}
    sandbox = root / "grader" / "sandbox.py"
    assert sandbox.read_bytes() == (vendor.KITS["cycles"][0] / "grader" / "sandbox.py").read_bytes()


def test_check_names_each_difference(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = project(tmp_path, "insncap")
    vendor.copy(root)
    sdk = root / "grader" / "arh_grader"
    (sdk / "client.py").write_text((sdk / "client.py").read_text() + "# mine\n")
    (sdk / "extra.py").write_text("")
    (root / "grader" / "native" / "qemu.patch").write_text("another patch\n")
    (root / "grader" / "native" / "valgrind.patch").write_text("")

    assert vendor.main(["check", f"--release={CURRENT}", "--kit=insncap", str(root)]) == 1

    assert capsys.readouterr().err.splitlines() == [
        f"{root}: grader/arh_grader/client.py isn't release {CURRENT}'s",
        f"{root}: grader/arh_grader/extra.py isn't release {CURRENT}'s",
        f"{root}: grader/native/valgrind.patch says it uses the cycles kit, which it shouldn't",
        f"{root}: grader/native/qemu.patch isn't the insncap kit's",
    ]


def test_a_copy_of_another_release_than_the_one_expected_fails(tmp_path: Path) -> None:
    root = project(tmp_path)
    vendor.copy(root)
    assert vendor.problems(root, 4, ()) == [f"grader/arh_grader says release {CURRENT}, not 4"]


def test_an_earlier_release_is_checked_by_its_digest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = project(tmp_path)
    sdk = root / "grader" / "arh_grader"
    sdk.mkdir()
    (sdk / "__init__.py").write_text('"""An earlier release."""\n\nRELEASE = 3\n')
    monkeypatch.setitem(vendor.RELEASES, 3, vendor.digest(sdk))
    assert vendor.problems(root, 3, ()) == []

    (sdk / "seed.py").write_text("")
    assert vendor.problems(root, 3, ()) == ["grader/arh_grader isn't release 3 (its digest differs)"]


def test_a_release_the_sdk_doesnt_know_or_none_at_all_is_refused(tmp_path: Path) -> None:
    root = project(tmp_path)
    sdk = root / "grader" / "arh_grader"
    sdk.mkdir()
    (sdk / "__init__.py").write_text("RELEASE = 2\n")
    assert vendor.problems(root, 2, ()) == ["release 2 is one the sdk doesn't know"]
    (sdk / "__init__.py").write_text("")
    assert vendor.problems(root, 2, ()) == [
        "grader/arh_grader/__init__.py says no RELEASE (release 2 expected)"
    ]


def test_a_symlink_in_a_copy_is_refused(tmp_path: Path) -> None:
    root = project(tmp_path)
    vendor.copy(root)
    (root / "grader" / "arh_grader" / "elsewhere").symlink_to(tmp_path)
    assert vendor.problems(root, CURRENT, ()) == ["grader/arh_grader holds a symlink"]


def test_the_digest_is_what_the_readmes_command_prints(tmp_path: Path) -> None:
    """grader/README.md, "Releases"."""
    (tmp_path / "b").mkdir()
    (tmp_path / "b" / "y.py").write_bytes(b"y\n")
    (tmp_path / "a.py").write_bytes(b"a\n")
    (tmp_path / "B.py").write_bytes(b"B\n")
    (tmp_path / "b" / "__pycache__").mkdir()
    (tmp_path / "b" / "__pycache__" / "y.pyc").write_bytes(b"ignored")
    command = (
        "(find . -type f -not -path '*/__pycache__/*' -printf '%P\\n' | LC_ALL=C sort | xargs sha256sum)"
        " | sha256sum"
    )
    printed = subprocess.run(command, shell=True, cwd=tmp_path, check=True, capture_output=True, text=True)
    assert printed.stdout.split()[0] == vendor.digest(tmp_path)
