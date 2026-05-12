-- Run this once in your Supabase SQL editor (Database → SQL Editor)
-- Creates the notification_settings table used by the tracker and dashboard

CREATE TABLE IF NOT EXISTS notification_settings (
  key        TEXT PRIMARY KEY,
  enabled    BOOLEAN NOT NULL DEFAULT true,
  updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- Auto-update updated_at on every change
CREATE OR REPLACE FUNCTION set_updated_at()
RETURNS TRIGGER AS $$
BEGIN
  NEW.updated_at = NOW();
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE TRIGGER notification_settings_updated_at
  BEFORE UPDATE ON notification_settings
  FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- Allow the anon role (used by the dashboard) to read and write
ALTER TABLE notification_settings ENABLE ROW LEVEL SECURITY;

CREATE POLICY "anon full access"
  ON notification_settings
  FOR ALL
  TO anon
  USING (true)
  WITH CHECK (true);

-- Seed the three master toggles (all enabled by default)
-- Product-level staleness rows are created automatically by tracker.py on first run
INSERT INTO notification_settings (key, enabled) VALUES
  ('master_price_drop',    true),
  ('master_weekly_summary', true),
  ('master_staleness',     true)
ON CONFLICT (key) DO NOTHING;
