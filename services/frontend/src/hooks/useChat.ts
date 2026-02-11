import { useState, useCallback } from 'react';
import { apiClient } from '@/lib/api/client';
import type { SourceInfo } from '@/lib/api/types';

export interface Message {
  id: string;
  role: 'user' | 'assistant';
  content: string;
  sources?: SourceInfo[];
  isStreaming?: boolean;
}

interface UseChatOptions {
  repositoryId: string;
}

interface UseChatResult {
  messages: Message[];
  sendMessage: (query: string) => Promise<void>;
  isStreaming: boolean;
  error: string | null;
  clearMessages: () => void;
}

export function useChat({ repositoryId }: UseChatOptions): UseChatResult {
  const [messages, setMessages] = useState<Message[]>([]);
  const [isStreaming, setIsStreaming] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const clearMessages = useCallback(() => {
    setMessages([]);
    setError(null);
  }, []);

  const sendMessage = useCallback(
    async (query: string) => {
      if (!query.trim() || isStreaming) return;

      setError(null);

      // Add user message
      const userMsg: Message = {
        id: crypto.randomUUID(),
        role: 'user',
        content: query.trim(),
      };
      setMessages((prev) => [...prev, userMsg]);

      // Add empty assistant message for streaming
      const assistantId = crypto.randomUUID();
      setMessages((prev) => [
        ...prev,
        { id: assistantId, role: 'assistant', content: '', isStreaming: true },
      ]);
      setIsStreaming(true);

      try {
        for await (const chunk of apiClient.streamChat({
          query: query.trim(),
          repository_id: repositoryId,
        })) {
          if (chunk.type === 'chunk' && chunk.content) {
            setMessages((prev) =>
              prev.map((m) =>
                m.id === assistantId
                  ? { ...m, content: m.content + chunk.content }
                  : m
              )
            );
          } else if (chunk.type === 'done') {
            setMessages((prev) =>
              prev.map((m) =>
                m.id === assistantId
                  ? { ...m, sources: chunk.sources, isStreaming: false }
                  : m
              )
            );
          } else if (chunk.type === 'error') {
            setMessages((prev) =>
              prev.map((m) =>
                m.id === assistantId
                  ? {
                      ...m,
                      content: `Error: ${chunk.error || 'Unknown error'}`,
                      isStreaming: false,
                    }
                  : m
              )
            );
            setError(chunk.error || 'Unknown error');
          }
        }
      } catch (e) {
        const errorMessage = e instanceof Error ? e.message : 'Chat failed';
        setMessages((prev) =>
          prev.map((m) =>
            m.id === assistantId
              ? { ...m, content: `Error: ${errorMessage}`, isStreaming: false }
              : m
          )
        );
        setError(errorMessage);
      } finally {
        setIsStreaming(false);
      }
    },
    [repositoryId, isStreaming]
  );

  return {
    messages,
    sendMessage,
    isStreaming,
    error,
    clearMessages,
  };
}
