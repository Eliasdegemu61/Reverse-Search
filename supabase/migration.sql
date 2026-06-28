-- Sodex reverse search address table
-- Run this in Supabase SQL Editor once

CREATE TABLE IF NOT EXISTS public.sodex_addresses (
  user_id INTEGER PRIMARY KEY,
  address TEXT NOT NULL,
  updated_at TIMESTAMPTZ DEFAULT now()
);

-- Index for fast prefix searches (e.g. WHERE address ILIKE '0x81%')
CREATE INDEX IF NOT EXISTS idx_sodex_addresses_address_prefix
  ON public.sodex_addresses (address);

-- GIN trigram index for fast substring/suffix searches (e.g. WHERE address ILIKE '%A60')
-- Requires pg_trgm extension
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE INDEX IF NOT EXISTS idx_sodex_addresses_address_trgm
  ON public.sodex_addresses USING gin (address gin_trgm_ops);

-- RLS off — the service role key bypasses RLS anyway,
-- but we explicitly disable so the anon key can SELECT for the website
ALTER TABLE public.sodex_addresses ENABLE ROW LEVEL SECURITY;

-- Allow public read access (website queries)
CREATE POLICY "Public can read sodex addresses"
  ON public.sodex_addresses
  FOR SELECT
  USING (true);

-- No public write — only service role (GitHub Actions) can write
