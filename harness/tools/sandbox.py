from pathlib import Path


class SandboxViolation(Exception):
    """Raised when a requested path would escape the sandbox root."""


def resolve_path(root: Path, relative: str) -> Path:
    """Resolve `relative` against `root`, refusing any escape attempt.

    Rejects absolute paths, '..' traversal, and symlinks that point
    outside the root — resolution happens *before* the containment
    check, so there's no window to reason about incorrectly.
    """
    if Path(relative).is_absolute():
        raise SandboxViolation(f"Absolute paths are not allowed: {relative}")

    candidate = (root / relative).resolve()
    root_resolved = root.resolve()

    try:
        candidate.relative_to(root_resolved)
    except ValueError:
        raise SandboxViolation(
            f"Path '{relative}' resolves outside the sandbox root."
        ) from None

    return candidate
