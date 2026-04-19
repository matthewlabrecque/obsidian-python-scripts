#!/usr/bin/env python3
import sys
import os
import re
from pathlib import Path


def extract_course_code(dirname: str) -> str | None:
    pattern = r'^\d+[A-Z]?\.\d+ - [A-Z]+\d* - .+'
    if re.match(pattern, dirname):
        parts = dirname.split(' - ')
        if len(parts) >= 3:
            return parts[1]
    return None


def extract_year(dirpath: Path) -> str | None:
    current = dirpath
    while current != current.parent:
        match = re.search(r'(YEAR\s*\d+[A-Z]?)', current.name, re.IGNORECASE)
        if match:
            year_text = match.group(1).upper()
            normalized = re.sub(r'\s+', '-', year_text)
            return normalized
        current = current.parent
    return None


def process_directory(target_dir: str):
    target_path = Path(target_dir).expanduser().resolve()

    if not target_path.exists():
        print(f"Error: Directory does not exist: {target_path}")
        return

    if not target_path.is_dir():
        print(f"Error: Path is not a directory: {target_path}")
        return

    for course_dir in target_path.iterdir():
        if not course_dir.is_dir():
            continue

        course_code = extract_course_code(course_dir.name)
        if not course_code:
            print(f"Skipping directory (no course code found): {course_dir.name}")
            continue

        year = extract_year(course_dir)
        if not year:
            print(f"Skipping directory (no year found): {course_dir.name}")
            continue

        md_files = list(course_dir.glob('*.md'))
        if not md_files:
            continue

        tag_lines = f"# Course Tag: {course_code}\n# Year of Study: {year}\n"

        for md_file in md_files:
            with open(md_file, 'r', encoding='utf-8') as f:
                content = f.read()

            if not content.endswith('\n'):
                content += '\n'

            with open(md_file, 'w', encoding='utf-8') as f:
                f.write(content + tag_lines)

            print(f"Tagged: {md_file.relative_to(target_path)}")


def main():
    if len(sys.argv) != 2:
        print("Usage: python tag_markdown_files.py <target_directory>")
        sys.exit(1)

    target_dir = sys.argv[1]
    process_directory(target_dir)


if __name__ == '__main__':
    main()
