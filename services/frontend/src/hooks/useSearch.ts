import { useState, useEffect, useCallback } from 'react';
import { apiClient } from '@/lib/api/client';
import type { ChunkResult } from '@/lib/api/types';

const DEBOUNCE_MS = 300;

interface UseSearchOptions {
  repositoryId: string;
  topK?: number;
}

interface UseSearchResult {
  query: string;
  setQuery: (query: string) => void;
  results: ChunkResult[];
  totalResults: number;
  isLoading: boolean;
  error: string | null;
  clearError: () => void;
}

export function useSearch({ repositoryId, topK = 10 }: UseSearchOptions): UseSearchResult {
  const [query, setQuery] = useState('');
  const [results, setResults] = useState<ChunkResult[]>([]);
  const [totalResults, setTotalResults] = useState(0);
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const clearError = useCallback(() => setError(null), []);

  useEffect(() => {
    // Clear results if query is empty
    if (!query.trim()) {
      setResults([]);
      setTotalResults(0);
      setError(null);
      return;
    }

    // Set up debounced search
    const timeoutId = setTimeout(async () => {
      setIsLoading(true);
      setError(null);

      try {
        const response = await apiClient.search({
          query: query.trim(),
          repository_id: repositoryId,
          top_k: topK,
        });

        setResults(response.results);
        setTotalResults(response.total_results);
      } catch (e) {
        const message = e instanceof Error ? e.message : 'Search failed';
        setError(message);
        setResults([]);
        setTotalResults(0);
      } finally {
        setIsLoading(false);
      }
    }, DEBOUNCE_MS);

    return () => clearTimeout(timeoutId);
  }, [query, repositoryId, topK]);

  return {
    query,
    setQuery,
    results,
    totalResults,
    isLoading,
    error,
    clearError,
  };
}
