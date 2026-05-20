-- Run this in Supabase SQL editor after running schema.sql
-- This function lets the worker safely claim one job at a time

create or replace function claim_next_job(p_worker_id text)
returns setof processing_jobs
language plpgsql
as $$
declare
  claimed_job processing_jobs;
begin
  select * into claimed_job
  from processing_jobs
  where status = 'queued'
    and (next_retry_at is null or next_retry_at <= now())
  order by priority asc, created_at asc
  limit 1
  for update skip locked;

  if not found then
    return;
  end if;

  update processing_jobs
  set status = 'running',
      worker_id = p_worker_id,
      locked_at = now(),
      updated_at = now()
  where id = claimed_job.id;

  claimed_job.status := 'running';
  claimed_job.worker_id := p_worker_id;
  return next claimed_job;
end;
$$;