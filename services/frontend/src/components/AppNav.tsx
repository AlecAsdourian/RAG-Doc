import { NavLink, useNavigate } from 'react-router-dom';
import { useAuth } from '@/hooks/useAuth';

// Nav items matching the Stitch reference SideNavBar
const NAV_ITEMS = [
  { to: '/org',      icon: 'workspaces',    label: 'Organization'   },
  { to: '/search',   icon: 'search',        label: 'Explorer'       },
  { to: '/graph',    icon: 'account_tree',  label: 'GraphView'      },
  { to: '/docs',     icon: 'description',   label: 'Documentation'  },
  { to: '/chat',     icon: 'forum',         label: 'Chat'           },
  { to: '/settings', icon: 'settings',      label: 'Settings'       },
];

export function AppNav() {
  const { user, signOut } = useAuth();
  const navigate = useNavigate();

  const handleSignOut = async () => {
    await signOut();
    navigate('/login');
  };

  return (
    <nav
      className="flex h-screen flex-col shrink-0"
      style={{
        width: 260,
        background: '#151921',
        borderRight: '1px solid rgba(255,255,255,0.05)',
      }}
    >
      {/* Brand header */}
      <div
        className="flex items-center gap-3 px-4 py-5"
        style={{ borderBottom: '1px solid rgba(255,255,255,0.05)' }}
      >
        <span
          className="material-symbols-outlined"
          style={{ color: '#c0c1ff', fontSize: 24 }}
        >
          account_tree
        </span>
        <span
          className="text-sm font-semibold tracking-tight"
          style={{ color: '#c0c1ff', fontFamily: 'Space Grotesk, sans-serif' }}
        >
          RAG Explorer
        </span>
      </div>

      {/* Nav links */}
      <div className="flex-1 overflow-y-auto py-3 space-y-0.5">
        {NAV_ITEMS.map(({ to, icon, label }) => (
          <NavLink
            key={to}
            to={to}
            className={({ isActive }) =>
              isActive ? 'flex items-center gap-3 px-4 py-2.5 text-sm transition-colors' : 'flex items-center gap-3 px-4 py-2.5 text-sm transition-colors'
            }
            style={({ isActive }) =>
              isActive
                ? {
                    background: 'rgba(192,193,255,0.08)',
                    borderLeft: '2px solid #c0c1ff',
                    paddingLeft: 14,
                    color: '#c0c1ff',
                  }
                : { color: '#c7c4d7' }
            }
          >
            {({ isActive }) => (
              <>
                <span
                  className="material-symbols-outlined shrink-0"
                  style={{ fontSize: 20, color: isActive ? '#c0c1ff' : '#908fa0' }}
                >
                  {icon}
                </span>
                <span style={{ fontFamily: 'Inter, sans-serif' }}>{label}</span>
              </>
            )}
          </NavLink>
        ))}
      </div>

      {/* New Analysis CTA */}
      <div className="px-3 pb-3" style={{ borderTop: '1px solid rgba(255,255,255,0.05)', paddingTop: 12 }}>
        <button
          className="w-full flex items-center justify-center gap-2 py-2.5 px-4 rounded text-sm font-medium transition-opacity"
          style={{ background: '#494bd6', color: '#ffffff', fontFamily: 'Inter, sans-serif' }}
          onMouseEnter={e => (e.currentTarget.style.opacity = '0.85')}
          onMouseLeave={e => (e.currentTarget.style.opacity = '1')}
        >
          <span className="material-symbols-outlined" style={{ fontSize: 18 }}>add</span>
          New Analysis
        </button>

        {/* User + sign out */}
        <div className="mt-3 flex items-center gap-2 px-2 py-1">
          <div
            className="flex h-7 w-7 shrink-0 items-center justify-center rounded-full text-xs font-semibold"
            style={{ background: 'rgba(192,193,255,0.12)', color: '#c0c1ff' }}
          >
            {user?.email?.[0]?.toUpperCase() ?? 'U'}
          </div>
          <span className="flex-1 truncate text-xs" style={{ color: '#908fa0' }}>
            {user?.email ?? 'dev@example.com'}
          </span>
          <button
            onClick={handleSignOut}
            className="shrink-0"
            title="Sign out"
            style={{ color: '#908fa0' }}
            onMouseEnter={e => ((e.currentTarget as HTMLElement).style.color = '#e4e1ed')}
            onMouseLeave={e => ((e.currentTarget as HTMLElement).style.color = '#908fa0')}
          >
            <span className="material-symbols-outlined" style={{ fontSize: 18 }}>logout</span>
          </button>
        </div>
      </div>
    </nav>
  );
}
