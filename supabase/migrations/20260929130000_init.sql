-- Unified Slack + Discord archive.
-- Canonical store: Postgres 16+ with pg_trgm + pgvector (original design).
-- docker compose uses pgvector/pgvector:pg16. tinbase can apply this only if
-- CREATE EXTENSION vector is skipped *and* you comment out the vector column;
-- do not use tinbase for the Hetzner deploy.

create extension if not exists pg_trgm;
create extension if not exists vector;

create type public.origin as enum ('slack', 'discord');

create table public.workspaces (
  origin public.origin not null,
  native_id text not null,
  name text,
  extra jsonb not null default '{}'::jsonb,
  primary key (origin, native_id)
);

create table public.users (
  origin public.origin not null,
  native_id text not null,
  workspace_id text not null,
  handle text,
  display_name text,
  is_bot boolean not null default false,
  is_deleted boolean not null default false,
  extra jsonb not null default '{}'::jsonb,
  primary key (origin, native_id)
);

create index users_handle_trgm on public.users using gin (handle gin_trgm_ops);
create index users_display_trgm on public.users using gin (display_name gin_trgm_ops);

create table public.channels (
  origin public.origin not null,
  native_id text not null,
  workspace_id text not null,
  parent_id text,
  name text,
  topic text,
  kind text not null,
  is_archived boolean not null default false,
  extra jsonb not null default '{}'::jsonb,
  primary key (origin, native_id)
);

create index channels_kind on public.channels (origin, kind);
create index channels_parent on public.channels (origin, parent_id);

create table public.messages (
  id text primary key,
  origin public.origin not null,
  native_id text not null,
  channel_id text not null,
  user_id text,
  thread_id text,
  reply_to_id text,
  sent_at timestamptz not null,
  edited_at timestamptz,
  subtype text,
  is_pinned boolean not null default false,
  text_raw text,
  text_plain text,
  raw jsonb not null,
  tsv tsvector generated always as (
    to_tsvector('english', coalesce(text_plain, ''))
  ) stored,
  unique (origin, native_id)
);

create index messages_tsv on public.messages using gin (tsv);
create index messages_plain_trgm on public.messages using gin (text_plain gin_trgm_ops);
create index messages_origin_sent on public.messages (origin, sent_at);
create index messages_user_sent on public.messages (origin, user_id, sent_at);
create index messages_channel_sent on public.messages (origin, channel_id, sent_at);
create index messages_thread on public.messages (origin, thread_id);

create table public.thread_docs (
  id text primary key,
  origin public.origin not null,
  channel_id text not null,
  thread_id text,
  sent_at timestamptz,
  participant_ids text[] not null default '{}',
  text_plain text,
  summary text,
  tsv tsvector generated always as (
    to_tsvector('english', coalesce(text_plain, ''))
  ) stored,
  embedding vector(1024)
);

create index thread_docs_tsv on public.thread_docs using gin (tsv);
create index thread_docs_origin_sent on public.thread_docs (origin, sent_at);
create index thread_docs_embedding on public.thread_docs
  using hnsw (embedding vector_cosine_ops);

create table public.files (
  origin public.origin not null,
  native_id text not null,
  message_id text references public.messages (id) on delete set null,
  filename text,
  mimetype text,
  size_bytes bigint,
  url text,
  local_path text,
  extra jsonb not null default '{}'::jsonb,
  primary key (origin, native_id)
);

create table public.reactions (
  message_id text not null references public.messages (id) on delete cascade,
  emoji text not null,
  user_ids text[] not null default '{}',
  count int not null default 0,
  primary key (message_id, emoji)
);

comment on type public.origin is 'Source system; filter every search with this or omit for both.';
comment on table public.thread_docs is 'RAG unit: Slack thread or Discord thread-channel; standalones are one-row docs.';
comment on column public.thread_docs.embedding is 'pgvector; 1024-d cosine via HNSW.';
