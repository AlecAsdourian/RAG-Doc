import { FileCode2, FileText, GitBranch, Check, Loader2, AlertCircle } from 'lucide-react';
import { cn } from '@/lib/utils';
import type { SearchResult, IndexedRepo } from '@/types';

interface SourceListProps {
  results: SearchResult[];
  repos: IndexedRepo[];
  selectedResultId: string | null;
  onSelectResult: (id: string) => void;
  selectedRepo: string | null;
  onSelectRepo: (repo: string | null) => void;
  hasSearched: boolean;
  isSearching: boolean;
}

export function SourceList({
  results,
  repos,
  selectedResultId,
  onSelectResult,
  selectedRepo,
  onSelectRepo,
  hasSearched,
  isSearching,
}: SourceListProps) {
  return (
    <aside className="flex w-72 shrink-0 flex-col border-r border-border bg-card">
      {/* Repositories section */}
      <div className="border-b border-border px-4 py-3">
        <h3 className="mb-2 text-[11px] font-medium uppercase tracking-wider text-muted-foreground">
          Repositories
        </h3>
        <div className="flex flex-col gap-0.5">
          <button
            onClick={() => onSelectRepo(null)}
            className={cn(
              'flex items-center gap-2 rounded px-2 py-1.5 text-left text-xs transition-colors',
              selectedRepo === null
                ? 'bg-accent/10 text-accent font-medium'
                : 'text-muted-foreground hover:bg-muted hover:text-foreground'
            )}
          >
            All repos
          </button>
          {repos.map((repo) => (
            <button
              key={repo.id}
              onClick={() =>
                repo.status === 'ready' ? onSelectRepo(repo.name) : undefined
              }
              className={cn(
                'flex items-center justify-between rounded px-2 py-1.5 text-left text-xs transition-colors',
                repo.status !== 'ready' && 'cursor-default opacity-60',
                selectedRepo === repo.name
                  ? 'bg-accent/10 text-accent font-medium'
                  : 'text-muted-foreground hover:bg-muted hover:text-foreground'
              )}
            >
              <span className="flex items-center gap-1.5 truncate">
                <GitBranch className="h-3 w-3 shrink-0" />
                <span className="truncate">{repo.name.split('/').pop()}</span>
              </span>
              <StatusDot status={repo.status} />
            </button>
          ))}
        </div>
      </div>

      {/* Sources list */}
      <div className="flex-1 overflow-y-auto">
        {isSearching && (
          <div className="flex items-center justify-center py-8">
            <Loader2 className="h-4 w-4 animate-spin text-muted-foreground" />
          </div>
        )}
        {hasSearched && !isSearching && results.length > 0 && (
          <div className="px-4 py-3">
            <h3 className="mb-2 text-[11px] font-medium uppercase tracking-wider text-muted-foreground">
              Sources
            </h3>
            <div className="flex flex-col gap-0.5">
              {results.map((result, index) => (
                <SourceItem
                  key={result.id}
                  result={result}
                  index={index}
                  isSelected={selectedResultId === result.id}
                  onSelect={() => onSelectResult(result.id)}
                />
              ))}
            </div>
          </div>
        )}
      </div>
    </aside>
  );
}

function SourceItem({
  result,
  index,
  isSelected,
  onSelect,
}: {
  result: SearchResult;
  index: number;
  isSelected: boolean;
  onSelect: () => void;
}) {
  const Icon = result.type === 'code' ? FileCode2 : FileText;

  return (
    <button
      onClick={onSelect}
      className={cn(
        'group flex w-full items-start gap-2.5 rounded-md px-2.5 py-2 text-left transition-colors',
        isSelected
          ? 'bg-accent/10'
          : 'hover:bg-muted'
      )}
    >
      <span className="mt-0.5 flex h-5 w-5 shrink-0 items-center justify-center rounded bg-muted text-[10px] font-medium text-muted-foreground">
        {index + 1}
      </span>
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-1.5">
          <Icon className={cn(
            'h-3.5 w-3.5 shrink-0',
            isSelected ? 'text-accent' : 'text-muted-foreground'
          )} />
          <span
            className={cn(
              'truncate font-mono text-xs',
              isSelected ? 'text-foreground font-medium' : 'text-foreground'
            )}
          >
            {result.fileName}
          </span>
        </div>
        <p className="mt-0.5 truncate text-[11px] leading-relaxed text-muted-foreground">
          {result.filePath}
        </p>
        <div className="mt-1 flex items-center gap-2">
          <span className="text-[10px] text-muted-foreground">
            {result.repoName.split('/').pop()}
          </span>
          <span className="text-[10px] text-muted-foreground">
            {Math.round(result.relevanceScore * 100)}%
          </span>
        </div>
      </div>
    </button>
  );
}

function StatusDot({ status }: { status: IndexedRepo['status'] }) {
  if (status === 'ready') {
    return <Check className="h-3 w-3 shrink-0 text-[oklch(0.55_0.12_155)]" />;
  }
  if (status === 'indexing') {
    return <Loader2 className="h-3 w-3 shrink-0 animate-spin text-muted-foreground" />;
  }
  return <AlertCircle className="h-3 w-3 shrink-0 text-destructive" />;
}
