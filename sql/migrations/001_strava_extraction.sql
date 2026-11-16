-- Upgrade a database created with the original Etapa 1 schema.
-- Run once in DBeaver before starting the Strava extractor.

BEGIN;

ALTER TABLE raw_strava_activities
    ADD COLUMN IF NOT EXISTS streams jsonb;

ALTER TABLE raw_strava_activities
    ADD COLUMN IF NOT EXISTS streams_status text NOT NULL DEFAULT 'pending';

UPDATE raw_strava_activities
SET streams_status = CASE
    WHEN payload ->> 'sport_type' = 'Run' OR payload ->> 'type' = 'Run'
        THEN 'pending'
    ELSE 'not_applicable'
END
WHERE streams_status = 'pending';

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'ck_raw_strava_activities_streams_status'
          AND conrelid = 'raw_strava_activities'::regclass
    ) THEN
        ALTER TABLE raw_strava_activities
            ADD CONSTRAINT ck_raw_strava_activities_streams_status
            CHECK (streams_status IN ('pending', 'available', 'unavailable', 'not_applicable'));
    END IF;
END
$$;

CREATE TABLE IF NOT EXISTS strava_sync_state (
    sync_name text PRIMARY KEY,
    sync_mode text NOT NULL CHECK (sync_mode IN ('backfill', 'incremental', 'idle')),
    backfill_before bigint NOT NULL,
    incremental_after bigint,
    incremental_before bigint,
    page integer NOT NULL CHECK (page > 0),
    latest_activity_at timestamptz,
    updated_at timestamptz NOT NULL DEFAULT now()
);

INSERT INTO strava_sync_state (
    sync_name,
    sync_mode,
    backfill_before,
    page,
    latest_activity_at
)
SELECT
    'activities',
    'backfill',
    extract(epoch FROM now())::bigint,
    1,
    max((payload ->> 'start_date')::timestamptz)
FROM raw_strava_activities
ON CONFLICT (sync_name) DO NOTHING;

COMMIT;
