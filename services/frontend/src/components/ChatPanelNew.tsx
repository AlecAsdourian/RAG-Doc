import { useState, useRef, useEffect, useCallback } from 'react';
import { X, ArrowUp, MessageSquare, Loader2 } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { cn } from '@/lib/utils';
import { useChat, type Message } from '@/hooks/useChat';

interface ChatPanelNewProps {
  isOpen: boolean;
  onClose: () => void;
  repositoryId: string;
}

export function ChatPanelNew({ isOpen, onClose, repositoryId }: ChatPanelNewProps) {
  const { messages, sendMessage, isStreaming, clearMessages } = useChat({ repositoryId });
  const [inputValue, setInputValue] = useState('');
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const scrollRef = useRef<HTMLDivElement>(null);

  const scrollToBottom = useCallback(() => {
    if (scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }
  }, []);

  useEffect(() => {
    scrollToBottom();
  }, [messages, scrollToBottom]);

  useEffect(() => {
    if (isOpen) {
      setTimeout(() => textareaRef.current?.focus(), 200);
    }
  }, [isOpen]);

  const handleSend = async () => {
    const text = inputValue.trim();
    if (!text || isStreaming) return;

    setInputValue('');
    await sendMessage(text);
  };

  const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      handleSend();
    }
  };

  if (!isOpen) return null;

  return (
    <aside className="flex h-full w-96 min-h-0 min-w-0 shrink-0 flex-col overflow-hidden border-l border-border bg-card">
      {/* Header */}
      <div className="flex items-center gap-2 border-b border-border px-4 py-3">
        <MessageSquare className="h-4 w-4 text-muted-foreground" />
        <span className="flex-1 text-sm font-medium text-foreground">Chat</span>
        {messages.length > 0 && (
          <Button
            variant="ghost"
            size="sm"
            onClick={clearMessages}
            className="h-7 px-2 text-xs text-muted-foreground hover:text-foreground"
          >
            Clear
          </Button>
        )}
        <Button
          variant="ghost"
          size="icon"
          onClick={onClose}
          className="h-8 w-8 shrink-0 text-muted-foreground hover:text-foreground"
        >
          <X className="h-4 w-4" />
          <span className="sr-only">Close chat</span>
        </Button>
      </div>

      {/* Messages area */}
      <div ref={scrollRef} className="min-h-0 flex-1 overflow-y-auto overflow-x-hidden px-4">
        {messages.length === 0 ? (
          <div className="flex h-full flex-col items-center justify-center py-20 text-center">
            <MessageSquare className="mb-3 h-8 w-8 text-muted-foreground/40" />
            <p className="text-sm font-medium text-foreground">
              Start a conversation
            </p>
            <p className="mt-1 max-w-[240px] text-xs leading-relaxed text-muted-foreground">
              Ask questions about your indexed codebase and get answers grounded
              in your repositories.
            </p>
          </div>
        ) : (
          <div className="flex min-w-0 flex-col gap-4 py-4">
            {messages.map((message) => (
              <ChatMessageBubble key={message.id} message={message} />
            ))}
          </div>
        )}
      </div>

      {/* Input area */}
      <div className="border-t border-border px-4 py-3">
        <div className="flex items-end gap-2 rounded-lg border border-border bg-background px-3 py-2 focus-within:ring-1 focus-within:ring-ring">
          <textarea
            ref={textareaRef}
            value={inputValue}
            onChange={(e) => setInputValue(e.target.value)}
            onKeyDown={handleKeyDown}
            placeholder="Ask about your codebase..."
            rows={1}
            className="max-h-32 min-h-[20px] flex-1 resize-none bg-transparent text-sm text-foreground placeholder:text-muted-foreground focus:outline-none"
            style={{ fieldSizing: 'content' } as React.CSSProperties}
          />
          <Button
            size="icon"
            onClick={handleSend}
            disabled={!inputValue.trim() || isStreaming}
            className="h-7 w-7 shrink-0 rounded-md"
          >
            {isStreaming ? (
              <Loader2 className="h-4 w-4 animate-spin" />
            ) : (
              <ArrowUp className="h-4 w-4" />
            )}
            <span className="sr-only">Send message</span>
          </Button>
        </div>
        <p className="mt-2 text-center text-[10px] text-muted-foreground">
          Responses are grounded in your indexed repositories.
        </p>
      </div>
    </aside>
  );
}

function ChatMessageBubble({ message }: { message: Message }) {
  return (
    <div className="flex min-w-0 flex-col gap-1">
      <div
        className={cn(
          'max-w-[85%] overflow-hidden break-words rounded-lg px-3 py-2 text-sm leading-relaxed',
          message.role === 'user'
            ? 'self-end bg-primary text-primary-foreground'
            : 'self-start bg-muted text-foreground'
        )}
      >
        {message.content.split('\n').map((line, i, arr) => {
          // Handle code blocks
          if (line.startsWith('```')) {
            return null;
          }
          const isInsideCode =
            message.content.includes('```') &&
            (() => {
              const before = message.content
                .split('\n')
                .slice(0, i)
                .join('\n');
              const opens = (before.match(/```/g) || []).length;
              return opens % 2 === 1;
            })();

          if (isInsideCode) {
            return (
              <code
                key={i}
                className="block overflow-x-auto whitespace-pre font-mono text-xs leading-5 text-foreground"
              >
                {line}
              </code>
            );
          }

          return (
            <span key={i}>
              {line}
              {i < arr.length - 1 && !isInsideCode && <br />}
            </span>
          );
        })}
        {message.isStreaming && (
          <span className="inline-block h-4 w-1.5 animate-pulse bg-current align-text-bottom" />
        )}
      </div>
      {/* Sources */}
      {message.sources && message.sources.length > 0 && (
        <div className="self-start ml-0 mt-1 flex flex-wrap gap-1">
          {message.sources.map((source, idx) => (
            <span
              key={idx}
              className="rounded bg-muted px-1.5 py-0.5 font-mono text-[10px] text-muted-foreground"
            >
              {source.file_path.split('/').pop()}:{source.start_line}
            </span>
          ))}
        </div>
      )}
    </div>
  );
}
