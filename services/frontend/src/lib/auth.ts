import { createClient, SupabaseClient } from '@supabase/supabase-js';

const supabaseUrl = import.meta.env.VITE_SUPABASE_URL;
const supabaseAnonKey = import.meta.env.VITE_SUPABASE_ANON_KEY;

// Check if we have valid Supabase credentials
export const isAuthConfigured = Boolean(
  supabaseUrl &&
  supabaseAnonKey &&
  supabaseUrl.startsWith('http')
);

// Create Supabase client (use dummy values for dev mode to prevent errors)
export const supabase: SupabaseClient = isAuthConfigured
  ? createClient(supabaseUrl, supabaseAnonKey)
  : createClient('https://dev.supabase.co', 'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImRldiIsInJvbGUiOiJhbm9uIiwiaWF0IjoxNjQxNzY5MjAwLCJleHAiOjE5NTczNDUyMDB9.dc6hdC09uR0D5JH4Sj9C9tDuZJR5Fj-Ou0SrxGO5E3M', {
      auth: {
        autoRefreshToken: false,
        persistSession: false,
        detectSessionInUrl: false,
      },
    });

/**
 * Sign in with GitHub OAuth
 */
export async function signInWithGitHub() {
  if (!isAuthConfigured) {
    console.warn('Supabase not configured - skipping GitHub OAuth');
    return { data: null, error: new Error('Auth not configured') };
  }

  const { data, error } = await supabase.auth.signInWithOAuth({
    provider: 'github',
    options: {
      redirectTo: window.location.origin,
    },
  });
  return { data, error };
}

/**
 * Sign in with GitLab OAuth
 */
export async function signInWithGitLab() {
  if (!isAuthConfigured) {
    console.warn('Supabase not configured - skipping GitLab OAuth');
    return { data: null, error: new Error('Auth not configured') };
  }

  const { data, error } = await supabase.auth.signInWithOAuth({
    provider: 'gitlab',
    options: {
      redirectTo: window.location.origin,
    },
  });
  return { data, error };
}

/**
 * Sign out the current user
 */
export async function signOut() {
  if (!isAuthConfigured) {
    return { error: null };
  }

  const { error } = await supabase.auth.signOut();
  return { error };
}

/**
 * Get the current session
 */
export async function getSession() {
  if (!isAuthConfigured) {
    return { data: { session: null }, error: null };
  }

  const { data, error } = await supabase.auth.getSession();
  return { data, error };
}
