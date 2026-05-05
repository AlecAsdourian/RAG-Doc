import { useState } from 'react';

interface DocSection {
  id: string;
  title: string;
  tag?: 'AI' | 'Manual';
  children?: DocItem[];
}

interface DocItem {
  id: string;
  title: string;
  tag?: 'AI' | 'Manual';
}

const DOC_SECTIONS: DocSection[] = [
  {
    id: 'getting-started', title: 'Getting Started', tag: 'Manual',
    children: [
      { id: 'overview',       title: 'Overview',             tag: 'AI'     },
      { id: 'installation',   title: 'Installation Guide',   tag: 'Manual' },
      { id: 'quickstart',     title: 'Quick Start',          tag: 'AI'     },
    ],
  },
  {
    id: 'architecture', title: 'Architecture', tag: 'AI',
    children: [
      { id: 'system-overview', title: 'System Overview',    tag: 'AI'     },
      { id: 'rag-pipeline',    title: 'RAG Pipeline',       tag: 'AI'     },
      { id: 'data-flow',       title: 'Data Flow',          tag: 'Manual' },
    ],
  },
  {
    id: 'api-reference', title: 'API Reference', tag: 'AI',
    children: [
      { id: 'search-api',  title: 'Search Endpoints',   tag: 'AI' },
      { id: 'ingest-api',  title: 'Ingest Endpoints',   tag: 'AI' },
      { id: 'auth-api',    title: 'Auth Endpoints',      tag: 'AI' },
    ],
  },
];

const ACTIVE_DOC = {
  id: 'rag-pipeline',
  title: 'RAG Pipeline Architecture',
  tag: 'AI' as const,
  lastSynced: '2 mins ago',
  branch: 'main',
  content: [
    { type: 'h1',      text: 'RAG Pipeline Architecture'                                                                                              },
    { type: 'meta',    text: 'AI-Generated • Last synced 2 mins ago • Source: src/pipeline/, docs/architecture.md'                                    },
    { type: 'callout-info', text: 'This document is automatically generated from indexed source files and kept in sync with every commit.'             },
    { type: 'h2',      text: 'Overview'                                                                                                               },
    { type: 'p',       text: 'The RAG (Retrieval-Augmented Generation) pipeline is the core of the knowledge engine. It transforms raw source code into semantically queryable vector representations, enabling natural-language search and AI-powered code analysis.' },
    { type: 'h2',      text: 'Pipeline Stages'                                                                                                        },
    { type: 'p',       text: 'The pipeline consists of four sequential stages, each responsible for a specific transformation:'                        },
    { type: 'grid',    items: [
        { icon: 'upload_file',  title: 'Ingestion',   desc: 'Raw source files are loaded from connected repositories or local uploads.'   },
        { icon: 'code',         title: 'Parsing',     desc: 'Language-specific AST parsers extract symbols, functions, and semantic units.' },
        { icon: 'hub',          title: 'Embedding',   desc: 'Chunks are encoded into high-dimensional vectors using text-embedding-3-small.' },
        { icon: 'storage',      title: 'Storage',     desc: 'Vectors are persisted in the Qdrant vector store for fast ANN retrieval.'     },
      ]
    },
    { type: 'code', lang: 'python', text: `# Example: Chunking strategy
def chunk_file(content: str, max_tokens: int = 512) -> list[str]:
    """Split source file into semantic chunks."""
    lines = content.splitlines()
    chunks, current = [], []
    for line in lines:
        current.append(line)
        if len(" ".join(current).split()) >= max_tokens:
            chunks.append("\\n".join(current))
            current = []
    if current:
        chunks.append("\\n".join(current))
    return chunks` },
    { type: 'callout-warn', text: 'Files larger than 2MB are skipped during chunking. Consider splitting large generated files before indexing.' },
    { type: 'h2',      text: 'Configuration'                                                                                                          },
    { type: 'p',       text: 'The pipeline is configurable via environment variables. Key settings control chunk size, embedding model, and batch upload size.' },
  ],
};

export function LivingDocsPage() {
  const [activeId, setActiveId] = useState('rag-pipeline');
  const [expanded, setExpanded] = useState<Set<string>>(new Set(['architecture']));

  const toggleSection = (id: string) =>
    setExpanded(prev => { const n = new Set(prev); n.has(id) ? n.delete(id) : n.add(id); return n; });

  return (
    <div className="flex flex-1 min-h-0 overflow-hidden" style={{ background: '#0B0E14', fontFamily: 'Inter, sans-serif' }}>
      {/* Left: Doc tree sidebar */}
      <aside
        className="flex flex-col shrink-0 overflow-y-auto"
        style={{ width: 260, background: '#151921', borderRight: '1px solid rgba(255,255,255,0.06)' }}
      >
        {/* Sidebar header */}
        <div
          className="px-4 py-3"
          style={{ borderBottom: '1px solid rgba(255,255,255,0.06)', background: 'rgba(255,255,255,0.02)' }}
        >
          <span
            className="uppercase"
            style={{ fontSize: 11, fontWeight: 700, letterSpacing: '0.08em', color: '#908fa0', fontFamily: 'Space Grotesk, sans-serif' }}
          >
            Documentation
          </span>
        </div>

        {/* Doc tree */}
        <div className="flex-1 overflow-y-auto py-2">
          {DOC_SECTIONS.map(section => (
            <div key={section.id}>
              <button
                onClick={() => toggleSection(section.id)}
                className="w-full flex items-center justify-between px-4 py-2 transition-colors"
                onMouseEnter={e => (e.currentTarget.style.background = 'rgba(255,255,255,0.03)')}
                onMouseLeave={e => (e.currentTarget.style.background = 'transparent')}
              >
                <div className="flex items-center gap-2">
                  <span className="material-symbols-outlined" style={{ fontSize: 14, color: '#908fa0' }}>
                    {expanded.has(section.id) ? 'expand_more' : 'chevron_right'}
                  </span>
                  <span style={{ fontSize: 13, color: '#c7c4d7', fontFamily: 'Inter, sans-serif' }}>{section.title}</span>
                </div>
                {section.tag && (
                  <span
                    style={{
                      fontSize: 9,
                      fontWeight: 700,
                      letterSpacing: '0.06em',
                      padding: '2px 6px',
                      borderRadius: 3,
                      fontFamily: 'Space Grotesk, sans-serif',
                      ...(section.tag === 'AI'
                        ? { color: '#c0c1ff', background: 'rgba(192,193,255,0.10)' }
                        : { color: '#908fa0', background: 'rgba(255,255,255,0.05)' }),
                    }}
                  >
                    {section.tag}
                  </span>
                )}
              </button>

              {expanded.has(section.id) && section.children && (
                <div>
                  {section.children.map(item => (
                    <button
                      key={item.id}
                      onClick={() => setActiveId(item.id)}
                      className="w-full flex items-center justify-between pl-10 pr-4 py-2 text-left transition-colors"
                      style={
                        activeId === item.id
                          ? { background: 'rgba(192,193,255,0.08)', borderLeft: '2px solid #c0c1ff', paddingLeft: 38 }
                          : {}
                      }
                      onMouseEnter={e => activeId !== item.id && (e.currentTarget.style.background = 'rgba(255,255,255,0.03)')}
                      onMouseLeave={e => activeId !== item.id && (e.currentTarget.style.background = 'transparent')}
                    >
                      <span style={{ fontSize: 13, color: activeId === item.id ? '#c0c1ff' : '#c7c4d7', fontFamily: 'Inter, sans-serif' }}>
                        {item.title}
                      </span>
                      {item.tag && (
                        <span
                          style={{
                            fontSize: 9,
                            fontWeight: 700,
                            padding: '2px 6px',
                            borderRadius: 3,
                            fontFamily: 'Space Grotesk, sans-serif',
                            ...(item.tag === 'AI'
                              ? { color: '#c0c1ff', background: 'rgba(192,193,255,0.10)' }
                              : { color: '#908fa0', background: 'rgba(255,255,255,0.05)' }),
                          }}
                        >
                          {item.tag}
                        </span>
                      )}
                    </button>
                  ))}
                </div>
              )}
            </div>
          ))}
        </div>

        {/* Generate docs CTA */}
        <div className="p-3" style={{ borderTop: '1px solid rgba(255,255,255,0.06)' }}>
          <button
            className="w-full flex items-center justify-center gap-2 py-2 rounded text-sm font-medium transition-opacity"
            style={{ background: '#494bd6', color: '#ffffff' }}
            onMouseEnter={e => (e.currentTarget.style.opacity = '0.85')}
            onMouseLeave={e => (e.currentTarget.style.opacity = '1')}
          >
            <span className="material-symbols-outlined" style={{ fontSize: 16 }}>auto_awesome</span>
            Generate Docs
          </button>
        </div>
      </aside>

      {/* Main: doc reader */}
      <main className="flex-1 flex flex-col overflow-hidden" style={{ background: '#13131b' }}>
        {/* Status bar */}
        <div
          className="flex items-center justify-between px-8 py-2 shrink-0"
          style={{ background: 'rgba(255,255,255,0.02)', borderBottom: '1px solid rgba(255,255,255,0.06)' }}
        >
          <div className="flex items-center gap-4">
            <div className="flex items-center gap-1.5">
              <span className="w-1.5 h-1.5 rounded-full" style={{ background: '#4edea3', boxShadow: '0 0 6px rgba(78,222,163,0.6)' }} />
              <span style={{ fontSize: 12, color: '#4edea3', fontFamily: 'Fira Code, monospace' }}>AI-Generated &amp; Synced</span>
            </div>
            <span style={{ fontSize: 12, color: '#908fa0', fontFamily: 'Fira Code, monospace' }}>
              Last synced {ACTIVE_DOC.lastSynced}
            </span>
          </div>
          <div className="flex items-center gap-2">
            <span className="material-symbols-outlined" style={{ fontSize: 16, color: '#908fa0' }}>commit</span>
            <span
              className="px-2 py-0.5 rounded"
              style={{ fontSize: 12, color: '#c7c4d7', background: '#1f1f27', border: '1px solid #464554', fontFamily: 'Fira Code, monospace' }}
            >
              {ACTIVE_DOC.branch}
            </span>
            <button
              className="flex items-center gap-1 px-2 py-0.5 rounded transition-opacity"
              style={{ background: 'rgba(192,193,255,0.08)', color: '#c0c1ff', fontSize: 12, fontFamily: 'Fira Code, monospace' }}
              onMouseEnter={e => (e.currentTarget.style.opacity = '0.7')}
              onMouseLeave={e => (e.currentTarget.style.opacity = '1')}
            >
              <span className="material-symbols-outlined" style={{ fontSize: 14 }}>sync</span>
              Regenerate
            </button>
          </div>
        </div>

        {/* Doc content */}
        <div className="flex-1 overflow-y-auto px-8 py-8 max-w-3xl">
          {ACTIVE_DOC.content.map((block, i) => {
            if (block.type === 'h1') return (
              <h1
                key={i}
                className="mb-2"
                style={{ fontSize: 32, fontWeight: 600, lineHeight: 1.2, letterSpacing: '-0.02em', color: '#e4e1ed', fontFamily: 'Inter, sans-serif' }}
              >
                {block.text}
              </h1>
            );
            if (block.type === 'meta') return (
              <p
                key={i}
                className="mb-6"
                style={{ fontSize: 12, color: '#908fa0', fontFamily: 'Fira Code, monospace' }}
              >
                {block.text}
              </p>
            );
            if (block.type === 'h2') return (
              <h2
                key={i}
                className="mt-8 mb-4"
                style={{ fontSize: 24, fontWeight: 600, lineHeight: 1.3, letterSpacing: '-0.01em', color: '#e4e1ed', fontFamily: 'Inter, sans-serif', borderBottom: '1px solid rgba(255,255,255,0.06)', paddingBottom: 8 }}
              >
                {block.text}
              </h2>
            );
            if (block.type === 'p') return (
              <p
                key={i}
                className="mb-4"
                style={{ fontSize: 14, color: '#c7c4d7', lineHeight: 1.7, fontFamily: 'Inter, sans-serif' }}
              >
                {block.text}
              </p>
            );
            if (block.type === 'callout-info') return (
              <div
                key={i}
                className="mb-4 px-4 py-3 rounded"
                style={{ background: 'rgba(192,193,255,0.06)', borderLeft: '3px solid #c0c1ff' }}
              >
                <div className="flex items-start gap-2">
                  <span className="material-symbols-outlined shrink-0" style={{ fontSize: 16, color: '#c0c1ff', marginTop: 1 }}>info</span>
                  <p style={{ fontSize: 13, color: '#c7c4d7', lineHeight: 1.6, fontFamily: 'Inter, sans-serif' }}>{block.text}</p>
                </div>
              </div>
            );
            if (block.type === 'callout-warn') return (
              <div
                key={i}
                className="mb-4 px-4 py-3 rounded"
                style={{ background: 'rgba(255,183,131,0.06)', borderLeft: '3px solid #ffb783' }}
              >
                <div className="flex items-start gap-2">
                  <span className="material-symbols-outlined shrink-0" style={{ fontSize: 16, color: '#ffb783', marginTop: 1 }}>warning</span>
                  <p style={{ fontSize: 13, color: '#c7c4d7', lineHeight: 1.6, fontFamily: 'Inter, sans-serif' }}>{block.text}</p>
                </div>
              </div>
            );
            if (block.type === 'grid' && block.items) return (
              <div key={i} className="grid grid-cols-2 gap-3 mb-6">
                {block.items.map((item, j) => (
                  <div
                    key={j}
                    className="flex gap-3 p-4 rounded"
                    style={{ background: '#1f1f27', border: '1px solid rgba(255,255,255,0.06)' }}
                  >
                    <span className="material-symbols-outlined shrink-0 mt-0.5" style={{ fontSize: 20, color: '#c0c1ff' }}>
                      {item.icon}
                    </span>
                    <div>
                      <p style={{ fontSize: 13, fontWeight: 600, color: '#e4e1ed', fontFamily: 'Inter, sans-serif', marginBottom: 4 }}>{item.title}</p>
                      <p style={{ fontSize: 12, color: '#908fa0', fontFamily: 'Inter, sans-serif', lineHeight: 1.5 }}>{item.desc}</p>
                    </div>
                  </div>
                ))}
              </div>
            );
            if (block.type === 'code') return (
              <div
                key={i}
                className="mb-6 rounded overflow-hidden"
                style={{ border: '1px solid rgba(255,255,255,0.06)' }}
              >
                <div
                  className="flex items-center justify-between px-4 py-2"
                  style={{ background: '#1f1f27', borderBottom: '1px solid rgba(255,255,255,0.06)' }}
                >
                  <span style={{ fontSize: 11, color: '#908fa0', fontFamily: 'Fira Code, monospace' }}>{block.lang}</span>
                  <button style={{ fontSize: 11, color: '#908fa0' }}>
                    <span className="material-symbols-outlined" style={{ fontSize: 14 }}>content_copy</span>
                  </button>
                </div>
                <pre
                  className="overflow-x-auto p-4"
                  style={{ background: '#0d0d15', fontSize: 12, color: '#c7c4d7', fontFamily: 'Fira Code, monospace', lineHeight: 1.6, margin: 0 }}
                >
                  <code>{block.text}</code>
                </pre>
              </div>
            );
            return null;
          })}
        </div>
      </main>
    </div>
  );
}
