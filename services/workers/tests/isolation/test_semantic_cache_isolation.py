"""Tenant isolation for the semantic cache (ISS-020).

WHY THIS FILE EXISTS SEPARATELY FROM THE OTHER ISOLATION TESTS.

Everything else in this directory proves that Postgres RLS withholds another
tenant's rows. This layer never reaches Postgres: `AnswerGenerator.generate`
consults the cache at the top and returns on a hit, so RLS is not a backstop
here and the cache key is the entire control.

WHY IT DOES NOT USE `with_two_orgs`. That fixture builds Postgres rows via
testcontainers. The cache is Redis-only and needs neither, so requiring them
would make this test harder to run than it has to be -- and it already runs in
fewer places than it should (see ISS-022: no CI job runs the Python worker
tests at all).

Point `REDIS_URL` at any reachable Redis to run it:

    docker run -d -p 6379:6379 redis:7-alpine
    REDIS_URL=redis://localhost:6379 pytest tests/isolation/test_semantic_cache_isolation.py
"""

import os
import uuid

import pytest
import redis as redis_lib

from workers.generation.semantic_cache import SemanticCache

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379")

# Identical embeddings, so similarity is 1.0 and a hit is guaranteed whenever
# the key matches. This isolates the key from the similarity threshold: if a
# leak happens, it is the scoping and not a fluke of cosine distance.
EMBEDDING = [0.1] * 16


def _redis_available() -> bool:
    try:
        redis_lib.from_url(REDIS_URL, socket_connect_timeout=2).ping()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _redis_available(),
    reason=f"no Redis at {REDIS_URL}; set REDIS_URL to run",
)


@pytest.fixture
def cache():
    """A cache whose keys are namespaced per test run, then cleaned up."""
    # `embedding_generator` is unused by the read/write paths under test --
    # both take the embedding as an argument -- so None is honest here.
    c = SemanticCache(redis_url=REDIS_URL, embedding_generator=None)
    yield c
    for key in c.redis_client.scan_iter(match="cache:query:*"):
        c.redis_client.delete(key)


def test_cache_does_not_leak_across_tenants(cache):
    """The core of ISS-020.

    Org B holds org A's repository_id -- which is all an attacker needed
    before this was fixed -- and asks an identical question. B must not
    receive A's answer.
    """
    org_a, org_b = uuid.uuid4(), uuid.uuid4()
    repo = uuid.uuid4()  # THE SAME repository id for both callers

    cache.cache_response(
        query="how does auth work",
        query_embedding=EMBEDDING,
        organization_id=org_a,
        repository_id=repo,
        response={"answer": "A's private answer", "sources": []},
    )

    # Org A gets its own answer back: the cache still does its job.
    hit = cache.get_cached_response(
        query="how does auth work",
        query_embedding=EMBEDDING,
        organization_id=org_a,
        repository_id=repo,
    )
    assert hit is not None, "org A should get its own cached answer"
    assert hit["answer"] == "A's private answer"

    # Org B, same repository, same question, identical embedding.
    leaked = cache.get_cached_response(
        query="how does auth work",
        query_embedding=EMBEDDING,
        organization_id=org_b,
        repository_id=repo,
    )
    assert leaked is None, (
        "ISS-020 REGRESSION: org B read org A's cached answer using only A's "
        "repository_id. The cache returns before Postgres is reached, so "
        "there is no RLS backstop -- the key is the only control."
    )


def test_near_match_does_not_leak_either(cache):
    """Matching is cosine similarity, not hash equality.

    `get_cached_response` scans the repository's entries and returns the best
    above the threshold; the `query` argument is not used for matching. So a
    *differently worded* question is enough to pull an entry, and the tenant
    check has to hold for near-matches too, not just identical text.
    """
    org_a, org_b = uuid.uuid4(), uuid.uuid4()
    repo = uuid.uuid4()

    cache.cache_response(
        query="how does authentication work",
        query_embedding=EMBEDDING,
        organization_id=org_a,
        repository_id=repo,
        response={"answer": "A's private answer", "sources": []},
    )

    leaked = cache.get_cached_response(
        query="explain the auth flow",  # different words, same embedding
        query_embedding=EMBEDDING,
        organization_id=org_b,
        repository_id=repo,
    )
    assert leaked is None, "ISS-020 REGRESSION: near-match leaked across tenants"


def test_entry_with_mismatched_org_is_skipped(cache):
    """The defence-in-depth check, exercised directly.

    If the key format ever drifts back to something tenant-blind, the stored
    `organization_id` still has to stop the read. Written by planting a
    hand-built key that a buggy scan would match.
    """
    org_a, org_b = uuid.uuid4(), uuid.uuid4()
    repo = uuid.uuid4()

    # A key shaped as though org_b owned it, but carrying org_a's data.
    key = f"cache:query:deadbeef:{org_b}:{repo}"
    cache.redis_client.hset(
        key,
        mapping={
            "query": "how does auth work",
            "embedding": __import__("json").dumps(EMBEDDING),
            "response": __import__("json").dumps({"answer": "A's private answer"}),
            "organization_id": str(org_a),  # mismatched on purpose
            "repository_id": str(repo),
            "timestamp": "0",
        },
    )

    leaked = cache.get_cached_response(
        query="how does auth work",
        query_embedding=EMBEDDING,
        organization_id=org_b,
        repository_id=repo,
    )
    assert leaked is None, (
        "the stored organization_id must be re-checked, so a future key-format "
        "change cannot silently reopen ISS-020"
    )


def test_clear_cache_is_tenant_scoped(cache):
    """Invalidation must not reach across organizations.

    The scan patterns are the half of this fix that fails *silently*: miss the
    organization segment and invalidation quietly stops matching instead of
    raising.
    """
    org_a, org_b = uuid.uuid4(), uuid.uuid4()
    repo = uuid.uuid4()

    for org in (org_a, org_b):
        cache.cache_response(
            query="how does auth work",
            query_embedding=EMBEDDING,
            organization_id=org,
            repository_id=repo,
            response={"answer": f"answer for {org}", "sources": []},
        )

    cache.clear_cache(organization_id=org_a, repository_id=repo)

    assert (
        cache.get_cached_response(
            query="how does auth work",
            query_embedding=EMBEDDING,
            organization_id=org_a,
            repository_id=repo,
        )
        is None
    ), "org A's entry should have been cleared"

    assert (
        cache.get_cached_response(
            query="how does auth work",
            query_embedding=EMBEDDING,
            organization_id=org_b,
            repository_id=repo,
        )
        is not None
    ), "clearing org A must not have cleared org B's entry for the same repository"


def test_clear_cache_refuses_repository_without_organization(cache):
    """A repository id alone no longer identifies a key.

    Matching on it without the tenant would reach across organizations, so the
    call is refused rather than silently doing the wrong thing.
    """
    with pytest.raises(ValueError, match="organization_id"):
        cache.clear_cache(repository_id=uuid.uuid4())

    with pytest.raises(ValueError, match="organization_id"):
        cache.get_cache_stats(repository_id=uuid.uuid4())
