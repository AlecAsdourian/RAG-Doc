"""The `recall` marker: D2's recall test (22.1-05), run on demand, not in CI.

The workers package has no pytest configuration file, so the marker is
registered here. CI's default run collects `tests/recall` and SKIPS it, with
the reason naming what it needs: `RECALL_VECTORS_PATH` (22.1-05's vector
export) and `DATABASE_TEST_URL` (a fresh, migrated scratch database). See
`22.1-05-recall.md`.
"""


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "recall: D2's recall test on real vectors (22.1-05); needs RECALL_VECTORS_PATH "
        "and a scratch DATABASE_TEST_URL, so it runs on demand",
    )
