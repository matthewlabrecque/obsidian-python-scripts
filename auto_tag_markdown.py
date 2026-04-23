#!/usr/bin/env python3
"""
auto_tag_markdown.py

Scans a directory of Markdown files, identifies those without existing tags,
and uses ML (sentence-transformers + HDBSCAN + TF-IDF) to discover and suggest
broad categorical tags. Outputs a report by default; use --apply to write tags
into YAML frontmatter.

Dependencies (pip install):
    sentence-transformers
    scikit-learn
    hdbscan
"""

import sys
import os
import re
import json
import logging
import tempfile
import argparse
from pathlib import Path
from datetime import datetime

import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.cluster import KMeans

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

ERROR_LOG = Path(__file__).parent / "auto_tag_errors.log"

logging.basicConfig(
    filename=str(ERROR_LOG),
    filemode="a",
    level=logging.ERROR,
    format="%(asctime)s  %(levelname)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_MIN_CHARS = 200
DEFAULT_MIN_CLUSTER_SIZE = 3
DEFAULT_MAX_TAGS = 12
DEFAULT_NUM_CLUSTERS = 8
INLINE_TAG_RE = re.compile(r"(?<!\w)#[\w-]+")
FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
TAGS_KEY_RE = re.compile(r"^tags\s*:", re.MULTILINE)
TAG_LIST_ITEM_RE = re.compile(r"^\s+-\s+(.+)$")
INLINE_TAG_LIST_RE = re.compile(r"^tags\s*:\s*\[(.+)\]", re.MULTILINE)
MD_SYNTAX_RE = re.compile(
    r"(```[\s\S]*?```|`[^`]+`|!\[.*?\]\(.*?\)|\[([^\]]*)\]\(.*?\)"
    r"|^#{1,6}\s+|^\s*[-*+]\s+|^\s*\d+\.\s+|~~([^~]+)~~"
    r"|(\*\*|__)(.*?)\2|(\*|_)(.*?)\4)",
    re.MULTILINE,
)
SLUGIFY_RE = re.compile(r"[^a-z0-9]+")

# ---------------------------------------------------------------------------
# Phase 1: Scan & Filter
# ---------------------------------------------------------------------------


def has_tags_in_frontmatter(content: str) -> bool:
    """Return True if the YAML frontmatter contains a non-empty tags list."""
    m = FRONTMATTER_RE.match(content)
    if not m:
        return False
    yaml_block = m.group(1)
    if not TAGS_KEY_RE.search(yaml_block):
        return False
    inline = INLINE_TAG_LIST_RE.search(yaml_block)
    if inline:
        items = [t.strip().strip("'\"") for t in inline.group(1).split(",")]
        return any(items)
    in_tags = False
    for line in yaml_block.splitlines():
        if TAGS_KEY_RE.match(line):
            in_tags = True
            continue
        if in_tags:
            m2 = TAG_LIST_ITEM_RE.match(line)
            if m2:
                tag = m2.group(1).strip()
                if tag:
                    return True
            elif line and not line.startswith(" ") and not line.startswith("\t"):
                in_tags = False
    return False


def has_inline_tags(body: str) -> bool:
    """Return True if the body contains inline #tag syntax."""
    return bool(INLINE_TAG_RE.search(body))


def extract_body(content: str) -> str:
    """Strip YAML frontmatter and return the body text."""
    m = FRONTMATTER_RE.match(content)
    if m:
        return content[m.end():]
    return content


def clean_text(body: str) -> str:
    """Remove Markdown syntax to produce plain text suitable for embedding."""
    text = MD_SYNTAX_RE.sub("", body)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def scan_directory(
    target: Path,
    min_chars: int,
    skip_dirs: set[str],
) -> list[dict]:
    """
    Walk *target* and return a list of dicts for files that:
      - are *.md
      - have no existing tags (frontmatter or inline)
      - have body text >= min_chars
      - are not inside a skipped directory
    Each dict: {"path": Path, "body": str, "clean_text": str}
    """
    results = []

    for md_file in sorted(target.rglob("*.md")):
        rel = md_file.relative_to(target)
        if any(part in skip_dirs for part in rel.parts[:-1]):
            continue

        try:
            content = md_file.read_text(encoding="utf-8")
        except Exception as exc:
            logger.error("Cannot read '%s': %s", rel, exc)
            continue

        body = extract_body(content)
        clean = clean_text(body)

        if len(clean) < min_chars:
            continue

        if has_tags_in_frontmatter(content) or has_inline_tags(body):
            continue

        results.append({"path": md_file, "body": body, "clean_text": clean})

    return results

# ---------------------------------------------------------------------------
# Phase 2 & 3: Embed, Cluster, Name Tags
# ---------------------------------------------------------------------------


def embed_documents(documents: list[str], model_name: str = "all-MiniLM-L6-v2"):
    """
    Embed a list of document strings using sentence-transformers.
    Returns (embeddings_array, model).
    """
    from sentence_transformers import SentenceTransformer

    print(f"  Loading embedding model: {model_name} ...")
    model = SentenceTransformer(model_name)
    print(f"  Embedding {len(documents)} documents ...")
    embeddings = model.encode(documents, show_progress_bar=True, convert_to_numpy=True)
    return embeddings, model


def cluster_documents(
    embeddings: np.ndarray,
    num_clusters: int,
    min_cluster_size: int,
) -> np.ndarray:
    """
    Cluster embeddings using KMeans.
    Returns an array of cluster labels (same length as embeddings).
    -1 means unclustered/outlier (not used with KMeans, all assigned).
    """
    n_samples = embeddings.shape[0]
    k = min(num_clusters, n_samples)
    if k < 2:
        return np.zeros(n_samples, dtype=int)

    print(f"  Clustering into {k} groups ...")
    km = KMeans(n_clusters=k, random_state=42, n_init=10)
    labels = km.fit_predict(embeddings)
    return labels


def name_clusters(
    documents: list[str],
    labels: np.ndarray,
    max_tags: int,
    min_cluster_size: int,
) -> dict[int, str]:
    """
    For each cluster, derive a tag name using TF-IDF.
    Returns {cluster_id: "tag-name"}.
    """
    cluster_names = {}

    for cid in sorted(set(labels)):
        mask = labels == cid
        cluster_docs = [documents[i] for i in range(len(documents)) if mask[i]]

        if len(cluster_docs) < min_cluster_size:
            cluster_names[cid] = None
            continue

        vectorizer = TfidfVectorizer(
            max_features=50,
            stop_words="english",
            ngram_range=(1, 2),
            min_df=1,
        )
        tfidf_matrix = vectorizer.fit_transform(cluster_docs)
        feature_names = vectorizer.get_feature_names_out()
        scores = tfidf_matrix.sum(axis=0).A1
        top_indices = scores.argsort()[-6:][::-1]
        top_terms = [feature_names[i] for i in top_indices]

        seen = set()
        selected = []
        for term in top_terms:
            words = term.split()
            if any(w in seen for w in words):
                continue
            selected.append(term)
            seen.update(words)
            if len(selected) >= 2:
                break

        if not selected:
            selected = top_terms[:2]

        tag_name = SLUGIFY_RE.sub("-", "-".join(selected).lower()).strip("-")
        cluster_names[cid] = tag_name

    return cluster_names

# ---------------------------------------------------------------------------
# Phase 4: Suggestion Report
# ---------------------------------------------------------------------------


def build_suggestions(
    files: list[dict],
    labels: np.ndarray,
    cluster_names: dict[int, str],
) -> list[dict]:
    """
    Build a list of per-file suggestions.
    Each: {"path": str, "tags": [str], "cluster": int, "confidence": float}
    """
    suggestions = []
    cluster_sizes = {}
    for cid in set(labels):
        cluster_sizes[cid] = int(np.sum(labels == cid))

    for i, f in enumerate(files):
        cid = int(labels[i])
        tag = cluster_names.get(cid)
        rel = str(f["path"])
        if tag:
            suggestions.append({
                "path": rel,
                "tags": [tag],
                "cluster": cid,
                "confidence": round(cluster_sizes[cid] / len(files), 2),
            })
        else:
            suggestions.append({
                "path": rel,
                "tags": [],
                "cluster": cid,
                "confidence": 0.0,
            })

    return suggestions


def print_report(suggestions: list[dict], cluster_names: dict[int, str]) -> None:
    """Print a human-readable report to stdout."""
    tag_counts = {}
    for s in suggestions:
        for t in s["tags"]:
            tag_counts[t] = tag_counts.get(t, 0) + 1

    print("\n" + "=" * 60)
    print("  DISCOVERED TAGS")
    print("=" * 60)

    if tag_counts:
        for tag, count in sorted(tag_counts.items(), key=lambda x: -x[1]):
            print(f"  #{tag:<30} ({count} files)")
    else:
        print("  No tags discovered.")

    print("\n" + "-" * 60)
    print("  PER-FILE SUGGESTIONS")
    print("-" * 60)

    tagged = [s for s in suggestions if s["tags"]]
    untagged = [s for s in suggestions if not s["tags"]]

    for s in tagged:
        tags_str = ", ".join(f"#{t}" for t in s["tags"])
        print(f"  {s['path']}")
        print(f"    -> {tags_str}  (confidence: {s['confidence']})")

    if untagged:
        print(f"\n  {len(untagged)} file(s) could not be confidently tagged:")
        for s in untagged[:10]:
            print(f"    - {s['path']}")
        if len(untagged) > 10:
            print(f"    ... and {len(untagged) - 10} more")

    print("\n" + "=" * 60)
    print(f"  Total scanned: {len(suggestions)}")
    print(f"  Tagged:        {len(tagged)}")
    print(f"  Untagged:      {len(untagged)}")
    print("=" * 60 + "\n")


def print_json_report(suggestions: list[dict], cluster_names: dict[int, str]) -> None:
    """Print a JSON report to stdout."""
    output = {
        "generated_at": datetime.now().isoformat(),
        "discovered_tags": {},
        "files": suggestions,
    }
    tag_counts = {}
    for s in suggestions:
        for t in s["tags"]:
            tag_counts[t] = tag_counts.get(t, 0) + 1
    output["discovered_tags"] = tag_counts
    print(json.dumps(output, indent=2))

# ---------------------------------------------------------------------------
# Phase 5: Apply Mode
# ---------------------------------------------------------------------------


def parse_frontmatter_fields(content: str) -> tuple[dict, str]:
    """Parse YAML frontmatter into a dict + body string."""
    m = FRONTMATTER_RE.match(content)
    if not m:
        return {}, content

    yaml_block = m.group(1)
    body = content[m.end():]
    fields = {}

    for line in yaml_block.splitlines():
        kv = re.match(r"^(\w[\w\s]*?)\s*:\s*(.*)$", line)
        if kv:
            key = kv.group(1).strip()
            val = kv.group(2).strip()
            fields[key] = val

    return fields, body


def build_frontmatter_block(fields: dict) -> str:
    """Build a YAML frontmatter string from a dict."""
    lines = ["---"]
    for key, val in fields.items():
        lines.append(f"{key}: {val}")
    lines.append("---")
    return "\n".join(lines)


def apply_tags_to_file(file_path: Path, tags: list[str]) -> None:
    """Write tags into the YAML frontmatter of *file_path* (atomic write)."""
    content = file_path.read_text(encoding="utf-8")
    fields, body = parse_frontmatter_fields(content)

    if "Date Created" not in fields:
        try:
            st = file_path.stat()
            ts = getattr(st, "st_birthtime", None) or st.st_mtime
            dt = datetime.fromtimestamp(ts)
            fields["Date Created"] = dt.strftime("%b %d, %Y %H:%M")
        except Exception:
            pass

    if "related" not in fields:
        fields["related"] = ""

    tags_yaml = "\n".join(f"  - {t}" for t in tags)
    fields["tags"] = f"\n{tags_yaml}"

    fm = build_frontmatter_block(fields)
    new_content = fm + "\n" + body if body.strip() else fm + body

    tmp_fd, tmp_path = tempfile.mkstemp(dir=file_path.parent, suffix=".tmp")
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
            f.write(new_content)
        os.replace(tmp_path, file_path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def apply_suggestions(suggestions: list[dict], base_dir: Path) -> int:
    """Apply suggested tags to files. Returns count of files modified."""
    modified = 0
    for s in suggestions:
        if not s["tags"]:
            continue
        fpath = Path(s["path"]) if os.path.isabs(s["path"]) else base_dir / s["path"]
        if not fpath.exists():
            print(f"  SKIP (not found): {s['path']}")
            continue
        try:
            apply_tags_to_file(fpath, s["tags"])
            print(f"  OK  {s['path']} -> {', '.join(s['tags'])}")
            modified += 1
        except Exception as exc:
            print(f"  ERR {s['path']} -> {exc}", file=sys.stderr)
            logger.error("Failed to tag '%s': %s", s["path"], exc)
    return modified

# ---------------------------------------------------------------------------
# Entry Point
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(
        description="Scan Markdown files and suggest auto-discovered tags using ML.",
    )
    parser.add_argument("directory", help="Directory to scan for Markdown files")
    parser.add_argument(
        "--min-chars",
        type=int,
        default=DEFAULT_MIN_CHARS,
        help=f"Minimum body text length to consider a file (default: {DEFAULT_MIN_CHARS})",
    )
    parser.add_argument(
        "--skip-dirs",
        nargs="*",
        default=[],
        help="Directory names to skip (e.g. .trash 98-META)",
    )
    parser.add_argument(
        "--clusters",
        type=int,
        default=DEFAULT_NUM_CLUSTERS,
        help=f"Number of tag clusters to discover (default: {DEFAULT_NUM_CLUSTERS})",
    )
    parser.add_argument(
        "--min-cluster-size",
        type=int,
        default=DEFAULT_MIN_CLUSTER_SIZE,
        help=f"Minimum files per cluster to generate a tag (default: {DEFAULT_MIN_CLUSTER_SIZE})",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write suggested tags into file frontmatter (default: dry-run / report only)",
    )
    parser.add_argument(
        "--output",
        choices=["text", "json"],
        default="text",
        help="Output format for the report (default: text)",
    )
    parser.add_argument(
        "--model",
        default="all-MiniLM-L6-v2",
        help="Sentence-transformers model to use (default: all-MiniLM-L6-v2)",
    )

    args = parser.parse_args()

    target = Path(args.directory).expanduser().resolve()
    if not target.exists():
        print(f"Error: Directory does not exist: {target}")
        sys.exit(1)
    if not target.is_dir():
        print(f"Error: Path is not a directory: {target}")
        sys.exit(1)

    skip_dirs = set(args.skip_dirs)

    print(f"Target directory: {target}")
    print(f"Min chars: {args.min_chars}")
    print(f"Skip dirs: {skip_dirs or '(none)'}")
    print()

    # Phase 1: Scan
    print("Phase 1: Scanning for untagged files ...")
    files = scan_directory(target, args.min_chars, skip_dirs)
    print(f"  Found {len(files)} untagged files eligible for tagging.\n")

    if len(files) < 2:
        print("Not enough files to cluster. Need at least 2.")
        sys.exit(0)

    documents = [f["clean_text"] for f in files]

    # Phase 2 & 3: Embed + Cluster + Name
    print("Phase 2-3: Embedding and clustering ...")
    embeddings, _ = embed_documents(documents, args.model)

    labels = cluster_documents(embeddings, args.clusters, args.min_cluster_size)
    cluster_names = name_clusters(documents, labels, args.clusters, args.min_cluster_size)

    valid_tags = {cid: name for cid, name in cluster_names.items() if name}
    print(f"  Discovered {len(valid_tags)} tags.\n")

    # Phase 4: Report
    print("Phase 4: Building suggestions ...")
    suggestions = build_suggestions(files, labels, cluster_names)

    if args.output == "json":
        print_json_report(suggestions, cluster_names)
    else:
        print_report(suggestions, cluster_names)

    # Phase 5: Apply
    if args.apply:
        print("Phase 5: Applying tags to files ...")
        modified = apply_suggestions(suggestions, target)
        print(f"\nDone. Modified {modified} file(s).")
    else:
        print("Dry-run mode. Use --apply to write tags to files.")


if __name__ == "__main__":
    main()
