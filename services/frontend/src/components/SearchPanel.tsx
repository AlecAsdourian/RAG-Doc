import { SearchInput } from '@/components/SearchInput';
import { SearchResults } from '@/components/SearchResults';
import { useSearch } from '@/hooks/useSearch';
import { ScrollArea } from '@/components/ui/scroll-area';

interface SearchPanelProps {
  repositoryId: string;
}

export function SearchPanel({ repositoryId }: SearchPanelProps) {
  const { query, setQuery, results, totalResults, isLoading, error } = useSearch({
    repositoryId,
  });

  return (
    <div className="flex flex-col h-full">
      {/* Header with search input */}
      <div className="p-4 border-b border-[var(--color-border)]">
        <SearchInput
          value={query}
          onChange={setQuery}
          isLoading={isLoading}
        />
      </div>

      {/* Results area */}
      <ScrollArea className="flex-1 p-4">
        <SearchResults
          query={query}
          results={results}
          totalResults={totalResults}
          isLoading={isLoading}
          error={error}
        />
      </ScrollArea>
    </div>
  );
}
