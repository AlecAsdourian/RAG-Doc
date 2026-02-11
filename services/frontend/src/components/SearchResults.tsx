import { Search, AlertCircle, FileQuestion } from 'lucide-react';
import { CodeCard } from '@/components/CodeCard';
import { Card } from '@/components/ui/card';
import type { ChunkResult } from '@/lib/api/types';

interface SearchResultsProps {
  query: string;
  results: ChunkResult[];
  totalResults: number;
  isLoading: boolean;
  error: string | null;
}

export function SearchResults({
  query,
  results,
  totalResults,
  isLoading,
  error,
}: SearchResultsProps) {
  // Empty state - no query
  if (!query.trim()) {
    return (
      <EmptyState
        icon={<Search className="h-12 w-12 text-[var(--color-muted-foreground)]" />}
        title="Search your codebase"
        description="Enter a query to search for code, functions, or documentation"
      />
    );
  }

  // Loading state
  if (isLoading) {
    return (
      <div className="space-y-4">
        <ResultsHeader query={query} count={null} />
        <div className="space-y-3">
          {[1, 2, 3].map((i) => (
            <SkeletonCard key={i} />
          ))}
        </div>
      </div>
    );
  }

  // Error state
  if (error) {
    return (
      <EmptyState
        icon={<AlertCircle className="h-12 w-12 text-[var(--color-destructive)]" />}
        title="Search failed"
        description={error}
      />
    );
  }

  // No results
  if (results.length === 0) {
    return (
      <EmptyState
        icon={<FileQuestion className="h-12 w-12 text-[var(--color-muted-foreground)]" />}
        title="No results found"
        description={`No matches for "${query}". Try different keywords.`}
      />
    );
  }

  // Results list
  return (
    <div className="space-y-4">
      <ResultsHeader query={query} count={totalResults} />
      <div className="space-y-3">
        {results.map((result) => (
          <CodeCard
            key={result.chunk_id}
            result={result}
            highlightQuery={query}
          />
        ))}
      </div>
    </div>
  );
}

function ResultsHeader({
  query,
  count,
}: {
  query: string;
  count: number | null;
}) {
  return (
    <div className="flex items-center justify-between">
      <p className="text-sm text-[var(--color-muted-foreground)]">
        {count !== null ? (
          <>
            Found{' '}
            <span className="font-medium text-[var(--color-foreground)]">
              {count}
            </span>{' '}
            result{count !== 1 ? 's' : ''} for{' '}
            <span className="font-medium text-[var(--color-foreground)]">
              "{query}"
            </span>
          </>
        ) : (
          <>Searching for "{query}"...</>
        )}
      </p>
    </div>
  );
}

function EmptyState({
  icon,
  title,
  description,
}: {
  icon: React.ReactNode;
  title: string;
  description: string;
}) {
  return (
    <div className="flex flex-col items-center justify-center py-16 text-center">
      <div className="mb-4">{icon}</div>
      <h3 className="text-lg font-medium text-[var(--color-foreground)] mb-1">
        {title}
      </h3>
      <p className="text-sm text-[var(--color-muted-foreground)] max-w-md">
        {description}
      </p>
    </div>
  );
}

function SkeletonCard() {
  return (
    <Card className="overflow-hidden">
      <div className="px-4 py-3 bg-[var(--color-secondary)]/30 border-b border-[var(--color-border)]">
        <div className="flex items-center gap-3">
          <div className="h-4 w-4 rounded bg-[var(--color-muted)] animate-pulse" />
          <div className="flex-1">
            <div className="h-4 w-32 rounded bg-[var(--color-muted)] animate-pulse mb-1" />
            <div className="h-3 w-48 rounded bg-[var(--color-muted)] animate-pulse" />
          </div>
        </div>
      </div>
      <div className="p-4 space-y-2">
        <div className="h-4 w-full rounded bg-[var(--color-muted)] animate-pulse" />
        <div className="h-4 w-3/4 rounded bg-[var(--color-muted)] animate-pulse" />
        <div className="h-4 w-5/6 rounded bg-[var(--color-muted)] animate-pulse" />
      </div>
    </Card>
  );
}
