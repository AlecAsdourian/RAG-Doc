import { GitBranch } from 'lucide-react';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import type { Repository } from '@/hooks/useRepositories';

interface RepoSelectorProps {
  repositories: Repository[];
  selectedRepoId: string;
  onSelect: (repoId: string) => void;
  isLoading?: boolean;
}

export function RepoSelector({
  repositories,
  selectedRepoId,
  onSelect,
  isLoading = false,
}: RepoSelectorProps) {
  const selectedRepo = repositories.find((r) => r.id === selectedRepoId);

  if (isLoading) {
    return (
      <div className="h-9 w-48 rounded-md bg-[var(--color-muted)] animate-pulse" />
    );
  }

  return (
    <Select value={selectedRepoId} onValueChange={onSelect}>
      <SelectTrigger className="w-48">
        <div className="flex items-center gap-2">
          <GitBranch className="h-4 w-4 text-[var(--color-muted-foreground)]" />
          <SelectValue placeholder="Select repository">
            {selectedRepo?.name || 'Select repository'}
          </SelectValue>
        </div>
      </SelectTrigger>
      <SelectContent>
        {repositories.map((repo) => (
          <SelectItem key={repo.id} value={repo.id}>
            <div className="flex items-center gap-2">
              <StatusIndicator status={repo.status} />
              <span>{repo.name}</span>
            </div>
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  );
}

function StatusIndicator({ status }: { status: Repository['status'] }) {
  const colors = {
    ready: 'bg-green-500',
    indexing: 'bg-yellow-500',
    error: 'bg-red-500',
  };

  return (
    <span
      className={`h-2 w-2 rounded-full ${colors[status]}`}
      title={status === 'ready' ? 'Ready' : status === 'indexing' ? 'Indexing...' : 'Error'}
    />
  );
}
