import type { Message } from '@/hooks/useChat';

interface ChatMessageBubbleProps {
  message: Message;
}

function renderContent(content: string, isUser: boolean) {
  const lines = content.split('\n');
  const elements: React.ReactNode[] = [];
  let inCode = false;
  let codeLines: string[] = [];
  let codeLang = '';

  lines.forEach((line, i) => {
    if (line.startsWith('```')) {
      if (!inCode) {
        inCode = true;
        codeLang = line.slice(3).trim();
        codeLines = [];
      } else {
        elements.push(
          <div
            key={`code-${i}`}
            className="rounded overflow-hidden my-3"
            style={{ border: '1px solid rgba(255,255,255,0.08)' }}
          >
            {codeLang && (
              <div
                className="flex items-center justify-between px-3 py-1.5"
                style={{ background: '#1f1f27', borderBottom: '1px solid rgba(255,255,255,0.06)' }}
              >
                <span style={{ fontSize: 11, color: '#908fa0', fontFamily: 'Fira Code, monospace' }}>{codeLang}</span>
              </div>
            )}
            <pre
              className="p-4 overflow-x-auto"
              style={{ background: '#0d0d15', fontSize: 12, color: '#c7c4d7', fontFamily: 'Fira Code, monospace', lineHeight: 1.6, margin: 0 }}
            >
              {codeLines.join('\n')}
            </pre>
          </div>
        );
        inCode = false;
        codeLines = [];
        codeLang = '';
      }
    } else if (inCode) {
      codeLines.push(line);
    } else if (line.trim() === '') {
      elements.push(<div key={i} style={{ height: 6 }} />);
    } else {
      elements.push(
        <p
          key={i}
          style={{
            fontSize: 14,
            lineHeight: 1.65,
            color: isUser ? '#e4e1ed' : '#c7c4d7',
            fontFamily: 'Inter, sans-serif',
            marginBottom: 2,
          }}
        >
          {line}
        </p>
      );
    }
  });

  return elements;
}

export function ChatMessageBubble({ message }: ChatMessageBubbleProps) {
  const isUser = message.role === 'user';

  if (isUser) {
    return (
      <div className="flex items-start gap-3 max-w-3xl ml-auto flex-row-reverse">
        <div
          className="w-7 h-7 rounded-full flex items-center justify-center shrink-0 text-xs font-semibold"
          style={{ background: '#1f1f27', border: '1px solid rgba(255,255,255,0.10)', color: '#c7c4d7' }}
        >
          U
        </div>
        <div
          className="px-4 py-3 rounded"
          style={{ background: '#1f1f27', border: '1px solid rgba(255,255,255,0.06)', maxWidth: 520 }}
        >
          {renderContent(message.content, true)}
        </div>
      </div>
    );
  }

  return (
    <div className="flex items-start gap-3 max-w-4xl">
      {/* AI avatar */}
      <div
        className="w-7 h-7 rounded flex items-center justify-center shrink-0"
        style={{ background: '#8083ff' }}
      >
        <span className="material-symbols-outlined" style={{ fontSize: 14, color: '#0d0096' }}>smart_toy</span>
      </div>

      <div className="flex-1 space-y-3">
        <div
          className="px-4 py-3 rounded"
          style={{ background: '#1b1b23', border: '1px solid rgba(255,255,255,0.06)' }}
        >
          {renderContent(message.content, false)}
          {message.isStreaming && (
            <span
              className="inline-block w-0.5 h-4 align-middle ml-0.5 animate-pulse"
              style={{ background: '#c0c1ff' }}
            />
          )}
        </div>

        {/* Knowledge graph link chips */}
        {message.sources && message.sources.length > 0 && (
          <div className="flex flex-wrap gap-1.5">
            {message.sources.map((source, idx) => (
              <span
                key={idx}
                className="flex items-center gap-1 px-2 py-1 rounded cursor-pointer transition-colors"
                style={{
                  background: 'rgba(192,193,255,0.08)',
                  border: '1px solid rgba(192,193,255,0.15)',
                  fontSize: 11,
                  color: '#c0c1ff',
                  fontFamily: 'Fira Code, monospace',
                }}
              >
                <span className="material-symbols-outlined" style={{ fontSize: 12 }}>account_tree</span>
                {source.file_path.split('/').pop()}:{source.start_line}
              </span>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
