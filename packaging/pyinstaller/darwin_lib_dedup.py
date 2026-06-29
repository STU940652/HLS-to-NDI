"""Collapse duplicate versioned/unversioned macOS dylibs in frozen app bundles."""
from __future__ import annotations

import re
import sys
from pathlib import Path


def darwin_dylib_stem(filename: str) -> str:
    name = filename[:-6] if filename.endswith(".dylib") else filename
    while True:
        match = re.match(r"^(.+)-\d+(?:\.\d+)*$", name)
        if not match:
            break
        name = match.group(1)
    return name


def version_tuple_from_dylib(filename: str) -> tuple[int, ...]:
    name = filename[:-6] if filename.endswith(".dylib") else filename
    match = re.search(r"-(\d+(?:\.\d+)*)$", name)
    if not match:
        return ()
    return tuple(int(part) for part in match.group(1).split("."))


def pick_canonical_dylib(names: list[str]) -> str:
    return max(names, key=lambda name: (version_tuple_from_dylib(name), len(name)))


def _lib_dirs_in_bundle(bundle_path: Path) -> list[Path]:
    if sys.platform != "darwin":
        return []
    if bundle_path.suffix == ".app":
        frameworks = bundle_path / "Contents" / "Frameworks"
    else:
        frameworks = bundle_path
    if not frameworks.is_dir():
        return []
    lib_dirs: list[Path] = []
    for package_root in sorted(frameworks.iterdir()):
        if not package_root.is_dir():
            continue
        lib_dir = package_root / "lib"
        if lib_dir.is_dir():
            lib_dirs.append(lib_dir)
    return lib_dirs


def deduplicate_lib_dir(lib_dir: Path) -> list[str]:
    """Keep one real dylib per logical library; replace extras with symlinks."""
    groups: dict[str, list[Path]] = {}
    for path in sorted(lib_dir.glob("*.dylib")):
        groups.setdefault(darwin_dylib_stem(path.name), []).append(path)

    actions: list[str] = []
    for stem, paths in sorted(groups.items()):
        if len(paths) <= 1:
            continue

        real_files = [path for path in paths if not path.is_symlink()]
        if len(real_files) <= 1:
            canonical_name = real_files[0].name if real_files else pick_canonical_dylib([p.name for p in paths])
            canonical = lib_dir / canonical_name
            for path in paths:
                if path.name == canonical_name:
                    continue
                if path.is_symlink() and path.resolve() == canonical.resolve():
                    continue
                path.unlink(missing_ok=True)
                alias = lib_dir / path.name
                if not alias.exists():
                    alias.symlink_to(canonical_name)
                    actions.append(f"{lib_dir.name}/{path.name} -> {canonical_name}")
            continue

        canonical_name = pick_canonical_dylib([path.name for path in real_files])
        canonical = lib_dir / canonical_name
        for path in real_files:
            if path.name == canonical_name:
                continue
            alias_name = path.name
            path.unlink()
            alias = lib_dir / alias_name
            alias.unlink(missing_ok=True)
            alias.symlink_to(canonical_name)
            actions.append(f"{lib_dir.name}/{alias_name} -> {canonical_name}")

        for path in paths:
            if path.is_symlink() and not path.exists():
                path.unlink(missing_ok=True)
                alias = lib_dir / path.name
                if not alias.exists():
                    alias.symlink_to(canonical_name)
                    actions.append(f"{lib_dir.name}/{path.name} -> {canonical_name}")

    return actions


def find_duplicate_real_dylibs(bundle_path: Path) -> list[str]:
    problems: list[str] = []
    for lib_dir in _lib_dirs_in_bundle(bundle_path):
        groups: dict[str, list[str]] = {}
        for path in lib_dir.glob("*.dylib"):
            if path.is_symlink():
                continue
            groups.setdefault(darwin_dylib_stem(path.name), []).append(path.name)
        for stem, names in sorted(groups.items()):
            if len(names) > 1:
                rel = lib_dir.relative_to(bundle_path)
                problems.append(f"{rel}: {stem} -> {', '.join(sorted(names))}")
    return problems


def fix_bundle(bundle_path: Path) -> list[str]:
    actions: list[str] = []
    for lib_dir in _lib_dirs_in_bundle(bundle_path):
        actions.extend(deduplicate_lib_dir(lib_dir))
    return actions


def main() -> int:
    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} <path-to-.app>", file=sys.stderr)
        return 2
    if sys.platform != "darwin":
        print("darwin_lib_dedup.py is macOS-only", file=sys.stderr)
        return 2

    bundle_path = Path(sys.argv[1]).resolve()
    if not bundle_path.exists():
        raise SystemExit(f"Bundle path not found: {bundle_path}")

    actions = fix_bundle(bundle_path)
    problems = find_duplicate_real_dylibs(bundle_path)
    if problems:
        raise SystemExit(
            "Duplicate real dylibs remain after normalization:\n" + "\n".join(problems)
        )

    if actions:
        print("Normalized duplicate dylibs:")
        for action in actions:
            print(f"  {action}")
    else:
        print("No duplicate dylibs found.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
