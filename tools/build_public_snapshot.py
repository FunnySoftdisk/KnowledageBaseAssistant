"""Build the allowlisted public engineering snapshot without private Git history."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]

EXACT_FILES = frozenset(
    {
        ".gitattributes",
        ".gitignore",
        "PUBLIC_EXPORT.md",
        "server/pyproject.toml",
        "server/uv.lock",
        "tools/build_public_snapshot.py",
        "tools/core_contract_exports.py",
        "tools/test_core_contract_exports.py",
        "tools/test_public_snapshot.py",
    }
)
PREFIXES = (
    "contracts/core/",
    "server/src/",
    "server/tests/",
)
REMAPPED_FILES = {
    "public/PUBLIC_EXPORT.md": "PUBLIC_EXPORT.md",
}

# Assemble credential markers so the scanner source does not match itself.
SECRET_PATTERNS = (
    re.compile(("github" + r"_pat_[A-Za-z0-9_]{20,}").encode()),
    re.compile(("gh" + r"p_[A-Za-z0-9]{20,}").encode()),
    re.compile((r"(?<![A-Za-z0-9])" + "sk" + r"-[A-Za-z0-9_-]{16,}").encode()),
    re.compile(("BEGIN " + r"(?:RSA |EC |OPENSSH )?PRIVATE KEY").encode()),
)


class PublicExportError(RuntimeError):
    """The snapshot cannot be proven to satisfy the public export policy."""


def tracked_files() -> list[str]:
    result = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=PROJECT_ROOT,
        check=True,
        capture_output=True,
    )
    return sorted(path.decode("utf-8") for path in result.stdout.split(b"\0") if path)


def destination_for(source: str) -> str | None:
    if source in REMAPPED_FILES:
        return REMAPPED_FILES[source]
    if source in EXACT_FILES or source.startswith(PREFIXES):
        return source
    return None


def validate_output_directory(output: Path) -> None:
    resolved = output.resolve()
    if resolved == PROJECT_ROOT or PROJECT_ROOT in resolved.parents:
        raise PublicExportError("OUTPUT_INSIDE_PRIVATE_PROJECT_FORBIDDEN")
    if output.exists() and (not output.is_dir() or any(output.iterdir())):
        raise PublicExportError("OUTPUT_MUST_BE_ABSENT_OR_EMPTY_DIRECTORY")


def scan_file(path: Path) -> None:
    body = path.read_bytes()
    if any(pattern.search(body) for pattern in SECRET_PATTERNS):
        raise PublicExportError(f"SECRET_PATTERN_DETECTED:{path.name}")


def build_snapshot(output: Path) -> dict[str, object]:
    validate_output_directory(output)
    selected: list[tuple[str, str]] = []
    tracked = tracked_files()
    uses_private_templates = all(source in tracked for source in REMAPPED_FILES)
    for source in tracked:
        if uses_private_templates and source in REMAPPED_FILES.values():
            continue
        destination = destination_for(source)
        if destination is not None:
            selected.append((source, destination))

    if not selected:
        raise PublicExportError("PUBLIC_ALLOWLIST_EMPTY")
    destinations = [destination for _, destination in selected]
    if len(destinations) != len(set(destinations)):
        raise PublicExportError("PUBLIC_DESTINATION_COLLISION")

    output.mkdir(parents=True, exist_ok=True)
    for source_name, destination_name in selected:
        source_path = PROJECT_ROOT / source_name
        if source_path.is_symlink() or not source_path.is_file():
            raise PublicExportError(f"PUBLIC_SOURCE_NOT_REGULAR_FILE:{source_name}")
        destination_path = output / destination_name
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source_path, destination_path)
        scan_file(destination_path)

    return {
        "status": "PASS",
        "file_count": len(selected),
        "source_scope": "TRACKED_ALLOWLIST_ONLY",
        "private_git_history_copied": False,
        "output": str(output.resolve()),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        report = build_snapshot(args.output)
    except (OSError, subprocess.CalledProcessError, PublicExportError) as exc:
        print(json.dumps({"status": "FAIL", "error": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
