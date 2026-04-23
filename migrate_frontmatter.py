#!/usr/bin/env python3
"""
Migrate frontmatter for all markdown files under the university directory.

Strips old property headers (inline metadata, YAML frontmatter, or none) and
replaces them with a standardized YAML frontmatter block:

---
course: <COURSE_CODE>
semester: <Season Year>
year: <N>
tags: [<concept tags>]
type: <Lecture|Reading|Assignment|Syllabus|Other>
date: <Mon D, YYYY>
---

Usage:
    python migrate_frontmatter.py                # live run (modifies files)
    python migrate_frontmatter.py --dry-run      # preview changes without writing
    python migrate_frontmatter.py --dry-run -v   # verbose dry run (show new headers)
"""

import argparse
import os
import re
import sys
from datetime import datetime
from pathlib import Path


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent  # 30-39 (UNIVERSITY)/

# Course code pattern: 2-5 uppercase letters followed by 3-4 digits, optional N suffix
COURSE_CODE_RE = re.compile(r'\b([A-Z]{2,5}\d{3,4}N?)\b')

# Tags that are structural (not concept tags) — to be excluded
STRUCTURAL_TAG_PATTERNS = [
    re.compile(r'^#?Year\d+$', re.IGNORECASE),
    re.compile(r'^#?NCC$', re.IGNORECASE),
    re.compile(r'^#?UNH$', re.IGNORECASE),
]

# Inline tag pattern in body text (e.g., #Year2 #CSCI230 #NCC at end of file)
INLINE_TAGS_LINE_RE = re.compile(r'^(\s*)(#\w+\s*)+\s*$')

# Date parsing patterns (order matters: try most specific first)
DATE_FORMATS_PARSE = [
    # ISO: 2026-01-28
    (re.compile(r'\b(\d{4}-\d{2}-\d{2})\b'), '%Y-%m-%d'),
    # US: 01-28-2026 or 10-01-2024
    (re.compile(r'\b(\d{2}-\d{2}-\d{4})\b'), '%m-%d-%Y'),
    # Full month: January 21, 2026  /  September 5, 2023
    (re.compile(r'\b((?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},?\s+\d{4})\b'), None),
    # Abbreviated month: Sep 08, 2025  /  Aug 26, 2025
    (re.compile(r'\b((?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{1,2},?\s+\d{4})\b'), None),
]

# Filename date patterns
FILENAME_DATE_PATTERNS = [
    # "September 5, 2023" or "January 17, 2024" (also handles "October 9 & 11, 2023" — takes first day)
    re.compile(r'((?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2})(?:\s*&\s*\d{1,2})?(,?\s+\d{4})'),
    # "Jan 17, 2024" abbreviated
    re.compile(r'((?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{1,2})(,?\s+\d{4})'),
]

# Full month name variants for parsing
FULL_MONTH_FORMATS = ['%B %d, %Y', '%B %d %Y']
ABBREV_MONTH_FORMATS = ['%b %d, %Y', '%b %d %Y']


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def parse_date_string(s: str) -> datetime | None:
    """Try to parse a date string into a datetime object."""
    s = s.strip().rstrip('.')
    # ISO
    try:
        return datetime.strptime(s, '%Y-%m-%d')
    except ValueError:
        pass
    # US MM-DD-YYYY
    try:
        return datetime.strptime(s, '%m-%d-%Y')
    except ValueError:
        pass
    # Full month
    for fmt in FULL_MONTH_FORMATS:
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            pass
    # Abbreviated month
    for fmt in ABBREV_MONTH_FORMATS:
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            pass
    return None


def format_date(dt: datetime) -> str:
    """Format a datetime as 'Mon D, YYYY' (e.g., 'Sep 8, 2025')."""
    # %b gives abbreviated month, %-d gives day without leading zero
    # On Windows, use %#d instead of %-d; handle both
    try:
        return dt.strftime('%-b %-d, %Y')
    except ValueError:
        pass
    try:
        return dt.strftime('%b %#d, %Y')
    except ValueError:
        pass
    # Manual fallback
    month = dt.strftime('%b')
    return f"{month} {dt.day}, {dt.year}"


def extract_date_from_text(text: str) -> datetime | None:
    """Try to find and parse a date from arbitrary text."""
    for pattern, fmt in DATE_FORMATS_PARSE:
        m = pattern.search(text)
        if m:
            dt = parse_date_string(m.group(1))
            if dt:
                return dt
    return None


def extract_date_from_filename(filename: str) -> datetime | None:
    """Try to extract a date from a filename."""
    stem = Path(filename).stem
    for pattern in FILENAME_DATE_PATTERNS:
        m = pattern.search(stem)
        if m:
            # Patterns now have 2 groups: (month+day) and (,? year)
            date_str = m.group(1) + m.group(2)
            dt = parse_date_string(date_str)
            if dt:
                return dt
    return None


def get_file_mtime(filepath: Path) -> datetime:
    """Get file modification time as datetime."""
    return datetime.fromtimestamp(filepath.stat().st_mtime)


def is_structural_tag(tag: str) -> bool:
    """Check if a tag is structural (year, institution, course code)."""
    clean = tag.lstrip('#').strip()
    if not clean:
        return True
    for pat in STRUCTURAL_TAG_PATTERNS:
        if pat.match('#' + clean) or pat.match(clean):
            return True
    # Check if it's a course code
    if COURSE_CODE_RE.fullmatch(clean):
        return True
    return False


def extract_all_tags(content: str) -> list[str]:
    """Extract all #tags from content (both inline and YAML)."""
    tags = set()

    # Find inline tags in body: lines like "#Year2 #CSCI230 #NCC"
    for m in re.finditer(r'#([A-Za-z0-9_/]+)', content):
        tag = m.group(1)
        # Skip markdown headings (## etc) — those follow a newline or start of line
        # We need context: if preceded by newline+optional spaces, it's likely a heading
        start = m.start()
        # Check character before #
        if start > 0 and content[start - 1] == '#':
            continue  # Part of ##, ###, etc.
        # Check if this is at start of line and followed by space (heading)
        line_start = content.rfind('\n', 0, start) + 1
        prefix = content[line_start:start]
        if prefix.strip() == '' and start + len(tag) + 1 < len(content) and content[start + len(tag) + 1:start + len(tag) + 2] == ' ':
            # Could be a heading if followed by text, but single-word tags won't have this
            pass
        tags.add(tag)

    return list(tags)


def extract_concept_tags(content: str, yaml_tags: list[str] | None = None) -> list[str]:
    """Extract only concept tags (not structural ones)."""
    all_tags = set()

    # From YAML tags array
    if yaml_tags:
        for t in yaml_tags:
            clean = t.lstrip('#').strip('"').lstrip('#').strip()
            if clean:
                all_tags.add(clean)

    # From inline tags in body
    # Find lines that are ONLY tags (typically at end of file)
    for line in content.split('\n'):
        stripped = line.strip()
        if stripped and re.match(r'^(#[A-Za-z0-9_/]+\s*)+$', stripped):
            for m in re.finditer(r'#([A-Za-z0-9_/]+)', stripped):
                all_tags.add(m.group(1))

    # Filter to concept tags only
    concept = []
    for tag in sorted(all_tags):
        if not is_structural_tag(tag):
            concept.append(tag)

    return concept


# ---------------------------------------------------------------------------
# Directory structure parsing
# ---------------------------------------------------------------------------

def find_hierarchy(filepath: Path) -> dict:
    """
    Walk up from a file to find year, semester, and course info.

    Directory structure (from file perspective):
      .../3X - Year N/[optional prefix] Season YYYY/[optional prefix] COURSECODE - Name/[optional subdir/]file.md

    Returns dict with keys: year, semester, course_code
    """
    parts = filepath.resolve().parts
    base_parts = BASE_DIR.resolve().parts
    # Get the relative parts after the base directory
    rel_parts = parts[len(base_parts):]
    # rel_parts[0] = year dir, rel_parts[1] = semester dir, rel_parts[2] = course dir, rest = subdirs + file

    result = {'year': '', 'semester': '', 'course_code': ''}

    if len(rel_parts) < 4:
        # Not deep enough to have year/semester/course/file
        return result

    year_dir = rel_parts[0]       # e.g., "31 - Year 1"
    semester_dir = rel_parts[1]   # e.g., "Fall 2024" or "31.11 - Fall 2023"
    course_dir = rel_parts[2]     # e.g., "CSCI106 - Intro to Computer Science" or "33A.11 - CS515 - ..."

    # Extract year number
    year_match = re.search(r'Year\s+(\d+)', year_dir)
    if year_match:
        result['year'] = int(year_match.group(1))

    # Extract semester (strip numeric prefix like "31.11 - ")
    sem_match = re.search(r'((?:Fall|Spring|Summer|Winter)\s+\d{4})', semester_dir)
    if sem_match:
        result['semester'] = sem_match.group(1)

    # Extract course code
    # Handle "33A.11 - CS515 - ..." pattern: find all course codes, pick the right one
    codes = COURSE_CODE_RE.findall(course_dir)
    if codes:
        result['course_code'] = codes[0]
    else:
        # Fallback: try to get something useful
        # Maybe directory like "ENG101 - ..."
        code_match = re.match(r'(?:\S+\s*-\s*)?([A-Z]+\d+\w*)', course_dir)
        if code_match:
            result['course_code'] = code_match.group(1)

    return result


# ---------------------------------------------------------------------------
# Type classification
# ---------------------------------------------------------------------------

def classify_type(filepath: Path) -> str:
    """Classify a markdown file into: Lecture, Reading, Assignment, Syllabus, Other."""
    stem = filepath.stem.lower()
    name = filepath.name.lower()
    # Check parent directory name for context
    parent = filepath.parent.name.lower()

    # Syllabus
    if 'syllabus' in stem:
        return 'Syllabus'

    # Lecture — starts with "lecture" or "seminar", or is a date-only filename (Fall 2023 pattern)
    if stem.startswith('lecture') or stem.startswith('seminar'):
        return 'Lecture'
    if parent in ('lectures', 'seminars'):
        return 'Lecture'
    # Date-only filenames like "September 5, 2023" are lecture notes
    # Also handles "October 9 & 11, 2023" or "September 18 & 20, 2023"
    if re.match(r'^(?:january|february|march|april|may|june|july|august|september|october|november|december)\s+\d{1,2}(?:\s*&\s*\d{1,2})?,?\s+\d{4}$', stem):
        return 'Lecture'
    # MLA presentation -> Lecture (it was an in-class presentation/lecture)
    if 'mla' in stem and 'presentation' in stem:
        return 'Lecture'

    # Assignment — file in Assignments/ dir or matching keywords
    if parent == 'assignments':
        return 'Assignment'
    assignment_keywords = ['assignment', 'essay', 'project', 'quiz review']
    for kw in assignment_keywords:
        if kw in stem:
            return 'Assignment'

    # Reading — chapter, section, unit, module files
    if stem.startswith('chapter') or stem.startswith('section') or stem.startswith('unit') or stem.startswith('module'):
        return 'Reading'
    if parent == 'section readings':
        return 'Reading'

    return 'Other'


# ---------------------------------------------------------------------------
# Old header parsing and stripping
# ---------------------------------------------------------------------------

def parse_and_strip_old_header(content: str) -> tuple[str, dict]:
    """
    Parse the old header/frontmatter from content, extract metadata,
    and return the content with the old header stripped.

    Returns: (stripped_content, extracted_metadata)
    extracted_metadata may contain: date, tags (from YAML)
    """
    lines = content.split('\n')
    metadata = {}
    body_start = 0

    if not lines:
        return content, metadata

    # --- Case 1: YAML frontmatter (starts with ---) ---
    if lines[0].strip() == '---':
        # Find closing ---
        closing = None
        for i in range(1, len(lines)):
            if lines[i].strip() == '---':
                closing = i
                break

        if closing is not None:
            yaml_block = lines[1:closing]
            body_start = closing + 1

            # Parse YAML block for metadata we care about
            yaml_tags = []
            in_tags = False
            for yl in yaml_block:
                stripped = yl.strip()

                # Date Created
                if stripped.lower().startswith('date created:') or stripped.lower().startswith('date:'):
                    val = stripped.split(':', 1)[1].strip().strip('"').strip("'")
                    if val:
                        dt = parse_date_string(val)
                        if dt:
                            metadata['date'] = dt

                # Tags
                if stripped.lower().startswith('tags:'):
                    in_tags = True
                    # Check inline tags: tags: ["foo", "bar"]
                    val = stripped.split(':', 1)[1].strip()
                    if val and val != '[]':
                        inline_tags = re.findall(r'#?([A-Za-z0-9_/]+)', val)
                        yaml_tags.extend(inline_tags)
                    continue

                if in_tags:
                    if stripped.startswith('- '):
                        tag = stripped[2:].strip().strip('"').strip("'").lstrip('#').strip()
                        if tag:
                            yaml_tags.append(tag)
                    else:
                        in_tags = False

            if yaml_tags:
                metadata['yaml_tags'] = yaml_tags

            # Skip any additional --- separators right after the YAML block
            while body_start < len(lines) and lines[body_start].strip() == '---':
                body_start += 1

    # --- Case 2: Inline properties (Course:/Date:/Lecture: without YAML fences) ---
    elif _looks_like_inline_properties(lines):
        # Consume lines that are: "Key: value", blank lines, or "Chapter X - Title" until ---
        i = 0
        while i < len(lines):
            stripped = lines[i].strip()

            # Blank line — continue scanning
            if stripped == '':
                i += 1
                continue

            # --- separator — marks end of header
            if stripped == '---':
                i += 1
                break

            # Inline property line: "Key: value" or "Key:" with wikilink
            if re.match(r'^(Course|Date|Lecture|Chapter)\s*:', stripped, re.IGNORECASE):
                # Extract date if present
                if stripped.lower().startswith('date:'):
                    val = stripped.split(':', 1)[1].strip()
                    if val:
                        dt = parse_date_string(val)
                        if dt:
                            metadata['date'] = dt
                i += 1
                continue

            # Chapter title line (e.g., "Chapter 2 - Ethics from Antiquity...")
            if re.match(r'^Chapter\s+\d+', stripped, re.IGNORECASE):
                i += 1
                continue

            # ***Related Chapter line (Fall 2024 pattern)
            if stripped.startswith('***'):
                # Consume until we hit actual content (after --- or non-related lines)
                i += 1
                while i < len(lines):
                    s = lines[i].strip()
                    if s == '---' or s.startswith('- ---'):
                        i += 1
                        break
                    if s.startswith('- [['):
                        i += 1
                        continue
                    break
                break

            # Not a property line — this is content, stop
            break

        body_start = i

    # If we didn't find an explicit header, body_start stays 0 (no header to strip)

    # Reconstruct body
    body_lines = lines[body_start:]

    # Strip leading blank lines from body
    while body_lines and body_lines[0].strip() == '':
        body_lines.pop(0)

    stripped_content = '\n'.join(body_lines)

    return stripped_content, metadata


def _looks_like_inline_properties(lines: list[str]) -> bool:
    """Check if the first non-blank lines look like inline property headers."""
    for line in lines[:5]:  # Check first 5 lines
        stripped = line.strip()
        if stripped == '':
            continue
        # Check for "Course:", "Date:", "Lecture:" patterns
        if re.match(r'^(Course|Date|Lecture)\s*:', stripped, re.IGNORECASE):
            return True
        break
    return False


# ---------------------------------------------------------------------------
# Inline tag stripping from body
# ---------------------------------------------------------------------------

def strip_inline_tag_lines(content: str, concept_tags: list[str]) -> str:
    """
    Remove lines that consist only of #tags from the body content.
    These are typically at the end of the file.
    """
    lines = content.split('\n')

    # Process from the end to find trailing tag-only lines
    result = lines[:]

    # Strip trailing blank lines first to find the tag line
    while result and result[-1].strip() == '':
        result.pop()

    # Check if the last line(s) are tag-only lines
    while result:
        stripped = result[-1].strip()
        if stripped and re.match(r'^(#[A-Za-z0-9_/]+\s*)+$', stripped):
            result.pop()
            # Also remove trailing blank lines before the tag line
            while result and result[-1].strip() == '':
                result.pop()
        else:
            break

    # Re-add a single trailing newline
    content = '\n'.join(result)
    if content and not content.endswith('\n'):
        content += '\n'

    return content


# ---------------------------------------------------------------------------
# Main processing
# ---------------------------------------------------------------------------

def build_new_frontmatter(course: str, semester: str, year, tags: list[str],
                          file_type: str, date_str: str) -> str:
    """Build the new YAML frontmatter string."""
    # Format tags
    if tags:
        tags_str = '[' + ', '.join(tags) + ']'
    else:
        tags_str = '[]'

    return f"""---
course: {course}
semester: {semester}
year: {year}
tags: {tags_str}
type: {file_type}
date: {date_str}
---"""


def process_file(filepath: Path, dry_run: bool = False, verbose: bool = False) -> dict:
    """
    Process a single markdown file: strip old header, build new frontmatter.
    Returns info dict for reporting.
    """
    info = {
        'path': str(filepath),
        'status': 'ok',
        'warnings': [],
    }

    try:
        content = filepath.read_text(encoding='utf-8')
    except Exception as e:
        info['status'] = 'error'
        info['warnings'].append(f"Could not read file: {e}")
        return info

    # 1. Extract hierarchy info from directory structure
    hierarchy = find_hierarchy(filepath)
    course = hierarchy['course_code']
    semester = hierarchy['semester']
    year = hierarchy['year']

    if not course:
        info['warnings'].append("Could not determine course code")
    if not semester:
        info['warnings'].append("Could not determine semester")
    if not year:
        info['warnings'].append("Could not determine year")

    # 2. Parse and strip old header
    stripped_content, old_metadata = parse_and_strip_old_header(content)

    # 3. Extract concept tags (from original content before stripping)
    yaml_tags = old_metadata.get('yaml_tags', [])
    concept_tags = extract_concept_tags(content, yaml_tags)

    # 4. Strip inline tag lines from body
    stripped_content = strip_inline_tag_lines(stripped_content, concept_tags)

    # 5. Determine date (priority: old header > filename > mtime)
    date_dt = old_metadata.get('date')
    if not date_dt:
        date_dt = extract_date_from_filename(filepath.name)
    if not date_dt:
        date_dt = get_file_mtime(filepath)
        if date_dt:
            info['warnings'].append("Date from mtime (no header/filename date found)")

    date_str = format_date(date_dt) if date_dt else ''

    # 6. Classify type
    file_type = classify_type(filepath)

    # 7. Build new frontmatter
    new_frontmatter = build_new_frontmatter(
        course=course,
        semester=semester,
        year=year,
        tags=concept_tags,
        file_type=file_type,
        date_str=date_str,
    )

    # 8. Combine
    new_content = new_frontmatter + '\n\n' + stripped_content

    # 9. Write or report
    if dry_run:
        info['new_header'] = new_frontmatter
        if verbose:
            print(f"\n{'='*60}")
            print(f"FILE: {filepath.relative_to(BASE_DIR)}")
            print(f"{'='*60}")
            print(new_frontmatter)
            print("---")
            if concept_tags:
                print(f"  Concept tags: {concept_tags}")
            if info['warnings']:
                for w in info['warnings']:
                    print(f"  WARNING: {w}")
    else:
        try:
            filepath.write_text(new_content, encoding='utf-8')
        except Exception as e:
            info['status'] = 'error'
            info['warnings'].append(f"Could not write file: {e}")

    return info


def main():
    parser = argparse.ArgumentParser(
        description='Migrate frontmatter for university markdown notes.'
    )
    parser.add_argument('--dry-run', action='store_true',
                        help='Preview changes without writing to files')
    parser.add_argument('-v', '--verbose', action='store_true',
                        help='Show detailed output for each file')
    args = parser.parse_args()

    print(f"Base directory: {BASE_DIR}")
    print(f"Mode: {'DRY RUN' if args.dry_run else 'LIVE (will modify files)'}")
    print()

    # Collect all markdown files
    md_files = sorted(BASE_DIR.rglob('*.md'))
    # Exclude this script if it somehow has .md extension (it doesn't, but safety)
    md_files = [f for f in md_files if f.name != Path(__file__).name]

    print(f"Found {len(md_files)} markdown files to process.")
    print()

    results = {
        'total': len(md_files),
        'ok': 0,
        'warnings': 0,
        'errors': 0,
    }

    type_counts = {}
    warning_files = []

    for filepath in md_files:
        info = process_file(filepath, dry_run=args.dry_run, verbose=args.verbose)

        if info['status'] == 'ok':
            results['ok'] += 1
        else:
            results['errors'] += 1

        if info['warnings']:
            results['warnings'] += 1
            warning_files.append(info)

        if not args.dry_run and not args.verbose:
            rel = filepath.relative_to(BASE_DIR)
            status = 'OK' if info['status'] == 'ok' else 'ERROR'
            print(f"  [{status}] {rel}")

    # Summary
    print()
    print("=" * 60)
    print("SUMMARY")
    print("=" * 60)
    print(f"Total files processed: {results['total']}")
    print(f"  Successful: {results['ok']}")
    print(f"  Errors: {results['errors']}")
    print(f"  Files with warnings: {results['warnings']}")

    if warning_files:
        print()
        print("Files with warnings:")
        for info in warning_files:
            rel = Path(info['path']).relative_to(BASE_DIR)
            for w in info['warnings']:
                print(f"  {rel}: {w}")

    if args.dry_run:
        print()
        print("(Dry run — no files were modified)")


if __name__ == '__main__':
    main()
