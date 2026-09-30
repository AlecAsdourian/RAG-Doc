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

---

## Results (added after the run; the questions above were committed first, in `f2bf121`)

Run on 2026-09-29, Windows 11, Python 3.13.7 (the worker and the RAG API
from this worktree, `python -m workers` and `uvicorn api.main:app`), the
backend built from the same commit (`go build`, then the binary run, so that
stopping it stops it; `go run .` leaves its child on Windows).

**There were two runs, and the first one found a bug.** Run 1 is kept
below as the finding it is; run 2, after the fix, is the proof.

### Run 1: the first live fetch refused the archive (a real finding, fixed)

Same setup as run 2 (below), job `0a2bf914-c0e2-44dc-9cdc-f5f26f30f63d`:

```
state=queued last_stage=fetch attempts=1
last_error: FetchFailed: archive top-level directory is not the expected 'AlecAsdourian-ES-SC-API-Navigator-f798806'
```

Everything before extraction worked against real GitHub -- the mint (with
the scope below), the head resolution, the 28,833-byte download from
`codeload.github.com`, and the revocation (HTTP 204) -- and the job
directory was removed on the failure path (the workdir was empty
afterwards). The worker was stopped at once so the deterministic failure
did not spend further attempts.

**Measured, through the user's own `gh` login, first member names only:**

| Repository | Visibility | Asked for | Authenticated | Top-level directory |
|---|---|---|---|---|
| `AlecAsdourian/ES-SC-API-Navigator` | private | full SHA | yes | `AlecAsdourian-ES-SC-API-Navigator-f798806452c0743312780e0cc3e97301286696bd` |
| `AlecAsdourian/ES-SC-API-Navigator` | private | branch `main` | yes | `AlecAsdourian-ES-SC-API-Navigator-f798806452c0743312780e0cc3e97301286696bd` |
| `mealie-recipes/mealie` | public | full SHA | no | `mealie-recipes-mealie-84b2677` |
| `octocat/Hello-World` | public | full SHA | no | `octocat-Hello-World-7fd1a60` |
| `octocat/Hello-World` | public | full SHA | **yes** | `octocat-Hello-World-7fd1a60` |

So GitHub names a **private** repository's archive with the **full** SHA and
a public one's with seven characters, whoever asks. 22-04's check was written
and validated against public mealie only, and as written it refused **every
private repository** -- the product's normal case. Fixed in `76fa5c0`: the
extractor accepts exactly the two measured forms (anything else, an
unobserved abbreviation length included, is still refused before anything is
written), and its message now names the directory the archive held when that
name is plain text. Tested both ways, and mutant M17 (back to sha7 only) is
killed by 29 tests. **This is not the scope check**, which passed against the
real API on the first mint and is unchanged.

Run 1's three logs were scanned like run 2's: no `ghs_`, `-----BEGIN`, `sk-`,
`Authorization:` or `Bearer`. Its scratch container was removed before run 2.

### Run 2: setup

```
$ python live_setup.py          # a scratch pgvector, migrated, seeded as rag_doc_app
container rag2205-live-pg-7d3194 on 127.0.0.1:58175 (image sha256:ccc6e83d6e35...)
pgvector 0.8.6
postgres 16.15 (Debian 16.15-1.pgdg12+2)
migrate up exit 0                # migrate/migrate:v4.19.1, 1/u ... 17/u partitioned_chunks
schema_migrations (17, False)
app role probe (current_user, rolsuper, rolbypassrls): ('rag_doc_app', False, False)
org A 4fd65b8a-03ca-403e-acfb-e96b6e71ba3e, project b0dae58b-241e-453a-bf6b-4d52fa4b9132,
      installation row cc800aa7-7ed6-4a40-aaa2-83617ed036cd, repository 11fe9e4a-8593-4b61-8db0-89fb92e4cc97
org B e7dad925-ebbb-42aa-87e1-7133b1858e00, project ab9b0791-d5eb-4e4d-89d2-18df0df0337e, no repository
enqueued job 811986b8-fbaf-4bb8-bdc3-de3064e72fc3 (was_existing=False)
```

- **The database:** `pgvector/pgvector:pg16` at 22-01's digest, on a free
  loopback port (58175; never 5434, never compose, never a compose volume).
  As its superuser: `CREATE EXTENSION vector`, every migration with the
  `migrate` CLI on the container's own network, and
  `CREATE ROLE rag_doc_app LOGIN NOSUPERUSER NOBYPASSRLS` with the Python
  harness's grants (`USAGE` on the schema; `SELECT, INSERT, UPDATE, DELETE`
  on every table; `USAGE, SELECT` on every sequence). Nothing else ran
  privileged.
- **The seed, as `rag_doc_app`:** organization A with its default project, a
  `github_installations` row for installation **160225622** under A's tenant,
  and a `repositories` row for `AlecAsdourian/ES-SC-API-Navigator`
  (`github_repo_id` **1103353668**, default branch `main`); organization B
  with a project and no repository.
- **The enqueue:** `ENQUEUE_UPSERT_SQL` (the producer's statement) and the
  `pending` projection, under A's tenant. **The real producer path** (the
  install callback, then `POST /api/repositories` with a Supabase session) is
  covered by Phases 20-21's tests; minting a real Supabase session against a
  scratch database is out of scope, as the plan says.
- **The three processes**, each logging to its own file and **each logging
  in as `rag_doc_app`** (the one scratch DSN, probed above):
  - the backend, its `.env` sourced as `docs/local-development.md` documents
    -- with the file's CRLF line endings stripped, since sourcing a CRLF file
    leaves a `\r` on every value -- then `DATABASE_URL` overridden, the
    internal listener on `127.0.0.1:58172`, the public one on 58173, Redis
    pointed at this session's scratch Redis, `LOG_FORMAT=json`;
  - the RAG API (`uvicorn api.main:app` on 58174), the workers `.env` sourced
    for the OpenAI key, `DATABASE_URL` overridden, no `REDIS_URL`;
  - the worker (`python -m workers`), the same, plus
    `INTERNAL_API_URL=http://127.0.0.1:58172` and a scratch `WORKER_WORKDIR`.
    **It never had the App key**: the backend's `.env` was sourced only into
    the backend's shell.
- **Secrets, checked by presence and length only** (never printed): the
  backend `.env` -- `GITHUB_APP_ID` 7 characters, `GITHUB_APP_PRIVATE_KEY_PATH`
  quoted, naming a 1,679-byte file **outside the repository**,
  `GITHUB_WEBHOOK_SECRET` 64, `SUPABASE_WEBHOOK_SECRET` 43,
  `GITHUB_APP_CLIENT_SECRET` 40; no BOM; CRLF endings. The workers `.env` --
  `OPENAI_API_KEY` 164 characters.
- **Every session in the database was `rag_doc_app`**, read from
  `pg_stat_activity` as the scratch superuser while the processes ran (the
  superuser's own probe session excluded):

  ```
  ('rag_doc_app', '(none)', '172.17.0.1', 3)          # the backend's pool and the RAG API's retrievers
  ('rag_doc_app', 'rag-doc-worker', '172.17.0.1', 1)  # the worker's loop (its heartbeat closes with each job)
  role: ('rag_doc_app', rolsuper=False, rolbypassrls=False, rolcanlogin=True)
  ```

### Run 2: the job's full transition log

The worker's log for job `811986b8-...` (worker id, which is the lease
owner, replaced with `<worker>`) and the backend's mint line, in order:

```
20:17:02,172 workers.jobs.runtime     worker <worker> starting: job_types=full_ingest,incremental lease=300s heartbeat=60s idle_poll=5.0s suspended_defer=3600s max_job_duration=7200s heartbeat_statement_timeout=15000ms
20:17:02,192 workers.jobs.transitions job 811986b8-...: claim queued->running job_type=full_ingest org=4fd65b8a-... repo=11fe9e4a-... attempt=1/5 worker=<worker>
20:17:02,200 workers.jobs.transitions job 811986b8-...: mark_started running->running ... sync_state=syncing
20:17:02,203 workers.jobs.runtime     job 811986b8-...: stage=fetch ...
20:17:03.598 backend (slog JSON)      "repository token minted" job=811986b8-... github_repo_id=1103353668 installation=160225622 full_name=AlecAsdourian/ES-SC-API-Navigator expires_at=2026-09-30T04:17:02Z reported_repository_ids=1103353668 reported_permissions=contents:read,metadata:read
20:17:05,176 workers.fetch.archive    downloaded archive for AlecAsdourian/ES-SC-API-Navigator@f798806452c0: 28833 bytes in 1.0s from codeload.github.com
20:17:05,208 workers.fetch.archive    fetched AlecAsdourian/ES-SC-API-Navigator@f798806452c0: 3 indexable files, 12 members, 133120 bytes expanded (123442 declared), skipped={'unsupported': 6}
20:17:05,717 workers.fetch.archive    revoked the repository token for AlecAsdourian/ES-SC-API-Navigator at api.github.com (HTTP 204)
20:17:05,717 workers.ingest.handler   job 811986b8-...: fetched ... (branch main): 3 indexable files, skipped {'unsupported': 6}; archive 28833 bytes via api.github.com to codeload.github.com; download link query parameter names ['token']
20:17:05,721 workers.jobs.runtime     job 811986b8-...: stage=parse ...
20:17:05,985 workers.jobs.runtime     job 811986b8-...: stage=embed ...
20:17:09,989 workers.jobs.runtime     job 811986b8-...: stage=store ...
20:17:10,204 workers.ingest.handler   job 811986b8-...: stored 82 chunks of AlecAsdourian/ES-SC-API-Navigator@f798806452c0 under run a17aa1ff-950e-4be3-979b-bb6992c78f03 (model text-embedding-ada-002), replacing 0
20:17:10,248 workers.jobs.transitions job 811986b8-...: complete running->completed ... sync_state=synced rerun_enqueued=False
```

**Claim to completion: 8.06 s** (fetch 3.5 s including the mint and the
revocation, parse 0.26 s, embed 4.0 s for one OpenAI batch of 82, store
0.26 s). The job waited in the queue for the 25 s it took to start the worker,
so the row's `created_at -> updated_at` reads 35.1 s. Well inside the
provisional two-hour `max_job_duration`.

**The token's scope, as GitHub reported it** (the mint line's
`reported_*` fields, which `47adbb8` added for this purpose): repository ids
`1103353668` -- exactly the one asked for -- and permissions
`contents:read,metadata:read`. **22-04's fail-closed scope check is
confirmed against the real API:** GitHub's reply lists `repositories` and
`permissions`, the one repository and `contents: read`, plus the
`metadata: read` it always adds, which the check allows. The mint was not
refused; the check was not changed. The by-id lookup
(`GET /repositories/{id}`) answered too.

**The revocation:** `DELETE /installation/token`, authenticated by the token
itself, answered **HTTP 204** at the end of the fetch, before parsing began --
on run 1's failed fetch as on run 2's two good ones.

### The nine items

**1. The job row: `completed`, one attempt, `last_stage = 'store'`, cumulative progress.**

```
$ python live_check.py record 811986b8-fbaf-4bb8-bdc3-de3064e72fc3
state=completed attempts=1/5 last_stage=store last_error=None leased=False stalled=False
created_at -> updated_at: 35.1 s (max_job_duration 7200 s)
progress: {"chunks": 82, "chunks_embedded": 82, "chunks_stored": 82, "files_indexable": 3,
           "files_parsed": 3, "parse_errors": 0, "skipped": {"unsupported": 6}}
```

The counts are what was predicted from the tree before the run: three
indexable files, six skipped as `unsupported`. `skipped`, reported at
`parse`, is still on the completed row -- the cumulative rule, live.

**2. The projection:** `sync_state=synced last_synced_at=2026-09-30 03:17:10.472252+00:00`.

**3. The run's commit equals GitHub's, read at the time:**

```
run a17aa1ff-950e-4be3-979b-bb6992c78f03 commit_sha=f798806452c0743312780e0cc3e97301286696bd branch=main
    status=completed chunks_processed=82 attached=True
$ gh api repos/AlecAsdourian/ES-SC-API-Navigator/commits/main --jq .sha
f798806452c0743312780e0cc3e97301286696bd
run commit == GitHub head: True
```

**4. The chunks:** 82, every one with a 1536-dimension vector and
`embedding_model = 'text-embedding-ada-002'`.

```
README.md:                            1 chunk,  all vectors, dims 1536, ['text-embedding-ada-002'], ['fixed_size']
scicrunch_gui_v5_column_filters.py:  68 chunks, all vectors, dims 1536, ['text-embedding-ada-002'], ['class', 'class_summary', 'file_summary', 'function']
scicrunch_poc_v5_column_filters.py:  13 chunks, all vectors, dims 1536, ['text-embedding-ada-002'], ['class', 'class_summary', 'file_summary', 'function']
every file_path tracked at the SHA: True (3 paths, 9 blobs)   # gh api .../git/trees/<sha>?recursive=1
deny-listed names among chunk paths: none
```

**5. Search, as A, for each pre-registered question** (the RAG API's
`/search`, `top_k` 10; recorded, not gated):

| # | Expected file | Its rank | Top five (file:lines, chunk type, breadcrumb) |
|---|---|---|---|
| Q1 | `scicrunch_poc_v5_column_filters.py` | **1** | 1. poc:745-756 function `find_filter_match`; 2. `README.md`:1-27; 3. poc:944-1004 `SciCrunchPOC.analyze_search`; 4. poc file summary; 5. poc:726-743 `build_searchable_index` |
| Q2 | `scicrunch_gui_v5_column_filters.py` | **1** | 1. gui:1458-1463 `SciCrunchGUI._execute_search_thread.progress_update`; 2. gui:372-388 `execute_query_all_results.append_hits`; 3. gui:331-442 `execute_query_all_results`; 4. gui:1723-1725 `_get_download_data.progress_update`; 5. gui:1707-1739 `_get_download_data` |
| Q3 | `README.md` | **2** | 1. gui:1185-1251 `SciCrunchGUI.save_api_key`; 2. `README.md`:1-27; 3. gui:39-71 `load_api_key`; 4. poc:1017-1020 `main`; 5. gui class summary `SciCrunchGUI` |

Every returned chunk's commit, read from its run as A:
`f798806452c0743312780e0cc3e97301286696bd` -- item 3's. (The API's response
model carries no `provenance` field, so the commit is read by chunk id; the
`QueryEngine` itself returns it, and the end-to-end test asserts it there.)
Each response's metadata read `fts_results: 0, vector_results: 50` -- the
keyword leg returned nothing for any of the three natural-language
questions, as ISS-029 predicts; these are vector-leg results. Q1 found the
exact function the question was written from; Q2's first hit is the
progress callback inside the thread that calls the expected function, which
ranks third; Q3's expected file ranks second behind the GUI's own
`save_api_key`, which is about the same keyring.

**6. Isolation, as the RAG API connected as `rag_doc_app`:** `/search` with
**B's** organization id and **A's** repository id, for each question:

```
Q1 as B: HTTP 200, total_results=0, results=0
Q2 as B: HTTP 200, total_results=0, results=0
Q3 as B: HTTP 200, total_results=0, results=0
```

**7. Idempotency:** enqueued again (`4fac68b9-e32a-42ec-a255-b259088ec9ab`,
the producer's statement) and run by the same worker:

```
state=completed last_stage=store attempts=1, same run a17aa1ff-950e-4be3-979b-bb6992c78f03
... stored 82 chunks of AlecAsdourian/ES-SC-API-Navigator@f798806452c0 under run a17aa1ff-... replacing 82
chunks after the second ingest: 82 (after the first: 82)
duplicate (file_path, start_line, end_line, content_hash): 0
runs referenced by the chunks: ['a17aa1ff-950e-4be3-979b-bb6992c78f03']
ids surviving from the first ingest: 0 of 82 (0 means replaced)
```

The second token was minted with the same reported scope and revoked with
HTTP 204 as well.

**8. Secrets:**

```
$ python live_check.py logs
backend.log:   6 lines; ghs_=0, -----BEGIN=0, sk- (key-shaped)=0, sk- (substring)=0, Authorization:=0, Bearer=0
worker.log:   50 lines; ghs_=0, -----BEGIN=0, sk- (key-shaped)=0, sk- (substring)=0, Authorization:=0, Bearer=0
rag.log:     112 lines; ghs_=0, -----BEGIN=0, sk- (key-shaped)=0, sk- (substring)=0, Authorization:=0, Bearer=0
jobs: [('811986b8-...', 'completed', 1, last_error IS NULL=True), ('4fac68b9-...', 'completed', 1, True)]
```

**The private download link carries a credential of its own.** The
redirect from `api.github.com` to `codeload.github.com` had one query
parameter, named **`token`** (the name only; the value was never read into a
log or this record). 22-04's public mealie link had none. This settles
22-CONTEXT's open question: a private repository's download link does carry
a credential, which is why the fetcher never logs a URL and holds `httpx` at
WARNING.

**9. Teardown:**

```
stopped: backend.exe, the RAG API (uvicorn) and the worker, found by this session's scratch path only
$ docker rm -f rag2205-live-pg-7d3194
workdir entries left: 0
docker ps -a: identical to the snapshot taken before run 1 (27 containers; every testtgsd-* compose container still exited)
compose volumes: testtgsd_postgres_data, testtgsd_qdrant_data -- identical inspect output (created 2026-01-09)
```

Compose was never started, and no compose container, volume or port (5434
included) was touched at any point.

**Cost:** two ingests of 30,728 tokens each and six one-question query
embeddings (18-19 tokens each) -- about 61,600 tokens of
`text-embedding-ada-002`, about $0.006.
