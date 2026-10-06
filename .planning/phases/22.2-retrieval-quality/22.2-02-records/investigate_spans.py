#!/usr/bin/env python3
"""For each chunk the investigation found CHANGED, what changed: its span before and after, and the
lines the after-span added (22.2-02, the tripwire's step 4: is each change what QD6 says?).

    investigate_spans.py <records-dir> <mealie-checkout>

Reads the census rows (census-before/, census-after/) and the corpus at its
pin; prints each chunk's lines before and after, and the source lines that
are in the after-span only. A QD6 change adds exactly the definition's
decorator lines above it, and nothing else; its breadcrumb is the same key.
"""
import gzip
import json
import sys
from pathlib import Path

CHUNKS = [
    ("mealie/services/scheduler/scheduler_service.py", "function", "SchedulerService.start"),
    ("mealie/services/event_bus_service/event_bus_listeners.py", "function",
     "AppriseEventListener.update_urls_with_event_data"),
    ("mealie/routes/recipe/recipe_crud_routes.py", "function", "RecipeController.test_parse_recipe_url"),
    ("mealie/routes/recipe/recipe_crud_routes.py", "function", "RecipeController.scrape_image_url"),
    ("mealie/routes/recipe/recipe_crud_routes.py", "class", "RecipeController"),
    ("mealie/routes/recipe/recipe_crud_routes.py", "class_summary", "RecipeController"),
    ("mealie/db/models/recipe/recipe.py", "function", "receive_description"),
]


def rows(records: Path, stage: str):
    out = {}
    with gzip.open(records / stage / "chunks-mealie.jsonl.gz", "rt", encoding="utf-8") as fh:
        for r in map(json.loads, fh):
            out.setdefault((r["file_path"], r["chunk_type"], r["breadcrumb"]), []).append(r)
    return out


def main(records: Path, corpus: Path) -> int:
    before, after = rows(records, "census-before"), rows(records, "census-after")
    for key in CHUNKS:
        b, a = before.get(key, []), after.get(key, [])
        print(f"== {key[0]} {key[1]} {key[2]}")
        for rb, ra in zip(b, a):
            print(f"   before lines {rb['start_line']}-{rb['end_line']} ({rb['chars']} chars, {rb['tokens']} tokens); "
                  f"after lines {ra['start_line']}-{ra['end_line']} ({ra['chars']} chars, {ra['tokens']} tokens)")
            lines = (corpus / key[0]).read_text(encoding="utf-8").splitlines()
            added = [n for n in range(ra["start_line"], ra["end_line"] + 1)
                     if not rb["start_line"] <= n <= rb["end_line"]]
            removed = [n for n in range(rb["start_line"], rb["end_line"] + 1)
                       if not ra["start_line"] <= n <= ra["end_line"]]
            for n in added:
                print(f"   + {n:>4} | {lines[n - 1]}")
            for n in removed:
                print(f"   - {n:>4} | {lines[n - 1]}")
            if not added and not removed:
                print("   (same lines: the text differs inside the span -- a summary chunk's text)")
        if len(b) != len(a):
            print(f"   rows: {len(b)} before, {len(a)} after")
    return 0


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1]), Path(sys.argv[2])))
