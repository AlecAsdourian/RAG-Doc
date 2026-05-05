import { useEffect, useRef } from 'react';
import { Search, MessageSquare, Building2 } from 'lucide-react';
import { Input } from '@/components/ui/input';
import { Button } from '@/components/ui/button';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { cn } from '@/lib/utils';
import type { FileType, Organization } from '@/types';

interface SearchHeaderProps {
  query: string;
  setQuery: (query: string) => void;
  fileTypeFilter: FileType;
  setFileTypeFilter: (filter: FileType) => void;
  resultCount?: number;
  isChatOpen: boolean;
  onToggleChat: () => void;
  organizations?: Organization[];
  selectedOrgId?: string;
  onSelectOrg?: (orgId: string) => void;
}

const fileTypes: { value: FileType; label: string }[] = [
  { value: 'all', label: 'All' },
  { value: 'code', label: 'Code' },
  { value: 'docs', label: 'Docs' },
];

export function SearchHeader({
  query,
  setQuery,
  fileTypeFilter,
  setFileTypeFilter,
  resultCount,
  isChatOpen,
  onToggleChat,
  organizations,
  selectedOrgId,
  onSelectOrg,
}: SearchHeaderProps) {
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key === 'k') {
        e.preventDefault();
        inputRef.current?.focus();
      }
    };

    document.addEventListener('keydown', handleKeyDown);
    return () => document.removeEventListener('keydown', handleKeyDown);
  }, []);

  return (
    <header className="shrink-0 border-b border-border bg-card">
      <div className="flex items-center gap-6 px-6 py-3">
        {/* Logo */}
        <span className="text-base font-semibold tracking-tight text-foreground">
          CodeSearch
        </span>

        {/* Divider */}
        <div className="h-5 w-px bg-border" />

        {/* Organization selector */}
        {organizations && organizations.length > 0 && onSelectOrg && (
          <Select value={selectedOrgId} onValueChange={onSelectOrg}>
            <SelectTrigger className="h-8 w-[200px] border-border bg-background text-sm">
              <div className="flex items-center gap-2">
                <Building2 className="h-3.5 w-3.5 text-muted-foreground" />
                <SelectValue placeholder="Select organization" />
              </div>
            </SelectTrigger>
            <SelectContent>
              {organizations.map((org) => (
                <SelectItem key={org.id} value={org.id}>
                  <span className="flex items-center gap-2">
                    <span>{org.name}</span>
                    <span className="text-[10px] text-muted-foreground">
                      {org.repoCount} repos
                    </span>
                  </span>
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        )}

        {/* Search Input */}
        <div className="relative max-w-xl flex-1">
          <Search className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
          <Input
            ref={inputRef}
            type="text"
            placeholder="Search functions, classes, APIs, concepts..."
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            className="h-9 w-full border-border bg-background pl-10 pr-16 text-sm text-foreground placeholder:text-muted-foreground focus-visible:ring-1 focus-visible:ring-ring"
          />
          <kbd className="pointer-events-none absolute right-3 top-1/2 flex -translate-y-1/2 select-none items-center gap-0.5 rounded border border-border px-1.5 py-0.5 font-mono text-[10px] text-muted-foreground">
            <span className="text-[10px]">⌘K</span>
          </kbd>
        </div>

        {/* Filter tabs */}
        <div className="flex items-center gap-1 rounded-md bg-muted p-0.5">
          {fileTypes.map((type) => (
            <button
              key={type.value}
              onClick={() => setFileTypeFilter(type.value)}
              className={cn(
                'rounded px-3 py-1 text-xs font-medium transition-colors',
                fileTypeFilter === type.value
                  ? 'bg-card text-foreground shadow-sm'
                  : 'text-muted-foreground hover:text-foreground'
              )}
            >
              {type.label}
            </button>
          ))}
        </div>

        {/* Result count */}
        {resultCount !== undefined && (
          <span className="text-xs text-muted-foreground">
            {resultCount} result{resultCount !== 1 ? 's' : ''}
          </span>
        )}

        {/* Spacer + actions on far right */}
        <div className="ml-auto flex items-center gap-1">
          <Button
            variant="ghost"
            size="sm"
            onClick={onToggleChat}
            className={cn(
              'gap-2 text-xs',
              isChatOpen
                ? 'bg-muted text-foreground'
                : 'text-muted-foreground hover:text-foreground'
            )}
          >
            <MessageSquare className="h-4 w-4" />
            Chat
          </Button>
        </div>
      </div>
    </header>
  );
}
