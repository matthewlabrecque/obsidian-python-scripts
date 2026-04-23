# auto_tag_markdown.py — Usage Guide

## What It Does

Scans a directory of Markdown files, identifies those without existing tags (in YAML frontmatter or inline `#tag` syntax), and uses machine learning to discover broad categorical tags. By default it outputs a suggestion report; use `--apply` to write tags directly into file frontmatter.

## Installation

### Prerequisites

- Python 3.10+
- pip

### Install Dependencies

```bash
pip install sentence-transformers scikit-learn hdbscan
```

**First-run note:** The embedding model (`all-MiniLM-L6-v2`, ~80MB) is downloaded automatically on first use. No GPU is required.

### Optional: Virtual Environment

```bash
python3 -m venv venv
source venv/bin/activate
pip install sentence-transformers scikit-learn hdbscan
```

## Usage

### Basic (Dry-Run / Report Only)

```bash
python auto_tag_markdown.py /path/to/your/markdir
```

This scans the directory, discovers tags, and prints a report. **No files are modified.**

### Apply Tags to Files

```bash
python auto_tag_markdown.py /path/to/your/markdir --apply
```

Writes discovered tags into the YAML frontmatter of each file.

### JSON Output

```bash
python auto_tag_markdown.py /path/to/your/markdir --output json
```

Outputs the report as JSON (useful for piping into other tools).

### Full Options

```
python auto_tag_markdown.py <directory> [OPTIONS]

Positional:
  directory               Directory to scan for Markdown files

Options:
  --min-chars N           Minimum body text length to consider a file (default: 200)
  --skip-dirs DIR1 DIR2   Directory names to skip (e.g. .trash 98-META)
  --clusters N            Number of tag clusters to discover (default: 8)
  --min-cluster-size N    Minimum files per cluster to generate a tag (default: 3)
  --apply                 Write suggested tags into file frontmatter
  --output text|json      Output format for the report (default: text)
  --model NAME            Sentence-transformers model to use (default: all-MiniLM-L6-v2)
  -h, --help              Show help message and exit
```

## Examples

### Tag a Second Brain vault, skip system dirs

```bash
python auto_tag_markdown.py ~/vault \
  --skip-dirs .trash 98-META 30-39-UNIVERSITY \
  --min-chars 300 \
  --clusters 10
```

### Tag with fewer, broader categories

```bash
python auto_tag_markdown.py ~/notes --clusters 5 --min-cluster-size 5
```

### Review as JSON, then apply selectively

```bash
python auto_tag_markdown.py ~/notes --output json > suggestions.json
# Review suggestions.json, then:
python auto_tag_markdown.py ~/notes --apply
```

## How It Works

1. **Scan** — Finds all `.md` files, skips those with existing tags, skips files below `--min-chars`, and respects `--skip-dirs`.
2. **Clean** — Strips Markdown syntax (code blocks, links, headers, bold/italic) to produce plain text.
3. **Embed** — Converts each document into a vector using `sentence-transformers` (semantic meaning, not keyword matching).
4. **Cluster** — Groups similar documents using KMeans into `--clusters` groups.
5. **Name** — Derives a tag name per cluster using TF-IDF to find distinctive terms, then slugifies them (e.g., "machine learning" → `machine-learning`).
6. **Report** — Shows discovered tags, per-file suggestions with confidence scores, and untagged outliers.
7. **Apply** (optional) — Writes tags into YAML frontmatter using atomic writes.

## What Gets Skipped

- Files with `tags:` in YAML frontmatter (even if empty list)
- Files with inline `#tag` syntax anywhere in the body
- Files with body text shorter than `--min-chars`
- Files inside directories listed in `--skip-dirs`

## Tag Format

Tags are written into YAML frontmatter as a list:

```yaml
---
Date Created: Apr 22, 2026 10:30
tags:
  - machine-learning
  - python-programming
related:
---
```

## Error Handling

Errors are logged to `auto_tag_errors.log` in the script's directory. File writes are atomic (temp file + rename) to prevent data loss on failure.

## Troubleshooting

**"Not enough files to cluster"** — You need at least 2 eligible files. Check that files aren't being skipped by `--min-chars` or existing tags.

**Slow first run** — The embedding model downloads on first use (~80MB). Subsequent runs use the cached model.

**Tags seem too specific** — Increase `--min-cluster-size` and decrease `--clusters` for broader categories.

**Tags seem too broad** — Increase `--clusters` for more granular categories.
