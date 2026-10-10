import shutil
from datetime import datetime
from pathlib import Path

from .sandbox import resolve_path


def create_folder(root: Path, path: str) -> str:
    target = resolve_path(root, path)
    target.mkdir(parents=True, exist_ok=True)
    return f"Created folder: {path}"


def write_file(root: Path, path: str, content: str) -> str:
    """Creates the file if absent, overwrites if present (this is the
    only 'modify' primitive — there's no separate delete-then-recreate
    path that could smuggle in delete semantics)."""
    target = resolve_path(root, path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return f"Wrote {len(content)} bytes to {path}"


def read_file(root: Path, path: str) -> str:
    target = resolve_path(root, path)
    if not target.is_file():
        raise FileNotFoundError(f"No such file: {path}")
    return target.read_text(encoding="utf-8")


def file_exists(root: Path, path: str) -> dict[str, bool | str]:
    target = resolve_path(root, path)
    result: dict[str, bool | str] = {"exists": target.exists()}
    if target.is_file():
        result["type"] = "file"
    elif target.is_dir():
        result["type"] = "directory"
    return result


def get_file_info(root: Path, path: str) -> dict[str, bool | int | str]:
    target = resolve_path(root, path)
    if not target.exists():
        raise FileNotFoundError(f"No such file or directory: {path}")
    stat = target.stat()
    return {
        "path": path,
        "type": "file" if target.is_file() else "directory" if target.is_dir() else "other",
        "size": stat.st_size,
        "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(),
    }


def find_files(root: Path, path: str, pattern: str) -> list[str]:
    target = resolve_path(root, path)
    if not target.is_dir():
        raise NotADirectoryError(f"No such directory: {path}")
    return sorted(
        str(candidate.relative_to(root.resolve()))
        for candidate in target.rglob(pattern)
        if candidate.is_file() or candidate.is_dir()
    )


def append_file(root: Path, path: str, content: str) -> str:
    target = resolve_path(root, path)
    if not target.is_file():
        raise FileNotFoundError(f"No such file: {path}")
    with target.open("a", encoding="utf-8") as file:
        file.write(content)
    return f"Appended {len(content)} bytes to {path}"


def replace_in_file(root: Path, path: str, old_text: str, new_text: str) -> str:
    target = resolve_path(root, path)
    if not target.is_file():
        raise FileNotFoundError(f"No such file: {path}")
    content = target.read_text(encoding="utf-8")
    occurrences = content.count(old_text)
    if occurrences == 0:
        raise ValueError(f"Text not found in {path}")
    if occurrences > 1:
        raise ValueError(f"Text occurs {occurrences} times in {path}; replacement is ambiguous")
    target.write_text(content.replace(old_text, new_text), encoding="utf-8")
    return f"Replaced text in {path}"


def copy_file(root: Path, source: str, destination: str) -> str:
    source_path = resolve_path(root, source)
    destination_path = resolve_path(root, destination)
    if not source_path.is_file():
        raise FileNotFoundError(f"No such source file: {source}")
    if destination_path.exists() and destination_path.is_dir():
        raise IsADirectoryError(f"Destination is a directory: {destination}")
    if not destination_path.parent.is_dir():
        raise FileNotFoundError(f"Destination directory does not exist: {destination}")
    shutil.copyfile(source_path, destination_path)
    return f"Copied {source} to {destination}"


def list_directory(root: Path, path: str = ".") -> list[str]:
    target = resolve_path(root, path)
    if not target.is_dir():
        raise NotADirectoryError(f"No such directory: {path}")
    entries = sorted(target.iterdir(), key=lambda p: p.name)
    return [f"{e.name}/" if e.is_dir() else e.name for e in entries]


def search_in_files(root: Path, path: str, query: str) -> dict[str, list[int]]:
    """Search for `query` (substring match) across text files under `path`.
    Returns {relative_file_path: [1-indexed line numbers]}.
    """
    target = resolve_path(root, path)
    results: dict[str, list[int]] = {}

    search_root = target if target.is_dir() else target.parent
    files = target.rglob("*") if target.is_dir() else [target]

    for f in files:
        if not f.is_file():
            continue
        try:
            lines = f.read_text(encoding="utf-8").splitlines()
        except (UnicodeDecodeError, OSError):
            continue
        hits = [i + 1 for i, line in enumerate(lines) if query in line]
        if hits:
            results[str(f.relative_to(root.resolve()))] = hits

    return results
