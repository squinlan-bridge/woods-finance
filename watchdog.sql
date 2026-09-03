-- ─────────────────────────────────────────────────────────────────────────
-- The Woods — Supabase pg_cron sync watchdog
-- ─────────────────────────────────────────────────────────────────────────
-- GitHub free-tier scheduled workflows run 1.5-2h late and are sometimes
-- dropped entirely (observed repeatedly on Bridge). This watchdog re-fires
-- the sync workflow via the GitHub API when heartbeats go stale.
--
-- SETUP (run in the Supabase SQL Editor on the WOODS project, zeewewahazzuzofhvwuu):
--   1. Create a GitHub fine-grained PAT:
--      github.com → Settings → Developer settings → Fine-grained tokens
--        Repository access: ONLY squinlan-bridge/woods-finance
--        Permissions: Actions → Read and write. Nothing else.
--        Expiration: 1 year (put the renewal date in your calendar).
--   2. Store it in Vault — paste your token into this line and run it:
--        select vault.create_secret('<YOUR_PAT_HERE>', 'github_dispatch_pat');
--   3. Run the rest of this file.
--
-- Thresholds (differ from Bridge because Woods has ONE daily cron, not two):
--   * Hourly staleness check: 26h. Normal gap between successful runs is
--     ~24h ± GitHub lag; anything tighter would re-fire every night and burn
--     shared account minutes. 26h only trips when the daily cron was dropped.
--   * Daily deadline check at 11:00 UTC (6am CT): if no sync has landed
--     since 08:00 UTC today (cron is 08:20 UTC), force a re-fire regardless
--     of the 26h rule.

create extension if not exists pg_cron;
create extension if not exists pg_net;

-- ── Re-fire function ──────────────────────────────────────────────────────
create or replace function public.refire_sync_if_stale(force boolean default false)
returns text
language plpgsql
security definer
set search_path to 'public'
as $function$
declare
  oldest timestamptz;
  token  text;
begin
  select min(last_run_at) into oldest from sync_freshness;

  -- 26h threshold: single daily cron → normal max gap ~24h + lag. See header.
  if not force and (oldest is null or oldest > now() - interval '26 hours') then
    return 'fresh (oldest ' || coalesce(oldest::text, 'n/a') || ') — no action';
  end if;

  select decrypted_secret into token
  from vault.decrypted_secrets
  where name = 'github_dispatch_pat';

  if token is null then
    return 'STALE but no github_dispatch_pat secret in Vault — cannot re-fire';
  end if;

  perform net.http_post(
    url     := 'https://api.github.com/repos/squinlan-bridge/woods-finance/actions/workflows/sync.yml/dispatches',
    headers := jsonb_build_object(
      'Authorization',        'Bearer ' || token,
      'Accept',               'application/vnd.github+json',
      'X-GitHub-Api-Version', '2022-11-28',
      'User-Agent',           'woods-sync-watchdog',
      'Content-Type',         'application/json'
    ),
    body    := jsonb_build_object('ref', 'main')
  );

  return 'dispatch requested (oldest feed ' || coalesce(oldest::text, 'n/a') || ')';
end $function$;

-- ── Daily deadline check ──────────────────────────────────────────────────
create or replace function public.deadline_sync_check()
returns text
language plpgsql
security definer
set search_path to 'public'
as $function$
declare
  oldest timestamptz;
begin
  select min(last_run_at) into oldest from sync_freshness;
  -- Cron fires 08:20 UTC; if every feed has synced since 08:00 UTC today,
  -- this morning's run landed. Otherwise force a re-fire.
  if oldest is not null and oldest >= date_trunc('day', now()) + interval '8 hours' then
    return 'synced this morning (' || oldest::text || ') — no action';
  end if;
  return public.refire_sync_if_stale(force => true);
end $function$;

-- ── Lock down: only postgres/cron may execute (functions are SECURITY
-- DEFINER and read Vault — must not be callable through the Data API).
revoke execute on function public.refire_sync_if_stale(boolean) from public, anon, authenticated;
revoke execute on function public.deadline_sync_check() from public, anon, authenticated;

-- ── Schedules ─────────────────────────────────────────────────────────────
select cron.schedule('woods-sync-watchdog', '45 * * * *', 'select public.refire_sync_if_stale()');
select cron.schedule('woods-sync-deadline', '0 11 * * *', 'select public.deadline_sync_check()');

-- ── Verify ────────────────────────────────────────────────────────────────
-- select jobname, schedule, command from cron.job;
-- select public.refire_sync_if_stale();  -- should say 'fresh … no action' after first sync
