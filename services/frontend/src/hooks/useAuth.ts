import { useState, useEffect, useCallback } from 'react';
import type { User, Session } from '@supabase/supabase-js';
import { supabase, signInWithGitHub, signInWithGitLab, signOut as authSignOut, isAuthConfigured } from '@/lib/auth';
import { apiClient } from '@/lib/api/client';

interface UseAuthResult {
  user: User | null;
  session: Session | null;
  loading: boolean;
  signInWithGitHub: () => Promise<void>;
  signInWithGitLab: () => Promise<void>;
  signOut: () => Promise<void>;
}

export function useAuth(): UseAuthResult {
  const [user, setUser] = useState<User | null>(null);
  const [session, setSession] = useState<Session | null>(null);
  const [loading, setLoading] = useState(true);

  // Update API client token when session changes
  const updateApiToken = useCallback((newSession: Session | null) => {
    if (newSession?.access_token) {
      apiClient.setToken(newSession.access_token);
    } else {
      apiClient.clearToken();
    }
  }, []);

  useEffect(() => {
    // If Supabase is not configured, skip auth
    if (!isAuthConfigured) {
      setLoading(false);
      return;
    }

    // Get initial session
    supabase.auth.getSession().then(({ data: { session: initialSession } }) => {
      setSession(initialSession);
      setUser(initialSession?.user ?? null);
      updateApiToken(initialSession);
      setLoading(false);
    }).catch((error) => {
      console.error('Error getting session:', error);
      setLoading(false);
    });

    // Listen for auth state changes
    const { data: { subscription } } = supabase.auth.onAuthStateChange(
      (_event, newSession) => {
        setSession(newSession);
        setUser(newSession?.user ?? null);
        updateApiToken(newSession);
      }
    );

    return () => {
      subscription.unsubscribe();
    };
  }, [updateApiToken]);

  const handleSignInWithGitHub = useCallback(async () => {
    const { error } = await signInWithGitHub();
    if (error) {
      console.error('GitHub sign in error:', error.message);
    }
  }, []);

  const handleSignInWithGitLab = useCallback(async () => {
    const { error } = await signInWithGitLab();
    if (error) {
      console.error('GitLab sign in error:', error.message);
    }
  }, []);

  const handleSignOut = useCallback(async () => {
    const { error } = await authSignOut();
    if (error) {
      console.error('Sign out error:', error.message);
    }
  }, []);

  return {
    user,
    session,
    loading,
    signInWithGitHub: handleSignInWithGitHub,
    signInWithGitLab: handleSignInWithGitLab,
    signOut: handleSignOut,
  };
}

/**
 * Check if auth is enabled (Supabase configured)
 */
export function isAuthEnabled(): boolean {
  return isAuthConfigured;
}
