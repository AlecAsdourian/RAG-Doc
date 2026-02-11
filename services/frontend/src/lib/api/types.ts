// API types matching Go backend (services/backend/pkg/client/types.go)

export interface SearchRequest {
  query: string;
  repository_id: string;
  top_k?: number;
}

export interface ChunkResult {
  chunk_id: string;
  content: string;
  content_preview: string;
  file_path: string;
  start_line: number;
  end_line: number;
  breadcrumb?: string;
  chunk_type?: string;
  score: number;
  rrf_score?: number;
  boost_multiplier?: number;
}

export interface SearchResponse {
  results: ChunkResult[];
  query_id?: string;
  total_results: number;
  metadata?: Record<string, unknown>;
}

export interface ChatRequest {
  query: string;
  repository_id: string;
  top_k?: number;
}

export interface SourceInfo {
  number: number;
  file_path: string;
  start_line: number;
  end_line: number;
  breadcrumb?: string;
  chunk_type?: string;
}

export interface ChatResponse {
  answer: string;
  sources: SourceInfo[];
  query_id?: string;
  cost: number;
  tokens_in: number;
  tokens_out: number;
  cache_hit: boolean;
  model?: string;
  chunks_retrieved?: number;
}

export interface ChatChunk {
  type: 'chunk' | 'done' | 'error';
  content?: string;
  sources?: SourceInfo[];
  query_id?: string;
  cost?: number;
  tokens_in?: number;
  tokens_out?: number;
  cache_hit?: boolean;
  error?: string;
}
