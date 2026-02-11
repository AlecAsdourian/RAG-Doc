import { useMemo } from 'react';
import { cn } from '@/lib/utils';

interface CodeBlockProps {
  code: string;
  startLine?: number;
  maxLines?: number;
  highlightQuery?: string;
  className?: string;
}

export function CodeBlock({
  code,
  startLine = 1,
  maxLines,
  highlightQuery,
  className,
}: CodeBlockProps) {
  const lines = useMemo(() => {
    const allLines = code.split('\n');
    return maxLines ? allLines.slice(0, maxLines) : allLines;
  }, [code, maxLines]);

  const highlightText = (text: string): React.ReactNode => {
    if (!highlightQuery || !highlightQuery.trim()) return text;

    const regex = new RegExp(`(${escapeRegex(highlightQuery)})`, 'gi');
    const parts = text.split(regex);

    return parts.map((part, index) => {
      if (part.toLowerCase() === highlightQuery.toLowerCase()) {
        return (
          <mark
            key={index}
            className="rounded bg-yellow-500/20 px-0.5 text-yellow-600 dark:text-yellow-400"
          >
            {part}
          </mark>
        );
      }
      return part;
    });
  };

  return (
    <div className={cn('overflow-x-auto font-mono text-sm', className)}>
      {lines.map((line, index) => {
        const lineNumber = startLine + index;
        return (
          <div key={index} className="flex min-h-6 hover:bg-[var(--color-accent)]/5">
            <span className="w-12 shrink-0 select-none pr-4 text-right text-xs text-[var(--color-muted-foreground)]">
              {lineNumber}
            </span>
            <code className="flex-1 whitespace-pre px-2 text-[var(--color-foreground)]">
              {highlightText(line) || ' '}
            </code>
          </div>
        );
      })}
    </div>
  );
}

function escapeRegex(string: string): string {
  return string.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}
