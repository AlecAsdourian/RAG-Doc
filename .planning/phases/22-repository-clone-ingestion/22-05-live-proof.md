# 22-05 live proof: `AlecAsdourian/ES-SC-API-Navigator`, indexed and searched end to end

The approved repository (U-approval of 2026-09-17: GitHub id 1103353668,
private, Python, 75 KB), fetched through the development App (`rag-doc-dev`,
id 4880866; installation 160225622) into a **scratch** database, never
compose, with every process running as `rag_doc_app`. Sending its code to
OpenAI's embedding API is part of that approval.

This file is written in two commits. **The questions below were committed
before any search ran**; the proof's results were added afterwards.

## Pre-registered questions (committed before any search)

Read on 2026-09-29, **before** the ingest and before any search, through the
user's own `gh` login, read-only:

```
$ gh api repos/AlecAsdourian/ES-SC-API-Navigator/commits/main --jq '{sha: .sha, date: .commit.committer.date}'
{"date":"2026-04-01T18:29:29Z","sha":"f798806452c0743312780e0cc3e97301286696bd"}

$ gh api "repos/AlecAsdourian/ES-SC-API-Navigator/git/trees/f798806452c0743312780e0cc3e97301286696bd?recursive=1" \
    --jq '.truncated, (.tree[] | "\(.type) \(.size // "-") \(.path)")'
false
tree - .github
tree - .github/workflows
blob 1003 .github/workflows/build-macos.yml
blob 806 .github/workflows/build-windows.yml
blob 1013 .gitignore
blob 1736 README.md
blob 338 requirements.txt
blob 1510 scicrunch_downloader.spec
blob 2091 scicrunch_downloader_mac.spec
blob 69449 scicrunch_gui_v5_column_filters.py
blob 45496 scicrunch_poc_v5_column_filters.py
```

**Expected from the filters (22-04), before running anything:** three
indexable files -- `README.md`, `scicrunch_gui_v5_column_filters.py`,
`scicrunch_poc_v5_column_filters.py` -- and six skipped as `unsupported`
(the two workflow `.yml` files, `.gitignore`, `requirements.txt` and the two
`.spec` files). No deny-listed name is in the tree.

The three files were read at that SHA (`gh api .../contents/<path>?ref=<sha>`)
and each question was written against a passage that answers it:

| # | Question (sent verbatim to `/search`) | Expected file | The passage |
|---|---|---|---|
| Q1 | `How does a free-text search term get matched to the facet and field it belongs to?` | `scicrunch_poc_v5_column_filters.py` | `find_filter_match(search_term, index_name=None)`, docstring "Find which facet a search term belongs to", over `build_searchable_index()`'s reverse lookup of subfacet value -> facet, field and query type |
| Q2 | `Which function pages through every matching record with a scroll_id and reports progress through a callback?` | `scicrunch_gui_v5_column_filters.py` | `execute_query_all_results(index_name, query, progress_callback=None, ...)`: `_search?scroll=2m` with a batch size of 1000, then a loop on `scroll_id` calling `progress_callback(downloaded, effective_total)` |
| Q3 | `Which operating-system credential stores keep the SciCrunch API key on Windows, macOS and Linux?` | `README.md` | "## API Key": Windows Credential Locker, macOS Keychain, Linux Secret Service API (GNOME Keyring / KWallet) |

**How they are judged: recorded, not gated** (the plan). For each question
the record is the rank of the expected file among the results, the top five
files, and the returned chunk's commit. "Searchable" means results from this
repository with the right provenance. Two caveats inherited from 22-03, stated
before the run so they cannot be used to explain a result away afterwards:
the keyword leg returns nothing for most natural-language questions
(ISS-029), so these are effectively vector-leg results; and at this size the
planner serves the vector leg by exact scan, never HNSW. Other files may
legitimately rank too -- `README.md` mentions the scroll API and the key's
storage in prose, and the GUI file also uses facets -- which is why the rank
of the expected file is recorded rather than a pass/fail.
