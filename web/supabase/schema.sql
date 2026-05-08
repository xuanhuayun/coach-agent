-- Minimal schema for coach-agent pilot (v1)
-- Run this in Supabase SQL editor.

create table if not exists students (
  id text primary key,
  name text not null,
  contact text not null default '',
  note text not null default '',
  venue text not null default '',
  created_at text not null default '',
  archived boolean not null default false,
  default_class_type text not null default '1:1'
);

create table if not exists lesson_types (
  id text primary key,
  code text not null,
  label text not null default '',
  participants integer not null default 1,
  -- legacy / compatibility (some existing JSON contains this)
  price_per_hour numeric not null default 0,
  price_per_lesson numeric not null default 0,
  currency text not null default 'SGD',
  active boolean not null default true
);

create table if not exists lessons (
  id text primary key,
  coach_id text not null default '',
  lesson_type_id text,
  class_type text not null default '',
  baseline_minutes integer not null default 120,
  price_per_lesson numeric not null default 0,
  -- legacy / compatibility (some existing JSON contains this)
  price_per_hour numeric not null default 0,
  currency text not null default 'SGD',
  start_at text not null default '',
  duration_minutes integer,
  participant_ids jsonb not null default '[]'::jsonb,
  participant_names_raw jsonb not null default '[]'::jsonb,
  draft boolean not null default false,
  topics jsonb not null default '[]'::jsonb,
  status text not null default '未说明',
  next_plan_time text,
  next_plan text not null default '',
  original_text text not null default '',
  venue text not null default '',
  created_at text not null default '',
  updated_at text not null default ''
);

create table if not exists coaches (
  id text primary key,
  name text not null default 'Default',
  contact text not null default '',
  created_at text not null default '',
  venues jsonb not null default '[]'::jsonb
);

create table if not exists payments (
  id text primary key,
  lesson_id text not null default '',
  student_id text not null default '',
  note text not null default '',
  amount numeric not null default 0,
  currency text not null default 'SGD',
  paid boolean not null default false,
  paid_at text,
  created_at text not null default '',
  updated_at text not null default ''
);

