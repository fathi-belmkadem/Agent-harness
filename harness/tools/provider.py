"""Tool providers: the sources a registry merges tools from.

A provider owns a namespace and returns its tools on `load()`. The built-in
filesystem provider is one; the memory, session, skill, MCP and sub-agent
providers follow the same two-member protocol.
"""
from __future__ import annotations

from pathlib import Path
from typing import Protocol

from . import filesystem as fs
from . import pdf as pdf_export
from .registry import Tool, tool


class ToolProvider(Protocol):
    """A source of tools."""

    @property
    def namespace(self) -> str:
        """Short identifier, used to label tools and their metrics."""
        ...

    def load(self) -> list[Tool]:
        """Return this provider's current tools. Safe to call repeatedly."""
        ...


def _str(description: str) -> dict:
    return {"type": "string", "description": description}


class FilesystemProvider:
    """The built-in sandboxed filesystem tools.

    The names are not namespaced: they are the harness's own contract with the
    model. There is deliberately no delete tool.
    """

    def __init__(self, sandbox_root: Path) -> None:
        self._root = sandbox_root

    @property
    def namespace(self) -> str:
        return "builtin"

    def load(self) -> list[Tool]:
        root = self._root
        path = _str("Relative path inside the workspace.")
        content = _str("Full text content.")
        return [
            tool("create_folder", "Create a folder (and any missing parents) inside the workspace.",
                 lambda path: fs.create_folder(root, path), {"path": path}, ["path"]),
            tool("write_file", "Create or overwrite a file with the given text content inside the workspace.",
                 lambda path, content: fs.write_file(root, path, content),
                 {"path": path, "content": content}, ["path", "content"]),
            tool("read_file", "Read and return the full text content of a file inside the workspace.",
                 lambda path: fs.read_file(root, path), {"path": path}, ["path"]),
            tool("file_exists", "Check whether a file or directory exists inside the workspace.",
                 lambda path: fs.file_exists(root, path), {"path": path}, ["path"]),
            tool("get_file_info", "Return metadata for a file or directory inside the workspace.",
                 lambda path: fs.get_file_info(root, path), {"path": path}, ["path"]),
            tool("find_files", "Find files and directories by filename pattern inside the workspace.",
                 lambda path, pattern: fs.find_files(root, path, pattern),
                 {"path": _str("Relative directory to search under."),
                  "pattern": _str("Filename pattern such as '*.py'.")}, ["path", "pattern"]),
            tool("append_file", "Append text to an existing file inside the workspace.",
                 lambda path, content: fs.append_file(root, path, content),
                 {"path": path, "content": _str("Text to append.")}, ["path", "content"]),
            tool("replace_in_file", "Replace exactly one occurrence of text in an existing workspace file.",
                 lambda path, old_text, new_text: fs.replace_in_file(root, path, old_text, new_text),
                 {"path": path, "old_text": _str("Text to replace."), "new_text": _str("Replacement text.")},
                 ["path", "old_text", "new_text"]),
            tool("copy_file", "Copy a file to another location inside the workspace.",
                 lambda source, destination: fs.copy_file(root, source, destination),
                 {"source": _str("Relative source file path."),
                  "destination": _str("Relative destination file path.")}, ["source", "destination"]),
            tool("list_directory", "List the immediate contents of a directory inside the workspace "
                 "(dirs shown with trailing '/').",
                 lambda path=".": fs.list_directory(root, path),
                 {"path": _str("Relative path of the directory. Defaults to workspace root.")}),
            tool("search_in_files", "Search for a text query across files under a path inside the "
                 "workspace; returns matching file paths and line numbers.",
                 lambda path, query: fs.search_in_files(root, path, query),
                 {"path": _str("Relative path (file or directory) to search under."),
                  "query": _str("Substring to search for.")}, ["path", "query"]),
            tool("export_pdf", "Render text into a downloadable PDF file inside the workspace. Provide "
                 "exactly one of 'content' (rendered as-is) or 'source_path' (an existing workspace "
                 "file to render). Returns a file:// link to open it.",
                 lambda path, content="", source_path="", title="": pdf_export.export_pdf(
                     root, path, content=content, source_path=source_path, title=title),
                 {"path": _str("Destination path for the PDF, e.g. 'report.pdf'."),
                  "content": _str("Text to render, if not rendering an existing file."),
                  "source_path": _str("Relative path of an existing workspace file to render."),
                  "title": _str("Optional title heading at the top of the PDF.")}, ["path"]),
        ]
