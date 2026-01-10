# Phase 12: LLM Answer Generation - Research

**Researched:** 2026-01-10
**Domain:** LLM API integration for RAG-based answer generation
**Confidence:** HIGH

<research_summary>
## Summary

Researched the LLM API ecosystem for generating accurate answers from retrieved context in a RAG system. The critical constraint is cost efficiency - with large codebases and many users, LLM API costs can explode without careful architecture.

The standard approach uses OpenAI or Anthropic APIs with aggressive cost optimization: prompt caching (90% cheaper cached tokens), semantic caching (40% hit rate = 40% free/instant responses), and model tiering (use cheaper models like GPT-4o Mini or Claude Haiku for most queries, escalate to powerful models only when needed).

Key finding: Don't hand-roll prompt caching or semantic caching. Provider-native prompt caching (OpenAI/Anthropic) is automatic with 50-90% cost reduction. Semantic caching via Redis/vector similarity prevents redundant LLM calls for similar queries. Streaming responses via SSE (not WebSockets) is the standard for chat-like UX.

**Primary recommendation:** Start with OpenAI GPT-4o Mini ($0.15/$0.60 per MTok) or Anthropic Claude Haiku 3.5 ($0.80/$4.00 per MTok) for initial implementation. Implement semantic caching (Redis + embeddings) immediately for cost control. Use provider-native prompt caching for RAG context. Add streaming via SSE for better UX. Implement exponential backoff with jitter for rate limit handling.

</research_summary>

<standard_stack>
## Standard Stack

The established libraries/tools for LLM answer generation:

### Core
| Library | Version | Purpose | Why Standard |
|---------|---------|---------|--------------|
| OpenAI Python SDK | 1.x+ | OpenAI API client | Official client, streaming support, automatic retries |
| Anthropic Python SDK | 0.x+ | Anthropic API client | Official client, prompt caching, streaming |
| python-dotenv | 1.0+ | Environment variables | Standard for managing API keys securely |
| pydantic | 2.x+ | Response validation | Type-safe API response handling |

### Supporting
| Library | Version | Purpose | When to Use |
|---------|---------|---------|-------------|
| redis-py | 5.x+ | Semantic caching | Cost optimization (40% hit rate typical) |
| tenacity | 8.x+ | Retry logic | Exponential backoff for rate limits |
| tiktoken | 0.x+ | Token counting | Pre-flight cost estimation, context management |
| langchain | 0.x+ | LLM orchestration | Complex agentic workflows (overkill for simple RAG) |

### Alternatives Considered
| Instead of | Could Use | Tradeoff |
|------------|-----------|----------|
| OpenAI GPT-4o | Claude Opus 4.5 | Claude Opus: $5/$25 vs GPT-4o $5/$15 - OpenAI cheaper output |
| Commercial APIs | Self-hosted Llama/Mixtral | Self-hosted: $0 API cost but infrastructure + maintenance burden |
| Semantic cache | Prompt cache only | Semantic cache catches paraphrased queries, prompt cache only exact matches |
| SSE streaming | WebSockets | WebSockets for bidirectional, SSE simpler for one-way LLM→client |

**Installation:**
```bash
pip install openai anthropic redis tenacity tiktoken python-dotenv pydantic
```

</standard_stack>

<architecture_patterns>
## Architecture Patterns

### Recommended Project Structure
```
services/workers/
├── workers/
│   ├── llm/
│   │   ├── client.py              # LLM client wrapper (OpenAI/Anthropic)
│   │   ├── prompt_builder.py      # RAG prompt construction
│   │   ├── semantic_cache.py      # Redis-based semantic caching
│   │   └── streaming.py           # SSE streaming response handler
│   ├── generation/
│   │   └── answer_generator.py    # Orchestrates retrieval → prompt → LLM
│   └── utils/
│       ├── token_counter.py       # Pre-flight token estimation
│       └── retry_handler.py       # Exponential backoff with jitter
```

### Pattern 1: RAG Prompt Construction with Citation Grounding
**What:** Explicitly restrict LLM to use only provided context, require citations
**When to use:** Always for RAG to prevent hallucination
**Example:**
```python
# Source: RAG best practices research
def build_rag_prompt(query: str, retrieved_chunks: list[dict]) -> str:
    """Build prompt that grounds LLM in retrieved context."""

    # Format retrieved chunks with source citations
    context_parts = []
    for i, chunk in enumerate(retrieved_chunks, 1):
        context_parts.append(
            f"[{i}] {chunk['file_path']}:{chunk['start_line']}-{chunk['end_line']}\n"
            f"{chunk['content']}\n"
        )

    context = "\n".join(context_parts)

    # Explicit grounding instructions
    prompt = f"""You are a code documentation assistant. Answer the user's question using ONLY the provided code context below.

Rules:
1. Only use information explicitly mentioned in the context
2. Cite sources using [number] notation (e.g., "The vector database client [1] implements...")
3. If the answer isn't in the context, say "I don't have enough information to answer that"
4. Do not add information from your training data

Context:
{context}

User question: {query}

Answer:"""

    return prompt
```

### Pattern 2: Semantic Caching with Vector Similarity
**What:** Cache LLM responses, match similar queries with embeddings
**When to use:** Production RAG systems with cost constraints
**Example:**
```python
# Source: Semantic caching research + Redis patterns
import redis
import numpy as np
from openai import OpenAI

class SemanticCache:
    def __init__(self, redis_client: redis.Redis, openai_client: OpenAI, threshold: float = 0.95):
        self.redis = redis_client
        self.openai = openai_client
        self.threshold = threshold

    def get_cached_response(self, query: str, query_embedding: list[float]) -> str | None:
        """Check cache for similar query, return response if hit."""

        # Search for similar queries in Redis
        # (Simplified - production uses Redis vector search or separate vector DB)
        cached_keys = self.redis.keys("cache:query:*")

        for key in cached_keys:
            cached_data = self.redis.hgetall(key)
            cached_embedding = np.array(eval(cached_data[b'embedding']))

            # Cosine similarity
            similarity = np.dot(query_embedding, cached_embedding) / (
                np.linalg.norm(query_embedding) * np.linalg.norm(cached_embedding)
            )

            if similarity >= self.threshold:
                # Cache hit!
                return cached_data[b'response'].decode('utf-8')

        return None

    def cache_response(self, query: str, query_embedding: list[float], response: str):
        """Store query and response in cache."""
        cache_key = f"cache:query:{hash(query)}"
        self.redis.hset(cache_key, mapping={
            'query': query,
            'embedding': str(query_embedding),
            'response': response
        })
        self.redis.expire(cache_key, 3600)  # 1 hour TTL
```

### Pattern 3: Streaming with SSE
**What:** Stream LLM tokens to client via Server-Sent Events
**When to use:** Chat-like interfaces for better UX
**Example:**
```python
# Source: OpenAI streaming docs
from openai import OpenAI

def stream_answer(prompt: str) -> Generator[str, None, None]:
    """Stream LLM response token by token."""
    client = OpenAI()

    stream = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": prompt}],
        stream=True
    )

    for chunk in stream:
        if chunk.choices[0].delta.content is not None:
            yield chunk.choices[0].delta.content

# Usage in Flask/FastAPI:
# @app.get("/query")
# def query_endpoint(query: str):
#     prompt = build_rag_prompt(query, retrieved_chunks)
#     return StreamingResponse(stream_answer(prompt), media_type="text/event-stream")
```

### Pattern 4: Exponential Backoff with Jitter
**What:** Retry failed API calls with randomized exponential delays
**When to use:** All production LLM API calls
**Example:**
```python
# Source: OpenAI cookbook + tenacity docs
from tenacity import retry, wait_random_exponential, stop_after_attempt

@retry(
    wait=wait_random_exponential(min=1, max=60),
    stop=stop_after_attempt(6)
)
def call_llm_with_retry(prompt: str, model: str = "gpt-4o-mini") -> str:
    """Call LLM with automatic retry on rate limits."""
    client = OpenAI()

    response = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}]
    )

    return response.choices[0].message.content
```

### Anti-Patterns to Avoid
- **Not using prompt caching:** Leaving 50-90% cost savings on the table for repeated context
- **Synchronous blocking calls:** Kills UX - always stream for chat interfaces
- **No semantic cache:** Paying full price for paraphrased queries users ask repeatedly
- **Aggressive retries without jitter:** Thundering herd problem causes more rate limit errors
- **Ignoring Retry-After headers:** Provider tells you when to retry, honor it
- **Using GPT-4 for everything:** Most queries work fine with GPT-4o Mini ($10x cheaper output)

</architecture_patterns>

<dont_hand_roll>
## Don't Hand-Roll

Problems that look simple but have existing solutions:

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| Prompt caching | Custom cache layer | OpenAI/Anthropic native prompt caching | Providers handle it automatically, 50-90% discount |
| Token counting | Manual string splitting | tiktoken library | Handles model-specific tokenization correctly |
| Retry logic | Manual sleep/retry loops | tenacity with exponential backoff | Handles jitter, max retries, honor Retry-After |
| Streaming responses | Custom chunking | OpenAI/Anthropic streaming APIs | Handles SSE, error recovery, proper chunking |
| Semantic similarity | Custom embedding comparisons | Redis vector search or Qdrant | Handles indexing, fast k-NN, production scale |
| Response validation | Manual parsing | Pydantic models | Type safety, automatic validation, better errors |

**Key insight:** LLM API integration has well-established patterns. Provider SDKs handle streaming, retries, and prompt caching automatically. Semantic caching is the only custom component needed, and even that uses standard Redis/vector DB patterns. Focus engineering effort on RAG prompt quality, not reinventing API client infrastructure.

</dont_hand_roll>

<common_pitfalls>
## Common Pitfalls

### Pitfall 1: Cost Runaway from No Caching
**What goes wrong:** Monthly API bills explode (thousands of dollars unexpectedly)
**Why it happens:** Every query hits LLM with full context, no reuse
**How to avoid:** Implement semantic caching immediately (40% hit rate = 40% cost savings). Use provider prompt caching for repeated RAG context.
**Warning signs:** Daily API costs increasing linearly with user count, identical queries costing full price

### Pitfall 2: Hallucination from Weak Grounding
**What goes wrong:** LLM invents code/features that don't exist in the codebase
**Why it happens:** Prompt doesn't explicitly restrict to provided context
**How to avoid:** Use "Only use information explicitly mentioned in the context" instruction. Require citations with [number] notation. Verify citations reference actual chunks.
**Warning signs:** Users report "helpful but wrong" answers, answers reference files not in retrieved chunks

### Pitfall 3: Context Window Overflow
**What goes wrong:** API calls fail with "context too long" errors
**Why it happens:** Retrieved chunks + prompt exceed model's token limit
**How to avoid:** Count tokens pre-flight with tiktoken. Limit retrieved chunks (top-k=5 default). Truncate if needed. Use models with larger windows (GPT-4o: 128K).
**Warning signs:** Random API errors for complex queries, failures only on large files

### Pitfall 4: Thundering Herd on Rate Limits
**What goes wrong:** Rate limit hit triggers simultaneous retries, making it worse
**Why it happens:** No jitter in retry delays - all clients retry at same time
**How to avoid:** Use tenacity with wait_random_exponential for jitter. Honor Retry-After headers from API.
**Warning signs:** Rate limit errors come in bursts, recovery takes minutes not seconds

### Pitfall 5: Poor Streaming UX
**What goes wrong:** Users see blank screen for 5-10 seconds, then full answer appears
**Why it happens:** Not using streaming, or buffering entire response before sending
**How to avoid:** Use SSE streaming for all chat-like interfaces. Stream token-by-token with flush.
**Warning signs:** User complaints about "loading", perceived slowness despite fast API

</common_pitfalls>

<code_examples>
## Code Examples

Verified patterns from official sources:

### OpenAI API Call with Prompt Caching
```python
# Source: OpenAI prompt caching docs
from openai import OpenAI

client = OpenAI()

# Prompt caching happens automatically when prompt prefix is identical
# Cache RAG context as system message for automatic caching
response = client.chat.completions.create(
    model="gpt-4o-mini",
    messages=[
        {
            "role": "system",
            "content": "You are a code documentation assistant. Answer using only the provided context.\n\n" + rag_context
        },
        {
            "role": "user",
            "content": user_query
        }
    ]
)

answer = response.choices[0].message.content
```

### Anthropic API Call with Extended Prompt Caching
```python
# Source: Anthropic prompt caching docs
from anthropic import Anthropic

client = Anthropic()

# Mark RAG context for caching with cache_control
response = client.messages.create(
    model="claude-haiku-3-5",
    max_tokens=1024,
    system=[
        {
            "type": "text",
            "text": "You are a code documentation assistant.",
        },
        {
            "type": "text",
            "text": f"Context:\n{rag_context}",
            "cache_control": {"type": "ephemeral"}  # Cache this part
        }
    ],
    messages=[
        {"role": "user", "content": user_query}
    ]
)

answer = response.content[0].text
```

### Token Counting Pre-Flight Check
```python
# Source: OpenAI tiktoken docs
import tiktoken

def count_tokens(text: str, model: str = "gpt-4o-mini") -> int:
    """Count tokens for a text string."""
    encoding = tiktoken.encoding_for_model(model)
    return len(encoding.encode(text))

def build_prompt_with_limit(query: str, chunks: list[dict], max_tokens: int = 100000) -> str:
    """Build prompt, truncate chunks if needed to fit context window."""
    base_prompt = f"Answer using only this context:\n\nQuestion: {query}\n\nAnswer:"
    base_tokens = count_tokens(base_prompt)

    context_parts = []
    context_tokens = 0

    for chunk in chunks:
        chunk_text = f"[{chunk['file_path']}]\n{chunk['content']}\n\n"
        chunk_tokens = count_tokens(chunk_text)

        if base_tokens + context_tokens + chunk_tokens > max_tokens:
            break  # Stop adding chunks

        context_parts.append(chunk_text)
        context_tokens += chunk_tokens

    return base_prompt.replace("context:", "context:\n" + "".join(context_parts))
```

### Complete Answer Generator with Error Handling
```python
# Source: Combining patterns from research
from openai import OpenAI
from tenacity import retry, wait_random_exponential, stop_after_attempt
import tiktoken

class AnswerGenerator:
    def __init__(self, api_key: str, model: str = "gpt-4o-mini"):
        self.client = OpenAI(api_key=api_key)
        self.model = model
        self.encoding = tiktoken.encoding_for_model(model)

    def build_prompt(self, query: str, chunks: list[dict]) -> str:
        """Build RAG prompt with citations."""
        context_parts = []
        for i, chunk in enumerate(chunks, 1):
            context_parts.append(
                f"[{i}] {chunk['file_path']}:{chunk['start_line']}\n{chunk['content']}\n"
            )

        context = "\n".join(context_parts)

        return f"""Answer the question using ONLY the provided code context. Cite sources with [number].

Context:
{context}

Question: {query}

Answer:"""

    @retry(wait=wait_random_exponential(min=1, max=60), stop=stop_after_attempt(6))
    def generate(self, query: str, chunks: list[dict]) -> dict:
        """Generate answer from query and retrieved chunks."""
        prompt = self.build_prompt(query, chunks)

        # Count tokens
        prompt_tokens = len(self.encoding.encode(prompt))
        if prompt_tokens > 100000:
            raise ValueError(f"Prompt too long: {prompt_tokens} tokens")

        # Call LLM
        response = self.client.chat.completions.create(
            model=self.model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.0  # Deterministic for factual answers
        )

        return {
            "answer": response.choices[0].message.content,
            "model": self.model,
            "prompt_tokens": response.usage.prompt_tokens,
            "completion_tokens": response.usage.completion_tokens,
            "total_cost": self._calculate_cost(response.usage)
        }

    def _calculate_cost(self, usage) -> float:
        """Calculate API cost for this request."""
        # GPT-4o Mini: $0.15/1M input, $0.60/1M output
        input_cost = (usage.prompt_tokens / 1_000_000) * 0.15
        output_cost = (usage.completion_tokens / 1_000_000) * 0.60
        return input_cost + output_cost
```

</code_examples>

<sota_updates>
## State of the Art (2025-2026)

What's changed recently:

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| GPT-4 for all queries | GPT-4o Mini for most, escalate when needed | 2024 | 10x cheaper output tokens ($0.60 vs $60 per MTok) |
| Manual prompt caching | Provider-native prompt caching | 2024-2025 | Automatic 50-90% discount on cached tokens |
| No semantic caching | Redis + embeddings for query similarity | 2024-2025 | 40% hit rate = 40% free responses |
| WebSockets for streaming | SSE (Server-Sent Events) | Ongoing | SSE simpler, better for one-way LLM→client |
| Llama 2 | Llama 4, Mixtral 8x22B | 2025-2026 | Open-source caught up to GPT-3.5 quality |

**New tools/patterns to consider:**
- **Extended prompt caching (24h):** OpenAI/Anthropic now cache prompts for 24 hours (was 5min-1hr). Huge savings for RAG where context is stable.
- **Batch APIs:** 50% discount for non-realtime requests (both OpenAI and Anthropic). Good for bulk documentation generation.
- **Claude Haiku 4.5:** New budget model ($1/$5 per MTok) competitive with GPT-4o Mini for RAG tasks.
- **Structured outputs:** OpenAI/Anthropic JSON mode ensures valid responses without parsing hacks.

**Deprecated/outdated:**
- **GPT-3.5 Turbo:** Superseded by GPT-4o Mini (cheaper and better quality)
- **Custom retry logic without tenacity:** Library handles it better, every time
- **LangChain for simple RAG:** Overkill - direct API calls cleaner for basic retrieve→prompt→generate

</sota_updates>

<open_questions>
## Open Questions

Things that couldn't be fully resolved:

1. **Model selection strategy**
   - What we know: GPT-4o Mini works for most queries, GPT-4o/Claude Opus for complex reasoning
   - What's unclear: How to detect when a query needs the expensive model vs cheap one
   - Recommendation: Start with GPT-4o Mini for all queries. Add user feedback ("Was this helpful?"). Switch to GPT-4o for queries with negative feedback or explicit "deep dive" mode.

2. **Open-source vs commercial tradeoff**
   - What we know: Llama 4/Mixtral competitive with GPT-3.5, but require infrastructure
   - What's unclear: At what scale self-hosted becomes cheaper than API costs
   - Recommendation: Start with commercial APIs (OpenAI/Anthropic). Self-hosting has hidden costs (DevOps, GPU/CPU, monitoring, model updates). Revisit if monthly API costs exceed $5K.

3. **Cache invalidation strategy**
   - What we know: Semantic cache reduces costs, but can serve stale answers when code changes
   - What's unclear: When to invalidate cache entries after repository updates
   - Recommendation: Add repository_id + commit_sha to cache keys. Invalidate all cache entries for a repository when new code is ingested. Accept some staleness for cost savings.

</open_questions>

<sources>
## Sources

### Primary (HIGH confidence)
- [Anthropic Pricing Docs](https://platform.claude.com/docs/en/about-claude/pricing) - Model pricing, prompt caching details, batch API
- [OpenAI Prompt Caching](https://openai.com/index/api-prompt-caching/) - Prompt caching feature, 50% discount
- [OpenAI Rate Limits Cookbook](https://cookbook.openai.com/examples/how_to_handle_rate_limits) - Exponential backoff, tenacity examples

### Secondary (MEDIUM confidence)
- [Redis Semantic Caching Blog](https://redis.io/blog/what-is-semantic-caching/) - Verified semantic caching patterns with production examples
- [RAG Prompt Engineering Guide](https://www.promptingguide.ai/techniques/rag) - Verified grounding strategies with research papers
- [LLM Pricing Comparison](https://www.cloudidr.com/llm-pricing) - Verified pricing data against official docs
- [SSE vs WebSockets for LLM Streaming](https://compute.hivenet.com/post/llm-streaming-sse-websockets) - Verified against OpenAI/Anthropic streaming docs

### Tertiary (LOW confidence - needs validation)
- None - all key findings cross-verified with official documentation

</sources>

<metadata>
## Metadata

**Research scope:**
- Core technology: OpenAI API, Anthropic API
- Ecosystem: Semantic caching (Redis), retry handling (tenacity), streaming (SSE)
- Patterns: RAG prompting, prompt caching, semantic caching, exponential backoff
- Pitfalls: Cost runaway, hallucination, context overflow, rate limits

**Confidence breakdown:**
- Standard stack: HIGH - Official SDKs, verified pricing from provider docs
- Architecture: HIGH - Patterns from official docs and production case studies
- Pitfalls: HIGH - Documented in provider docs, verified in community reports
- Code examples: HIGH - From official OpenAI/Anthropic documentation

**Research date:** 2026-01-10
**Valid until:** 2026-02-10 (30 days - LLM APIs evolve quickly but core patterns stable)

</metadata>

---

*Phase: 12-llm-answer-generation*
*Research completed: 2026-01-10*
*Ready for planning: yes*
