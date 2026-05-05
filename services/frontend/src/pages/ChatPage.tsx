import { useState, useRef, useEffect, useCallback } from 'react';
import { ArrowUp, Loader2 } from 'lucide-react';
import { ChatMessageBubble } from '@/components/ChatMessageBubble';
import { useChat } from '@/hooks/useChat';
import { useRepositories } from '@/hooks/useRepositories';

const RECENT_SESSIONS = [
  { id: 's1', group: 'Today',     title: 'RAG pipeline architecture',     time: '2h ago'    },
  { id: 's2', group: 'Today',     title: 'Auth middleware deep dive',      time: '4h ago'    },
  { id: 's3', group: 'Yesterday', title: 'Vector store optimization',      time: 'Yesterday' },
  { id: 's4', group: 'Yesterday', title: 'API gateway rate limiting',      time: 'Yesterday' },
];

const SUGGESTED_QUERIES = [
  'How does the indexer handle large files?',
  'What services depend on auth-service?',
  'Explain the chunking strategy in parser.py',
  'Show me the API endpoints for the search module',
];

export function ChatPage() {
  const { selectedRepo } = useRepositories();
  const repositoryId = selectedRepo?.id || 'demo-repo';
  const { messages, sendMessage, isStreaming, clearMessages } = useChat({ repositoryId });
  const [inputValue, setInputValue] = useState('');
  const [activeSession, setActiveSession] = useState('s1');
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const scrollRef = useRef<HTMLDivElement>(null);

  const scrollToBottom = useCallback(() => {
    if (scrollRef.current) scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
  }, []);

  useEffect(() => { scrollToBottom(); }, [messages, scrollToBottom]);
  useEffect(() => { textareaRef.current?.focus(); }, []);

  const handleSend = async (text?: string) => {
    const content = (text ?? inputValue).trim();
    if (!content || isStreaming) return;
    setInputValue('');
    await sendMessage(content);
  };

  const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); handleSend(); }
  };

  const groups = Array.from(new Set(RECENT_SESSIONS.map(s => s.group)));

  return (
    <div className="flex flex-1 min-h-0 overflow-hidden" style={{ background: '#0B0E14', fontFamily: 'Inter, sans-serif' }}>
      {/* Recent Sessions sidebar */}
      <aside
        className="hidden md:flex flex-col shrink-0 overflow-y-auto"
        style={{ width: 260, background: '#151921', borderRight: '1px solid rgba(255,255,255,0.06)' }}
      >
        {/* Header */}
        <div
          className="p-4"
          style={{ borderBottom: '1px solid rgba(255,255,255,0.06)', background: 'rgba(255,255,255,0.02)' }}
        >
          <div className="flex items-center justify-between mb-3">
            <span
              className="uppercase"
              style={{ fontSize: 11, fontWeight: 700, letterSpacing: '0.08em', color: '#908fa0', fontFamily: 'Space Grotesk, sans-serif' }}
            >
              Recent Sessions
            </span>
          </div>
          <button
            className="w-full flex items-center justify-center gap-2 py-2 px-4 rounded text-sm font-medium transition-opacity"
            style={{ background: '#494bd6', color: '#ffffff' }}
            onClick={clearMessages}
            onMouseEnter={e => (e.currentTarget.style.opacity = '0.85')}
            onMouseLeave={e => (e.currentTarget.style.opacity = '1')}
          >
            <span className="material-symbols-outlined" style={{ fontSize: 16 }}>add</span>
            New Thread
          </button>
        </div>

        {/* Session list */}
        <div className="flex-1 py-3 overflow-y-auto">
          {groups.map(group => (
            <div key={group} className="mb-4">
              <p
                className="px-4 mb-1 uppercase"
                style={{ fontSize: 10, fontWeight: 700, letterSpacing: '0.08em', color: '#464554', fontFamily: 'Space Grotesk, sans-serif' }}
              >
                {group}
              </p>
              {RECENT_SESSIONS.filter(s => s.group === group).map(session => (
                <button
                  key={session.id}
                  onClick={() => setActiveSession(session.id)}
                  className="w-full text-left px-4 py-2.5 transition-colors"
                  style={
                    activeSession === session.id
                      ? { background: 'rgba(192,193,255,0.08)', borderLeft: '2px solid #c0c1ff', paddingLeft: 14 }
                      : { color: '#c7c4d7' }
                  }
                >
                  <p
                    style={{
                      fontSize: 13,
                      color: activeSession === session.id ? '#c0c1ff' : '#c7c4d7',
                      fontFamily: 'Inter, sans-serif',
                      overflow: 'hidden',
                      textOverflow: 'ellipsis',
                      whiteSpace: 'nowrap',
                    }}
                  >
                    {session.title}
                  </p>
                  <p style={{ fontSize: 11, color: '#908fa0', marginTop: 2 }}>{session.time}</p>
                </button>
              ))}
            </div>
          ))}
        </div>
      </aside>

      {/* Main chat area */}
      <main className="flex-1 flex flex-col h-full relative" style={{ background: '#0B0E14' }}>
        {/* Messages */}
        <div ref={scrollRef} className="flex-1 overflow-y-auto p-6 space-y-6 pb-40">
          {messages.length === 0 ? (
            <div className="flex h-full flex-col items-center justify-center py-20 text-center">
              {/* Empty state */}
              <div
                className="flex h-14 w-14 items-center justify-center mb-4"
                style={{ background: 'rgba(192,193,255,0.08)', border: '1px solid rgba(192,193,255,0.15)', borderRadius: 12 }}
              >
                <span className="material-symbols-outlined" style={{ fontSize: 28, color: '#c0c1ff' }}>forum</span>
              </div>
              <p className="text-sm font-medium mb-1" style={{ color: '#e4e1ed' }}>Ask about your codebase</p>
              <p className="max-w-xs text-xs leading-relaxed mb-8" style={{ color: '#908fa0' }}>
                Get answers grounded in your indexed repositories. Try asking about functions, patterns, or service dependencies.
              </p>

              {/* Suggested queries */}
              <div className="grid grid-cols-2 gap-2 w-full max-w-lg">
                {SUGGESTED_QUERIES.map((q, i) => (
                  <button
                    key={i}
                    onClick={() => handleSend(q)}
                    className="text-left px-4 py-3 rounded transition-all"
                    style={{
                      background: '#1f1f27',
                      border: '1px solid rgba(255,255,255,0.06)',
                      fontSize: 12,
                      color: '#c7c4d7',
                      fontFamily: 'Inter, sans-serif',
                      lineHeight: 1.5,
                    }}
                    onMouseEnter={e => {
                      (e.currentTarget as HTMLElement).style.borderColor = 'rgba(192,193,255,0.2)';
                      (e.currentTarget as HTMLElement).style.color = '#e4e1ed';
                    }}
                    onMouseLeave={e => {
                      (e.currentTarget as HTMLElement).style.borderColor = 'rgba(255,255,255,0.06)';
                      (e.currentTarget as HTMLElement).style.color = '#c7c4d7';
                    }}
                  >
                    {q}
                  </button>
                ))}
              </div>
            </div>
          ) : (
            messages.map(message => (
              <ChatMessageBubble key={message.id} message={message} />
            ))
          )}
        </div>

        {/* Input bar */}
        <div
          className="absolute bottom-0 left-0 right-0 p-4"
          style={{ background: 'linear-gradient(to top, #0B0E14 65%, transparent)' }}
        >
          <div
            className="mx-auto rounded relative"
            style={{ maxWidth: 760, background: '#1b1b23', border: '1px solid rgba(255,255,255,0.08)' }}
          >
            <textarea
              ref={textareaRef}
              value={inputValue}
              onChange={e => setInputValue(e.target.value)}
              onKeyDown={handleKeyDown}
              placeholder={`Ask a question about ${selectedRepo?.name ?? 'the repository'}...`}
              rows={1}
              className="w-full bg-transparent border-none outline-none resize-none px-4 pt-3 pb-12 text-sm max-h-40 overflow-y-auto"
              style={{ color: '#e4e1ed', caretColor: '#c0c1ff', fontFamily: 'Inter, sans-serif' }}
            />
            <div className="absolute bottom-7 left-8 flex items-center gap-3">
              <button style={{ color: '#908fa0' }}>
                <span className="material-symbols-outlined" style={{ fontSize: 18 }}>attach_file</span>
              </button>
              <button style={{ color: '#908fa0' }}>
                <span className="material-symbols-outlined" style={{ fontSize: 18 }}>account_tree</span>
              </button>
              {/* Index synced indicator */}
              <div className="flex items-center gap-1.5">
                <span className="w-1.5 h-1.5 rounded-full" style={{ background: '#4edea3', boxShadow: '0 0 6px rgba(78,222,163,0.6)' }} />
                <span style={{ fontSize: 10, color: '#4edea3', fontFamily: 'Fira Code, monospace' }}>Index synced</span>
              </div>
            </div>
            <div className="absolute bottom-6 right-7">
              <button
                onClick={() => handleSend()}
                disabled={!inputValue.trim() || isStreaming}
                className="h-8 w-8 rounded flex items-center justify-center transition-opacity active:scale-95 disabled:opacity-30"
                style={{ background: '#494bd6', color: '#ffffff' }}
              >
                {isStreaming ? <Loader2 className="w-4 h-4 animate-spin" /> : <ArrowUp className="w-4 h-4" />}
              </button>
            </div>
          </div>
        </div>
      </main>
    </div>
  );
}
