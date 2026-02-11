import type {
  SearchRequest,
  SearchResponse,
  ChatRequest,
  ChatChunk,
} from './types';

const API_BASE = import.meta.env.VITE_API_URL || 'http://localhost:8080';

export class ApiClient {
  private token: string | null = null;

  setToken(token: string) {
    this.token = token;
  }

  clearToken() {
    this.token = null;
  }

  private headers(): HeadersInit {
    const h: HeadersInit = { 'Content-Type': 'application/json' };
    if (this.token) {
      h['Authorization'] = `Bearer ${this.token}`;
    }
    return h;
  }

  async search(req: SearchRequest): Promise<SearchResponse> {
    const res = await fetch(`${API_BASE}/api/search`, {
      method: 'POST',
      headers: this.headers(),
      body: JSON.stringify(req),
    });

    if (!res.ok) {
      throw new Error(`Search failed: ${res.status} ${res.statusText}`);
    }

    return res.json();
  }

  async *streamChat(req: ChatRequest): AsyncGenerator<ChatChunk> {
    const res = await fetch(`${API_BASE}/api/chat/stream`, {
      method: 'POST',
      headers: this.headers(),
      body: JSON.stringify(req),
    });

    if (!res.ok) {
      throw new Error(`Chat failed: ${res.status} ${res.statusText}`);
    }

    const reader = res.body?.getReader();
    if (!reader) {
      throw new Error('No response body');
    }

    const decoder = new TextDecoder();
    let buffer = '';

    try {
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;

        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split('\n\n');
        buffer = lines.pop() || '';

        for (const line of lines) {
          if (line.startsWith('data: ')) {
            const json = line.slice(6);
            if (json.trim()) {
              yield JSON.parse(json) as ChatChunk;
            }
          }
        }
      }

      // Process any remaining buffer
      if (buffer.startsWith('data: ')) {
        const json = buffer.slice(6);
        if (json.trim()) {
          yield JSON.parse(json) as ChatChunk;
        }
      }
    } finally {
      reader.releaseLock();
    }
  }
}

export const apiClient = new ApiClient();
