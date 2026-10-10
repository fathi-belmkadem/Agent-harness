from pathlib import Path

from fpdf import FPDF

from .sandbox import resolve_path

# A handful of well-known install locations for a Unicode-coverage TTF font.
# Without one, fpdf2 falls back to its built-in Latin-1-only core fonts,
# which raise on anything outside that range (e.g. an em-dash or accented
# character) — common in agent-written prose. The Docker image installs
# fonts-dejavu-core specifically so the first path here always resolves;
# the rest are best-effort for local, non-container runs.
_UNICODE_FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",       # Debian/Ubuntu (this project's Docker image)
    "/usr/share/fonts/dejavu-sans-fonts/DejaVuSans.ttf",     # Fedora/RHEL
    "C:/Windows/Fonts/arial.ttf",                            # Windows
    "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",  # macOS
]


def _find_unicode_font() -> str | None:
    return next((c for c in _UNICODE_FONT_CANDIDATES if Path(c).is_file()), None)


def export_pdf(
    root: Path,
    path: str,
    content: str = "",
    source_path: str = "",
    title: str = "",
) -> str:
    """Render text into a PDF written inside the sandbox.

    Exactly one of `content` (rendered as-is) or `source_path` (an existing
    workspace file, read and rendered) must be given.
    """
    if bool(content) == bool(source_path):
        raise ValueError("provide exactly one of 'content' or 'source_path'")
    if not path.lower().endswith(".pdf"):
        raise ValueError(f"destination path must end with .pdf: {path}")

    if source_path:
        source = resolve_path(root, source_path)
        if not source.is_file():
            raise FileNotFoundError(f"No such source file: {source_path}")
        text = source.read_text(encoding="utf-8")
    else:
        text = content

    target = resolve_path(root, path)
    target.parent.mkdir(parents=True, exist_ok=True)

    pdf = FPDF()
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()

    font_path = _find_unicode_font()
    if font_path:
        pdf.add_font("Body", "", font_path)
        font = "Body"
    else:
        font = "Helvetica"  # Latin-1 only; non-Latin-1 content will raise.

    if title:
        pdf.set_font(font, size=16)
        pdf.multi_cell(0, 10, title, new_x="LMARGIN", new_y="NEXT")
        pdf.ln(4)

    pdf.set_font(font, size=12)
    for line in text.splitlines() or [""]:
        pdf.multi_cell(0, 8, line, new_x="LMARGIN", new_y="NEXT")

    pdf.output(str(target))

    return (
        f"Wrote PDF to {path}. Open it at: {target.as_uri()}\n"
        f"(running in Docker? the file is also at agent-harness/workspace/{path} "
        "on your host machine, since that folder is bind-mounted)"
    )
