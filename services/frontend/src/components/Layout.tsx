import { Button } from '@/components/ui/button';
import { Moon, Sun } from 'lucide-react';
import { toggleTheme } from '@/lib/theme';
import { LoginButton } from '@/components/LoginButton';
import type { User } from '@supabase/supabase-js';

interface LayoutProps {
  children: React.ReactNode;
  user?: User | null;
  onSignIn?: () => void;
  onSignOut?: () => void;
  showAuth?: boolean;
}

export function Layout({
  children,
  user = null,
  onSignIn,
  onSignOut,
  showAuth = true,
}: LayoutProps) {
  return (
    <div className="min-h-screen">
      <header className="border-b border-[var(--color-border)] px-6 py-4 flex items-center justify-between">
        <h1 className="text-lg font-semibold tracking-tight">Smart Docs</h1>
        <div className="flex items-center gap-2">
          {showAuth && onSignIn && onSignOut && (
            <LoginButton user={user} onSignIn={onSignIn} onSignOut={onSignOut} />
          )}
          <Button
            variant="ghost"
            size="icon"
            onClick={toggleTheme}
            aria-label="Toggle theme"
          >
            <Sun className="h-5 w-5 dark:hidden" />
            <Moon className="h-5 w-5 hidden dark:block" />
          </Button>
        </div>
      </header>
      <main>{children}</main>
    </div>
  );
}
