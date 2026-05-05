import { useEffect, useMemo, useState } from 'react';
import { SearchHeader } from '@/components/SearchHeader';
import { SourceList } from '@/components/SourceList';
import { SearchResultDetail } from '@/components/SearchResultDetail';
import { EmptyState } from '@/components/EmptyState';
import { ChatPanelNew } from '@/components/ChatPanelNew';
import { useSearch } from '@/hooks/useSearch';
import { useRepositories } from '@/hooks/useRepositories';
import { mapChunkToSearchResult, type FileType, type IndexedRepo } from '@/types';

export function SearchPage() {
  const { repositories, selectedRepo } = useRepositories();
  const repositoryId = selectedRepo?.id || 'demo-repo';
  const { query, setQuery, results, isLoading: isSearching } = useSearch({ repositoryId });

  const [isChatOpen, setIsChatOpen] = useState(false);
  const [selectedResultId, setSelectedResultId] = useState<string | null>(null);
  const [fileTypeFilter, setFileTypeFilter] = useState<FileType>('all');
  const [selectedRepoFilter, setSelectedRepoFilter] = useState<string | null>(null);

  const indexedRepos: IndexedRepo[] = useMemo(
    () => repositories.map((r) => ({ id: r.id, name: r.name, status: r.status })),
    [repositories]
  );

  const repoName = selectedRepo?.name || 'unknown/repo';
  const searchResults = useMemo(() => {
    let mapped = results.map((chunk, i) => mapChunkToSearchResult(chunk, i, repoName));
    if (fileTypeFilter !== 'all') mapped = mapped.filter((r) => r.type === fileTypeFilter);
    if (selectedRepoFilter) mapped = mapped.filter((r) => r.repoName === selectedRepoFilter);
    return mapped;
  }, [results, fileTypeFilter, selectedRepoFilter, repoName]);

  const selectedResult = useMemo(
    () => searchResults.find((r) => r.id === selectedResultId) || null,
    [searchResults, selectedResultId]
  );

  const hasSearched = query.trim().length > 0;

  useEffect(() => {
    if (searchResults.length > 0 && !selectedResult) {
      setSelectedResultId(searchResults[0].id);
    }
  }, [searchResults, selectedResult]);

  useEffect(() => {
    if (!hasSearched) setSelectedResultId(null);
  }, [hasSearched]);

  let centerContent: React.ReactNode;
  if (isSearching) {
    centerContent = <EmptyState type="loading" />;
  } else if (hasSearched && searchResults.length === 0) {
    centerContent = <EmptyState type="no-results" query={query} />;
  } else if (selectedResult) {
    centerContent = <SearchResultDetail result={selectedResult} query={query} />;
  } else {
    centerContent = (
      <EmptyState type="initial" onSuggestionClick={(suggestion) => setQuery(suggestion)} />
    );
  }

  return (
    <div className="flex flex-1 min-h-0 flex-col overflow-hidden">
      <SearchHeader
        query={query}
        setQuery={setQuery}
        fileTypeFilter={fileTypeFilter}
        setFileTypeFilter={setFileTypeFilter}
        resultCount={hasSearched ? searchResults.length : undefined}
        isChatOpen={isChatOpen}
        onToggleChat={() => setIsChatOpen((prev) => !prev)}
      />
      <div className="flex flex-1 overflow-hidden">
        <SourceList
          results={searchResults}
          repos={indexedRepos}
          selectedResultId={selectedResultId}
          onSelectResult={setSelectedResultId}
          selectedRepo={selectedRepoFilter}
          onSelectRepo={setSelectedRepoFilter}
          hasSearched={hasSearched}
          isSearching={isSearching}
        />
        <main className="flex-1 overflow-y-auto">{centerContent}</main>
        {isChatOpen && (
          <ChatPanelNew
            isOpen={isChatOpen}
            onClose={() => setIsChatOpen(false)}
            repositoryId={repositoryId}
          />
        )}
      </div>
    </div>
  );
}
