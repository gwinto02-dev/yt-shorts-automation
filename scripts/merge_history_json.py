"""
merge_history_json.py

Content-aware merge for the pipeline's append-only JSON history log files
(data/*.json). These files are lists of dict entries that different pipeline
runs append to independently. Git's default line-based text merge/rebase is
poorly suited to this — two runs appending different entries near the same
position in the file produces a textual conflict even though the actual
*data* isn't in conflict at all (it's just two different new entries that
both need to be kept).

This script merges two versions of the same JSON list file by taking the
union of all entries (based on exact content equality — duplicate entries
collapse to one), then sorts by the "date" field if present so the merged
history stays in chronological order. This avoids git-level conflicts
entirely for this specific file shape.

Usage:
    python scripts/merge_history_json.py <ours_path> <theirs_path> <output_path>

If either input file doesn't exist or isn't valid JSON, the other one is
used as-is. If both exist, their entries are unioned.
"""

import json
import sys
from pathlib import Path


def _load_list(path: Path):
    if not path.exists():
        return []
    try:
        content = path.read_text(encoding="utf-8")
        if not content.strip():
            return []
        data = json.loads(content)
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, UnicodeDecodeError):
        return []


def merge_json_lists(ours_path: str, theirs_path: str, output_path: str) -> None:
    ours = _load_list(Path(ours_path))
    theirs = _load_list(Path(theirs_path))

    # Union by exact-content equality — an entry appearing in both sides
    # (e.g. because nothing actually diverged for that entry) collapses to
    # a single copy rather than being duplicated.
    seen = set()
    merged = []
    for entry in ours + theirs:
        key = json.dumps(entry, sort_keys=True)
        if key not in seen:
            seen.add(key)
            merged.append(entry)

    # Keep chronological order where a "date" field exists, so the merged
    # file reads the same way a normal sequential log would.
    def _sort_key(entry):
        return entry.get("date", "") if isinstance(entry, dict) else ""

    merged.sort(key=_sort_key)

    Path(output_path).write_text(
        json.dumps(merged, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(f"Merged {len(ours)} local + {len(theirs)} remote entries -> {len(merged)} unique entries in {output_path}")


if __name__ == "__main__":
    if len(sys.argv) != 4:
        print("Usage: python merge_history_json.py <ours_path> <theirs_path> <output_path>")
        sys.exit(1)
    merge_json_lists(sys.argv[1], sys.argv[2], sys.argv[3])
