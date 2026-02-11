import { Button } from '@/components/ui/button';
import { Moon, Sun } from 'lucide-react';
import { toggleTheme } from '@/lib/theme';

interface LayoutProps {
  children: React.ReactNode;
}

export function Layout({ children }: LayoutProps) {
  return (
    <div className="min-h-screen">
      <header className="border-b border-[var(--color-border)] px-6 py-4 flex items-center justify-between">
        <h1 className="text-lg font-semibold tracking-tight">Smart Docs</h1>
        <Button
          variant="ghost"
          size="icon"
          onClick={toggleTheme}
          aria-label="Toggle theme"
        >
          <Sun className="h-5 w-5 dark:hidden" />
          <Moon className="h-5 w-5 hidden dark:block" />
        </Button>
      </header>
      <main>{children}</main>
    </div>
  );
}
