# Phase 5: API Framework - Research

**Researched:** 2026-02-01
**Domain:** Go Chi REST API with SSE streaming and Python service integration
**Confidence:** HIGH

<research_summary>
## Summary

Researched the Go Chi ecosystem for building a REST API that calls Python FastAPI services and streams LLM responses via Server-Sent Events (SSE). The standard approach uses Chi v5 router with go-chi ecosystem libraries (httplog, render, jwtauth) for middleware, responses, and auth.

Key finding: Don't hand-roll SSE streaming, JSON responses, or request validation. Chi ecosystem provides battle-tested solutions. For SSE, disable request timeouts and monitor `r.Context().Done()` for client disconnections to prevent goroutine leaks.

The Go → Python integration should use HTTP (not gRPC) for simplicity since both services run internally. FastAPI automatically generates OpenAPI spec, enabling type-safe Go client generation if needed.

**Primary recommendation:** Use Chi v5 + httplog + render + validator stack. Wrap Python QueryEngine/AnswerGenerator in FastAPI service, call via Go http.Client, stream responses via SSE.
</research_summary>

<standard_stack>
## Standard Stack

### Core
| Library | Version | Purpose | Why Standard |
|---------|---------|---------|--------------|
| chi/v5 | v5.0.12+ | HTTP router | Lightweight, idiomatic, production-proven (Cloudflare, Heroku) |
| go-chi/httplog | latest | Structured logging | Built on log/slog, auto log levels by status code, panic recovery |
| go-chi/render | latest | Request/response helpers | JSON marshaling, content negotiation, error responses |
| go-chi/jwtauth/v5 | latest | JWT middleware | Works with Supabase JWTs, integrates with Chi middleware chain |

### Supporting
| Library | Version | Purpose | When to Use |
|---------|---------|---------|-------------|
| go-playground/validator/v10 | v10.20+ | Request validation | Validate all incoming JSON request bodies |
| github.com/lestrrat-go/jwx | v2 | JWT parsing | Underlying JWT library for jwtauth, Supabase JWKS support |
| net/http | stdlib | HTTP client | Calling Python FastAPI service |

### Alternatives Considered
| Instead of | Could Use | Tradeoff |
|------------|-----------|----------|
| chi | gin | Gin is faster but chi is more idiomatic, stdlib-compatible |
| chi | echo | Echo has more features but more opinionated, heavier |
| httplog | zerolog | zerolog faster but httplog integrates better with chi |
| render | manual json.Marshal | render handles content negotiation, error patterns |

**Installation:**
```bash
go get github.com/go-chi/chi/v5
go get github.com/go-chi/httplog/v2
go get github.com/go-chi/render
go get github.com/go-chi/jwtauth/v5
go get github.com/go-playground/validator/v10
```
</standard_stack>

<architecture_patterns>
## Architecture Patterns

### Recommended Project Structure
```
services/backend/
├── cmd/
│   └── server/
│       └── main.go           # Entrypoint, DI setup
├── pkg/
│   ├── api/
│   │   ├── router.go         # Chi router setup, middleware chain
│   │   ├── handlers/         # HTTP handlers by domain
│   │   │   ├── search.go     # POST /api/search
│   │   │   ├── chat.go       # POST /api/chat (SSE)
│   │   │   └── health.go     # GET /api/health
│   │   └── middleware/       # Custom middleware
│   │       └── tenant.go     # Tenant context injection
│   ├── client/
│   │   └── rag_client.go     # HTTP client for Python RAG service
│   └── auth/                 # (existing) JWT validation
├── internal/
│   └── config/
│       └── config.go         # Configuration loading
└── go.mod
```

### Pattern 1: Chi Router with Middleware Chain
**What:** Set up Chi with middleware in correct order
**When to use:** Every Chi application
**Example:**
```go
// Source: go-chi docs + httplog docs
func NewRouter(logger *httplog.Logger, jwtAuth *jwtauth.JWTAuth) chi.Router {
    r := chi.NewRouter()

    // Middleware order matters: logging → recovery → auth
    r.Use(httplog.RequestLogger(logger))
    r.Use(middleware.Recoverer)
    r.Use(middleware.RequestID)
    r.Use(middleware.RealIP)

    // Public routes
    r.Get("/health", handlers.Health)

    // Protected routes
    r.Group(func(r chi.Router) {
        r.Use(jwtauth.Verifier(jwtAuth))
        r.Use(jwtauth.Authenticator(jwtAuth))
        r.Post("/api/search", handlers.Search)
        r.Post("/api/chat", handlers.Chat)
    })

    return r
}
```

### Pattern 2: SSE Streaming Handler
**What:** Stream responses without timeouts, handle client disconnection
**When to use:** Any endpoint that streams data (LLM responses)
**Example:**
```go
// Source: go-zero SSE docs + community patterns
func (h *Handler) Chat(w http.ResponseWriter, r *http.Request) {
    // Set SSE headers
    w.Header().Set("Content-Type", "text/event-stream")
    w.Header().Set("Cache-Control", "no-cache")
    w.Header().Set("Connection", "keep-alive")

    flusher, ok := w.(http.Flusher)
    if !ok {
        http.Error(w, "SSE not supported", http.StatusInternalServerError)
        return
    }

    ctx := r.Context()
    responseCh := h.ragClient.StreamChat(ctx, query)

    for {
        select {
        case <-ctx.Done():
            // Client disconnected
            return
        case chunk, ok := <-responseCh:
            if !ok {
                // Stream complete
                fmt.Fprintf(w, "event: done\ndata: {}\n\n")
                flusher.Flush()
                return
            }
            fmt.Fprintf(w, "data: %s\n\n", chunk)
            flusher.Flush()
        }
    }
}
```

### Pattern 3: Request Validation with render + validator
**What:** Validate and decode JSON request bodies
**When to use:** All POST/PUT/PATCH endpoints
**Example:**
```go
// Source: go-playground/validator + go-chi/render docs
type SearchRequest struct {
    Query        string `json:"query" validate:"required,min=1,max=1000"`
    RepositoryID string `json:"repository_id" validate:"required,uuid"`
    TopK         int    `json:"top_k" validate:"omitempty,min=1,max=50"`
}

func (sr *SearchRequest) Bind(r *http.Request) error {
    // Bind is called after JSON decode
    if sr.TopK == 0 {
        sr.TopK = 10 // default
    }
    return validate.Struct(sr)
}

func Search(w http.ResponseWriter, r *http.Request) {
    var req SearchRequest
    if err := render.Bind(r, &req); err != nil {
        render.Render(w, r, ErrInvalidRequest(err))
        return
    }
    // ... use req.Query, req.RepositoryID, req.TopK
}
```

### Pattern 4: HTTP Client for Python Service
**What:** Call Python FastAPI from Go with proper timeout/retry
**When to use:** Go → Python RAG service communication
**Example:**
```go
// Source: net/http patterns
type RAGClient struct {
    baseURL    string
    httpClient *http.Client
}

func NewRAGClient(baseURL string) *RAGClient {
    return &RAGClient{
        baseURL: baseURL,
        httpClient: &http.Client{
            Timeout: 30 * time.Second,
        },
    }
}

func (c *RAGClient) Search(ctx context.Context, req SearchRequest) (*SearchResponse, error) {
    body, _ := json.Marshal(req)
    httpReq, _ := http.NewRequestWithContext(ctx, "POST", c.baseURL+"/search", bytes.NewReader(body))
    httpReq.Header.Set("Content-Type", "application/json")

    resp, err := c.httpClient.Do(httpReq)
    if err != nil {
        return nil, fmt.Errorf("RAG service call failed: %w", err)
    }
    defer resp.Body.Close()

    var result SearchResponse
    if err := json.NewDecoder(resp.Body).Decode(&result); err != nil {
        return nil, fmt.Errorf("failed to decode response: %w", err)
    }
    return &result, nil
}
```

### Anti-Patterns to Avoid
- **Logger after Recoverer:** Logger must come BEFORE Recoverer to log panics
- **Manual JSON encoding in handlers:** Use render.JSON() or render.Render()
- **No timeout on HTTP client:** Always set client timeout for external calls
- **Blocking SSE handlers:** Must check ctx.Done() to prevent goroutine leaks
- **Storing context in structs:** Pass context explicitly as first parameter
</architecture_patterns>

<dont_hand_roll>
## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| JSON response encoding | `json.Marshal` + write | `render.JSON()` or `render.Render()` | Handles content-type, escaping, status codes |
| Request validation | `if req.Field == ""` | go-playground/validator | Tags, cross-field validation, proper error messages |
| Structured logging | `log.Printf()` | httplog | Request correlation, status-based levels, slog integration |
| JWT parsing | Manual token parsing | jwtauth | JWKS support, middleware chain, Supabase compatible |
| SSE message format | `fmt.Fprintf("data: ...")` | Consider go-sse library | Spec-compliant, handles reconnection IDs |
| Error response format | Ad-hoc error JSON | Implement render.Renderer | Consistent error format across API |

**Key insight:** Chi's ecosystem libraries (render, httplog, jwtauth) are designed to work together. Using them ensures consistent patterns and proper integration. Manual alternatives lead to inconsistent APIs and missed edge cases (like HTML escaping in JSON).
</dont_hand_roll>

<common_pitfalls>
## Common Pitfalls

### Pitfall 1: SSE Timeout Kills Connection
**What goes wrong:** SSE stream closes after 10-30 seconds
**Why it happens:** Default http.Server WriteTimeout or framework timeout
**How to avoid:** Disable timeout for SSE routes, or set very long timeout
**Warning signs:** Streaming works initially then abruptly stops

### Pitfall 2: Goroutine Leak in SSE Handler
**What goes wrong:** Memory usage grows over time, eventual OOM
**Why it happens:** SSE handler doesn't exit when client disconnects
**How to avoid:** Always `select` on `ctx.Done()` in streaming loops
**Warning signs:** Goroutine count increases but doesn't decrease

### Pitfall 3: Middleware Order Breaks Logging
**What goes wrong:** Panics not logged, or logged twice
**Why it happens:** Recoverer before Logger swallows panic before logging
**How to avoid:** Order: RequestID → Logger → Recoverer → others
**Warning signs:** 500 errors in responses but nothing in logs

### Pitfall 4: JWT Validation Ignores Expiry
**What goes wrong:** Expired tokens accepted
**Why it happens:** Using wrong jwtauth configuration or skipping Authenticator
**How to avoid:** Use both Verifier (decode) AND Authenticator (validate) middleware
**Warning signs:** Logged-out users can still access protected routes

### Pitfall 5: Python Service Connection Pooling
**What goes wrong:** "too many open files" errors under load
**Why it happens:** Creating new http.Client per request
**How to avoid:** Reuse http.Client, it handles connection pooling
**Warning signs:** Performance degrades under concurrent load, file descriptor errors
</common_pitfalls>

<code_examples>
## Code Examples

### Chi v5 Middleware Chain Setup
```go
// Source: go-chi docs
import (
    "github.com/go-chi/chi/v5"
    "github.com/go-chi/chi/v5/middleware"
    "github.com/go-chi/httplog/v2"
)

func main() {
    logger := httplog.NewLogger("smart-docs-api", httplog.Options{
        JSON:             true, // Production: JSON logs
        LogLevel:         slog.LevelInfo,
        Concise:          true,
        RequestHeaders:   true,
        ResponseHeaders:  false,
    })

    r := chi.NewRouter()
    r.Use(httplog.RequestLogger(logger))
    r.Use(middleware.Recoverer)
    r.Use(middleware.RequestID)
    r.Use(middleware.Timeout(60 * time.Second)) // Not for SSE routes!

    // Mount sub-routers
    r.Mount("/api", apiRouter(logger))
}
```

### Supabase JWT Configuration
```go
// Source: go-chi/jwtauth + Supabase docs
import "github.com/go-chi/jwtauth/v5"

func NewJWTAuth(supabaseJWTSecret string) *jwtauth.JWTAuth {
    // For HS256 (legacy Supabase or project JWT secret)
    return jwtauth.New("HS256", []byte(supabaseJWTSecret), nil)

    // For RS256 (modern Supabase), fetch JWKS:
    // keySet, _ := jwk.Fetch(ctx, "https://YOUR_PROJECT.supabase.co/.well-known/jwks.json")
    // return jwtauth.New("RS256", nil, keySet)
}

// Protected route group
r.Group(func(r chi.Router) {
    r.Use(jwtauth.Verifier(tokenAuth))
    r.Use(jwtauth.Authenticator(tokenAuth))

    // Access claims in handler:
    // _, claims, _ := jwtauth.FromContext(r.Context())
    // userID := claims["sub"].(string)
})
```

### Error Response Pattern
```go
// Source: go-chi/render examples
type ErrResponse struct {
    Err            error  `json:"-"`
    HTTPStatusCode int    `json:"-"`
    StatusText     string `json:"status"`
    ErrorText      string `json:"error,omitempty"`
}

func (e *ErrResponse) Render(w http.ResponseWriter, r *http.Request) error {
    render.Status(r, e.HTTPStatusCode)
    return nil
}

func ErrInvalidRequest(err error) render.Renderer {
    return &ErrResponse{
        Err:            err,
        HTTPStatusCode: 400,
        StatusText:     "Invalid request",
        ErrorText:      err.Error(),
    }
}

func ErrUnauthorized() render.Renderer {
    return &ErrResponse{
        HTTPStatusCode: 401,
        StatusText:     "Unauthorized",
    }
}
```
</code_examples>

<sota_updates>
## State of the Art (2025-2026)

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| chi v4 | chi v5 | 2022 | Go modules support, security fixes |
| log package | log/slog (Go 1.21+) | 2023 | httplog v2 built on slog, structured logging |
| manual JWT parsing | jwtauth with JWKS | 2024 | Supabase RS256 support, automatic key rotation |
| json.Encoder streaming | SSE libraries | 2024+ | Spec-compliant, reconnection support |

**New tools/patterns to consider:**
- **httplog/v2:** Built on Go 1.21 slog, auto log levels by status, ECS/OTEL formats
- **JWKS rotation:** Supabase exposes /.well-known/jwks.json for automatic key rotation

**Deprecated/outdated:**
- **chi v4:** Has security vulnerability GO-2026-4316 in RedirectSlashes
- **manual logging middleware:** Use httplog for production-ready logging
</sota_updates>

<open_questions>
## Open Questions

1. **SSE vs WebSocket for chat**
   - What we know: SSE is simpler, unidirectional, sufficient for LLM streaming
   - What's unclear: Whether we need bidirectional for future features (cancel mid-stream)
   - Recommendation: Start with SSE, WebSocket is easy to add later if needed

2. **Python FastAPI internal service port**
   - What we know: Go calls Python, both run in Docker
   - What's unclear: Container networking setup (docker-compose service names vs ports)
   - Recommendation: Use environment variable for RAG_SERVICE_URL, default to `http://workers:8000`
</open_questions>

<sources>
## Sources

### Primary (HIGH confidence)
- [go-chi/chi GitHub](https://github.com/go-chi/chi) - Router overview, middleware chain
- [go-chi/httplog GitHub](https://github.com/go-chi/httplog) - Structured logging setup
- [go-chi/render GitHub](https://github.com/go-chi/render) - Request/response patterns
- [go-chi/jwtauth GitHub](https://github.com/go-chi/jwtauth) - JWT middleware
- [go-playground/validator](https://github.com/go-playground/validator) - Request validation
- [Go SSE implementation guide](https://www.freecodecamp.org/news/how-to-implement-server-sent-events-in-go/) - SSE patterns

### Secondary (MEDIUM confidence)
- [Chi REST API best practices 2026](https://oneuptime.com/blog/post/2026-01-07-go-rest-api-chi/view) - Project structure patterns
- [Go timeouts guide](https://betterstack.com/community/guides/scaling-go/golang-timeouts/) - Context timeout patterns
- [Supabase JWT docs](https://supabase.com/docs/guides/auth/jwts) - JWKS verification

### Tertiary (LOW confidence - needs validation)
- None - all findings verified against official sources
</sources>

<metadata>
## Metadata

**Research scope:**
- Core technology: Chi v5 router for Go
- Ecosystem: httplog, render, jwtauth, validator
- Patterns: Middleware chain, SSE streaming, request validation, service clients
- Pitfalls: Timeouts, goroutine leaks, middleware ordering

**Confidence breakdown:**
- Standard stack: HIGH - verified with official GitHub repos and pkg.go.dev
- Architecture: HIGH - patterns from official chi examples and community best practices
- Pitfalls: HIGH - documented in official docs and community issues
- Code examples: HIGH - from official documentation with minor adaptation

**Research date:** 2026-02-01
**Valid until:** 2026-03-01 (30 days - Chi ecosystem stable)
</metadata>

---

*Phase: 05-api-framework*
*Research completed: 2026-02-01*
*Ready for planning: yes*
