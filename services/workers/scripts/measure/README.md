# `scripts/measure` — 22.1-05's measurements

The scripts that produced `22.1-05-operating-numbers.md` and the vectors
`22.1-05-recall.md` was seeded with. The rules they serve are that record's
"Rules" section, committed before anything was measured; **a re-run never
edits a rule**.

| Script | What it does |
|---|---|
| `scratch_db.py` | `up`: a scratch `pgvector/pgvector:pg16` (22-01's digest) on a Docker-assigned loopback port, migrated, with `rag_doc_app` LOGIN NOSUPERUSER NOBYPASSRLS. `down`: removes exactly that container. Never compose, never 5434. DSNs go to `<state-dir>/state.json`, never to the terminal. |
| `ingest_timings.py` | One real ingest through the real `Worker`, timed stage by stage, with the heartbeat and the store's statements timed client-side, every OpenAI response hooked, peak RSS and peak workdir bytes. `--no-embed` is the free dry run. |
| `ledger.py` | The one spend ledger. `show` prints the total. |
| `export_vectors.py` | Exports a scratch database's real vectors (no content) to `float32` `.npy` files with a manifest. |
| `at_cap.py` | Builds the synthetic archives for the at-cap runs (`build`) and the 500 MB disk run (`bomb`). |

## Running them

Run everything **inside `python:3.11-slim`**, the production image, with
`requirements.txt` installed and tiktoken's `cl100k_base` file cached in the
image (tiktoken downloads it on first use otherwise, and a dry run should
need no network but GitHub's). Mount the repository **read-only** and a
scratch directory **outside the repository** read-write, on the scratch
database's network namespace:

```bash
docker run --rm --network container:<scratch-pg> \
  -v <repo>:/repo:ro -v <scratch>:/scratch <image> \
  python scripts/measure/ingest_timings.py --state /scratch/state/state.json --inside \
    --full-name miniflux/v2 --sha 76889f08b12c2577b37e524e645afa5dd46ff050 --branch main \
    --label miniflux --no-embed --records /scratch/records --record-name dry-miniflux
```

- **A dry run** (`--no-embed`) refuses to start if `OPENAI_API_KEY` is in its
  environment, and spends nothing by construction.
- **An embedding run** needs `OPENAI_API_KEY` (passed by name, `-e
  OPENAI_API_KEY`, never on a command line), `--ledger`, `--budget-usd` and
  `--projected-usd`; the ledger refuses a run that would pass the cap, and
  guards every request.
- **An at-cap run** passes `--local-archive` (an `at_cap.py` archive),
  `--replay-vectors` (an `export_vectors.py` array) and `--tag-copies`, and
  refuses to start with a key in its environment.
- To measure **a specific version of the code**, mount a `git archive` of
  that commit instead of the working tree, and pass `--code-label`; every
  record also states the store order it measured (`write_results_order`).
- `--copies N --workers N` is N4's concurrency confirmation: N repository
  rows, every job enqueued before any worker starts.
- `--job-type incremental` runs the full ingest until 22.1-02 (one handler
  is registered for both), and the record says so.

## Which trigger calls for which run

The triggers are listed in `22.1-05-operating-numbers.md` with the rules.

| Trigger | Re-run |
|---|---|
| Phase 24's host | everything: the real runs, the at-cap runs before deciding N1, N2, N3, N6; the confirmation for N4 |
| 22.2-05 adopting `text-embedding-3-small` | the real runs (N4, N5) and the recall test on 3-small vectors; **its re-embed is new spend and needs its own approval** |
| 22.2-02 changing chunks per repository by more than 20 % | the dry runs (`--no-embed`, free) for N1's parse and tokens; then the at-cap runs |
| 22.1-02's incremental ingest | `--job-type incremental`, against a repository already ingested |
| A change to U6's caps | `at_cap.py` at the new caps, then the at-cap runs |
| A change of OpenAI key or tier | one small real run (the headers give `TPM`/`RPM`), then N4 and N5 |
