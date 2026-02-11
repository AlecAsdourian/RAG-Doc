import { SearchInput } from '@/components/SearchInput';
import { SearchResults } from '@/components/SearchResults';
import { RepoSelector } from '@/components/RepoSelector';
import { useSearch } from '@/hooks/useSearch';
import { useRepositories } from '@/hooks/useRepositories';
import { ScrollArea } from '@/components/ui/scroll-area';

interface SearchPanelProps {
  repositoryId: string;
  onRepoChange?: (repoId: string) => void;
}

export function SearchPanel({ repositoryId, onRepoChange }: SearchPanelProps) {
  const { repositories, isLoading: reposLoading } = useRepositories(repositoryId);
  const { query, setQuery, results, totalResults, isLoading, error } = useSearch({
    repositoryId,
  });

  const handleRepoChange = (repoId: string) => {
    onRepoChange?.(repoId);
  };

  return (
    <div className="flex flex-col h-full">
      {/* Header with repo selector and search input */}
      <div className="p-4 border-b border-[var(--color-border)] space-y-3">
        <div className="flex items-center justify-between">
          <RepoSelector
            repositories={repositories}
            selectedRepoId={repositoryId}
            onSelect={handleRepoChange}
            isLoading={reposLoading}
          />
        </div>
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
