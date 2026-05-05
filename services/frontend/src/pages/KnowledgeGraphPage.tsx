import { useState, useCallback } from 'react';
import {
  ReactFlow,
  Background,
  BackgroundVariant,
  type Node,
  type Edge,
  type NodeProps,
  Handle,
  Position,
  useNodesState,
  useEdgesState,
  useReactFlow,
  ReactFlowProvider,
} from '@xyflow/react';
import '@xyflow/react/dist/style.css';

// ─── Node data shape ────────────────────────────────────────────────────────
interface NodeData {
  label: string;
  icon: string;
  iconColor?: string;
  [key: string]: unknown;
}

// ─── Custom node: circle icon + label below ──────────────────────────────────
// Matches reference: flex-col, rounded-full circle, label span underneath
function GraphNode({ data, selected }: NodeProps<Node<NodeData>>) {
  const circleSize = selected ? 48 : 40;
  const iconSize   = selected ? 22 : 18;
  const iconColor  = selected ? '#c0c1ff' : (data.iconColor ?? '#c7c4d7');

  return (
    // Outer wrapper — flex-col so label sits below the circle
    <div
      style={{
        display: 'flex',
        flexDirection: 'column',
        alignItems: 'center',
        gap: 4,
        cursor: 'pointer',
        // Handles are placed at the center of the circle, which is at the top
        // of this container. React Flow picks them up for edge routing.
      }}
    >
      {/* ── Both handles pinned to circle center ─────────────────────────── */}
      {/* Using top-positioned handles at 50% so straight edges connect centers */}
      <Handle
        type="target"
        position={Position.Top}
        style={{
          top: circleSize / 2,
          left: '50%',
          transform: 'translate(-50%, -50%)',
          opacity: 0,
          pointerEvents: 'none',
          width: 1,
          height: 1,
          minWidth: 0,
          minHeight: 0,
          background: 'transparent',
          border: 'none',
        }}
      />
      <Handle
        type="source"
        position={Position.Top}
        style={{
          top: circleSize / 2,
          left: '50%',
          transform: 'translate(-50%, -50%)',
          opacity: 0,
          pointerEvents: 'none',
          width: 1,
          height: 1,
          minWidth: 0,
          minHeight: 0,
          background: 'transparent',
          border: 'none',
        }}
      />

      {/* ── Circle icon ────────────────────────────────────────────────────── */}
      <div
        style={{
          width: circleSize,
          height: circleSize,
          borderRadius: '50%',
          background: '#151921',
          border: selected
            ? '2px solid #c0c1ff'
            : '1px solid rgba(255,255,255,0.20)',
          boxShadow: selected
            ? '0 0 15px rgba(192,193,255,0.2)'
            : 'none',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          transition: 'border-color 0.15s, box-shadow 0.15s',
          flexShrink: 0,
        }}
      >
        <span
          className="material-symbols-outlined"
          style={{
            fontSize: iconSize,
            color: iconColor,
            fontVariationSettings: "'FILL' 0, 'wght' 300",
            lineHeight: 1,
          }}
        >
          {data.icon as string}
        </span>
      </div>

      {/* ── Label below circle ────────────────────────────────────────────── */}
      <span
        style={{
          fontSize: 11,
          fontFamily: 'Fira Code, monospace',
          lineHeight: 1.4,
          color: selected ? '#ffffff' : '#c7c4d7',
          background: selected ? '#151921' : '#0B0E14',
          padding: selected ? '1px 8px' : '0 4px',
          border: selected ? '1px solid rgba(255,255,255,0.10)' : 'none',
          borderRadius: 4,
          whiteSpace: 'nowrap',
          pointerEvents: 'none',
          userSelect: 'none',
        }}
      >
        {data.label as string}
      </span>
    </div>
  );
}

const nodeTypes = { graphNode: GraphNode };

// ─── Mock data ───────────────────────────────────────────────────────────────
const INITIAL_NODES: Node[] = [
  { id: 'n1', type: 'graphNode', position: { x: 200, y: 220 }, data: { label: 'auth_service.py',   icon: 'data_object', iconColor: '#c7c4d7' } },
  { id: 'n2', type: 'graphNode', position: { x: 430, y: 300 }, data: { label: 'UserSessionModel',  icon: 'hub',         iconColor: '#c0c1ff' } },
  { id: 'n3', type: 'graphNode', position: { x: 640, y: 200 }, data: { label: 'v1/endpoints.py',   icon: 'api',         iconColor: '#4edea3' } },
  { id: 'n4', type: 'graphNode', position: { x: 580, y: 430 }, data: { label: 'db_schema.sql',     icon: 'database',    iconColor: '#ffb783' } },
  { id: 'n5', type: 'graphNode', position: { x: 260, y: 460 }, data: { label: 'utils.py',          icon: 'description', iconColor: '#c7c4d7' } },
  { id: 'n6', type: 'graphNode', position: { x: 760, y: 360 }, data: { label: 'redis_client.py',   icon: 'memory',      iconColor: '#c7c4d7' } },
  { id: 'n7', type: 'graphNode', position: { x: 380, y: 120 }, data: { label: 'middleware/jwt.ts', icon: 'security',    iconColor: '#c7c4d7' } },
];

const INITIAL_EDGES: Edge[] = [
  { id: 'e1', source: 'n1', target: 'n2', type: 'straight', style: { stroke: 'rgba(255,255,255,0.12)', strokeWidth: 1 } },
  { id: 'e2', source: 'n3', target: 'n2', type: 'straight', style: { stroke: 'rgba(255,255,255,0.12)', strokeWidth: 1 } },
  // active edge — lavender, slightly thicker
  { id: 'e3', source: 'n2', target: 'n4', type: 'straight', style: { stroke: '#c0c1ff', strokeWidth: 2 }, label: 'imports', labelStyle: { fill: '#908fa0', fontSize: 10, fontFamily: 'Fira Code, monospace' }, labelBgStyle: { fill: '#151921', fillOpacity: 0.95 }, labelBgPadding: [4, 2] as [number, number], labelBgBorderRadius: 2 },
  { id: 'e4', source: 'n2', target: 'n5', type: 'straight', style: { stroke: 'rgba(255,255,255,0.10)', strokeWidth: 1 } },
  { id: 'e5', source: 'n3', target: 'n6', type: 'straight', style: { stroke: 'rgba(255,255,255,0.08)', strokeWidth: 1 } },
  { id: 'e6', source: 'n7', target: 'n1', type: 'straight', style: { stroke: 'rgba(255,255,255,0.10)', strokeWidth: 1 } },
  { id: 'e7', source: 'n7', target: 'n2', type: 'straight', style: { stroke: 'rgba(255,255,255,0.10)', strokeWidth: 1 } },
];

const CROSS_REFS = [
  { file: 'auth_service.py',   rel: 'Imports & Instantiates' },
  { file: 'v1/endpoints.py',   rel: 'Type Hinting'           },
  { file: 'test_session.py',   rel: 'Mocked Reference'       },
];

const LAYER_TOGGLES = [
  { id: 'deps',     label: 'Code Dependencies'     },
  { id: 'semantic', label: 'Semantic Similarity'   },
  { id: 'service',  label: 'Service Relationships' },
];

// ─── Inner graph component (needs ReactFlowProvider context) ─────────────────
function GraphInner() {
  const [nodes, , onNodesChange] = useNodesState(INITIAL_NODES);
  const [edges, , onEdgesChange] = useEdgesState(INITIAL_EDGES);
  const [activeLayers, setActiveLayers]   = useState(new Set(['deps', 'service']));
  const [selectedNode, setSelectedNode]   = useState<Node<NodeData> | null>(null);
  const [filterText, setFilterText]       = useState('');
  const { fitView, zoomIn, zoomOut }      = useReactFlow();

  const toggleLayer = (id: string) =>
    setActiveLayers(prev => { const n = new Set(prev); n.has(id) ? n.delete(id) : n.add(id); return n; });

  const onNodeClick = useCallback((_: unknown, node: Node) => {
    setSelectedNode(node as Node<NodeData>);
  }, []);

  const onPaneClick = useCallback(() => {
    setSelectedNode(null);
  }, []);

  // Filter nodes by name if filter text is set
  const visibleNodes = filterText
    ? nodes.map(n => ({
        ...n,
        style: {
          ...(n.style ?? {}),
          opacity: (n.data.label as string).toLowerCase().includes(filterText.toLowerCase()) ? 1 : 0.2,
        },
      }))
    : nodes;

  return (
    <div className="relative flex flex-1 min-h-0 w-full overflow-hidden" style={{ background: '#0B0E14' }}>
      <ReactFlow
        nodes={visibleNodes}
        edges={edges}
        onNodesChange={onNodesChange}
        onEdgesChange={onEdgesChange}
        nodeTypes={nodeTypes}
        onNodeClick={onNodeClick}
        onPaneClick={onPaneClick}
        fitView
        fitViewOptions={{ padding: 0.3 }}
        style={{ background: '#0B0E14' }}
        minZoom={0.2}
        maxZoom={3}
        // Remove default React Flow attribution
        proOptions={{ hideAttribution: true }}
      >
        {/* Dot-grid background matching reference */}
        <Background
          variant={BackgroundVariant.Dots}
          gap={24}
          size={1}
          color="#464554"
          style={{ opacity: 0.2 }}
        />
      </ReactFlow>

      {/* ── Branch selector — top-left corner ─────────────────────────────── */}
      <div
        className="absolute top-0 left-0 z-20 flex items-center gap-2 cursor-pointer"
        style={{
          background: 'rgba(27,27,35,0.90)',
          backdropFilter: 'blur(12px)',
          border: '1px solid rgba(255,255,255,0.10)',
          borderTop: 'none',
          borderLeft: 'none',
          borderBottomRightRadius: 8,
          padding: '8px 12px',
        }}
      >
        <span className="material-symbols-outlined" style={{ fontSize: 18, color: '#c0c1ff' }}>account_tree</span>
        <div className="flex flex-col">
          <span style={{ fontSize: 9, color: 'rgba(199,196,215,0.70)', letterSpacing: '0.08em', fontWeight: 700, fontFamily: 'Space Grotesk, sans-serif', textTransform: 'uppercase', lineHeight: 1 }}>Branch</span>
          <span style={{ fontSize: 13, color: '#ffffff', fontFamily: 'Fira Code, monospace', lineHeight: 1.3 }}>main</span>
        </div>
        <span className="material-symbols-outlined" style={{ fontSize: 18, color: '#c7c4d7', marginLeft: 8 }}>expand_more</span>
      </div>

      {/* ── Layer toggles — floating panel top-right (when no inspector) ──── */}
      {!selectedNode && (
        <div
          className="absolute top-4 right-4 z-20 flex flex-col gap-1"
          style={{
            background: 'rgba(21,25,33,0.85)',
            backdropFilter: 'blur(16px)',
            border: '1px solid rgba(255,255,255,0.10)',
            borderRadius: 8,
            padding: 12,
          }}
        >
          <p style={{ fontSize: 10, fontWeight: 700, letterSpacing: '0.08em', color: '#908fa0', fontFamily: 'Space Grotesk, sans-serif', textTransform: 'uppercase', marginBottom: 4 }}>
            Layers
          </p>
          {LAYER_TOGGLES.map(layer => (
            <button
              key={layer.id}
              onClick={() => toggleLayer(layer.id)}
              className="flex items-center gap-2 text-left transition-colors"
              style={{ fontSize: 12, color: activeLayers.has(layer.id) ? '#c0c1ff' : '#908fa0', fontFamily: 'Inter, sans-serif' }}
            >
              <div
                style={{
                  width: 12, height: 12, borderRadius: 2, flexShrink: 0,
                  border: `1px solid ${activeLayers.has(layer.id) ? '#c0c1ff' : '#464554'}`,
                  background: activeLayers.has(layer.id) ? 'rgba(192,193,255,0.15)' : 'transparent',
                }}
              />
              {layer.label}
            </button>
          ))}

          {/* Filter input */}
          <div
            className="flex items-center gap-2 mt-2 pt-2"
            style={{ borderTop: '1px solid rgba(255,255,255,0.07)' }}
          >
            <span className="material-symbols-outlined" style={{ fontSize: 14, color: '#908fa0' }}>search</span>
            <input
              placeholder="Filter nodes..."
              value={filterText}
              onChange={e => setFilterText(e.target.value)}
              className="bg-transparent outline-none"
              style={{ fontSize: 11, color: '#e4e1ed', fontFamily: 'Fira Code, monospace', width: 110 }}
            />
          </div>
        </div>
      )}

      {/* ── Custom zoom controls — bottom-left ────────────────────────────── */}
      <div
        className="absolute z-20 flex gap-1"
        style={{
          bottom: 16, left: 16,
          background: 'rgba(27,27,35,0.80)',
          backdropFilter: 'blur(12px)',
          border: '1px solid rgba(255,255,255,0.05)',
          borderRadius: 8,
          padding: 4,
        }}
      >
        {[
          { icon: 'zoom_in',    fn: () => zoomIn()              },
          { icon: 'zoom_out',   fn: () => zoomOut()             },
          { icon: 'fit_screen', fn: () => fitView({ padding: 0.3 }) },
        ].map(({ icon, fn }) => (
          <button
            key={icon}
            onClick={fn}
            className="flex items-center justify-center transition-colors"
            style={{
              width: 32, height: 32, borderRadius: 6,
              background: 'rgba(255,255,255,0.05)',
              color: '#c7c4d7',
            }}
            onMouseEnter={e => (e.currentTarget.style.background = 'rgba(255,255,255,0.10)')}
            onMouseLeave={e => (e.currentTarget.style.background = 'rgba(255,255,255,0.05)')}
          >
            <span className="material-symbols-outlined" style={{ fontSize: 18 }}>{icon}</span>
          </button>
        ))}
      </div>

      {/* ── Inspector panel — slides from right when node selected ─────────── */}
      {selectedNode && (
        <aside
          className="absolute top-0 right-0 bottom-0 flex flex-col z-20"
          style={{
            width: 340,
            background: '#151921',
            borderLeft: '1px solid rgba(255,255,255,0.07)',
            boxShadow: '-4px 0 24px rgba(0,0,0,0.5)',
          }}
        >
          {/* Inspector header */}
          <div
            className="p-4"
            style={{ borderBottom: '1px solid rgba(255,255,255,0.07)', background: 'rgba(255,255,255,0.02)' }}
          >
            <div className="flex items-start justify-between mb-2">
              <div className="flex items-center gap-2" style={{ color: '#c0c1ff' }}>
                <span className="material-symbols-outlined" style={{ fontSize: 20 }}>
                  {selectedNode.data.icon as string}
                </span>
                <span style={{ fontSize: 11, fontWeight: 700, letterSpacing: '0.08em', color: '#c7c4d7', fontFamily: 'Space Grotesk, sans-serif', textTransform: 'uppercase' }}>
                  Node
                </span>
              </div>
              <button
                onClick={() => setSelectedNode(null)}
                style={{ color: '#c7c4d7' }}
                onMouseEnter={e => ((e.currentTarget as HTMLElement).style.color = '#ffffff')}
                onMouseLeave={e => ((e.currentTarget as HTMLElement).style.color = '#c7c4d7')}
              >
                <span className="material-symbols-outlined" style={{ fontSize: 18 }}>close</span>
              </button>
            </div>
            <h2
              className="truncate"
              style={{ fontSize: 24, fontWeight: 600, lineHeight: 1.3, letterSpacing: '-0.01em', color: '#ffffff', fontFamily: 'Inter, sans-serif' }}
              title={selectedNode.data.label as string}
            >
              {selectedNode.data.label as string}
            </h2>
            <p style={{ fontSize: 12, color: '#c7c4d7', fontFamily: 'Fira Code, monospace', marginTop: 4 }}>
              core/models/session.py
            </p>
          </div>

          {/* Scrollable content */}
          <div className="flex-1 overflow-y-auto p-4 flex flex-col gap-6">
            {/* AI Summary */}
            <section>
              <div className="flex items-center gap-2 mb-2">
                <span className="material-symbols-outlined" style={{ fontSize: 16, color: '#4edea3' }}>auto_awesome</span>
                <h3 style={{ fontSize: 11, fontWeight: 700, letterSpacing: '0.08em', color: '#ffffff', fontFamily: 'Space Grotesk, sans-serif', textTransform: 'uppercase' }}>
                  AI Summary
                </h3>
              </div>
              <div
                className="p-3 rounded-lg"
                style={{
                  background: 'rgba(21,25,33,0.80)',
                  backdropFilter: 'blur(20px)',
                  border: '1px solid rgba(255,255,255,0.10)',
                }}
              >
                <p style={{ fontSize: 14, color: '#c7c4d7', lineHeight: 1.6, fontFamily: 'Inter, sans-serif' }}>
                  Core data model representing an active user session. Handles token validation, expiration tracking, and role-based access scope mapping. Intimately tied to the Redis cache layer for fast lookups.
                </p>
              </div>
            </section>

            {/* Metadata tags */}
            <section>
              <h3
                className="mb-2 uppercase"
                style={{ fontSize: 11, fontWeight: 700, letterSpacing: '0.08em', color: '#c7c4d7', fontFamily: 'Space Grotesk, sans-serif' }}
              >
                Metadata
              </h3>
              <div className="flex flex-wrap gap-2">
                <span className="flex items-center gap-1 px-2 py-1 rounded" style={{ background: '#34343d', fontSize: 11, color: '#ffffff', fontFamily: 'Fira Code, monospace' }}>
                  <span className="material-symbols-outlined" style={{ fontSize: 12 }}>code</span> Python
                </span>
                <span className="flex items-center gap-1 px-2 py-1 rounded" style={{ background: '#34343d', fontSize: 11, color: '#4edea3', fontFamily: 'Fira Code, monospace' }}>
                  <span className="material-symbols-outlined" style={{ fontSize: 12 }}>verified</span> High Coverage
                </span>
                <span className="flex items-center gap-1 px-2 py-1 rounded" style={{ background: 'rgba(147,0,10,0.20)', fontSize: 11, color: '#ffb4ab', fontFamily: 'Fira Code, monospace' }}>
                  <span className="material-symbols-outlined" style={{ fontSize: 12 }}>warning</span> Tech Debt
                </span>
              </div>
            </section>

            {/* Cross references */}
            <section>
              <div className="flex items-center justify-between mb-2">
                <h3
                  className="uppercase"
                  style={{ fontSize: 11, fontWeight: 700, letterSpacing: '0.08em', color: '#ffffff', fontFamily: 'Space Grotesk, sans-serif' }}
                >
                  Cross References
                </h3>
                <span
                  className="rounded"
                  style={{ fontSize: 11, color: '#c7c4d7', background: 'rgba(255,255,255,0.05)', padding: '1px 6px' }}
                >
                  12 Places
                </span>
              </div>

              <ul
                className="rounded-lg overflow-hidden"
                style={{ border: '1px solid rgba(255,255,255,0.05)', display: 'flex', flexDirection: 'column', gap: 1, background: 'rgba(255,255,255,0.05)' }}
              >
                {CROSS_REFS.map((ref, i) => (
                  <li
                    key={i}
                    className="flex items-center justify-between cursor-pointer transition-colors"
                    style={{ background: '#151921', padding: '8px 12px' }}
                    onMouseEnter={e => (e.currentTarget.style.background = 'rgba(255,255,255,0.03)')}
                    onMouseLeave={e => (e.currentTarget.style.background = '#151921')}
                  >
                    <div className="flex flex-col">
                      <span style={{ fontSize: 12, color: '#ffffff', fontFamily: 'Fira Code, monospace' }}>{ref.file}</span>
                      <span style={{ fontSize: 11, color: '#c7c4d7', fontFamily: 'Inter, sans-serif' }}>{ref.rel}</span>
                    </div>
                    <span className="material-symbols-outlined" style={{ fontSize: 16, color: '#908fa0' }}>arrow_forward</span>
                  </li>
                ))}
                <li
                  className="cursor-pointer transition-colors text-center"
                  style={{ background: '#151921', padding: '8px 12px' }}
                  onMouseEnter={e => (e.currentTarget.style.background = 'rgba(255,255,255,0.03)')}
                  onMouseLeave={e => (e.currentTarget.style.background = '#151921')}
                >
                  <span style={{ fontSize: 12, color: '#c0c1ff', fontWeight: 500, fontFamily: 'Inter, sans-serif' }}>View all 12 references</span>
                </li>
              </ul>
            </section>
          </div>

          {/* Footer actions */}
          <div
            className="flex gap-2 p-4"
            style={{ borderTop: '1px solid rgba(255,255,255,0.07)', background: 'rgba(255,255,255,0.02)' }}
          >
            <button
              className="flex-1 flex items-center justify-center gap-1 py-2 rounded text-sm font-medium transition-colors"
              style={{ border: '1px solid #464554', color: '#ffffff' }}
              onMouseEnter={e => (e.currentTarget.style.borderColor = '#ffffff')}
              onMouseLeave={e => (e.currentTarget.style.borderColor = '#464554')}
            >
              <span className="material-symbols-outlined" style={{ fontSize: 18 }}>history</span>
              History
            </button>
            <button
              className="flex-1 flex items-center justify-center gap-1 py-2 rounded text-sm font-medium transition-opacity"
              style={{ background: '#494bd6', color: '#ffffff', boxShadow: '0 0 10px rgba(73,75,214,0.3)' }}
              onMouseEnter={e => (e.currentTarget.style.opacity = '0.85')}
              onMouseLeave={e => (e.currentTarget.style.opacity = '1')}
            >
              <span className="material-symbols-outlined" style={{ fontSize: 18 }}>code</span>
              View Code
            </button>
          </div>
        </aside>
      )}
    </div>
  );
}

// ─── Exported page (wraps with provider for useReactFlow hook) ───────────────
export function KnowledgeGraphPage() {
  return (
    <ReactFlowProvider>
      <GraphInner />
    </ReactFlowProvider>
  );
}
