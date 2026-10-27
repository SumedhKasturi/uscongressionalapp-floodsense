-- FloodSense Supabase schema. Run once in the Supabase SQL editor (safe to re-run).

-- Per-ZIP places data, one version per ZIP per build day (story 2).
create table if not exists public.places_versions (
  zip        text        not null check (zip ~ '^[0-9]{5}$'),
  version    date        not null,               -- build date (UTC), YYYY-MM-DD
  built_at   timestamptz not null default now(),
  status     jsonb       not null default '{}'::jsonb,  -- per-section statuses, copied out of data
  data       jsonb       not null,                      -- the whole places record
  primary key (zip, version)                          -- the app upserts on this key
);

-- The app uses the anon key: it may read, insert and update (upsert) versions, never delete.
-- With row-level security on, a role can only do what a policy below allows. There is
-- deliberately no delete policy: the anon key ships with the app, so anyone could use it.
alter table public.places_versions enable row level security;

-- Anon may read every row. (Each policy is dropped first so the script can be re-run.)
drop policy if exists places_versions_anon_select on public.places_versions;
create policy places_versions_anon_select on public.places_versions
  for select to anon using (true);

-- Anon may insert rows: the app saves each new build.
drop policy if exists places_versions_anon_insert on public.places_versions;
create policy places_versions_anon_insert on public.places_versions
  for insert to anon with check (true);

-- Anon may update rows: needed for the upsert when a ZIP is rebuilt the same UTC day.
drop policy if exists places_versions_anon_update on public.places_versions;
create policy places_versions_anon_update on public.places_versions
  for update to anon using (true) with check (true);

-- Table privileges for anon (the policies above still apply). No delete.
grant select, insert, update on public.places_versions to anon;

-- Weekly cleanup (cleanup.yml): keep only the latest version per ZIP. Returns the deleted count.
create or replace function public.delete_old_place_versions()
returns integer
language plpgsql
-- security definer: runs with the owner's rights, so it can delete even though anon cannot.
-- The fixed search_path stops it from being pointed at look-alike tables in another schema.
security definer
set search_path = public
as $$
declare
  deleted integer;
begin
  -- Delete every row older than the newest version of the same ZIP.
  delete from public.places_versions as p
  where p.version < (select max(q.version) from public.places_versions as q where q.zip = p.zip);
  get diagnostics deleted = row_count;
  return deleted;
end;
$$;

-- Postgres lets everyone execute a new function by default; take that away and allow only
-- the service role (the key used by the GitHub Actions cleanup).
revoke all on function public.delete_old_place_versions() from public, anon, authenticated;
grant execute on function public.delete_old_place_versions() to service_role;

-- Phone-alert sign-ups (story 6). Only the ZIP, an optional name and the consent flag are stored.
create table if not exists public.registrations (
  id         bigint      generated always as identity primary key,
  zip        text        not null check (zip ~ '^[0-9]{5}$'),
  name       text        check (name is null or char_length(name) <= 50),
  consent    boolean     not null check (consent = true),
  created_at timestamptz not null default now()
);

-- Anon may only insert: the app never reads the registration list back.
alter table public.registrations enable row level security;

drop policy if exists registrations_anon_insert on public.registrations;
create policy registrations_anon_insert on public.registrations
  for insert to anon with check (consent = true);

-- No select policy and no select grant for anon. Re-running revokes anything broader first.
revoke all on public.registrations from anon, authenticated;
grant insert on public.registrations to anon;
-- The scheduled job (service role) reads the registered ZIPs.
grant select on public.registrations to service_role;

-- Last risk level pushed per ZIP (story 6). Only the service role (the scheduled job) uses it.
create table if not exists public.zip_state (
  zip        text        primary key check (zip ~ '^[0-9]{5}$'),
  last_risk  text        not null check (last_risk in ('low', 'elevated', 'high')),
  updated_at timestamptz not null default now()
);

-- Row-level security on with no policies: anon and authenticated get nothing.
-- The service role bypasses row-level security.
alter table public.zip_state enable row level security;

-- Push cooldown (story 6 review): the last level pushed and when, so the same level is not
-- pushed again within 6 hours. Added separately so re-running upgrades an existing table.
alter table public.zip_state add column if not exists last_pushed_level text
  check (last_pushed_level is null or last_pushed_level in ('low', 'elevated', 'high'));
alter table public.zip_state add column if not exists last_pushed_at timestamptz;
revoke all on public.zip_state from anon, authenticated;
grant select, insert, update on public.zip_state to service_role;
