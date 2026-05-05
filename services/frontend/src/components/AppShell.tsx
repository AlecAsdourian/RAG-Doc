import { Outlet, useLocation } from 'react-router-dom';
import { AppNav } from '@/components/AppNav';

const PAGE_TITLES: Record<string, string> = {
  '/org':      'Organization',
  '/search':   'Explorer',
  '/graph':    'GraphView',
  '/docs':     'Documentation',
  '/chat':     'Chat',
  '/settings': 'Settings',
};

export function AppShell() {
  const { pathname } = useLocation();
  const pageTitle = PAGE_TITLES[pathname] ?? 'RAG Explorer';

  return (
    <div className="flex h-screen overflow-hidden" style={{ background: '#0B0E14' }}>
      <AppNav />

      {/* Right side: top bar + outlet */}
      <div className="flex flex-1 flex-col overflow-hidden min-h-0">
        {/* Top nav bar */}
        <header
          className="flex h-16 shrink-0 items-center justify-between px-6"
          style={{
            background: 'rgba(21,25,33,0.80)',
            backdropFilter: 'blur(16px)',
            borderBottom: '1px solid rgba(255,255,255,0.07)',
            fontFamily: 'Inter, sans-serif',
          }}
        >
          <span
            className="text-xs font-medium uppercase tracking-widest"
            style={{ color: '#c7c4d7' }}
          >
            {pageTitle}
          </span>

          <div className="flex items-center gap-4">
            <button
              className="transition-colors"
              style={{ color: '#908fa0' }}
              onMouseEnter={e => ((e.currentTarget as HTMLElement).style.color = '#c0c1ff')}
              onMouseLeave={e => ((e.currentTarget as HTMLElement).style.color = '#908fa0')}
            >
              <span className="material-symbols-outlined" style={{ fontSize: 20 }}>notifications</span>
            </button>
            <button
              className="transition-colors"
              style={{ color: '#908fa0' }}
              onMouseEnter={e => ((e.currentTarget as HTMLElement).style.color = '#c0c1ff')}
              onMouseLeave={e => ((e.currentTarget as HTMLElement).style.color = '#908fa0')}
            >
              <span className="material-symbols-outlined" style={{ fontSize: 20 }}>cloud_done</span>
            </button>
            <div className="h-4 w-px" style={{ background: 'rgba(255,255,255,0.10)' }} />
            <button
              className="text-sm font-medium transition-colors"
              style={{ color: '#494bd6' }}
              onMouseEnter={e => ((e.currentTarget as HTMLElement).style.color = '#8083ff')}
              onMouseLeave={e => ((e.currentTarget as HTMLElement).style.color = '#494bd6')}
            >
              Sync Status
            </button>
            <div
              className="h-8 w-8 rounded-full flex items-center justify-center text-xs font-semibold"
              style={{ background: '#292932', border: '1px solid rgba(255,255,255,0.10)', color: '#c0c1ff' }}
            >
              U
            </div>
          </div>
        </header>

        {/* Page content */}
        <div className="flex flex-1 overflow-hidden min-h-0">
          <Outlet />
        </div>
      </div>
    </div>
  );
}
