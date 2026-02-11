import { useState } from 'react';
import { FileCode, ChevronDown, ChevronUp } from 'lucide-react';
import { Card } from '@/components/ui/card';
import { CodeBlock } from '@/components/CodeBlock';
import { cn } from '@/lib/utils';
import type { ChunkResult } from '@/lib/api/types';

interface CodeCardProps {
  result: ChunkResult;
  highlightQuery?: string;
  defaultExpanded?: boolean;
}

const PREVIEW_LINES = 5;

export function CodeCard({
  result,
  highlightQuery,
  defaultExpanded = false,
}: CodeCardProps) {
  const [isExpanded, setIsExpanded] = useState(defaultExpanded);

  const lineCount = result.content.split('\n').length;
  const hasMoreLines = lineCount > PREVIEW_LINES;
  const displayContent = isExpanded
    ? result.content
    : result.content.split('\n').slice(0, PREVIEW_LINES).join('\n');

  // Format score as percentage
  const scorePercent = Math.round((result.rrf_score ?? result.score) * 100);

  // Extract filename from path
  const fileName = result.file_path.split('/').pop() || result.file_path;

  return (
    <Card
      className={cn(
        'overflow-hidden transition-all duration-200',
        'hover:border-[var(--color-accent)]/50',
        isExpanded && 'ring-1 ring-[var(--color-ring)]/20'
      )}
    >
      {/* Header */}
      <button
        onClick={() => setIsExpanded(!isExpanded)}
        className="w-full flex items-center justify-between gap-3 px-4 py-3 bg-[var(--color-secondary)]/30 border-b border-[var(--color-border)] hover:bg-[var(--color-secondary)]/50 transition-colors duration-150"
      >
        <div className="flex items-center gap-3 min-w-0">
          <FileCode className="h-4 w-4 shrink-0 text-[var(--color-muted-foreground)]" />
          <div className="flex flex-col items-start gap-0.5 min-w-0">
            <span className="font-mono text-sm font-medium text-[var(--color-foreground)] truncate">
              {fileName}
            </span>
            <span className="text-xs text-[var(--color-muted-foreground)] truncate">
              {result.breadcrumb || result.file_path}
            </span>
          </div>
        </div>

        <div className="flex items-center gap-3 shrink-0">
          {/* Line range */}
          <span className="hidden sm:block text-xs text-[var(--color-muted-foreground)]">
            Lines {result.start_line}–{result.end_line}
          </span>

          {/* Score badge */}
          <span className="text-xs px-2 py-0.5 rounded-full bg-[var(--color-accent)] text-[var(--color-accent-foreground)]">
            {scorePercent}%
          </span>

          {/* Expand indicator */}
          {hasMoreLines && (
            isExpanded ? (
              <ChevronUp className="h-4 w-4 text-[var(--color-muted-foreground)]" />
            ) : (
              <ChevronDown className="h-4 w-4 text-[var(--color-muted-foreground)]" />
            )
          )}
        </div>
      </button>

      {/* Code content */}
      <div className="py-2">
        <CodeBlock
          code={displayContent}
          startLine={result.start_line}
          highlightQuery={highlightQuery}
        />
      </div>

      {/* Expand hint for collapsed cards */}
      {!isExpanded && hasMoreLines && (
        <div className="px-4 py-2 border-t border-[var(--color-border)] bg-[var(--color-secondary)]/20">
          <span className="text-xs text-[var(--color-muted-foreground)]">
            +{lineCount - PREVIEW_LINES} more lines • Click to expand
          </span>
        </div>
      )}
    </Card>
  );
}
