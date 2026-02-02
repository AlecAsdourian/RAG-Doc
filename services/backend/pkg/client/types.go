package client

// SearchRequest represents a search request to the RAG service
type SearchRequest struct {
	Query        string `json:"query"`
	RepositoryID string `json:"repository_id"`
	TopK         int    `json:"top_k,omitempty"`
}

// ChunkResult represents a single search result chunk
type ChunkResult struct {
	ChunkID        string  `json:"chunk_id"`
	Content        string  `json:"content"`
	ContentPreview string  `json:"content_preview"`
	FilePath       string  `json:"file_path"`
	StartLine      int     `json:"start_line"`
	EndLine        int     `json:"end_line"`
	Breadcrumb     *string `json:"breadcrumb,omitempty"`
	ChunkType      *string `json:"chunk_type,omitempty"`
	Score          float64 `json:"score"`
	RRFScore       *float64 `json:"rrf_score,omitempty"`
	BoostMultiplier *float64 `json:"boost_multiplier,omitempty"`
}

// SearchResponse represents the response from a search request
type SearchResponse struct {
	Results      []ChunkResult          `json:"results"`
	QueryID      string                 `json:"query_id,omitempty"`
	TotalResults int                    `json:"total_results"`
	Metadata     map[string]interface{} `json:"metadata,omitempty"`
}

// ChatRequest represents a chat/answer generation request
type ChatRequest struct {
	Query        string `json:"query"`
	RepositoryID string `json:"repository_id"`
	TopK         int    `json:"top_k,omitempty"`
}

// SourceInfo represents source citation information
type SourceInfo struct {
	Number    int     `json:"number"`
	FilePath  string  `json:"file_path"`
	StartLine int     `json:"start_line"`
	EndLine   int     `json:"end_line"`
	Breadcrumb *string `json:"breadcrumb,omitempty"`
	ChunkType  *string `json:"chunk_type,omitempty"`
}

// ChatResponse represents the response from a chat request
type ChatResponse struct {
	Answer          string       `json:"answer"`
	Sources         []SourceInfo `json:"sources"`
	QueryID         string       `json:"query_id,omitempty"`
	Cost            float64      `json:"cost"`
	TokensIn        int          `json:"tokens_in"`
	TokensOut       int          `json:"tokens_out"`
	CacheHit        bool         `json:"cache_hit"`
	Model           *string      `json:"model,omitempty"`
	ChunksRetrieved *int         `json:"chunks_retrieved,omitempty"`
}

// ChatChunk represents a streaming chat chunk for SSE
type ChatChunk struct {
	Type    string       `json:"type"`    // "chunk", "done", "error"
	Content string       `json:"content,omitempty"`
	Sources []SourceInfo `json:"sources,omitempty"`
	QueryID string       `json:"query_id,omitempty"`
	Cost    float64      `json:"cost,omitempty"`
	Error   string       `json:"error,omitempty"`
}
