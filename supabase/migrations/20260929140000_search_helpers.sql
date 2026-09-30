-- Search helpers for ingest + MCP.
-- Existing volume: apply by hand (docs/POSTGRES.md). Fresh volume: runs after init.

-- Content vs system noise. Slack content rows have subtype null / thread_broadcast /
-- file_share / me_message; Discord content rows are Default / Reply. Everything else
-- (channel_join, bot_message, huddle_thread, GuildMemberJoin, ...) is noise.
create or replace function public.is_content(subtype text)
returns boolean
language sql
immutable
parallel safe
as $$
  select subtype is null
      or subtype in ('thread_broadcast', 'file_share', 'me_message', 'Default', 'Reply')
$$;

comment on function public.is_content(text) is
  'True for human message subtypes; FTS/thread_docs exclude everything else by default.';

create index if not exists thread_docs_channel on public.thread_docs (origin, channel_id);
create index if not exists messages_channel_thread on public.messages (origin, channel_id, thread_id);
create index if not exists files_message on public.files (message_id);
