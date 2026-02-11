import { useMemo } from 'react';
import { User, Bot, Loader2 } from 'lucide-react';
import { SourcesList } from '@/components/SourcesList';
import { cn } from '@/lib/utils';
import type { SourceInfo } from '@/lib/api/types';

interface ChatMessageProps {
  role: 'user' | 'assistant';
  content: string;
  sources?: SourceInfo[];
  isStreaming?: boolean;
  onSourceClick?: (source: SourceInfo) => void;
}

export function ChatMessage({
  role,
  content,
  sources,
  isStreaming,
  onSourceClick,
}: ChatMessageProps) {
  const isUser = role === 'user';

  // Parse citations [1], [2] into clickable elements
  const parsedContent = useMemo(() => {
    if (isUser || !sources || sources.length === 0) {
      return <span>{content}</span>;
    }

    const parts: React.ReactNode[] = [];
    const regex = /\[(\d+)\]/g;
    let lastIndex = 0;
    let match;

    while ((match = regex.exec(content)) !== null) {
      // Add text before the citation
      if (match.index > lastIndex) {
        parts.push(
          <span key={`text-${lastIndex}`}>
            {content.slice(lastIndex, match.index)}
          </span>
        );
      }

      const num = parseInt(match[1], 10);
      const source = sources.find((s) => s.number === num);

      if (source) {
        parts.push(
          <button
            key={`cite-${match.index}`}
            onClick={() => onSourceClick?.(source)}
            className="inline-flex items-center justify-center w-5 h-5 mx-0.5 rounded-full bg-[var(--color-primary)] text-[var(--color-primary-foreground)] text-xs font-medium hover:opacity-80 transition-opacity"
            title={`${source.file_path}:${source.start_line}`}
          >
            {num}
          </button>
        );
      } else {
        parts.push(<span key={`cite-${match.index}`}>{match[0]}</span>);
      }

      lastIndex = match.index + match[0].length;
    }

    // Add remaining text
    if (lastIndex < content.length) {
      parts.push(
        <span key={`text-${lastIndex}`}>{content.slice(lastIndex)}</span>
      );
    }

    return <>{parts}</>;
  }, [content, sources, isUser, onSourceClick]);

  return (
    <div
      className={cn(
        'flex gap-3 py-4',
        isUser ? 'flex-row-reverse' : 'flex-row'
      )}
    >
      {/* Avatar */}
      <div
        className={cn(
          'flex items-center justify-center w-8 h-8 rounded-full shrink-0',
          isUser
            ? 'bg-[var(--color-primary)]'
            : 'bg-[var(--color-secondary)]'
        )}
      >
        {isUser ? (
          <User className="h-4 w-4 text-[var(--color-primary-foreground)]" />
        ) : (
          <Bot className="h-4 w-4 text-[var(--color-secondary-foreground)]" />
        )}
      </div>

      {/* Message content */}
      <div
        className={cn(
          'flex-1 max-w-[80%]',
          isUser ? 'text-right' : 'text-left'
        )}
      >
        <div
          className={cn(
            'inline-block px-4 py-2 rounded-2xl text-sm',
            isUser
              ? 'bg-[var(--color-primary)] text-[var(--color-primary-foreground)] rounded-tr-sm'
              : 'bg-[var(--color-secondary)] text-[var(--color-secondary-foreground)] rounded-tl-sm'
          )}
        >
          {content ? (
            <div className="whitespace-pre-wrap">{parsedContent}</div>
          ) : isStreaming ? (
            <div className="flex items-center gap-2 text-[var(--color-muted-foreground)]">
              <Loader2 className="h-4 w-4 animate-spin" />
              <span>Thinking...</span>
            </div>
          ) : null}

          {/* Streaming cursor */}
          {isStreaming && content && (
            <span className="inline-block w-2 h-4 ml-1 bg-current animate-pulse" />
          )}
        </div>

        {/* Sources (only for assistant messages after streaming is done) */}
        {!isUser && !isStreaming && sources && sources.length > 0 && (
          <SourcesList sources={sources} onSourceClick={onSourceClick} />
        )}
      </div>
    </div>
  );
}
