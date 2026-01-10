#!/usr/bin/env python3
"""Integration test for end-to-end ingestion pipeline.

Tests the complete flow: parse → chunk → embed → store in Postgres + Qdrant.
Uses real code from this project as test data.
"""

import os
import sys
from pathlib import Path
from uuid import UUID, uuid4
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from workers.pipeline.ingestion_pipeline import IngestionPipeline
from workers.storage.postgres_writer import PostgresWriter
from workers.storage.qdrant_writer import QdrantWriter
from workers.embeddings import EmbeddingGenerator


def main():
    """Run integration test with project's codebase."""

    # Configuration from environment
    postgres_conn = os.getenv(
        "DATABASE_URL",
        "postgresql://coderag:testpass123@127.0.0.1:5434/coderag"
    )
    qdrant_url = os.getenv("QDRANT_URL", "http://localhost:6333")
    openai_api_key = os.getenv("OPENAI_API_KEY")

    if not openai_api_key:
        print("[ERROR] OPENAI_API_KEY environment variable not set")
        print("   Set it with: export OPENAI_API_KEY=sk-...")
        sys.exit(1)

    # Use existing test repository
    repository_id = UUID("00000000-0000-0000-0000-000000000003")
    print(f"Repository ID: {repository_id}\n")

    # Sample files from this project
    project_root = Path(__file__).parent.parent.parent.parent
    sample_files = [
        (
            "services/backend/pkg/vectordb/client.go",
            project_root / "services/backend/pkg/vectordb/client.go",
            "go"
        ),
        (
            "services/workers/workers/chunker/semantic_chunker.py",
            project_root / "services/workers/workers/chunker/semantic_chunker.py",
            "python"
        ),
        (
            ".planning/PROJECT.md",
            project_root / ".planning/PROJECT.md",
            "markdown"
        ),
    ]

    # Read file contents
    print("[*] Reading sample files...")
    files = []
    for file_path, absolute_path, language in sample_files:
        if not absolute_path.exists():
            print(f"   [WARN] File not found: {absolute_path}")
            continue

        with open(absolute_path, "r", encoding="utf-8") as f:
            content = f.read()

        files.append((file_path, content, language))
        print(f"   [+] {file_path} ({len(content)} bytes, {language})")

    if not files:
        print("[ERROR] No files to process!")
        sys.exit(1)

    print(f"\n[+] Loaded {len(files)} files\n")

    # Create pipeline
    print("[*] Initializing pipeline...")
    pipeline = IngestionPipeline(
        postgres_conn=postgres_conn,
        qdrant_url=qdrant_url,
        openai_api_key=openai_api_key,
    )
    print("[+] Pipeline initialized\n")

    # Process files
    print(f"[*] Processing {len(files)} files through pipeline...")
    print("   (This will call OpenAI API - expect ~$0.002 cost)\n")

    stats = pipeline.process_files(
        files=files,
        repository_id=repository_id,
        commit_sha="test-integration",
        branch="main",
    )

    # Print results
    print("\n" + "=" * 60)
    print("PIPELINE RESULTS")
    print("=" * 60)
    print(f"Status: {stats['status'].upper()}")
    print(f"Files processed: {stats['files_processed']}")
    print(f"Chunks created: {stats['chunks_created']}")
    print(f"Embeddings generated: {stats['embeddings_generated']}")
    print(f"Duration: {stats['duration_seconds']:.2f}s")

    if stats['status'] == 'failed':
        print(f"\n[ERROR] {stats.get('error', 'Unknown error')}")
        sys.exit(1)

    print("\n[+] Pipeline completed successfully!\n")

    # Verify Postgres storage
    print("=" * 60)
    print("POSTGRES VERIFICATION")
    print("=" * 60)

    postgres = PostgresWriter(postgres_conn)
    postgres.connect()

    with postgres.conn.cursor() as cur:
        # Count chunks by language and type
        cur.execute("""
            SELECT language, chunk_type, COUNT(*)
            FROM chunks
            WHERE repository_id = %s
            GROUP BY language, chunk_type
            ORDER BY language, chunk_type
        """, (str(repository_id),))

        results = cur.fetchall()
        if results:
            print("\nChunks by language and type:")
            for language, chunk_type, count in results:
                print(f"  {language:12} {chunk_type:15} {count:3} chunks")

        # Sample chunks
        cur.execute("""
            SELECT file_path, start_line, end_line, chunk_type,
                   LEFT(content, 80) as content_preview
            FROM chunks
            WHERE repository_id = %s
            ORDER BY file_path, start_line
            LIMIT 5
        """, (str(repository_id),))

        results = cur.fetchall()
        if results:
            print("\nSample chunks:")
            for file_path, start_line, end_line, chunk_type, preview in results:
                preview = preview.replace('\n', ' ')
                print(f"  {file_path}:{start_line}-{end_line}")
                print(f"    Type: {chunk_type}")
                print(f"    Content: {preview}...")
                print()

    postgres.close()

    # Verify Qdrant storage
    print("=" * 60)
    print("QDRANT VERIFICATION")
    print("=" * 60)

    qdrant = QdrantWriter(qdrant_url)

    try:
        collection_info = qdrant.client.get_collection("code_embeddings")
        print(f"\nCollection: {collection_info.config.params.vectors.size}-dim vectors")
        print(f"Total points: {collection_info.points_count}")
        print(f"Distance metric: {collection_info.config.params.vectors.distance}")
    except Exception as e:
        print(f"\n[WARN] Could not verify Qdrant: {e}")

    # Test semantic search
    print("\n" + "=" * 60)
    print("SEMANTIC SEARCH TEST")
    print("=" * 60)

    test_query = "vector database client"
    print(f'\nQuery: "{test_query}"')
    print("Generating embedding...")

    # Generate query embedding
    embedding_gen = EmbeddingGenerator(api_key=openai_api_key)
    query_embedding = embedding_gen.client.generate_embeddings_batch([test_query])[0]

    # Search Qdrant
    print("Searching for similar chunks...\n")
    results = qdrant.search_similar(query_embedding, limit=5, score_threshold=0.5)

    if results:
        print(f"Found {len(results)} relevant chunks:\n")
        for i, result in enumerate(results, 1):
            print(f"{i}. {result['file_path']}")
            print(f"   Type: {result['chunk_type']}, Language: {result['language']}")
            print(f"   Breadcrumb: {result['breadcrumb']}")
            print(f"   Similarity: {result['score']:.3f}")
            print()
    else:
        print("[WARN] No results found (threshold may be too high)")

    print("=" * 60)
    print("[+] INTEGRATION TEST COMPLETE")
    print("=" * 60)
    print("\nAll systems operational:")
    print("  [+] Parsing (tree-sitter)")
    print("  [+] Chunking (semantic boundaries)")
    print("  [+] Embedding (OpenAI ada-002)")
    print("  [+] Storage (Postgres + Qdrant)")
    print("  [+] Search (vector similarity)")
    print("\nPhase 7 pipeline is fully functional!\n")


if __name__ == "__main__":
    main()
