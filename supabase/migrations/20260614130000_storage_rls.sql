-- Storage RLS for the per-user file buckets (#79 storage hardening).
--
-- Both buckets are private and path-keyed by the owning user
-- (`<user_id>/<file>`). These policies make Postgres/Storage the control:
-- an authenticated user can only read/write objects whose first path
-- segment matches their own auth.uid(). The application also drops the
-- legacy `anon/` fallback, so every object is owned by a real user and
-- storage access goes through the JWT-bound user client. Service-role
-- (background/operator) bypasses these, as elsewhere.

-- Ensure the buckets exist and are private (idempotent — prod buckets
-- created via the dashboard are left in place, just forced private).
insert into storage.buckets (id, name, public)
values
    ('resume-uploads', 'resume-uploads', false),
    ('tailored-resumes', 'tailored-resumes', false)
on conflict (id) do update set public = false;

-- ---- resume-uploads: owner-only ------------------------------------------
create policy "resume_uploads_owner_read" on storage.objects
    for select to authenticated
    using (
        bucket_id = 'resume-uploads'
        and (storage.foldername(name))[1] = (select auth.uid())::text
    );

create policy "resume_uploads_owner_insert" on storage.objects
    for insert to authenticated
    with check (
        bucket_id = 'resume-uploads'
        and (storage.foldername(name))[1] = (select auth.uid())::text
    );

create policy "resume_uploads_owner_update" on storage.objects
    for update to authenticated
    using (
        bucket_id = 'resume-uploads'
        and (storage.foldername(name))[1] = (select auth.uid())::text
    )
    with check (
        bucket_id = 'resume-uploads'
        and (storage.foldername(name))[1] = (select auth.uid())::text
    );

-- ---- tailored-resumes: owner-only ----------------------------------------
create policy "tailored_resumes_owner_read" on storage.objects
    for select to authenticated
    using (
        bucket_id = 'tailored-resumes'
        and (storage.foldername(name))[1] = (select auth.uid())::text
    );

create policy "tailored_resumes_owner_insert" on storage.objects
    for insert to authenticated
    with check (
        bucket_id = 'tailored-resumes'
        and (storage.foldername(name))[1] = (select auth.uid())::text
    );

create policy "tailored_resumes_owner_update" on storage.objects
    for update to authenticated
    using (
        bucket_id = 'tailored-resumes'
        and (storage.foldername(name))[1] = (select auth.uid())::text
    )
    with check (
        bucket_id = 'tailored-resumes'
        and (storage.foldername(name))[1] = (select auth.uid())::text
    );
