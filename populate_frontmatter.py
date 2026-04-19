#!/usr/bin/env python3
"""
populate_frontmatter.py

Walks all Markdown files in the Second Brain vault and ensures each file has a
YAML frontmatter block at the top containing:

  - Date Created  (preserved if already present, otherwise from file birth time)
  - tags          (existing tags preserved and merged with a directory-derived tag)
  - related       (kept blank if not already populated)

Skipped directories / files:
  - 30-39 (UNIVERSITY)/
  - 98 - META/
  - .trash/
  - README.md (vault root only)

Errors are written to populate_frontmatter_errors.log in the vault root.
"""

import os
import re
import sys
import stat
import subprocess
import tempfile
import logging
from datetime import datetime
from pathlib import Path

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

VAULT_ROOT = Path(__file__).parent.resolve()

SKIP_DIRS = {
    "30-39 (UNIVERSITY)",
    "98 - META",
    ".trash",
}

ERROR_LOG = VAULT_ROOT / "populate_frontmatter_errors.log"

DATE_FORMAT = "%b %d, %Y %H:%M"   # e.g. "Apr 08, 2026 14:30"

# Matches a leading numeric prefix like "22 - ", "3 - ", or "11.11 - "
DIR_PREFIX_RE = re.compile(r"^[\d]+[\d\.\-]*\s*-\s*")

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

logging.basicConfig(
    filename=str(ERROR_LOG),
    filemode="a",
    level=logging.ERROR,
    format="%(asctime)s  %(levelname)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers: file creation time
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
      1. os.stat().st_birthtime  (Python attribute, Linux kernel >= 4.11 + statx)
      2. `stat --format=%W`      (subprocess, filesystem must support birth time)
      3. os.stat().st_mtime      (last-modified, universal fallback)
    """
    st = path.stat()

    # 1. st_birthtime (may exist on some Linux builds)
    ts = getattr(st, "st_birthtime", None)
    if ts and ts > 0:
        return datetime.fromtimestamp(ts)

    # 2. stat command
    ts = _get_birth_time_via_stat_cmd(path)
    if ts:
        return datetime.fromtimestamp(ts)

    # 3. mtime fallback
    return datetime.fromtimestamp(st.st_mtime)


# ---------------------------------------------------------------------------
# Helpers: directory-based tag
# ---------------------------------------------------------------------------

def derive_dir_tag(file_path: Path) -> str | None:
    """
    Return a tag derived from the immediate parent directory of *file_path*,
    or None if the parent is the vault root or a top-level directory.

    Derivation rules:
      - Strip leading numeric prefix  (e.g. "22 - " from "22 - Linux")
      - Lowercase
      - Replace spaces with hyphens
    """
    parent = file_path.parent

    # File is directly in the vault root — no tag
    if parent == VAULT_ROOT:
        return None

    # File is directly in a top-level directory — no tag
    if parent.parent == VAULT_ROOT:
        return None

    dir_name = parent.name
    # Strip numeric prefix
    tag = DIR_PREFIX_RE.sub("", dir_name).strip()
    # Lowercase and spaces → hyphens
    tag = tag.lower().replace(" ", "-")
    return tag if tag else None


# ---------------------------------------------------------------------------
# Helpers: frontmatter parsing
# ---------------------------------------------------------------------------

def split_frontmatter(content: str) -> tuple[dict, str]:
    """
    Split *content* into (frontmatter_fields, body).

    frontmatter_fields is a dict with keys 'date_created', 'tags', 'related'
    extracted from the YAML block (if present), or defaults otherwise.

    Returns the remaining body (everything after the closing ---).
    """
    defaults = {"date_created": None, "tags": [], "related": ""}

    if not content.startswith("---"):
        return defaults, content

    # Find the closing ---
    end = content.find("\n---", 3)
    if end == -1:
        # Malformed — treat whole content as body
        return defaults, content

    yaml_block = content[3:end]          # between the two ---
    body = content[end + 4:]             # after closing ---\n
    if body.startswith("\n"):
        body = body[1:]

    fields = dict(defaults)

    # Extract Date Created
    m = re.search(r"^Date Created:\s*(.+)$", yaml_block, re.MULTILINE)
    if m:
        fields["date_created"] = m.group(1).strip()

    # Extract tags
    tags = []
    in_tags = False
    for line in yaml_block.splitlines():
        if re.match(r"^tags\s*:", line):
            in_tags = True
            # Inline tags: tags: [a, b]
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
    # Filter out malformed tags that may have been generated by an earlier broken
    # prefix regex (e.g. "11.11---coffee", "11.12---health").
    MALFORMED_TAG_RE = re.compile(r"^\d[\d\.]*---")
    fields["tags"] = [t for t in tags if t and not MALFORMED_TAG_RE.match(t)]

    # Extract related
    m = re.search(r"^related:\s*(.*)$", yaml_block, re.MULTILINE)
    if m:
        fields["related"] = m.group(1).strip()

    return fields, body


# ---------------------------------------------------------------------------
# Helpers: frontmatter building
# ---------------------------------------------------------------------------

def build_frontmatter(date_created: str, tags: list[str], related: str) -> str:
    """Render the YAML frontmatter block as a string."""
    lines = ["---", f"Date Created: {date_created}", "tags:"]
    if tags:
        for tag in tags:
            lines.append(f"  - {tag}")
    else:
        lines.append("  - ")
    lines.append(f"related: {related}")
    lines.append("---")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Core: process a single file
# ---------------------------------------------------------------------------

def process_file(path: Path) -> None:
    """Read *path*, update its frontmatter in-place, write atomically."""
    content = path.read_text(encoding="utf-8")

    # Parse existing frontmatter (if any)
    fields, body = split_frontmatter(content)

    # Date Created: preserve existing value, otherwise resolve from filesystem
    if fields["date_created"]:
        date_created = fields["date_created"]
    else:
        dt = get_creation_time(path)
        date_created = dt.strftime(DATE_FORMAT)

    # Tags: start from preserved tags, merge in directory tag
    tags = list(fields["tags"])
    dir_tag = derive_dir_tag(path)
    if dir_tag and dir_tag not in tags:
        tags.append(dir_tag)

    related = fields["related"]

    # Build new content
    new_frontmatter = build_frontmatter(date_created, tags, related)
    new_content = new_frontmatter + "\n" + body if body.strip() else new_frontmatter + body

    # Atomic write via temp file
    tmp_fd, tmp_path = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
            f.write(new_content)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# Core: walk the vault
# ---------------------------------------------------------------------------

def is_skipped(path: Path) -> bool:
    """Return True if *path* should be skipped."""
    # Root README.md
    if path == VAULT_ROOT / "README.md":
        return True

    # Any part of the path is in SKIP_DIRS
    for part in path.relative_to(VAULT_ROOT).parts[:-1]:  # exclude filename itself
        if part in SKIP_DIRS:
            return True

    return False


def walk_vault() -> None:
    processed = 0
    skipped = 0
    errors = 0

    md_files = sorted(VAULT_ROOT.rglob("*.md"))

    for path in md_files:
        if is_skipped(path):
            skipped += 1
            continue

        try:
            process_file(path)
            print(f"  OK  {path.relative_to(VAULT_ROOT)}")
            processed += 1
        except Exception as exc:
            rel = path.relative_to(VAULT_ROOT)
            print(f" ERR  {rel}  →  {exc}", file=sys.stderr)
            logger.error("Failed to process '%s': %s: %s", rel, type(exc).__name__, exc)
            errors += 1

    print()
    print(f"Done.  Processed: {processed}  Skipped: {skipped}  Errors: {errors}")
    if errors:
        print(f"Error details written to: {ERROR_LOG.relative_to(VAULT_ROOT)}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print(f"Vault: {VAULT_ROOT}")
    print(f"Scanning for Markdown files...\n")
    walk_vault()
