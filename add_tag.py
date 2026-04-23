#!/usr/bin/env python3
"""
add_tag.py

Adds a user-specified tag to the YAML frontmatter of Markdown files.

Usage:
    python add_tag.py <tag> [file_or_directory ...]

Arguments:
    tag              Tag to add (without the leading '#', e.g. "project-x")
    file_or_directory  One or more files or directories to process.
                       Defaults to the current directory if omitted.

Features:
    - Preserves existing tags (no duplicates)
    - Creates frontmatter if missing
    - Atomic writes via temp file
    - Errors logged to add_tag_errors.log
"""

import os
import re
import sys
import logging
import tempfile
from pathlib import Path

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).parent.resolve()
ERROR_LOG = SCRIPT_DIR / "add_tag_errors.log"

logging.basicConfig(
    filename=str(ERROR_LOG),
    filemode="a",
    level=logging.ERROR,
    format="%(asctime)s  %(levelname)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Helpers: frontmatter parsing
# ---------------------------------------------------------------------------

FM_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)


def parse_frontmatter(content: str) -> tuple[list[str], str | None]:
    """Return (tags_list, full_fm_string_or_None) from *content*."""
    m = FM_RE.match(content)
    if not m:
        return [], None

    yaml_text = m.group(1)
    tags = []
    in_tags = False

    for line in yaml_text.splitlines():
        if re.match(r"^tags\s*:", line):
            in_tags = True
            inline = re.match(r"^tags\s*:\s*\[(.+)\]", line)
            if inline:
                tags = [t.strip().strip('"\'') for t in inline.group(1).split(",")]
                in_tags = False
            continue
        if in_tags:
            m2 = re.match(r"^\s+-\s+(.+)$", line)
            if m2:
                tags.append(m2.group(1).strip())
            elif line and not line.startswith(" "):
                in_tags = False

    return tags, m.group(0)


def build_frontmatter_block(existing_yaml: str | None, tags: list[str]) -> str:
    """Return a complete frontmatter block with the updated tags."""
    if existing_yaml:
        # Rebuild: keep everything except the old tags section, then append new tags
        lines = []
        in_tags = False
        for line in existing_yaml.splitlines():
            if re.match(r"^tags\s*:", line):
                in_tags = True
                continue
            if in_tags:
                if re.match(r"^\s+-\s+", line):
                    continue
                elif line and not line.startswith(" "):
                    in_tags = False
            lines.append(line)

        yaml_body = "\n".join(lines)
    else:
        yaml_body = ""

    tag_lines = "\n".join(f"  - {t}" for t in tags) if tags else "  []"
    return f"---\n{yaml_body}\ntags:\n{tag_lines}\n---\n"


# ---------------------------------------------------------------------------
# Core: process a single file
# ---------------------------------------------------------------------------

def process_file(path: Path, tag: str) -> bool:
    """Add *tag* to *path*'s frontmatter. Returns True on success."""
    content = path.read_text(encoding="utf-8")
    existing_tags, fm_block = parse_frontmatter(content)

    if tag in existing_tags:
        return False  # already present

    existing_tags.append(tag)
    new_fm = build_frontmatter_block(fm_block, existing_tags)

    body = content[len(fm_block):] if fm_block else content
    new_content = new_fm + body

    tmp_fd, tmp_path = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
            f.write(new_content)
        os.replace(tmp_path, path)
        return True
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python add_tag.py <tag> [file_or_directory ...]")
        sys.exit(1)

    tag_arg = sys.argv[1].lstrip("#")
    targets = [Path(t) for t in sys.argv[2:]] if len(sys.argv) > 2 else [Path(".")]

    processed = 0
    skipped = 0
    errors = 0

    for target in targets:
        if target.is_file():
            paths = [target]
        elif target.is_dir():
            paths = sorted(target.rglob("*.md"))
        else:
            print(f"  WARN  {target} not found, skipping")
            skipped += 1
            continue

        for p in paths:
            try:
                added = process_file(p, tag_arg)
                if added:
                    print(f"  OK    {p}")
                    processed += 1
                else:
                    print(f"  SKIP  {p}  (tag already present)")
                    skipped += 1
            except Exception as exc:
                print(f"  ERR   {p}  →  {exc}", file=sys.stderr)
                logger.error("Failed to process '%s': %s: %s", p, type(exc).__name__, exc)
                errors += 1

    print(f"\nDone.  Added: {processed}  Skipped: {skipped}  Errors: {errors}")
    if errors:
        print(f"Error details written to: {ERROR_LOG}")
