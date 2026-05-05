#!/usr/bin/env python3
"""Test LLM answer generation with semantic caching."""

import os
import sys
from pathlib import Path
from uuid import UUID
from dotenv import load_dotenv

load_dotenv()
sys.path.insert(0, str(Path(__file__).parent.parent))

from workers.retrieval import QueryEngine
from workers.generation import AnswerGenerator, SemanticCache
from workers.embeddings import EmbeddingGenerator

# Config
repository_id = UUID("00000000-0000-0000-0000-000000000003")  # Test repo

print("="*70)
print("LLM ANSWER GENERATION TEST")
print("="*70)

# Initialize components
print("\n[*] Initializing components...")
query_engine = QueryEngine(
    postgres_conn=os.getenv("DATABASE_URL"),
    qdrant_url=os.getenv("QDRANT_URL"),
    openai_api_key=os.getenv("OPENAI_API_KEY")
)

embedding_gen = EmbeddingGenerator(api_key=os.getenv("OPENAI_API_KEY"))
semantic_cache = SemanticCache(
    redis_url="redis://localhost:6379",
    embedding_generator=embedding_gen
)

answer_generator = AnswerGenerator(
    query_engine=query_engine,
    openai_api_key=os.getenv("OPENAI_API_KEY"),
    semantic_cache=semantic_cache
)
print("[+] Components initialized successfully")

# Test queries
test_queries = [
    "How does the vector database client work?",
    "What is semantic chunking?",
    "Explain the QueryEngine architecture"
]

for query in test_queries:
    print(f"\n{'='*70}")
    print(f"Query: {query}")
    print('='*70)

    response = answer_generator.generate(query, repository_id)

    print(f"\nAnswer:\n{response['answer']}\n")
    print(f"Model: {response['model']}")
    print(f"Tokens: {response['prompt_tokens']} in, {response['completion_tokens']} out")
    print(f"Cost: ${response['total_cost']:.4f}")
    print(f"Cache hit: {response.get('cache_hit', False)}")
    print(f"Chunks retrieved: {response.get('chunks_retrieved', 0)}")

print("\n" + "="*70)
print("Testing cache hit (repeat first query)...")
print("="*70)

response2 = answer_generator.generate(test_queries[0], repository_id)
print(f"\nCache hit: {response2.get('cache_hit', False)} (should be True)")
print(f"Cost: ${response2['total_cost']:.4f} (should be $0.00 if cached)")

print("\n" + "="*70)
print("[+] TEST COMPLETE")
print("="*70)
print("\nVerify:")
print("  - All queries returned answers with [1], [2] citations")
print("  - Answers reference actual code from codebase")
print("  - Second query showed cache_hit=True")
print("  - Cached response had $0.00 cost")
