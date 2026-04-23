#!/usr/bin/env python3
"""
convert_to_markdown.py

Batch-converts ODT and PDF files from a directory into Markdown files with
YAML frontmatter.

Conversion tools:
  - ODT → Markdown via pandoc
  - PDF → text via pdftotext (poppler-utils)

YAML frontmatter added to each file:
  ---
  tags:
    - <user-supplied tags>
  type: <user-supplied type>
  date: <file creation date>
  ---

Usage:
    python convert_to_markdown.py <input_directory> [options]

Examples:
    python convert_to_markdown.py ./documents --type "Reference" --tags "imported,archive"
    python convert_to_markdown.py ./documents --output ./vault/imports --recursive --type "Note"
    python convert_to_markdown.py ./documents --dry-run -v
"""

import argparse
import logging
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DATE_FORMAT = "%b %d, %Y %H:%M"  # e.g. "Apr 08, 2026 14:30"
SUPPORTED_EXTENSIONS = {".odt", ".pdf"}

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Dependency checks
# ---------------------------------------------------------------------------

def check_dependencies() -> list[str]:
    """Return a list of missing external commands."""
    missing = []
    if shutil.which("pandoc") is None:
        missing.append("pandoc")
    if shutil.which("pdftotext") is None:
        missing.append("pdftotext (install poppler-utils)")
    return missing


# ---------------------------------------------------------------------------
# File creation time
# ---------------------------------------------------------------------------

def _get_birth_time_via_stat_cmd(path: Path) -> float | None:
    """Use the `stat` command to retrieve birth time (%W) on Linux."""
    try:
        result = subprocess.run(
            ["stat", "--format=%W", str(path)],
            capture_output=True,
            text=True,
            timeout=5,
        )
        ts = float(result.stdout.strip())
        return ts if ts > 0 else None
    except Exception:
        return None


def get_creation_time(path: Path) -> datetime:
    """
    Return the best available creation timestamp for *path*.

    Priority:
      1. os.stat().st_birthtime  (Python attribute, Linux kernel >= 4.11)
      2. `stat --format=%W`      (subprocess fallback)
      3. os.stat().st_mtime      (last-modified, universal fallback)
    """
    st = path.stat()

    ts = getattr(st, "st_birthtime", None)
    if ts and ts > 0:
        return datetime.fromtimestamp(ts)

    ts = _get_birth_time_via_stat_cmd(path)
    if ts:
        return datetime.fromtimestamp(ts)

    return datetime.fromtimestamp(st.st_mtime)


# ---------------------------------------------------------------------------
# Conversion functions
# ---------------------------------------------------------------------------

def convert_odt(path: Path) -> str:
    """Convert an ODT file to Markdown using pandoc."""
    result = subprocess.run(
        ["pandoc", "-f", "odt", "-t", "markdown", "--wrap=none", str(path)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode != 0:
        raise RuntimeError(f"pandoc failed: {result.stderr.strip()}")
    return result.stdout


def convert_pdf(path: Path) -> str:
    """Convert a PDF file to text using pdftotext."""
    result = subprocess.run(
        ["pdftotext", "-layout", str(path), "-"],
        capture_output=True,
        text=True,
        errors="replace",
        timeout=60,
    )
    if result.returncode != 0:
        raise RuntimeError(f"pdftotext failed: {result.stderr.strip()}")
    text = result.stdout
    if not text.strip():
        raise RuntimeError("pdftotext produced empty output (scanned PDF?)")
    return text


CONVERTERS = {
    ".odt": convert_odt,
    ".pdf": convert_pdf,
}


# ---------------------------------------------------------------------------
# Frontmatter
# ---------------------------------------------------------------------------

def build_frontmatter(tags: list[str], file_type: str, date_str: str) -> str:
    """Render the YAML frontmatter block."""
    lines = ["---", "tags:"]
    if tags:
        for tag in tags:
            lines.append(f"  - {tag}")
    else:
        lines.append("  - ")
    lines.append(f"type: {file_type}")
    lines.append(f"date: {date_str}")
    lines.append("---")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# File discovery
# ---------------------------------------------------------------------------

def discover_files(input_dir: Path, recursive: bool) -> list[Path]:
    """Find all ODT and PDF files in *input_dir*."""
    files = []
    if recursive:
        for ext in SUPPORTED_EXTENSIONS:
            files.extend(input_dir.rglob(f"*{ext}"))
    else:
        for ext in SUPPORTED_EXTENSIONS:
            files.extend(input_dir.glob(f"*{ext}"))
    return sorted(set(files))


# ---------------------------------------------------------------------------
# Output path resolution
# ---------------------------------------------------------------------------

def resolve_output_path(
    source: Path, input_dir: Path, output_dir: Path
) -> Path:
    """Determine the output .md path, preserving subdirectory structure."""
    rel = source.relative_to(input_dir)
    return output_dir / rel.with_suffix(".md")


# ---------------------------------------------------------------------------
# Atomic file write
# ---------------------------------------------------------------------------

def write_file(path: Path, content: str) -> None:
    """Write *content* to *path* atomically via a temp file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_fd, tmp_path = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# Core: process a single file
# ---------------------------------------------------------------------------

def process_file(
    source: Path,
    output_path: Path,
    tags: list[str],
    file_type: str,
    dry_run: bool,
    verbose: bool,
) -> dict:
    """Convert a single file and write the result. Returns an info dict."""
    info = {"path": str(source), "output": str(output_path), "status": "ok", "warning": ""}

    # Check for existing output
    if output_path.exists():
        info["status"] = "skipped"
        info["warning"] = f"Output already exists: {output_path.name}"
        return info

    # Get creation date
    dt = get_creation_time(source)
    date_str = dt.strftime(DATE_FORMAT)

    # Convert
    ext = source.suffix.lower()
    converter = CONVERTERS.get(ext)
    if converter is None:
        info["status"] = "skipped"
        info["warning"] = f"Unsupported extension: {ext}"
        return info

    try:
        markdown_body = converter(source)
    except Exception as exc:
        info["status"] = "error"
        info["warning"] = str(exc)
        return info

    # Build output content
    frontmatter = build_frontmatter(tags, file_type, date_str)
    full_content = frontmatter + "\n" + markdown_body

    if dry_run:
        if verbose:
            print(f"\n{'=' * 60}")
            print(f"FILE: {source.name}  ->  {output_path.name}")
            print(f"{'=' * 60}")
            print(frontmatter)
            print(f"  (body: {len(markdown_body)} chars)")
        return info

    # Write
    try:
        write_file(output_path, full_content)
    except Exception as exc:
        info["status"] = "error"
        info["warning"] = f"Write failed: {exc}"

    return info


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Convert ODT and PDF files to Markdown with YAML frontmatter."
    )
    parser.add_argument(
        "input_dir",
        type=Path,
        help="Directory containing ODT/PDF files.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Output directory for .md files (default: same as input).",
    )
    parser.add_argument(
        "--type",
        dest="file_type",
        default="",
        help="Value for the 'type' frontmatter field (e.g. Reference, Note).",
    )
    parser.add_argument(
        "--tags",
        default="",
        help="Comma-separated tags applied to all files (e.g. 'history,research').",
    )
    parser.add_argument(
        "--recursive",
        action="store_true",
        help="Process subdirectories recursively.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview what would be converted without writing files.",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Show detailed output.",
    )
    args = parser.parse_args()

    # --- Validate input ---
    input_dir = args.input_dir.expanduser().resolve()
    if not input_dir.is_dir():
        print(f"Error: Not a directory: {input_dir}", file=sys.stderr)
        sys.exit(1)

    output_dir = (args.output or args.input_dir).expanduser().resolve()

    # --- Check dependencies ---
    missing = check_dependencies()
    if missing:
        print("Error: Missing required external tools:", file=sys.stderr)
        for m in missing:
            print(f"  - {m}", file=sys.stderr)
        sys.exit(1)

    # --- Parse tags ---
    tags = [t.strip() for t in args.tags.split(",") if t.strip()] if args.tags else []

    # --- Setup error log ---
    error_log = input_dir / "convert_to_markdown_errors.log"
    logging.basicConfig(
        filename=str(error_log),
        filemode="a",
        level=logging.ERROR,
        format="%(asctime)s  %(levelname)s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # --- Discover files ---
    files = discover_files(input_dir, args.recursive)
    if not files:
        print("No ODT or PDF files found.")
        sys.exit(0)

    # --- Report config ---
    print(f"Input:     {input_dir}")
    print(f"Output:    {output_dir}")
    print(f"Type:      {args.file_type or '(empty)'}")
    print(f"Tags:      {tags or '(none)'}")
    print(f"Recursive: {args.recursive}")
    print(f"Mode:      {'DRY RUN' if args.dry_run else 'LIVE'}")
    print(f"Files:     {len(files)}")
    print()

    # --- Process ---
    counts = {"ok": 0, "skipped": 0, "error": 0}

    for source in files:
        out_path = resolve_output_path(source, input_dir, output_dir)
        info = process_file(
            source=source,
            output_path=out_path,
            tags=tags,
            file_type=args.file_type,
            dry_run=args.dry_run,
            verbose=args.verbose,
        )

        status = info["status"]
        counts[status] = counts.get(status, 0) + 1
        rel = source.relative_to(input_dir)

        if status == "ok":
            print(f"  OK    {rel}")
        elif status == "skipped":
            print(f"  SKIP  {rel}  ({info['warning']})")
        elif status == "error":
            print(f"  ERR   {rel}  ({info['warning']})", file=sys.stderr)
            logger.error("Failed '%s': %s", rel, info["warning"])

    # --- Summary ---
    print()
    print(f"Done.  Converted: {counts['ok']}  Skipped: {counts['skipped']}  Errors: {counts['error']}")
    if counts["error"]:
        print(f"Error details: {error_log.relative_to(input_dir)}")
    if args.dry_run:
        print("(Dry run — no files were modified)")


if __name__ == "__main__":
    main()
