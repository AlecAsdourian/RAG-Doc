-- =============================================================================
-- Supabase User Provisioning Trigger
-- =============================================================================
-- This script creates:
-- 1. An intermediate table (auth_user_events) to capture auth.users events
-- 2. A trigger function that fires when users are created in auth.users
-- 3. A trigger that calls the function on INSERT
--
-- Why this approach:
-- - Supabase doesn't allow webhooks directly on auth.users (protected schema)
-- - This trigger copies events to a public table that CAN have webhooks
-- - Your backend webhook receives events from auth_user_events
-- =============================================================================

-- Step 1: Create the intermediate events table
-- This table captures user creation events from auth.users
CREATE TABLE IF NOT EXISTS public.auth_user_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    supabase_user_id UUID NOT NULL,
    email TEXT,
    raw_user_meta_data JSONB,
    event_type TEXT NOT NULL DEFAULT 'INSERT',
    created_at TIMESTAMPTZ DEFAULT NOW(),
    processed_at TIMESTAMPTZ  -- Set by your backend after processing (optional)
);

-- Add comment explaining the table's purpose
COMMENT ON TABLE public.auth_user_events IS
    'Captures auth.users events for webhook processing. Populated by trigger on auth.users.';

-- Create index for faster lookups (optional, for debugging/auditing)
CREATE INDEX IF NOT EXISTS idx_auth_user_events_supabase_user_id
    ON public.auth_user_events(supabase_user_id);

CREATE INDEX IF NOT EXISTS idx_auth_user_events_created_at
    ON public.auth_user_events(created_at DESC);

-- Step 2: Create the trigger function
-- SECURITY DEFINER: Runs with the privileges of the function creator (service role)
-- This is required because the trigger fires from Supabase Auth's internal operations
CREATE OR REPLACE FUNCTION public.on_auth_user_created()
RETURNS TRIGGER AS $$
BEGIN
    INSERT INTO public.auth_user_events (
        supabase_user_id,
        email,
        raw_user_meta_data,
        event_type
    ) VALUES (
        NEW.id,
        NEW.email,
        NEW.raw_user_meta_data,
        TG_OP  -- 'INSERT', 'UPDATE', or 'DELETE'
    );
    RETURN NEW;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

-- Add comment explaining the function
COMMENT ON FUNCTION public.on_auth_user_created() IS
    'Copies new user data from auth.users to public.auth_user_events for webhook processing.';

-- Step 3: Create the trigger on auth.users
-- Drop first if exists (for idempotent re-runs)
DROP TRIGGER IF EXISTS on_auth_user_created_trigger ON auth.users;

CREATE TRIGGER on_auth_user_created_trigger
    AFTER INSERT ON auth.users
    FOR EACH ROW
    EXECUTE FUNCTION public.on_auth_user_created();

-- =============================================================================
-- Verification Query (run after creating to confirm setup)
-- =============================================================================
-- SELECT
--     tgname AS trigger_name,
--     tgenabled AS enabled,
--     pg_get_triggerdef(oid) AS definition
-- FROM pg_trigger
-- WHERE tgrelid = 'auth.users'::regclass;

-- =============================================================================
-- Test: Create a test user to verify the trigger works
-- (Only run this in development!)
-- =============================================================================
-- INSERT INTO auth.users (
--     id,
--     email,
--     raw_user_meta_data,
--     created_at,
--     updated_at
-- ) VALUES (
--     gen_random_uuid(),
--     'test-trigger@example.com',
--     '{"full_name": "Test User", "provider": "email"}'::jsonb,
--     NOW(),
--     NOW()
-- );
--
-- Then check: SELECT * FROM public.auth_user_events;
