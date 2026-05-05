import { useState } from 'react';
import { CodeBlockV2 } from '@/components/CodeBlockV2';
import type { SearchResult } from '@/types';
import { ChevronDown, Copy, Check, FileCode2, FileText } from 'lucide-react';
import { cn } from '@/lib/utils';

interface SearchResultDetailProps {
  result: SearchResult;
  query: string;
}

export function SearchResultDetail({ result, query }: SearchResultDetailProps) {
  const [copied, setCopied] = useState(false);
  const [showMeta, setShowMeta] = useState(false);
  const isCode = result.type === 'code';
  const Icon = isCode ? FileCode2 : FileText;

  const handleCopy = () => {
    navigator.clipboard.writeText(result.content);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };

  return (
    <div className="mx-auto max-w-4xl px-8 py-8">
      {/* Title & path */}
      <div className="mb-6">
        <div className="flex items-center gap-2.5">
          <Icon className="h-5 w-5 text-muted-foreground" />
          <h1 className="font-mono text-lg font-semibold text-foreground">
            {result.fileName}
          </h1>
          <span className="rounded bg-muted px-2 py-0.5 text-[11px] font-medium text-muted-foreground">
            {result.language}
          </span>
        </div>
        <p className="mt-1 pl-[30px] font-mono text-sm text-muted-foreground">
          {result.filePath}
        </p>
      </div>

      {/* Summary */}
      <p className="mb-6 text-sm leading-relaxed text-foreground/80">
        {result.summary}
      </p>

      {/* Chunk card */}
      <div className="overflow-hidden rounded-lg border border-border bg-card shadow-sm">
        {/* Card header */}
        <div className="flex items-center justify-between border-b border-border px-4 py-2.5">
          <div className="flex items-center gap-3">
            <span className="text-xs text-muted-foreground">
              {result.repoName}
            </span>
            <span className="text-border">{'/'}</span>
            <span className="font-mono text-xs text-muted-foreground">
              chunk {result.chunkIndex}{result.totalChunks > 0 ? ` of ${result.totalChunks}` : ''}
            </span>
          </div>
          <div className="flex items-center gap-1">
            <button
              onClick={handleCopy}
              className="flex items-center gap-1.5 rounded-md px-2.5 py-1 text-xs text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
              aria-label="Copy code to clipboard"
            >
              {copied ? (
                <>
                  <Check className="h-3.5 w-3.5" />
                  <span>Copied</span>
                </>
              ) : (
                <>
                  <Copy className="h-3.5 w-3.5" />
                  <span>Copy</span>
                </>
              )}
            </button>
          </div>
        </div>

        {/* Code content */}
        <div className="overflow-x-auto">
          <CodeBlockV2
            code={result.content}
            language={result.language}
            highlightedLines={result.matchedLines}
            query={query}
          />
        </div>

        {/* Progressive disclosure metadata */}
        <div className="border-t border-border">
          <button
            onClick={() => setShowMeta(!showMeta)}
            className="flex w-full items-center justify-between px-4 py-2 text-xs text-muted-foreground transition-colors hover:bg-muted/50"
          >
            <span>Retrieval metadata</span>
            <ChevronDown
              className={cn(
                'h-3.5 w-3.5 transition-transform',
                showMeta && 'rotate-180'
              )}
            />
          </button>
          {showMeta && (
            <div className="border-t border-border bg-muted/30 px-4 py-3">
              <dl className="grid grid-cols-2 gap-x-8 gap-y-2 text-xs">
                <div>
                  <dt className="text-muted-foreground">Relevance score</dt>
                  <dd className="mt-0.5 font-mono text-foreground">
                    {result.relevanceScore.toFixed(3)}
                  </dd>
                </div>
                <div>
                  <dt className="text-muted-foreground">Source path</dt>
                  <dd className="mt-0.5 font-mono text-foreground">
                    {result.filePath}
                  </dd>
                </div>
                <div>
                  <dt className="text-muted-foreground">Repository</dt>
                  <dd className="mt-0.5 font-mono text-foreground">
                    {result.repoName}
                  </dd>
                </div>
                <div>
                  <dt className="text-muted-foreground">Chunk</dt>
                  <dd className="mt-0.5 font-mono text-foreground">
                    {result.chunkIndex}{result.totalChunks > 0 ? ` / ${result.totalChunks}` : ''}
                  </dd>
                </div>
                <div>
                  <dt className="text-muted-foreground">Matched lines</dt>
                  <dd className="mt-0.5 font-mono text-foreground">
                    {result.matchedLines.join(', ') || 'N/A'}
                  </dd>
                </div>
                <div>
                  <dt className="text-muted-foreground">File type</dt>
                  <dd className="mt-0.5 font-mono capitalize text-foreground">
                    {result.type}
                  </dd>
                </div>
              </dl>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
