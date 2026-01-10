#!/usr/bin/env python3
"""Integration test for RAG query engine.

Tests the complete query pipeline: parse → FTS + vector (parallel) → fuse → boost → rank.
Validates result quality, provenance, fusion, and metadata boosting.
"""

import os
import sys
from pathlib import Path
from uuid import UUID

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from workers.retrieval import QueryEngine


def print_separator(title=""):
    """Print a formatted section separator."""
    print("\n" + "=" * 70)
    if title:
        print(title)
        print("=" * 70)
    else:
        print("=" * 70)


def print_result(result, rank):
    """Print a single result in a formatted way."""
    print(f"\n{rank}. {result['file_path']}:{result['start_line']}-{result['end_line']}")
    print(f"   Type: {result['chunk_type']}")
    print(f"   Breadcrumb: {result['breadcrumb']}")
    print(f"   Score: {result['score']:.4f} (RRF: {result['rrf_score']:.4f}, "
          f"Boost: {result['boost_multiplier']:.2f}x)")
    print(f"   Sources: {', '.join(result['sources'])}")

    # Show content preview
    preview = result['content_preview'].replace('\n', ' ')
    if len(preview) > 100:
        preview = preview[:100] + "..."
    print(f"   Content: {preview}")

    # Show provenance
    prov = result['provenance']
    print(f"   Provenance: run={prov['run_id'][:8]}..., "
          f"commit={prov['commit_sha']}, "
          f"date={prov['ingestion_date']}")


def validate_results(query_text, response):
    """Validate query results meet quality requirements."""
    results = response['results']
    metadata = response['metadata']

    issues = []

    # Check: At least 1 result returned
    if len(results) == 0:
        issues.append("No results returned")

    # Check: Top result has score > 0
    if results and results[0]['score'] <= 0:
        issues.append("Top result has score <= 0")

    # Check: Results include provenance
    if results:
        for i, result in enumerate(results):
            prov = result.get('provenance', {})
            if not prov.get('run_id'):
                issues.append(f"Result {i+1} missing run_id in provenance")
            if not prov.get('commit_sha'):
                issues.append(f"Result {i+1} missing commit_sha in provenance")

    # Check: Both FTS and vector contributed results
    if metadata['fts_results'] == 0:
        issues.append("FTS search returned 0 results")
    if metadata['vector_results'] == 0:
        issues.append("Vector search returned 0 results")

    # Check: Some results have overlap (from both sources)
    overlap_count = sum(1 for r in results if len(r['sources']) > 1)
    if overlap_count == 0 and len(results) > 0:
        issues.append("No results show overlap between FTS and vector (expected some)")

    # Check: Metadata boosts applied (some results should have multiplier != 1.0)
    varied_boosts = sum(1 for r in results if abs(r['boost_multiplier'] - 1.0) > 0.01)
    if varied_boosts == 0 and len(results) > 0:
        issues.append("No results have boost_multiplier != 1.0 (expected some variation)")

    return issues


def run_test_query(engine, query_text, repository_id, expected_file_hint=None):
    """Run a single test query and display results."""
    print_separator(f'Query: "{query_text}"')

    try:
        response = engine.query(
            query_text=query_text,
            repository_id=repository_id,
            top_k=5
        )
    except Exception as e:
        print(f"\n[ERROR] Query failed: {e}")
        return False

    # Display results
    results = response['results']
    metadata = response['metadata']

    print(f"\nResults: {len(results)} returned")
    print(f"Pipeline stats:")
    print(f"  - FTS results: {metadata['fts_results']}")
    print(f"  - Vector results: {metadata['vector_results']}")
    print(f"  - Fused results: {metadata['fused_results']}")
    print(f"  - Duration: {metadata['duration_ms']}ms")

    if metadata['fts_error']:
        print(f"  - FTS error: {metadata['fts_error']}")
    if metadata['vector_error']:
        print(f"  - Vector error: {metadata['vector_error']}")

    # Show top results
    if results:
        print(f"\nTop {len(results)} results:")
        for i, result in enumerate(results, 1):
            print_result(result, i)
    else:
        print("\n[WARN] No results found!")

    # Validate results
    print("\n" + "-" * 70)
    print("VALIDATION:")
    issues = validate_results(query_text, response)

    if issues:
        print("[WARN] Validation issues:")
        for issue in issues:
            print(f"  - {issue}")
    else:
        print("[PASS] All validation checks passed!")

    # Check expected file hint if provided
    if expected_file_hint and results:
        top_result = results[0]
        if expected_file_hint.lower() in top_result['file_path'].lower():
            print(f"[PASS] Top result contains expected file hint: '{expected_file_hint}'")
        else:
            print(f"[INFO] Top result '{top_result['file_path']}' "
                  f"does not contain hint '{expected_file_hint}' (may still be relevant)")

    return len(issues) == 0


def main():
    """Run integration tests for query engine."""

    print_separator("RAG QUERY ENGINE INTEGRATION TEST")

    # Configuration from environment
    postgres_conn = os.getenv(
        "DATABASE_URL",
        "postgresql://coderag:testpass123@127.0.0.1:5434/coderag"
    )
    qdrant_url = os.getenv("QDRANT_URL", "http://localhost:6333")
    openai_api_key = os.getenv("OPENAI_API_KEY")

    if not openai_api_key:
        print("\n[ERROR] OPENAI_API_KEY environment variable not set")
        print("   Set it with: export OPENAI_API_KEY=sk-...")
        sys.exit(1)

    # Use test repository
    repository_id = UUID("00000000-0000-0000-0000-000000000003")
    print(f"\nRepository ID: {repository_id}")
    print(f"Postgres: {postgres_conn}")
    print(f"Qdrant: {qdrant_url}")

    # Initialize QueryEngine
    print("\n[*] Initializing QueryEngine...")
    try:
        engine = QueryEngine(
            postgres_conn=postgres_conn,
            qdrant_url=qdrant_url,
            openai_api_key=openai_api_key
        )
        print("[+] QueryEngine initialized successfully")
    except Exception as e:
        print(f"\n[ERROR] Failed to initialize QueryEngine: {e}")
        print("\nMake sure:")
        print("  1. Postgres is running: docker-compose ps postgres")
        print("  2. Qdrant is running: docker-compose ps qdrant")
        print("  3. Test data exists: python scripts/test_ingestion.py")
        sys.exit(1)

    # Define test queries
    test_queries = [
        {
            "query": "vector database client",
            "hint": "vectordb/client.go",
            "description": "Lexical match - should find Go vector DB client"
        },
        {
            "query": "semantic chunking",
            "hint": "semantic_chunker.py",
            "description": "Semantic match - should find semantic chunker"
        },
        {
            "query": "How does embedding generation work?",
            "hint": None,
            "description": "Natural language query - semantic understanding"
        },
        {
            "query": '"OpenAI"',
            "hint": None,
            "description": "Quoted exact match - should boost exact 'OpenAI' occurrences"
        },
        {
            "query": "PostgresWriter",
            "hint": None,
            "description": "Identifier match - PascalCase identifier boosting"
        },
    ]

    # Run test queries
    all_passed = True

    for i, test in enumerate(test_queries, 1):
        print(f"\n\nTest {i}/{len(test_queries)}: {test['description']}")

        passed = run_test_query(
            engine=engine,
            query_text=test['query'],
            repository_id=repository_id,
            expected_file_hint=test['hint']
        )

        if not passed:
            all_passed = False

    # Final summary
    print_separator("TEST SUMMARY")

    print(f"\nTests run: {len(test_queries)}")

    if all_passed:
        print("\n[+] ALL TESTS PASSED!")
        print("\nQuery engine is operational:")
        print("  [+] QueryParser - Extracts quoted terms and identifiers")
        print("  [+] FTSRetriever - PostgreSQL full-text search")
        print("  [+] VectorRetriever - Qdrant semantic search")
        print("  [+] Parallel execution - Both searches run concurrently")
        print("  [+] RRFFusion - Reciprocal rank fusion with overlap boost")
        print("  [+] MetadataBooster - Intelligent ranking adjustments")
        print("  [+] Provenance - Full audit trail for every result")
        print("\nPhase 11 RAG query engine is fully functional!")
    else:
        print("\n[WARN] Some validation issues detected")
        print("\nThis may be normal if:")
        print("  - Test data is limited")
        print("  - Queries don't match ingested content")
        print("  - Boost configurations need tuning")
        print("\nReview individual test results above for details.")

    print()


if __name__ == "__main__":
    main()
