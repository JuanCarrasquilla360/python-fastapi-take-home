-- Coffee health schema. Applied by the Postgres entrypoint when the volume is first created
-- (`make db-reset` to re-apply). See NOTES.md for the reasoning behind types and indexes.

-- Closed vocabularies as enum types: 4 bytes per value, typo-proof, and they sort in
-- declaration order (Poor < Fair < Good < Excellent), which a text column would not.
create type gender as enum ('Male', 'Female', 'Other');
create type sleep_quality as enum ('Poor', 'Fair', 'Good', 'Excellent');
create type stress_level as enum ('Low', 'Medium', 'High');
create type health_issues as enum ('None', 'Mild', 'Moderate', 'Severe');
create type occupation as enum ('Office', 'Healthcare', 'Service', 'Student', 'Other');

-- One row per person. `id` is the source system's identifier, not a surrogate key:
-- it is what makes the loader idempotent (insert ... on conflict (id)).
-- CHECK constraints mirror app/cleaning.py LIMITS: the loader rejects bad rows with a
-- readable reason first; the constraints guarantee no other writer can bypass the rules.
create table participants (
  id                      integer       primary key check (id > 0),
  age                     smallint      not null check (age between 18 and 110),
  gender                  gender        not null,
  country                 text          not null check (length(country) between 1 and 64),
  coffee_intake           numeric(3, 1) not null check (coffee_intake between 0 and 20),
  caffeine_mg             numeric(5, 1) not null check (caffeine_mg between 0 and 2000),
  sleep_hours             numeric(3, 1) not null check (sleep_hours between 1 and 18),
  sleep_quality           sleep_quality not null,
  bmi                     numeric(3, 1) not null check (bmi between 10 and 70),
  heart_rate              smallint      not null check (heart_rate between 25 and 220),
  stress_level            stress_level  not null,
  physical_activity_hours numeric(4, 1) not null check (physical_activity_hours between 0 and 100),
  health_issues           health_issues not null,
  occupation              occupation    not null,
  smoking                 boolean       not null,
  alcohol_consumption     boolean       not null
);

-- Indexes for the API filters (see NOTES.md "Indexes"). Every list query is
-- `... where <filters> and id > :cursor order by id limit :n`, so:
--   * (country, id) serves the most common filter AND the keyset order in one index scan.
--   * age / bmi / coffee_intake are the range filters; single-column btrees let the planner
--     combine them with bitmap AND/OR when they are selective.
-- Low-cardinality enums (gender, stress, smoking, ...) get no index: a value matching
-- 1/3 of the table is read faster with a sequential scan than through an index.
create index participants_country_id_idx on participants (country, id);
create index participants_age_idx on participants (age);
create index participants_bmi_idx on participants (bmi);
create index participants_coffee_intake_idx on participants (coffee_intake);

-- Audit trail of loader runs.
create table load_runs (
  id            bigint      generated always as identity primary key,
  source_file   text        not null,
  started_at    timestamptz not null default now(),
  finished_at   timestamptz,
  rows_read     integer,
  rows_blank    integer,
  rows_fixed    integer,
  rows_invalid  integer,
  rows_duplicate integer,
  rows_conflict integer,
  rows_inserted integer,
  rows_updated  integer,
  rows_unchanged integer
);

-- Every CSV line a run did not load, and why:
--   invalid   - failed parsing/validation (raw_fields holds the row as read)
--   duplicate - exact copy of an earlier line with the same ID (the earliest one is loaded)
--   conflict  - same ID as another line but different values; none of them is loaded
create table load_rejections (
  run_id      bigint  not null references load_runs (id) on delete cascade,
  line_number integer not null,
  kind        text    not null check (kind in ('invalid', 'duplicate', 'conflict')),
  source_id   text,   -- raw ID field, to look the row up quickly
  reason      text    not null,
  raw_fields  text[],
  primary key (run_id, line_number)
);
