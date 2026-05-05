import { useState, useRef, useEffect } from 'react';
import { Github } from 'lucide-react';

interface IndexedRepo {
  id: string;
  name: string;
  status: 'ready' | 'indexing' | 'error';
  lastSynced: string;
}

interface TeamMember {
  id: string;
  name: string;
  email: string;
  role: 'Owner' | 'Editor' | 'Viewer';
}

interface LogLine {
  level: 'INFO' | 'WORK' | 'SYNC' | 'DONE' | 'ERR';
  timestamp: string;
  text: string;
}

const MOCK_REPOS: IndexedRepo[] = [
  { id: 'r1', name: 'nebula-core',  status: 'ready',    lastSynced: 'Synced 2m ago'           },
  { id: 'r2', name: 'frontend-v3',  status: 'indexing', lastSynced: 'Indexing in progress...'  },
  { id: 'r3', name: 'api-gateway',  status: 'ready',    lastSynced: 'Synced 4h ago'            },
  { id: 'r4', name: 'legacy-utils', status: 'error',    lastSynced: 'Indexing failed'          },
];

const MOCK_TEAM: TeamMember[] = [
  { id: 't1', name: 'Alex Chen (You)', email: 'alex@rag.io',    role: 'Owner'  },
  { id: 't2', name: 'Sarah Miller',    email: 's.miller@rag.io', role: 'Editor' },
];

const MOCK_LOG: LogLine[] = [
  { level: 'INFO', timestamp: '14:22:01', text: 'Starting vector embedding pipeline...'           },
  { level: 'WORK', timestamp: '14:22:05', text: 'Extracted AST from lib/parser.py'               },
  { level: 'WORK', timestamp: '14:22:12', text: 'Chunking src/core/engine.ts (847 tokens)'       },
  { level: 'SYNC', timestamp: '14:22:18', text: 'Embedding generated (128 vectors)'              },
  { level: 'ERR',  timestamp: '14:22:25', text: 'Parse error: test/mocks/invalid_syntax.js'     },
  { level: 'DONE', timestamp: '14:22:31', text: 'Indexing complete: src/api/routes.rs'           },
  { level: 'INFO', timestamp: '14:22:45', text: 'Uploading batch to vector store...'             },
  { level: 'SYNC', timestamp: '14:22:50', text: 'Batch 42/50 uploaded successfully'              },
  { level: 'WORK', timestamp: '14:22:55', text: 'Processing src/ui/components/Navbar.tsx...'    },
];

const LOG_COLOR: Record<LogLine['level'], string> = {
  INFO: '#c0c1ff',
  WORK: '#c7c4d7',
  SYNC: '#4edea3',
  DONE: '#4edea3',
  ERR:  '#ffb4ab',
};

const STATUS_CFG = {
  ready:    { label: 'SYNCED',   color: '#4edea3', bg: 'rgba(78,222,163,0.08)',  glow: 'rgba(78,222,163,0.6)',  animate: false },
  indexing: { label: 'INDEXING', color: '#ffb783', bg: 'rgba(255,183,131,0.08)', glow: 'rgba(255,183,131,0.6)', animate: true  },
  error:    { label: 'ERROR',    color: '#ffb4ab', bg: 'rgba(255,180,171,0.08)', glow: 'rgba(255,180,171,0.6)', animate: false },
};

function StatusChip({ status }: { status: IndexedRepo['status'] }) {
  const cfg = STATUS_CFG[status];
  return (
    <div
      className="flex items-center gap-1 px-2 py-0.5"
      style={{ background: cfg.bg, borderRadius: 4 }}
    >
      <span
        className={cfg.animate ? 'animate-pulse' : ''}
        style={{ width: 6, height: 6, borderRadius: '50%', background: cfg.color, boxShadow: `0 0 6px ${cfg.glow}`, display: 'inline-block' }}
      />
      <span
        style={{ fontSize: 10, fontWeight: 700, letterSpacing: '0.05em', color: cfg.color, fontFamily: 'Space Grotesk, sans-serif' }}
      >
        {cfg.label}
      </span>
    </div>
  );
}

export function RepoSettingsPage() {
  const [inviteEmail, setInviteEmail] = useState('');
  const [isDragging, setIsDragging] = useState(false);
  const logRef = useRef<HTMLDivElement>(null);
  const progress = 68;

  useEffect(() => {
    if (logRef.current) logRef.current.scrollTop = logRef.current.scrollHeight;
  }, []);

  return (
    <div
      className="flex flex-1 min-h-0 overflow-hidden"
      style={{ background: '#0B0E14', color: '#e4e1ed', fontFamily: 'Inter, sans-serif' }}
    >
      {/* Left column */}
      <div className="flex w-7/12 flex-col" style={{ borderRight: '1px solid rgba(255,255,255,0.06)' }}>
        {/* Connect Data Source */}
        <section
          className="flex flex-col p-6 shrink-0"
          style={{ minHeight: 220, borderBottom: '1px solid rgba(255,255,255,0.06)', background: 'rgba(21,25,33,0.40)' }}
        >
          <h2
            className="text-base font-semibold uppercase tracking-widest flex items-center gap-2 mb-6"
            style={{ color: '#c7c4d7', fontFamily: 'Space Grotesk, sans-serif', letterSpacing: '0.08em', fontSize: 11 }}
          >
            <span className="material-symbols-outlined" style={{ fontSize: 18, color: '#c0c1ff' }}>link</span>
            Connect Data Source
          </h2>

          <div className="flex gap-4 h-full items-center">
            {/* GitHub */}
            <button
              className="flex-1 flex items-center justify-center gap-2 py-3 px-4 transition-all rounded"
              style={{ background: '#c0c1ff', color: '#1000a9', fontWeight: 600, fontSize: 14 }}
              onMouseEnter={e => (e.currentTarget.style.background = '#e1e0ff')}
              onMouseLeave={e => (e.currentTarget.style.background = '#c0c1ff')}
            >
              <Github className="w-4 h-4" />
              Connect GitHub
            </button>

            {/* Divider */}
            <div className="flex flex-col items-center gap-1 self-stretch py-2">
              <div className="w-px flex-1" style={{ background: 'rgba(255,255,255,0.08)' }} />
              <span style={{ fontSize: 10, color: '#908fa0', fontFamily: 'Fira Code, monospace' }}>OR</span>
              <div className="w-px flex-1" style={{ background: 'rgba(255,255,255,0.08)' }} />
            </div>

            {/* Upload Local */}
            <div
              onDragOver={e => { e.preventDefault(); setIsDragging(true); }}
              onDragLeave={() => setIsDragging(false)}
              onDrop={e => { e.preventDefault(); setIsDragging(false); }}
              className="flex-1 flex flex-col items-center justify-center gap-2 p-4 cursor-pointer transition-all rounded"
              style={{
                border: `1px dashed ${isDragging ? '#c0c1ff' : 'rgba(255,255,255,0.15)'}`,
                background: isDragging ? 'rgba(192,193,255,0.04)' : 'transparent',
                minHeight: 80,
              }}
              onMouseEnter={e => !isDragging && ((e.currentTarget as HTMLElement).style.borderColor = 'rgba(192,193,255,0.4)')}
              onMouseLeave={e => !isDragging && ((e.currentTarget as HTMLElement).style.borderColor = 'rgba(255,255,255,0.15)')}
            >
              <span className="material-symbols-outlined" style={{ fontSize: 24, color: isDragging ? '#c0c1ff' : '#908fa0' }}>
                upload_file
              </span>
              <p style={{ fontSize: 12, color: isDragging ? '#e4e1ed' : '#c7c4d7' }}>Upload Local Directory</p>
              <p style={{ fontSize: 10, color: '#908fa0', fontFamily: 'Fira Code, monospace' }}>ZIP, TAR, or Folder • 2GB max</p>
            </div>
          </div>
        </section>

        {/* Knowledge Indexing */}
        <section className="flex flex-1 flex-col gap-4 overflow-hidden p-6">
          <div className="flex items-center justify-between">
            <h2
              className="font-semibold uppercase tracking-widest flex items-center gap-2"
              style={{ fontSize: 11, color: '#c7c4d7', fontFamily: 'Space Grotesk, sans-serif', letterSpacing: '0.08em' }}
            >
              <span className="material-symbols-outlined" style={{ fontSize: 18, color: '#c0c1ff' }}>memory</span>
              Knowledge Indexing
            </h2>
            <div className="text-right">
              <p style={{ fontSize: 13, color: '#c0c1ff', fontFamily: 'Fira Code, monospace', fontWeight: 500 }}>
                847 / 1203 files
              </p>
              <p style={{ fontSize: 10, color: '#908fa0', textTransform: 'uppercase', letterSpacing: '0.08em' }}>
                {progress}% Complete
              </p>
            </div>
          </div>

          {/* Shimmer progress bar */}
          <div className="w-full h-1.5 rounded-full overflow-hidden" style={{ background: 'rgba(255,255,255,0.06)' }}>
            <div
              className="h-full rounded-full relative overflow-hidden"
              style={{ width: `${progress}%`, background: '#494bd6' }}
            >
              <div
                className="absolute inset-0 animate-pulse"
                style={{ background: 'linear-gradient(90deg, transparent, rgba(192,193,255,0.4), transparent)' }}
              />
            </div>
          </div>

          {/* Terminal log */}
          <div
            ref={logRef}
            className="flex-1 overflow-y-auto p-4 space-y-1 rounded"
            style={{ background: '#0d0d15', border: '1px solid rgba(255,255,255,0.06)', fontFamily: 'Fira Code, monospace', fontSize: 12 }}
          >
            {MOCK_LOG.map((line, i) => (
              <div key={i} className="flex gap-3">
                <span style={{ color: '#464554', flexShrink: 0 }}>{line.timestamp}</span>
                <span style={{ color: LOG_COLOR[line.level], flexShrink: 0, minWidth: 36 }}>[{line.level}]</span>
                <span style={{ color: '#c7c4d7' }}>{line.text}</span>
              </div>
            ))}
            <div className="flex gap-3">
              <span style={{ color: '#464554' }}>14:22:55</span>
              <span className="animate-pulse" style={{ color: '#4edea3' }}>█</span>
            </div>
          </div>
        </section>
      </div>

      {/* Right sidebar */}
      <div className="flex w-5/12 flex-col overflow-hidden" style={{ background: '#13131b' }}>
        {/* Indexed Repositories */}
        <div className="flex flex-col overflow-hidden" style={{ flex: '3', borderBottom: '1px solid rgba(255,255,255,0.06)' }}>
          <div
            className="px-5 py-3 flex items-center justify-between"
            style={{ borderBottom: '1px solid rgba(255,255,255,0.06)', background: 'rgba(255,255,255,0.02)' }}
          >
            <h2
              className="flex items-center gap-2 font-semibold uppercase tracking-widest"
              style={{ fontSize: 11, color: '#c7c4d7', fontFamily: 'Space Grotesk, sans-serif', letterSpacing: '0.08em' }}
            >
              <span className="material-symbols-outlined" style={{ fontSize: 16, color: '#c0c1ff' }}>storage</span>
              Indexed Repositories
            </h2>
            <span
              style={{ fontSize: 10, color: '#908fa0', background: '#1f1f27', padding: '2px 8px', borderRadius: 4, fontFamily: 'Fira Code, monospace' }}
            >
              {MOCK_REPOS.length} TOTAL
            </span>
          </div>

          <div className="flex-1 overflow-y-auto px-4 py-3 space-y-2">
            {MOCK_REPOS.map(repo => (
              <div
                key={repo.id}
                className="flex items-center justify-between px-3 py-2.5 rounded transition-colors cursor-pointer"
                style={{ background: 'rgba(13,13,21,0.6)', border: '1px solid rgba(255,255,255,0.05)' }}
                onMouseEnter={e => (e.currentTarget.style.borderColor = 'rgba(192,193,255,0.2)')}
                onMouseLeave={e => (e.currentTarget.style.borderColor = 'rgba(255,255,255,0.05)')}
              >
                <div className="flex items-center gap-2 min-w-0">
                  <span className="material-symbols-outlined shrink-0" style={{ fontSize: 16, color: '#908fa0' }}>folder</span>
                  <div className="min-w-0">
                    <p style={{ fontSize: 13, fontWeight: 500, color: '#e4e1ed', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>
                      {repo.name}
                    </p>
                    <p style={{ fontSize: 11, color: '#908fa0', fontFamily: 'Fira Code, monospace' }}>
                      {repo.lastSynced}
                    </p>
                  </div>
                </div>
                <StatusChip status={repo.status} />
              </div>
            ))}
          </div>

          <div className="px-4 pb-3">
            <button
              className="w-full py-2 rounded text-xs font-medium transition-colors"
              style={{ border: '1px solid rgba(255,255,255,0.08)', color: '#c7c4d7' }}
              onMouseEnter={e => { (e.currentTarget.style.background = 'rgba(255,255,255,0.04)'); (e.currentTarget.style.color = '#e4e1ed'); }}
              onMouseLeave={e => { (e.currentTarget.style.background = 'transparent'); (e.currentTarget.style.color = '#c7c4d7'); }}
            >
              Manage All Repositories
            </button>
          </div>
        </div>

        {/* Workspace Access */}
        <div className="flex flex-col overflow-hidden" style={{ flex: '2' }}>
          <div
            className="px-5 py-3"
            style={{ borderBottom: '1px solid rgba(255,255,255,0.06)', background: 'rgba(255,255,255,0.02)' }}
          >
            <h2
              className="flex items-center gap-2 font-semibold uppercase tracking-widest"
              style={{ fontSize: 11, color: '#c7c4d7', fontFamily: 'Space Grotesk, sans-serif', letterSpacing: '0.08em' }}
            >
              <span className="material-symbols-outlined" style={{ fontSize: 16, color: '#c0c1ff' }}>group</span>
              Workspace Access
            </h2>
          </div>

          <div className="flex-1 overflow-y-auto px-4 py-3 space-y-3">
            {MOCK_TEAM.map(member => (
              <div key={member.id} className="flex items-center justify-between">
                <div className="flex items-center gap-2">
                  <div
                    className="w-8 h-8 rounded-full flex items-center justify-center text-sm font-semibold shrink-0"
                    style={{ background: '#1f1f27', border: '1px solid #464554', color: '#c0c1ff' }}
                  >
                    {member.name[0]}
                  </div>
                  <div>
                    <p style={{ fontSize: 13, fontWeight: 500, color: '#e4e1ed' }}>{member.name}</p>
                    <p style={{ fontSize: 11, color: '#908fa0', fontFamily: 'Fira Code, monospace' }}>{member.email}</p>
                  </div>
                </div>
                <span
                  style={
                    member.role === 'Owner'
                      ? { fontSize: 10, fontWeight: 700, color: '#c0c1ff', background: 'rgba(192,193,255,0.12)', border: '1px solid rgba(192,193,255,0.2)', padding: '2px 8px', borderRadius: 4, fontFamily: 'Space Grotesk, sans-serif' }
                      : { fontSize: 10, fontWeight: 700, color: '#908fa0', background: '#1f1f27', padding: '2px 8px', borderRadius: 4, fontFamily: 'Space Grotesk, sans-serif' }
                  }
                >
                  {member.role}
                </span>
              </div>
            ))}
          </div>

          {/* Invite */}
          <div className="px-4 py-3 space-y-2" style={{ borderTop: '1px solid rgba(255,255,255,0.06)' }}>
            <p
              className="uppercase"
              style={{ fontSize: 11, fontWeight: 700, letterSpacing: '0.08em', color: '#908fa0', fontFamily: 'Space Grotesk, sans-serif' }}
            >
              Invite Team Member
            </p>
            <div className="flex gap-2">
              <input
                type="email"
                placeholder="dev@example.com"
                value={inviteEmail}
                onChange={e => setInviteEmail(e.target.value)}
                className="flex-1 rounded outline-none transition-all"
                style={{
                  background: '#0d0d15',
                  border: '1px solid rgba(255,255,255,0.08)',
                  padding: '6px 12px',
                  fontSize: 12,
                  color: '#e4e1ed',
                  fontFamily: 'Fira Code, monospace',
                }}
                onFocus={e => (e.currentTarget.style.borderColor = '#c0c1ff')}
                onBlur={e => (e.currentTarget.style.borderColor = 'rgba(255,255,255,0.08)')}
              />
              <button
                className="flex items-center justify-center rounded transition-opacity"
                style={{ background: '#494bd6', color: '#ffffff', padding: '6px 10px' }}
                onMouseEnter={e => (e.currentTarget.style.opacity = '0.85')}
                onMouseLeave={e => (e.currentTarget.style.opacity = '1')}
              >
                <span className="material-symbols-outlined" style={{ fontSize: 18 }}>send</span>
              </button>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
