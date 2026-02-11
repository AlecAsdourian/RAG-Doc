import { createClient } from '@supabase/supabase-js';

const supabaseUrl = import.meta.env.VITE_SUPABASE_URL || '';
const supabaseAnonKey = import.meta.env.VITE_SUPABASE_ANON_KEY || '';

// Create Supabase client (will be initialized even without keys for dev mode)
export const supabase = createClient(supabaseUrl, supabaseAnonKey);

/**
 * Sign in with GitHub OAuth
 */
export async function signInWithGitHub() {
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
  const { error } = await supabase.auth.signOut();
  return { error };
}

/**
 * Get the current session
 */
export async function getSession() {
  const { data, error } = await supabase.auth.getSession();
  return { data, error };
}
