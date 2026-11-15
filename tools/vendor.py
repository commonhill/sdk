"""Vendoring the sdk into a project or a template (PER-478): the grader SDK, as
`grader/arh_grader`, and the kits a project uses: the instruction-count kit (its marker
`grader/native/insncap.c`) and the cycles kit (`grader/native/valgrind.patch`).

    python3 tools/vendor.py copy [--kit KIT]... DIR...
        the current SDK release into each DIR's grader/arh_grader, and each kit named or found
        by its marker in DIR
    python3 tools/vendor.py copy --kits-only [--kit KIT]... DIR...
        the kits alone (a project keeps the SDK release it pins until its next version)
    python3 tools/vendor.py check --release N [--kit KIT]... DIR...
        each DIR's copy is exactly release N of the SDK, and it has exactly the kits named, each
        the sdk's byte for byte; exit 1 saying what differs

`check` is told what DIR should have rather than reading it from DIR, so a copy deleted, or a
kit's marker deleted with it, fails it too: a project's CI states its release and kits there,
running the sdk at any tag from the one it pins on (README.md, "Releases"). An earlier release is
checked by its digest (`RELEASES`), the current one (`grader/src/arh_grader`) file by file
(grader/README.md, "Releases"). A vendored copy is a frozen file of its project's reward
versions, so `copy` is for a template, a new project, or a project's next reward version.
"""

import argparse
import hashlib
import re
import shutil
import sys
from collections.abc import Collection
from pathlib import Path

SDK_ROOT = Path(__file__).resolve().parents[1]
SDK = SDK_ROOT / "grader" / "src" / "arh_grader"
VENDORED = Path("grader") / "arh_grader"
# Each kit: its files (laid out as in a project) and the file that says a project uses it.
KITS = {
    "insncap": (SDK_ROOT / "kits" / "insncap" / "project", Path("grader") / "native" / "insncap.c"),
    "cycles": (SDK_ROOT / "kits" / "cycles" / "project", Path("grader") / "native" / "valgrind.patch"),
}
# The earlier releases a copy may pin, by digest (`digest`); the current one is `SDK` itself.
RELEASES = {
    4: "c9e652634d85166ee1e6cb13b423ac6afe222d8a9e62fe3727198bc952a39b66",
    5: "8599504601d35f65dd99621a3357230ab4cfc5ee6d0118f8fa5ded38512cdc3f",
    6: "cd25b625c8a4862158fbc022588e2f70f9d986f5d87f18f53d75de6605f9efb4",
}
RELEASE_LINE = re.compile(r"^RELEASE = (\d+)$", re.MULTILINE)


def files(root: Path) -> dict[str, bytes]:
    """`root`'s files by their path under it, `__pycache__` left out."""
    return {
        p.relative_to(root).as_posix(): p.read_bytes()
        for p in root.rglob("*")
        if p.is_file() and "__pycache__" not in p.parts
    }


def digest(root: Path) -> str:
    """A release's digest: what `(cd root && find . -type f -not -path '*/__pycache__/*' -printf
    '%P\\n' | LC_ALL=C sort | xargs sha256sum) | sha256sum` prints."""
    listing = "".join(
        f"{hashlib.sha256(data).hexdigest()}  {name}\n" for name, data in sorted(files(root).items())
    )
    return hashlib.sha256(listing.encode()).hexdigest()


def release(sdk: Path) -> int | None:
    """The release an SDK tree says it is (its `__init__.py`'s `RELEASE`)."""
    init = sdk / "__init__.py"
    found = RELEASE_LINE.search(init.read_text()) if init.is_file() else None
    return int(found.group(1)) if found else None


CURRENT = release(SDK)


def kits_of(directory: Path) -> list[str]:
    """The kits whose markers `directory` has."""
    return [name for name, (_, marker) in KITS.items() if (directory / marker).is_file()]


def problems(directory: Path, pinned: int, kits: Collection[str]) -> list[str]:
    """How `directory`'s vendored copies differ from release `pinned` of the SDK and exactly the
    kits `kits`; nothing when they don't."""
    found: list[str] = []
    vendored = directory / VENDORED
    if not vendored.is_dir():
        found.append(f"no {VENDORED} (release {pinned} expected)")
    elif any(p.is_symlink() for p in vendored.rglob("*")):
        found.append(f"{VENDORED} holds a symlink")
    elif (said := release(vendored)) is None:
        found.append(f"{VENDORED}/__init__.py says no RELEASE (release {pinned} expected)")
    elif said != pinned:
        found.append(f"{VENDORED} says release {said}, not {pinned}")
    elif pinned == CURRENT:
        theirs, ours = files(vendored), files(SDK)
        changed = sorted(name for name in theirs.keys() | ours.keys() if theirs.get(name) != ours.get(name))
        found += [f"{VENDORED}/{name} isn't release {pinned}'s" for name in changed]
    elif pinned not in RELEASES:
        found.append(f"release {pinned} is one the sdk doesn't know")
    elif digest(vendored) != RELEASES[pinned]:
        found.append(f"{VENDORED} isn't release {pinned} (its digest differs)")
    for kit in sorted(set(kits_of(directory)) - set(kits)):
        found.append(f"{KITS[kit][1]} says it uses the {kit} kit, which it shouldn't")
    for kit in kits:
        for name, data in sorted(files(KITS[kit][0]).items()):
            path = directory / name
            if not path.is_file():
                found.append(f"no {name} (the {kit} kit's)")
            elif path.read_bytes() != data:
                found.append(f"{name} isn't the {kit} kit's")
    return found


def copy(directory: Path, kits_only: bool = False, kits: Collection[str] = ()) -> None:
    """The current SDK release (unless `kits_only`), and the kits `kits` names and those whose
    markers `directory` has, into it."""
    if not kits_only:
        vendored = directory / VENDORED
        if vendored.exists():
            shutil.rmtree(vendored)
        shutil.copytree(SDK, vendored, ignore=shutil.ignore_patterns("__pycache__"))
    for kit in sorted({*kits, *kits_of(directory)}):
        shutil.copytree(
            KITS[kit][0], directory, dirs_exist_ok=True, ignore=shutil.ignore_patterns("__pycache__")
        )


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="vendor.py", description="Vendor the grader SDK and the sdk's kits, or check a copy."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    copying = commands.add_parser("copy", help="vendor the current SDK release and the kits each DIR uses")
    copying.add_argument("--kits-only", action="store_true", help="the kits alone, not the SDK")
    copying.add_argument("--kit", action="append", choices=sorted(KITS), default=[], help="a kit to vendor")
    copying.add_argument("directories", nargs="+", type=Path, metavar="DIR")
    checking = commands.add_parser(
        "check", help="check each DIR's vendored copies against what it should have"
    )
    checking.add_argument("--release", type=int, required=True, help="the SDK release DIR should pin")
    checking.add_argument(
        "--kit", action="append", choices=sorted(KITS), default=[], help="a kit DIR should vendor"
    )
    checking.add_argument("directories", nargs="+", type=Path, metavar="DIR")
    args = parser.parse_args(argv)
    directories: list[Path] = args.directories
    kits: list[str] = args.kit
    if args.command == "copy":
        for directory in directories:
            copy(directory, args.kits_only, kits)
        return 0
    failed = False
    for directory in directories:
        for problem in problems(directory, args.release, kits):
            print(f"{directory}: {problem}", file=sys.stderr)
            failed = True
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
