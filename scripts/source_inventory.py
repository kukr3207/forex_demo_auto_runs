#!/usr/bin/env python3
"""Audit first-party source without counting dependencies or generated files."""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterator, Mapping, Optional, Sequence, Tuple


LANGUAGES: Mapping[str, str] = {
    ".py": "Python",
    ".js": "JavaScript",
    ".ts": "TypeScript",
    ".css": "CSS",
    ".html": "HTML",
    ".sh": "Shell",
}

EXCLUDED_DIRECTORIES = frozenset(
    {
        ".git",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        ".venv",
        "__pycache__",
        "build",
        "coverage",
        "dist",
        "htmlcov",
        "node_modules",
        "site-packages",
        "vendor",
    }
)


@dataclass(frozen=True)
class FileCount:
    path: str
    language: str
    physical: int
    substantive: int
    blank: int
    comments: int

    def as_dict(self) -> Dict[str, object]:
        return {
            "path": self.path,
            "language": self.language,
            "physical": self.physical,
            "substantive": self.substantive,
            "blank": self.blank,
            "comments": self.comments,
        }


@dataclass(frozen=True)
class Inventory:
    root: str
    files: Tuple[FileCount, ...]

    @property
    def physical(self) -> int:
        return sum(item.physical for item in self.files)

    @property
    def substantive(self) -> int:
        return sum(item.substantive for item in self.files)

    @property
    def by_language(self) -> Dict[str, Dict[str, int]]:
        result: Dict[str, Dict[str, int]] = {}
        for item in self.files:
            values = result.setdefault(
                item.language,
                {"files": 0, "physical": 0, "substantive": 0, "blank": 0, "comments": 0},
            )
            values["files"] += 1
            values["physical"] += item.physical
            values["substantive"] += item.substantive
            values["blank"] += item.blank
            values["comments"] += item.comments
        return result

    def as_dict(self, include_files: bool = False) -> Dict[str, object]:
        value: Dict[str, object] = {
            "root": self.root,
            "fileCount": len(self.files),
            "physical": self.physical,
            "substantive": self.substantive,
            "languages": self.by_language,
        }
        if include_files:
            value["files"] = [item.as_dict() for item in self.files]
        return value


def source_files(root: Path) -> Iterator[Path]:
    resolved = root.resolve()
    if not resolved.is_dir():
        raise ValueError("source root must be a directory")
    for path in sorted(resolved.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in LANGUAGES:
            continue
        relative = path.relative_to(resolved)
        if any(part in EXCLUDED_DIRECTORIES for part in relative.parts[:-1]):
            continue
        if path.name.endswith((".min.js", ".min.css")):
            continue
        yield path


def line_kind(line: str, suffix: str, state: Dict[str, object]) -> str:
    stripped = line.strip()
    if not stripped:
        return "blank"
    if suffix == ".py":
        delimiter = state.get("python_docstring")
        if delimiter:
            if stripped.count(str(delimiter)) % 2:
                state.pop("python_docstring", None)
            return "comments"
        if stripped.startswith("#"):
            return "comments"
        for candidate in ('"""', "'''"):
            if stripped.startswith(candidate):
                if stripped.count(candidate) == 1:
                    state["python_docstring"] = candidate
                return "comments"
        return "substantive"
    if suffix in {".js", ".ts", ".css"}:
        if state.get("block_comment"):
            if "*/" in stripped:
                state.pop("block_comment", None)
                if stripped.split("*/", 1)[1].strip():
                    return "substantive"
            return "comments"
        if stripped.startswith("//"):
            return "comments"
        if stripped.startswith("/*") or stripped.startswith("*"):
            if "*/" not in stripped:
                state["block_comment"] = True
            return "comments"
        return "substantive"
    if suffix == ".html":
        if state.get("html_comment"):
            if "-->" in stripped:
                state.pop("html_comment", None)
                if stripped.split("-->", 1)[1].strip():
                    return "substantive"
            return "comments"
        if stripped.startswith("<!--"):
            if "-->" not in stripped:
                state["html_comment"] = True
            return "comments"
        return "substantive"
    if suffix == ".sh" and stripped.startswith("#") and not stripped.startswith("#!"):
        return "comments"
    return "substantive"


def inspect(path: Path, root: Path) -> FileCount:
    lines = path.read_text(encoding="utf-8").splitlines()
    counts = {"blank": 0, "comments": 0, "substantive": 0}
    state: Dict[str, object] = {}
    for line in lines:
        counts[line_kind(line, path.suffix.lower(), state)] += 1
    return FileCount(
        path.relative_to(root).as_posix(),
        LANGUAGES[path.suffix.lower()],
        len(lines),
        counts["substantive"],
        counts["blank"],
        counts["comments"],
    )


def inventory(root: Path) -> Inventory:
    resolved = root.resolve()
    return Inventory(str(resolved), tuple(inspect(path, resolved) for path in source_files(resolved)))


def render(result: Inventory) -> str:
    rows = [("Language", "Files", "Physical", "Substantive", "Blank", "Comments")]
    for language, values in sorted(result.by_language.items()):
        rows.append(
            (
                language,
                str(values["files"]),
                str(values["physical"]),
                str(values["substantive"]),
                str(values["blank"]),
                str(values["comments"]),
            )
        )
    rows.append(("TOTAL", str(len(result.files)), str(result.physical), str(result.substantive), "", ""))
    widths = [max(len(row[index]) for row in rows) for index in range(6)]
    return "\n".join(
        "  ".join(value.ljust(widths[index]) for index, value in enumerate(row)).rstrip()
        for row in rows
    )


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description="Audit first-party source lines")
    value.add_argument("root", nargs="?", default=".", type=Path)
    value.add_argument("--minimum", type=int)
    value.add_argument("--json", action="store_true")
    value.add_argument("--files", action="store_true")
    return value


def main(arguments: Optional[Sequence[str]] = None) -> int:
    options = parser().parse_args(arguments)
    try:
        result = inventory(options.root)
    except (OSError, ValueError) as error:
        print("inventory error: %s" % error)
        return 2
    if options.json:
        print(json.dumps(result.as_dict(options.files), indent=2, sort_keys=True))
    else:
        print(render(result))
    if options.minimum is not None and result.substantive < options.minimum:
        print("Substantive source is below the required minimum of %d." % options.minimum)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
