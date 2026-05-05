import { useNavigate } from 'react-router-dom';

const ORGS = [
  { icon: 'domain',  name: 'Acme Corp',         handle: 'org_x9a8f2m', members: 124, repos: 42,  active: true  },
  { icon: 'person',  name: 'Personal Projects',  handle: 'user_jdoe99', members: 1,   repos: 8,   active: false },
];

const ACTIVITY = [
  { icon: 'sync',           color: 'rgba(192,193,255,0.15)', iconColor: '#c0c1ff', border: 'rgba(192,193,255,0.2)', text: 'Index refresh completed', sub: 'acme-corp/nebula-core', time: '2m ago' },
  { icon: 'person_add',     color: 'rgba(78,222,163,0.12)',  iconColor: '#4edea3', border: 'rgba(78,222,163,0.2)',  text: 'New member joined',       sub: 's.miller@acme.io',     time: '1h ago' },
  { icon: 'error_outline',  color: 'rgba(255,180,171,0.12)', iconColor: '#ffb4ab', border: 'rgba(255,180,171,0.2)',text: 'Indexing failed',          sub: 'legacy-monolith',      time: '3h ago' },
  { icon: 'model_training', color: 'rgba(192,193,255,0.15)', iconColor: '#c0c1ff', border: 'rgba(192,193,255,0.2)', text: 'Model update deployed',   sub: 'text-embedding-3-small', time: '1d ago' },
];

export function OrgSelectPage() {
  const navigate = useNavigate();

  return (
    <div className="flex flex-1 min-h-0 overflow-hidden" style={{ background: '#13131b' }}>
      {/* Main content: org grid */}
      <div className="flex-1 overflow-y-auto p-6 flex flex-col gap-6">
        {/* Header */}
        <div className="flex flex-col gap-1">
          <h1
            className="font-semibold"
            style={{ fontSize: 32, lineHeight: 1.2, letterSpacing: '-0.02em', color: '#e4e1ed', fontFamily: 'Inter, sans-serif' }}
          >
            Select Workspace
          </h1>
          <p style={{ fontSize: 14, color: '#c7c4d7', fontFamily: 'Inter, sans-serif' }}>
            Choose an organization to view repositories and team activity.
          </p>
        </div>

        {/* Org cards grid */}
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
          {ORGS.map(org => (
            <button
              key={org.name}
              onClick={() => navigate('/search')}
              className="group relative flex flex-col text-left transition-all duration-200 overflow-hidden"
              style={{
                background: '#1f1f27',
                border: '1px solid #464554',
                borderRadius: 12,
                padding: 16,
                cursor: 'pointer',
              }}
              onMouseEnter={e => {
                (e.currentTarget as HTMLElement).style.background = 'rgba(255,255,255,0.03)';
                (e.currentTarget as HTMLElement).style.borderColor = '#908fa0';
              }}
              onMouseLeave={e => {
                (e.currentTarget as HTMLElement).style.background = '#1f1f27';
                (e.currentTarget as HTMLElement).style.borderColor = '#464554';
              }}
            >
              {/* Top row: icon + active badge */}
              <div className="flex justify-between items-start mb-4">
                <div
                  className="w-12 h-12 rounded flex items-center justify-center transition-colors"
                  style={{ background: '#34343d', border: '1px solid #464554' }}
                >
                  <span
                    className="material-symbols-outlined"
                    style={{ fontSize: 24, color: '#c7c4d7' }}
                  >
                    {org.icon}
                  </span>
                </div>
                {org.active && (
                  <div
                    className="flex items-center gap-1 px-2 py-1 rounded"
                    style={{ background: '#292932', border: '1px solid #464554' }}
                  >
                    <span className="w-2 h-2 rounded-full" style={{ background: '#4edea3' }} />
                    <span style={{ fontSize: 12, color: '#c7c4d7', fontFamily: 'Fira Code, monospace' }}>Active</span>
                  </div>
                )}
              </div>

              {/* Name + handle */}
              <div className="flex-1">
                <h2
                  className="font-semibold mb-1"
                  style={{ fontSize: 24, lineHeight: 1.3, letterSpacing: '-0.01em', color: '#e4e1ed', fontFamily: 'Inter, sans-serif' }}
                >
                  {org.name}
                </h2>
                <p
                  className="mb-4"
                  style={{ fontSize: 13, color: '#c7c4d7', fontFamily: 'Fira Code, monospace' }}
                >
                  {org.handle}
                </p>

                {/* Stats */}
                <div className="flex gap-2">
                  <div
                    className="flex items-center gap-1 px-2 py-1 rounded"
                    style={{ background: '#292932', border: '1px solid rgba(70,69,84,0.5)' }}
                  >
                    <span className="material-symbols-outlined" style={{ fontSize: 14, color: '#c7c4d7' }}>group</span>
                    <span style={{ fontSize: 12, color: '#e4e1ed', fontFamily: 'Fira Code, monospace' }}>{org.members}</span>
                  </div>
                  <div
                    className="flex items-center gap-1 px-2 py-1 rounded"
                    style={{ background: '#292932', border: '1px solid rgba(70,69,84,0.5)' }}
                  >
                    <span className="material-symbols-outlined" style={{ fontSize: 14, color: '#c7c4d7' }}>source</span>
                    <span style={{ fontSize: 12, color: '#e4e1ed', fontFamily: 'Fira Code, monospace' }}>{org.repos} Repos</span>
                  </div>
                </div>
              </div>
            </button>
          ))}

          {/* Create / Join card */}
          <button
            className="group relative flex flex-col items-center justify-center text-center transition-all duration-200 min-h-[200px]"
            style={{
              background: '#13131b',
              border: '1px dashed #464554',
              borderRadius: 12,
              padding: 16,
              cursor: 'pointer',
            }}
            onMouseEnter={e => {
              (e.currentTarget as HTMLElement).style.borderColor = '#c0c1ff';
              (e.currentTarget as HTMLElement).style.background = 'rgba(192,193,255,0.04)';
            }}
            onMouseLeave={e => {
              (e.currentTarget as HTMLElement).style.borderColor = '#464554';
              (e.currentTarget as HTMLElement).style.background = '#13131b';
            }}
          >
            <div
              className="w-12 h-12 rounded-full flex items-center justify-center mb-2 transition-colors"
              style={{ background: '#1f1f27', border: '1px solid #464554' }}
            >
              <span className="material-symbols-outlined" style={{ fontSize: 24, color: '#c7c4d7' }}>add</span>
            </div>
            <h3
              className="font-medium mb-1"
              style={{ fontSize: 14, color: '#e4e1ed', fontFamily: 'Inter, sans-serif' }}
            >
              Join or Create Organization
            </h3>
            <p
              className="max-w-[80%]"
              style={{ fontSize: 12, color: '#c7c4d7', fontFamily: 'Fira Code, monospace' }}
            >
              Set up a new workspace or enter an invite code.
            </p>
          </button>
        </div>
      </div>

      {/* Right: Activity Inspector panel */}
      <aside
        className="hidden lg:flex flex-col"
        style={{
          width: 320,
          background: '#1b1b23',
          borderLeft: '1px solid #464554',
          flexShrink: 0,
        }}
      >
        {/* Panel header */}
        <div
          className="flex items-center px-4"
          style={{
            height: 56,
            borderBottom: '1px solid #464554',
            background: 'rgba(255,255,255,0.02)',
          }}
        >
          <span
            className="uppercase tracking-wider"
            style={{ fontSize: 11, fontWeight: 700, letterSpacing: '0.08em', color: '#c7c4d7', fontFamily: 'Space Grotesk, sans-serif' }}
          >
            Global Activity
          </span>
        </div>

        {/* Activity items */}
        <div className="flex-1 overflow-y-auto p-4 flex flex-col gap-4">
          {ACTIVITY.map((item, i) => (
            <div key={i} className="flex gap-2">
              <div className="mt-0.5">
                <div
                  className="w-6 h-6 rounded-full flex items-center justify-center"
                  style={{ background: item.color, border: `1px solid ${item.border}` }}
                >
                  <span className="material-symbols-outlined" style={{ fontSize: 12, color: item.iconColor }}>
                    {item.icon}
                  </span>
                </div>
              </div>
              <div className="flex-1 min-w-0">
                <p style={{ fontSize: 13, color: '#e4e1ed', fontFamily: 'Inter, sans-serif', lineHeight: 1.4 }}>
                  {item.text}
                </p>
                <p
                  className="truncate"
                  style={{ fontSize: 12, color: '#c7c4d7', fontFamily: 'Fira Code, monospace' }}
                >
                  {item.sub}
                </p>
                <p style={{ fontSize: 11, color: '#908fa0', fontFamily: 'Inter, sans-serif', marginTop: 2 }}>
                  {item.time}
                </p>
              </div>
            </div>
          ))}
        </div>
      </aside>
    </div>
  );
}
