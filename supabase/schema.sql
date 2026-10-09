-- DineWolfie accounts: each signed-in person's memory (preferences + past plans), one row per user.
--
-- Run this once in your Supabase project: Dashboard -> SQL Editor -> New query -> paste -> Run.
-- Safe to run again; it only creates what's missing and resets the policies.
--
-- Security model: the web app uses the public "publishable" key, so the rules that keep people
-- apart live here, in the database. Row Level Security lets a signed-in user read and write only
-- the row whose user_id is their own Supabase user id. Visitors who aren't signed in get nothing.

create table if not exists public.dinewolfie_memory (
  user_id    uuid primary key references auth.users (id) on delete cascade,
  prefs      jsonb not null default '{}'::jsonb,
  history    jsonb not null default '[]'::jsonb,
  updated_at timestamptz not null default now()
);

alter table public.dinewolfie_memory enable row level security;

drop policy if exists "read own memory" on public.dinewolfie_memory;
drop policy if exists "add own memory" on public.dinewolfie_memory;
drop policy if exists "change own memory" on public.dinewolfie_memory;

create policy "read own memory" on public.dinewolfie_memory
  for select to authenticated
  using ((select auth.uid()) = user_id);

create policy "add own memory" on public.dinewolfie_memory
  for insert to authenticated
  with check ((select auth.uid()) = user_id);

-- UPDATE needs both: USING picks rows you may change, WITH CHECK stops you handing a row to someone else.
create policy "change own memory" on public.dinewolfie_memory
  for update to authenticated
  using ((select auth.uid()) = user_id)
  with check ((select auth.uid()) = user_id);

-- Table access for the Data API (newer projects don't grant it automatically).
-- Signed-out visitors (anon) get none; signed-in users get read/insert/update, filtered by the policies above.
-- Start from nothing, then grant only what the app uses (no delete, no truncate).
revoke all on public.dinewolfie_memory from anon, authenticated;
grant select, insert, update on public.dinewolfie_memory to authenticated;

-- Keep-alive: free Supabase projects pause after about a week without database activity.
-- The scheduled GitHub job .github/workflows/supabase-keepalive.yml calls this every 3 days.
-- It reads no data: it only returns the current time.
create or replace function public.dinewolfie_ping()
returns timestamptz
language sql
stable
security invoker
set search_path = ''
as $$ select now() $$;

revoke all on function public.dinewolfie_ping() from public;
grant execute on function public.dinewolfie_ping() to anon, authenticated;
