import { useState } from 'react';
import { ChevronDown, ChevronUp, FileCode } from 'lucide-react';
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from '@/components/ui/collapsible';
import type { SourceInfo } from '@/lib/api/types';

interface SourcesListProps {
  sources: SourceInfo[];
  onSourceClick?: (source: SourceInfo) => void;
}

export function SourcesList({ sources, onSourceClick }: SourcesListProps) {
  const [isOpen, setIsOpen] = useState(false);

  if (sources.length === 0) return null;

  return (
    <Collapsible open={isOpen} onOpenChange={setIsOpen} className="mt-3">
      <CollapsibleTrigger className="flex items-center gap-2 text-sm text-[var(--color-muted-foreground)] hover:text-[var(--color-foreground)] transition-colors duration-150">
        {isOpen ? (
          <ChevronUp className="h-4 w-4" />
        ) : (
          <ChevronDown className="h-4 w-4" />
        )}
        <span>
          {sources.length} source{sources.length !== 1 ? 's' : ''}
        </span>
      </CollapsibleTrigger>
      <CollapsibleContent className="mt-2 space-y-1">
        {sources.map((source) => (
          <button
            key={source.number}
            onClick={() => onSourceClick?.(source)}
            className="flex items-center gap-2 w-full p-2 rounded-md text-left text-sm hover:bg-[var(--color-accent)] transition-colors duration-150"
          >
            <span className="flex items-center justify-center w-5 h-5 rounded-full bg-[var(--color-secondary)] text-xs font-medium text-[var(--color-secondary-foreground)]">
              {source.number}
            </span>
            <FileCode className="h-4 w-4 text-[var(--color-muted-foreground)]" />
            <span className="flex-1 truncate text-[var(--color-foreground)]">
              {source.file_path}
            </span>
            <span className="text-xs text-[var(--color-muted-foreground)]">
              L{source.start_line}–{source.end_line}
            </span>
          </button>
        ))}
      </CollapsibleContent>
    </Collapsible>
  );
}
