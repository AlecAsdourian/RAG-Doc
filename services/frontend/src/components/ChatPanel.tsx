import { useState, useRef, useEffect } from 'react';
import { Send, Trash2 } from 'lucide-react';
import { ScrollArea } from '@/components/ui/scroll-area';
import { Input } from '@/components/ui/input';
import { Button } from '@/components/ui/button';
import { ChatMessage } from '@/components/ChatMessage';
import { useChat } from '@/hooks/useChat';
import type { SourceInfo } from '@/lib/api/types';

interface ChatPanelProps {
  repositoryId: string;
  onSourceClick?: (source: SourceInfo) => void;
}

export function ChatPanel({ repositoryId, onSourceClick }: ChatPanelProps) {
  const [input, setInput] = useState('');
  const scrollRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  const { messages, sendMessage, isStreaming, clearMessages } = useChat({
    repositoryId,
  });

  // Auto-scroll to bottom when messages change
  useEffect(() => {
    if (scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }
  }, [messages]);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!input.trim() || isStreaming) return;

    const query = input;
    setInput('');
    await sendMessage(query);
    inputRef.current?.focus();
  };

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      handleSubmit(e);
    }
  };

  return (
    <div className="flex flex-col h-full">
      {/* Messages area */}
      <ScrollArea
        ref={scrollRef}
        className="flex-1 px-4"
      >
        {messages.length === 0 ? (
          <div className="flex flex-col items-center justify-center h-full py-16 text-center">
            <div className="text-4xl mb-4">💬</div>
            <h3 className="text-lg font-medium text-[var(--color-foreground)] mb-1">
              Ask a question
            </h3>
            <p className="text-sm text-[var(--color-muted-foreground)] max-w-md">
              Ask questions about the codebase and get answers with source citations.
            </p>
          </div>
        ) : (
          <div className="py-4">
            {messages.map((message) => (
              <ChatMessage
                key={message.id}
                role={message.role}
                content={message.content}
                sources={message.sources}
                isStreaming={message.isStreaming}
                onSourceClick={onSourceClick}
              />
            ))}
          </div>
        )}
      </ScrollArea>

      {/* Input area */}
      <div className="border-t border-[var(--color-border)] p-4">
        <form onSubmit={handleSubmit} className="flex gap-2">
          <Input
            ref={inputRef}
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={handleKeyDown}
            placeholder="Ask about the codebase..."
            disabled={isStreaming}
            className="flex-1 bg-[var(--color-secondary)] border-[var(--color-border)]"
          />
          <Button
            type="submit"
            disabled={!input.trim() || isStreaming}
            size="icon"
            aria-label="Send message"
          >
            <Send className="h-4 w-4" />
          </Button>
          {messages.length > 0 && (
            <Button
              type="button"
              variant="ghost"
              size="icon"
              onClick={clearMessages}
              disabled={isStreaming}
              aria-label="Clear chat"
            >
              <Trash2 className="h-4 w-4" />
            </Button>
          )}
        </form>
      </div>
    </div>
  );
}
