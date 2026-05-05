import { useState } from 'react';
import { useAuth } from '@/hooks/useAuth';

export function LoginPage() {
  const { signInWithGitHub } = useAuth();
  const [email, setEmail] = useState('');

  return (
    <div
      className="min-h-screen flex items-center justify-center relative overflow-hidden antialiased"
      style={{
        background: '#0B0E14',
        backgroundImage: `
          linear-gradient(to right, rgba(255,255,255,0.03) 1px, transparent 1px),
          linear-gradient(to bottom, rgba(255,255,255,0.03) 1px, transparent 1px)
        `,
        backgroundSize: '24px 24px',
        fontFamily: 'Inter, sans-serif',
        color: '#e4e1ed',
      }}
    >
      {/* Decorative glow orbs */}
      <div
        className="absolute top-1/4 left-1/4 w-96 h-96 rounded-full pointer-events-none"
        style={{ background: 'rgba(192,193,255,0.06)', filter: 'blur(120px)' }}
      />
      <div
        className="absolute bottom-1/4 right-1/4 w-64 h-64 rounded-full pointer-events-none"
        style={{ background: 'rgba(78,222,163,0.05)', filter: 'blur(100px)' }}
      />

      <main className="relative z-10 w-full max-w-md px-6">
        {/* Login card — Level 2 glassmorphism overlay */}
        <div
          className="flex flex-col gap-6 p-8 shadow-2xl"
          style={{
            background: 'rgba(21,25,33,0.80)',
            backdropFilter: 'blur(20px)',
            border: '1px solid rgba(255,255,255,0.10)',
            borderRadius: 12,
          }}
        >
          {/* Header */}
          <div className="text-center space-y-2">
            <div className="flex justify-center mb-4" style={{ color: '#c0c1ff' }}>
              <span className="material-symbols-outlined" style={{ fontSize: 48 }}>
                account_tree
              </span>
            </div>
            <h1
              className="font-semibold"
              style={{ fontSize: 32, lineHeight: 1.2, letterSpacing: '-0.02em', color: '#e4e1ed', fontFamily: 'Inter, sans-serif' }}
            >
              RAG Explorer
            </h1>
            <p
              style={{ fontSize: 13, lineHeight: 1.5, color: '#c7c4d7', fontFamily: 'Fira Code, monospace' }}
            >
              Welcome back, developer.
            </p>
          </div>

          {/* OAuth buttons */}
          <div className="space-y-2">
            <button
              onClick={signInWithGitHub}
              className="w-full flex items-center justify-center gap-2 px-4 py-3 transition-colors"
              style={{
                background: '#34343d',
                border: '1px solid rgba(255,255,255,0.05)',
                borderRadius: 4,
                color: '#e4e1ed',
                fontSize: 14,
                fontFamily: 'Inter, sans-serif',
              }}
              onMouseEnter={e => (e.currentTarget.style.background = 'rgba(255,255,255,0.05)')}
              onMouseLeave={e => (e.currentTarget.style.background = '#34343d')}
            >
              {/* GitHub SVG */}
              <svg className="w-5 h-5 fill-current" viewBox="0 0 24 24">
                <path d="M12 .297c-6.63 0-12 5.373-12 12 0 5.303 3.438 9.8 8.205 11.385.6.113.82-.258.82-.577 0-.285-.01-1.04-.015-2.04-3.338.724-4.042-1.61-4.042-1.61C4.422 18.07 3.633 17.7 3.633 17.7c-1.087-.744.084-.729.084-.729 1.205.084 1.838 1.236 1.838 1.236 1.07 1.835 2.809 1.305 3.495.998.108-.776.417-1.305.76-1.605-2.665-.3-5.466-1.332-5.466-5.93 0-1.31.465-2.38 1.235-3.22-.135-.303-.54-1.523.105-3.176 0 0 1.005-.322 3.3 1.23.96-.267 1.98-.399 3-.405 1.02.006 2.04.138 3 .405 2.28-1.552 3.285-1.23 3.285-1.23.645 1.653.24 2.873.12 3.176.765.84 1.23 1.91 1.23 3.22 0 4.61-2.805 5.625-5.475 5.92.42.36.81 1.096.81 2.22 0 1.606-.015 2.896-.015 3.286 0 .315.21.69.825.57C20.565 22.092 24 17.592 24 12.297c0-6.627-5.373-12-12-12" />
              </svg>
              <span className="font-medium">Continue with GitHub</span>
            </button>

            <button
              className="w-full flex items-center justify-center gap-2 px-4 py-3 transition-colors"
              style={{
                background: '#34343d',
                border: '1px solid rgba(255,255,255,0.05)',
                borderRadius: 4,
                color: '#e4e1ed',
                fontSize: 14,
                fontFamily: 'Inter, sans-serif',
              }}
              onMouseEnter={e => (e.currentTarget.style.background = 'rgba(255,255,255,0.05)')}
              onMouseLeave={e => (e.currentTarget.style.background = '#34343d')}
            >
              {/* Google SVG */}
              <svg className="w-5 h-5 fill-current" viewBox="0 0 24 24">
                <path d="M12.48 10.92v3.28h7.84c-.24 1.84-.853 3.187-1.787 4.133-1.147 1.147-2.933 2.4-6.053 2.4-4.827 0-8.6-3.893-8.6-8.72s3.773-8.72 8.6-8.72c2.6 0 4.507 1.027 5.907 2.347l2.307-2.307C18.747 1.44 16.133 0 12.48 0 5.867 0 .307 5.387.307 12s5.56 12 12.173 12c3.573 0 6.267-1.173 8.373-3.36 2.16-2.16 2.84-5.213 2.84-7.667 0-.76-.053-1.467-.173-2.053H12.48z" />
              </svg>
              <span className="font-medium">Continue with Google</span>
            </button>
          </div>

          {/* OR divider */}
          <div className="flex items-center gap-4">
            <div className="h-px flex-1" style={{ background: 'rgba(255,255,255,0.10)' }} />
            <span
              style={{ fontSize: 11, letterSpacing: '0.08em', fontWeight: 700, color: '#c7c4d7', fontFamily: 'Space Grotesk, sans-serif', textTransform: 'uppercase' }}
            >
              or
            </span>
            <div className="h-px flex-1" style={{ background: 'rgba(255,255,255,0.10)' }} />
          </div>

          {/* Magic link form */}
          <form className="space-y-4" onSubmit={e => e.preventDefault()}>
            <div className="space-y-1">
              <label
                className="block uppercase"
                style={{ fontSize: 11, letterSpacing: '0.08em', fontWeight: 700, color: '#c7c4d7', fontFamily: 'Space Grotesk, sans-serif' }}
              >
                Email Address
              </label>
              <input
                type="email"
                placeholder="dev@example.com"
                value={email}
                onChange={e => setEmail(e.target.value)}
                className="w-full outline-none transition-colors"
                style={{
                  background: '#0B0E14',
                  border: '1px solid rgba(255,255,255,0.10)',
                  borderRadius: 4,
                  padding: '8px 12px',
                  color: '#e4e1ed',
                  fontSize: 13,
                  fontFamily: 'Fira Code, monospace',
                }}
                onFocus={e => (e.currentTarget.style.borderColor = '#c0c1ff')}
                onBlur={e => (e.currentTarget.style.borderColor = 'rgba(255,255,255,0.10)')}
              />
            </div>
            <button
              type="button"
              className="w-full px-4 py-2 font-medium transition-colors"
              style={{
                background: '#c0c1ff',
                color: '#1000a9',
                borderRadius: 4,
                fontSize: 14,
                fontFamily: 'Inter, sans-serif',
              }}
              onMouseEnter={e => (e.currentTarget.style.background = '#e1e0ff')}
              onMouseLeave={e => (e.currentTarget.style.background = '#c0c1ff')}
            >
              Send Magic Link
            </button>
          </form>

          {/* Request access */}
          <div className="text-center">
            <a
              href="#"
              className="transition-colors"
              style={{ fontSize: 12, color: '#c0c1ff', fontFamily: 'Fira Code, monospace' }}
              onMouseEnter={e => (e.currentTarget.style.color = '#e1e0ff')}
              onMouseLeave={e => (e.currentTarget.style.color = '#c0c1ff')}
            >
              Request Access
            </a>
          </div>
        </div>

        {/* Footer version */}
        <div className="mt-8 text-center">
          <p style={{ fontSize: 12, color: '#908fa0', fontFamily: 'Fira Code, monospace' }}>
            v1.2.0-stable • System Nominal
          </p>
        </div>
      </main>
    </div>
  );
}
