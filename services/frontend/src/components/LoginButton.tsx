import { LogIn, LogOut, User } from 'lucide-react';
import { Button } from '@/components/ui/button';
import type { User as SupabaseUser } from '@supabase/supabase-js';

interface LoginButtonProps {
  user: SupabaseUser | null;
  onSignIn: () => void;
  onSignOut: () => void;
}

export function LoginButton({ user, onSignIn, onSignOut }: LoginButtonProps) {
  if (user) {
    return (
      <div className="flex items-center gap-3">
        {/* User avatar or icon */}
        <div className="flex items-center gap-2 text-sm text-[var(--color-muted-foreground)]">
          {user.user_metadata?.avatar_url ? (
            <img
              src={user.user_metadata.avatar_url}
              alt={user.user_metadata?.name || 'User'}
              className="h-6 w-6 rounded-full"
            />
          ) : (
            <User className="h-5 w-5" />
          )}
          <span className="hidden sm:inline max-w-[120px] truncate">
            {user.user_metadata?.name || user.email || 'User'}
          </span>
        </div>

        {/* Sign out button */}
        <Button
          variant="ghost"
          size="sm"
          onClick={onSignOut}
          className="gap-1.5 text-[var(--color-muted-foreground)] hover:text-[var(--color-foreground)]"
        >
          <LogOut className="h-4 w-4" />
          <span className="hidden sm:inline">Sign out</span>
        </Button>
      </div>
    );
  }

  return (
    <Button
      variant="ghost"
      size="sm"
      onClick={onSignIn}
      className="gap-1.5"
    >
      <LogIn className="h-4 w-4" />
      Sign in
    </Button>
  );
}
