-- ============================================================
-- PatchBrain database schema
-- Paste this entire file into Supabase SQL editor and run it
-- ============================================================

-- Enable pgvector extension for embeddings
create extension if not exists vector;

-- ============================================================
-- COMPANIES
-- ============================================================
create table companies (
  id          uuid primary key default gen_random_uuid(),
  name        text not null,
  slug        text not null unique,
  created_at  timestamptz not null default now()
);

-- ============================================================
-- USERS (linked to Supabase Auth)
-- ============================================================
create table users (
  id          uuid primary key references auth.users(id) on delete cascade,
  company_id  uuid not null references companies(id) on delete cascade,
  email       text not null,
  role        text not null default 'member' check (role in ('owner','admin','member')),
  created_at  timestamptz not null default now()
);

-- ============================================================
-- FILES
-- ============================================================
create table files (
  id                      uuid primary key default gen_random_uuid(),
  company_id              uuid not null references companies(id) on delete cascade,
  logical_name            text not null,
  current_file_version_id uuid,  -- updated after successful ingestion
  deleted_at              timestamptz,
  created_at              timestamptz not null default now()
);

-- ============================================================
-- FILE VERSIONS
-- Each upload = new version. Only one version is "active" at a time.
-- ============================================================
create table file_versions (
  id                      uuid primary key default gen_random_uuid(),
  file_id                 uuid not null references files(id) on delete cascade,
  company_id              uuid not null references companies(id) on delete cascade,
  storage_bucket          text not null default 'company-files',
  storage_path            text not null,
  original_filename       text not null,
  mime_type               text,
  size_bytes              bigint,
  sha256                  text,
  status                  text not null default 'uploaded'
                            check (status in (
                              'uploaded','queued','parsing','parsed',
                              'chunking','embedding','ready','failed','needs_review'
                            )),
  error_code              text,
  error_message           text,
  parser_name             text,
  retry_count             int not null default 0,
  next_retry_at           timestamptz,
  worker_id               text,
  started_at              timestamptz,
  finished_at             timestamptz,
  created_by              uuid references auth.users(id),
  created_at              timestamptz not null default now()
);

-- ============================================================
-- PROCESSING JOBS
-- ============================================================
create table processing_jobs (
  id               uuid primary key default gen_random_uuid(),
  company_id       uuid not null references companies(id) on delete cascade,
  file_version_id  uuid not null references file_versions(id) on delete cascade,
  job_type         text not null default 'ingest',
  status           text not null default 'queued'
                     check (status in ('queued','running','done','failed')),
  priority         int not null default 5,
  retry_count      int not null default 0,
  next_retry_at    timestamptz,
  worker_id        text,
  locked_at        timestamptz,
  error_code       text,
  error_message    text,
  created_at       timestamptz not null default now(),
  updated_at       timestamptz not null default now()
);

-- ============================================================
-- FILE CHUNKS (the searchable pieces + embeddings)
-- ============================================================
create table file_chunks (
  id               uuid primary key default gen_random_uuid(),
  company_id       uuid not null references companies(id) on delete cascade,
  file_id          uuid not null references files(id) on delete cascade,
  file_version_id  uuid not null references file_versions(id) on delete cascade,
  chunk_index      int not null,
  content          text not null,
  embedding        vector(1536),   -- OpenAI text-embedding-3-small
  token_count      int,
  source_type      text not null
                     check (source_type in (
                       'pdf_text','pdf_table','sheet_rows',
                       'docx_section','csv_rows','txt_chunk'
                     )),
  -- PDF-specific
  page_number      int,
  bbox_json        jsonb,
  -- Spreadsheet-specific
  sheet_name       text,
  row_start        int,
  row_end          int,
  -- Extra metadata
  metadata_jsonb   jsonb,
  created_at       timestamptz not null default now()
);

-- ============================================================
-- QUESTIONS + ANSWERS + CITATIONS
-- ============================================================
create table questions (
  id            uuid primary key default gen_random_uuid(),
  company_id    uuid not null references companies(id) on delete cascade,
  user_id       uuid references auth.users(id),
  question_text text not null,
  created_at    timestamptz not null default now()
);

create table answers (
  id              uuid primary key default gen_random_uuid(),
  question_id     uuid not null references questions(id) on delete cascade,
  model_name      text not null,
  answer_text     text not null,
  refusal_reason  text,
  latency_ms      int,
  tokens_in       int,
  tokens_out      int,
  created_at      timestamptz not null default now()
);

create table citations (
  id               uuid primary key default gen_random_uuid(),
  answer_id        uuid not null references answers(id) on delete cascade,
  chunk_id         uuid not null references file_chunks(id),
  order_index      int not null,
  page_number      int,
  sheet_name       text,
  row_start        int,
  row_end          int,
  quote_text       text,
  bbox_json        jsonb,
  relevance_score  float,
  created_at       timestamptz not null default now()
);

-- ============================================================
-- INDEXES
-- ============================================================

-- Fast lookup by company (every query filters by this)
create index idx_files_company           on files(company_id) where deleted_at is null;
create index idx_file_versions_company   on file_versions(company_id);
create index idx_file_versions_file      on file_versions(file_id);
create index idx_file_chunks_company     on file_chunks(company_id);
create index idx_file_chunks_file        on file_chunks(file_id);
create index idx_file_chunks_version     on file_chunks(file_version_id);
create index idx_processing_jobs_status  on processing_jobs(status, priority, created_at)
                                          where status = 'queued';

-- Vector index for similarity search (build after you have data)
-- Run this separately after inserting your first chunks:
-- create index idx_chunks_embedding on file_chunks
--   using hnsw (embedding vector_cosine_ops)
--   with (m = 16, ef_construction = 64);

-- Full-text search index for keyword retrieval
create index idx_chunks_fts on file_chunks
  using gin(to_tsvector('english', content));

-- ============================================================
-- ROW LEVEL SECURITY
-- ============================================================

alter table companies        enable row level security;
alter table users            enable row level security;
alter table files            enable row level security;
alter table file_versions    enable row level security;
alter table processing_jobs  enable row level security;
alter table file_chunks      enable row level security;
alter table questions        enable row level security;
alter table answers          enable row level security;
alter table citations        enable row level security;

-- Users can only read their own company
create policy "users_own_company" on companies
  for select using (
    id = (select company_id from users where id = auth.uid())
  );

-- Users can only see users in their company
create policy "users_own_company_users" on users
  for select using (
    company_id = (select company_id from users where id = auth.uid())
  );

-- Files: company-scoped
create policy "files_company_select" on files
  for select using (
    company_id = (select company_id from users where id = auth.uid())
  );
create policy "files_company_insert" on files
  for insert with check (
    company_id = (select company_id from users where id = auth.uid())
  );

-- File versions: company-scoped
create policy "file_versions_company_select" on file_versions
  for select using (
    company_id = (select company_id from users where id = auth.uid())
  );

-- File chunks: company-scoped (most important - search results)
create policy "chunks_company_select" on file_chunks
  for select using (
    company_id = (select company_id from users where id = auth.uid())
  );

-- Questions: own company
create policy "questions_company" on questions
  for all using (
    company_id = (select company_id from users where id = auth.uid())
  );

-- Answers: via questions
create policy "answers_via_questions" on answers
  for select using (
    question_id in (
      select id from questions
      where company_id = (select company_id from users where id = auth.uid())
    )
  );

-- Citations: via answers
create policy "citations_via_answers" on citations
  for select using (
    answer_id in (
      select id from answers
      where question_id in (
        select id from questions
        where company_id = (select company_id from users where id = auth.uid())
      )
    )
  );

-- ============================================================
-- HYBRID SEARCH FUNCTION
-- Called by FastAPI retrieval engine
-- ============================================================
create or replace function hybrid_search(
  p_company_id    uuid,
  p_query_embedding vector(1536),
  p_query_text    text,
  p_match_count   int default 20
)
returns table (
  chunk_id        uuid,
  content         text,
  source_type     text,
  page_number     int,
  sheet_name      text,
  row_start       int,
  row_end         int,
  file_id         uuid,
  file_version_id uuid,
  vector_score    float,
  text_score      float,
  combined_score  float
)
language sql stable
as $$
  with vector_results as (
    select
      fc.id,
      fc.content,
      fc.source_type,
      fc.page_number,
      fc.sheet_name,
      fc.row_start,
      fc.row_end,
      fc.file_id,
      fc.file_version_id,
      1 - (fc.embedding <=> p_query_embedding) as vscore
    from file_chunks fc
    join file_versions fv on fv.id = fc.file_version_id
    where fc.company_id = p_company_id
      and fv.status = 'ready'
    order by fc.embedding <=> p_query_embedding
    limit p_match_count * 2
  ),
  text_results as (
    select
      fc.id,
      ts_rank(to_tsvector('english', fc.content),
              plainto_tsquery('english', p_query_text)) as tscore
    from file_chunks fc
    join file_versions fv on fv.id = fc.file_version_id
    where fc.company_id = p_company_id
      and fv.status = 'ready'
      and to_tsvector('english', fc.content) @@ plainto_tsquery('english', p_query_text)
    limit p_match_count * 2
  )
  select
    vr.id,
    vr.content,
    vr.source_type,
    vr.page_number,
    vr.sheet_name,
    vr.row_start,
    vr.row_end,
    vr.file_id,
    vr.file_version_id,
    vr.vscore,
    coalesce(tr.tscore, 0),
    (vr.vscore * 0.7) + (coalesce(tr.tscore, 0) * 0.3) as combined
  from vector_results vr
  left join text_results tr on tr.id = vr.id
  order by combined desc
  limit p_match_count;
$$;