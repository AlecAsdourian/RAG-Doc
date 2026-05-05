import { useCallback, useMemo } from 'react';
import { cn } from '@/lib/utils';
import type { JSX } from 'react';

interface CodeBlockV2Props {
  code: string;
  language: string;
  highlightedLines?: number[];
  query?: string;
}

export function CodeBlockV2({
  code,
  language,
  highlightedLines = [],
  query,
}: CodeBlockV2Props) {
  const lines = code.split('\n');

  const highlightQuery = useCallback(
    (text: string) => {
      if (!query) return text;

      const regex = new RegExp(`(${escapeRegex(query)})`, 'gi');
      const parts = text.split(regex);

      return parts.map((part, index) => {
        if (part.toLowerCase() === query.toLowerCase()) {
          return (
            <mark
              key={index}
              className="rounded-sm bg-accent/20 px-0.5 text-accent"
            >
              {part}
            </mark>
          );
        }
        return part;
      });
    },
    [query]
  );

  const tokenize = useCallback(
    (line: string) => {
      const patterns: { regex: RegExp; className: string }[] = [
        {
          regex: /("(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*'|`(?:[^`\\]|\\.)*`)/g,
          className: 'text-[oklch(0.55_0.12_155)]',
        },
        {
          regex: /(\/\/.*$|\/\*[\s\S]*?\*\/)/gm,
          className: 'text-muted-foreground italic',
        },
        {
          regex:
            /\b(const|let|var|function|return|if|else|for|while|import|export|from|default|async|await|class|extends|new|this|try|catch|throw|typeof|instanceof|in|of)\b/g,
          className: 'text-[oklch(0.55_0.1_250)]',
        },
        {
          regex:
            /\b(true|false|null|undefined|void|never|any|string|number|boolean|object|unknown)\b/g,
          className: 'text-[oklch(0.55_0.12_40)]',
        },
        {
          regex: /\b([a-zA-Z_$][a-zA-Z0-9_$]*)\s*(?=\()/g,
          className: 'text-[oklch(0.45_0.08_260)]',
        },
        {
          regex: /\b(\d+\.?\d*)\b/g,
          className: 'text-[oklch(0.55_0.12_40)]',
        },
        {
          regex: /:\s*([A-Z][a-zA-Z0-9_$]*(?:<[^>]+>)?)/g,
          className: 'text-[oklch(0.5_0.08_200)]',
        },
        {
          regex: /(<\/?[A-Z][a-zA-Z0-9]*)/g,
          className: 'text-[oklch(0.5_0.08_200)]',
        },
        {
          regex: /^(#{1,6}\s.*)$/gm,
          className: 'text-foreground font-semibold',
        },
      ];

      if (language === 'markdown') {
        if (line.startsWith('```')) {
          return <span className="text-muted-foreground">{line}</span>;
        }
        if (line.startsWith('#')) {
          return (
            <span className="font-semibold text-foreground">
              {highlightQuery(line)}
            </span>
          );
        }
      }

      let result: (string | JSX.Element)[] = [line];

      for (const { regex, className } of patterns) {
        result = result.flatMap((segment, segmentIndex) => {
          if (typeof segment !== 'string') return segment;

          const parts: (string | JSX.Element)[] = [];
          let lastIndex = 0;

          const localRegex = new RegExp(regex.source, regex.flags);
          let match: RegExpExecArray | null = localRegex.exec(segment);
          while (match !== null) {
            if (match.index > lastIndex) {
              parts.push(segment.slice(lastIndex, match.index));
            }
            parts.push(
              <span
                key={`${segmentIndex}-${match.index}`}
                className={className}
              >
                {match[1] || match[0]}
              </span>
            );
            lastIndex = match.index + match[0].length;
            match = localRegex.exec(segment);
          }

          if (lastIndex < segment.length) {
            parts.push(segment.slice(lastIndex));
          }

          return parts.length > 0 ? parts : [segment];
        });
      }

      if (query) {
        result = result.map((segment, idx) => {
          if (typeof segment === 'string') {
            return <span key={`text-${idx}`}>{highlightQuery(segment)}</span>;
          }
          return segment;
        });
      }

      return result;
    },
    [language, query, highlightQuery]
  );

  const renderedLines = useMemo(() => {
    return lines.map((line, index) => {
      const lineNumber = index + 1;
      const isHighlighted = highlightedLines.includes(lineNumber);

      return (
        <div
          key={index}
          className={cn(
            'flex min-h-6 items-center',
            isHighlighted && 'bg-accent/8 border-l-2 border-accent'
          )}
        >
          <span className="w-12 shrink-0 select-none pr-4 text-right font-mono text-xs leading-6 text-muted-foreground/60">
            {lineNumber}
          </span>
          <code className="flex-1 whitespace-pre px-4 font-mono text-[13px] leading-6 text-foreground">
            {tokenize(line)}
          </code>
        </div>
      );
    });
  }, [lines, highlightedLines, tokenize]);

  return <div className="py-3">{renderedLines}</div>;
}

function escapeRegex(string: string) {
  return string.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}
