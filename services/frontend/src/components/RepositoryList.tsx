import { GitBranch, Check, Loader2, AlertCircle, Clock } from 'lucide-react';
import { ScrollArea } from '@/components/ui/scroll-area';
import { Badge } from '@/components/ui/badge';
import { cn } from '@/lib/utils';

export interface Repository {
  id: string;
  name: string;
  filesIndexed: number;
  lastUpdated: string;
  status: 'ready' | 'indexing' | 'error';
}

interface RepositoryListProps {
  repositories: Repository[];
  selectedRepoId?: string;
  onSelect?: (repoId: string) => void;
}

export function RepositoryList({
  repositories,
  selectedRepoId,
  onSelect,
}: RepositoryListProps) {
  return (
    <div className="flex flex-col h-full">
      <div className="px-4 py-3 border-b border-[var(--color-border)]">
        <div className="flex items-center justify-between">
          <h3 className="text-xs font-semibold text-[var(--color-muted-foreground)] uppercase tracking-wide">
            Repositories
          </h3>
          <span className="text-xs text-[var(--color-muted-foreground)] font-medium">
            {repositories.length}
          </span>
        </div>
      </div>

      <ScrollArea className="flex-1">
        <div className="p-2 space-y-1">
          {repositories.map((repo) => (
            <RepositoryCard
              key={repo.id}
              repository={repo}
              isSelected={repo.id === selectedRepoId}
              onClick={() => onSelect?.(repo.id)}
            />
          ))}
        </div>
      </ScrollArea>
    </div>
  );
}

interface RepositoryCardProps {
  repository: Repository;
  isSelected: boolean;
  onClick: () => void;
}

function RepositoryCard({ repository, isSelected, onClick }: RepositoryCardProps) {
  return (
    <button
      onClick={onClick}
      className={cn(
        'w-full text-left p-3 rounded-lg transition-all duration-200',
        'border border-transparent',
        'hover:bg-[var(--color-secondary)] hover:border-[var(--color-border)]',
        'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--color-accent)]',
        isSelected && 'bg-[var(--color-accent)]/10 border-[var(--color-accent)]/30'
      )}
    >
      <div className="flex items-start gap-2.5">
        <GitBranch className="h-4 w-4 text-[var(--color-muted-foreground)] mt-0.5 flex-shrink-0" />
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 mb-1">
            <span className={cn(
              'text-sm font-medium truncate',
              isSelected ? 'text-[var(--color-accent)]' : 'text-[var(--color-foreground)]'
            )}>
              {repository.name}
            </span>
            <StatusBadge status={repository.status} />
          </div>
          <div className="flex items-center gap-2 text-xs text-[var(--color-muted-foreground)]">
            <span className="font-medium">{repository.filesIndexed.toLocaleString()} files</span>
            <span>·</span>
            <div className="flex items-center gap-1">
              <Clock className="h-3 w-3" />
              <span>{repository.lastUpdated}</span>
            </div>
          </div>
        </div>
      </div>
    </button>
  );
}

interface StatusBadgeProps {
  status: Repository['status'];
}

function StatusBadge({ status }: StatusBadgeProps) {
  const config = {
    ready: {
      icon: Check,
      label: 'Ready',
      className: 'bg-green-500/10 text-green-600 dark:text-green-400 border-green-500/20',
    },
    indexing: {
      icon: Loader2,
      label: 'Indexing',
      className: 'bg-amber-500/10 text-amber-600 dark:text-amber-400 border-amber-500/20',
      animate: true,
    },
    error: {
      icon: AlertCircle,
      label: 'Error',
      className: 'bg-red-500/10 text-red-600 dark:text-red-400 border-red-500/20',
    },
  };

  const statusConfig = config[status];
  const { icon: Icon, label, className } = statusConfig;
  const animate = 'animate' in statusConfig ? statusConfig.animate : false;

  return (
    <Badge
      variant="outline"
      className={cn('h-5 px-1.5 text-xs font-medium border', className)}
    >
      <Icon className={cn('h-3 w-3 mr-1', animate && 'animate-spin')} />
      {label}
    </Badge>
  );
}
