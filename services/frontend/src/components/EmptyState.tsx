import { Search, Loader2 } from 'lucide-react';

interface EmptyStateProps {
  type: 'initial' | 'loading' | 'no-results';
  query?: string;
  onSuggestionClick?: (suggestion: string) => void;
}

export function EmptyState({ type, query, onSuggestionClick }: EmptyStateProps) {
  if (type === 'loading') {
    return (
      <div className="flex flex-col items-center justify-center py-32">
        <Loader2 className="h-5 w-5 animate-spin text-muted-foreground" />
        <p className="mt-4 text-sm text-muted-foreground">
          Searching across repositories...
        </p>
      </div>
    );
  }

  if (type === 'no-results') {
    return (
      <div className="flex flex-col items-center justify-center py-32">
        <h3 className="text-base font-medium text-foreground">
          No results found
        </h3>
        <p className="mt-2 max-w-sm text-center text-sm leading-relaxed text-muted-foreground">
          {`No code or documentation matched "${query}". Try different search terms or check your active filters.`}
        </p>
      </div>
    );
  }

  return (
    <div className="flex flex-col items-center justify-center py-32">
      <Search className="h-6 w-6 text-muted-foreground/50" />
      <h3 className="mt-4 text-base font-medium text-foreground">
        Search your codebase
      </h3>
      <p className="mt-2 max-w-sm text-center text-sm leading-relaxed text-muted-foreground">
        Find functions, classes, APIs, and documentation across all your indexed
        repositories.
      </p>
      <div className="mt-6 flex flex-wrap items-center justify-center gap-2">
        {['useAuth hook', 'authentication flow', 'API middleware', 'error handling'].map(
          (suggestion) => (
            <button
              key={suggestion}
              onClick={() => onSuggestionClick?.(suggestion)}
              className="rounded-md border border-border bg-card px-3 py-1.5 text-xs text-muted-foreground shadow-sm transition-colors hover:bg-muted hover:text-foreground"
            >
              {suggestion}
            </button>
          )
        )}
      </div>
    </div>
  );
}
