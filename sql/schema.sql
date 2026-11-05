

BEGIN;

CREATE TABLE raw_strava_activities (
    raw_activity_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    strava_activity_id bigint NOT NULL,
    payload jsonb NOT NULL,
    ingested_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ck_raw_strava_activities_payload_object
        CHECK (jsonb_typeof(payload) = 'object')
);

CREATE INDEX ix_raw_strava_activities_strava_ingested
    ON raw_strava_activities (strava_activity_id, ingested_at DESC);

CREATE TABLE dim_date (
    date_key integer PRIMARY KEY,
    date_value date NOT NULL UNIQUE,
    calendar_year smallint NOT NULL,
    quarter smallint NOT NULL CHECK (quarter BETWEEN 1 AND 4),
    month smallint NOT NULL CHECK (month BETWEEN 1 AND 12),
    day_of_month smallint NOT NULL CHECK (day_of_month BETWEEN 1 AND 31),
    day_of_week smallint NOT NULL CHECK (day_of_week BETWEEN 1 AND 7),
    day_of_year smallint NOT NULL CHECK (day_of_year BETWEEN 1 AND 366),
    iso_year smallint NOT NULL,
    iso_week smallint NOT NULL CHECK (iso_week BETWEEN 1 AND 53),
    is_weekend boolean NOT NULL,
    CONSTRAINT ck_dim_date_key_matches_date
        CHECK (date_key = to_char(date_value, 'YYYYMMDD')::integer)
);

INSERT INTO dim_date (
    date_key,
    date_value,
    calendar_year,
    quarter,
    month,
    day_of_month,
    day_of_week,
    day_of_year,
    iso_year,
    iso_week,
    is_weekend
)
SELECT
    to_char(d, 'YYYYMMDD')::integer,
    d::date,
    extract(year FROM d)::smallint,
    extract(quarter FROM d)::smallint,
    extract(month FROM d)::smallint,
    extract(day FROM d)::smallint,
    extract(isodow FROM d)::smallint,
    extract(doy FROM d)::smallint,
    extract(isoyear FROM d)::smallint,
    extract(week FROM d)::smallint,
    extract(isodow FROM d) IN (6, 7)
FROM generate_series(
    date '2000-01-01',
    date '2050-12-31',
    interval '1 day'
) AS dates(d);

CREATE TABLE dim_race (
    race_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name text NOT NULL,
    race_date date NOT NULL REFERENCES dim_date (date_value),
    distance_m numeric(12, 2) NOT NULL CHECK (distance_m > 0),
    city text,
    state text,
    country text,
    course_name text,
    notes text,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX ix_dim_race_race_date ON dim_race (race_date);

CREATE TABLE fact_activities (
    strava_activity_id bigint PRIMARY KEY,
    raw_activity_id bigint NOT NULL UNIQUE
        REFERENCES raw_strava_activities (raw_activity_id),
    date_key integer NOT NULL REFERENCES dim_date (date_key),
    race_id bigint REFERENCES dim_race (race_id),
    name text,
    sport_type text NOT NULL,
    start_date timestamptz NOT NULL,
    start_date_local timestamp without time zone NOT NULL,
    distance_m numeric(12, 2) NOT NULL CHECK (distance_m >= 0),
    moving_time_seconds integer NOT NULL CHECK (moving_time_seconds >= 0),
    elapsed_time_seconds integer NOT NULL CHECK (elapsed_time_seconds >= moving_time_seconds),
    total_elevation_gain_m numeric(10, 2) CHECK (total_elevation_gain_m >= 0),
    average_heartrate_bpm numeric(5, 2) CHECK (average_heartrate_bpm > 0),
    max_heartrate_bpm smallint CHECK (max_heartrate_bpm > 0),
    average_speed_mps numeric(8, 4) CHECK (average_speed_mps >= 0),
    max_speed_mps numeric(8, 4) CHECK (max_speed_mps >= 0),
    average_pace_seconds_per_km numeric(8, 2)
        CHECK (average_pace_seconds_per_km > 0),
    best_pace_seconds_per_km numeric(8, 2) CHECK (best_pace_seconds_per_km > 0),
    average_cadence_spm numeric(6, 2) CHECK (average_cadence_spm >= 0),
    average_watts numeric(8, 2) CHECK (average_watts >= 0),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ck_fact_activities_max_hr_gte_average
        CHECK (
            max_heartrate_bpm IS NULL
            OR average_heartrate_bpm IS NULL
            OR max_heartrate_bpm >= average_heartrate_bpm
        )
);

CREATE INDEX ix_fact_activities_date_key ON fact_activities (date_key);
CREATE INDEX ix_fact_activities_race_id ON fact_activities (race_id);
CREATE INDEX ix_fact_activities_start_date ON fact_activities (start_date);

CREATE TABLE fact_activity_streams (
    strava_activity_id bigint NOT NULL
        REFERENCES fact_activities (strava_activity_id) ON DELETE CASCADE,
    elapsed_time_seconds integer NOT NULL CHECK (elapsed_time_seconds >= 0),
    distance_m numeric(12, 2) CHECK (distance_m >= 0),
    altitude_m numeric(8, 2),
    heartrate_bpm smallint CHECK (heartrate_bpm > 0),
    velocity_mps numeric(8, 4) CHECK (velocity_mps >= 0),
    cadence_spm numeric(6, 2) CHECK (cadence_spm >= 0),
    watts numeric(8, 2) CHECK (watts >= 0),
    PRIMARY KEY (strava_activity_id, elapsed_time_seconds)
);

CREATE TABLE fact_race_profile (
    race_id bigint NOT NULL REFERENCES dim_race (race_id) ON DELETE CASCADE,
    segment_number smallint NOT NULL CHECK (segment_number > 0),
    segment_start_m numeric(10, 2) NOT NULL CHECK (segment_start_m >= 0),
    segment_end_m numeric(10, 2) NOT NULL,
    start_elevation_m numeric(8, 2),
    end_elevation_m numeric(8, 2),
    elevation_gain_m numeric(8, 2),
    PRIMARY KEY (race_id, segment_number),
    CONSTRAINT ck_fact_race_profile_segment_length
        CHECK (segment_end_m > segment_start_m)
);

CREATE TABLE fact_weather (
    weather_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    race_id bigint REFERENCES dim_race (race_id),
    strava_activity_id bigint REFERENCES fact_activities (strava_activity_id),
    weather_type text NOT NULL CHECK (weather_type IN ('forecast', 'observed')),
    source text NOT NULL,
    recorded_at timestamptz NOT NULL,
    valid_at timestamptz NOT NULL,
    temperature_c numeric(5, 2),
    feels_like_c numeric(5, 2),
    humidity_pct numeric(5, 2) CHECK (humidity_pct BETWEEN 0 AND 100),
    precipitation_mm numeric(8, 2) CHECK (precipitation_mm >= 0),
    wind_speed_mps numeric(8, 4) CHECK (wind_speed_mps >= 0),
    wind_direction_degrees smallint
        CHECK (wind_direction_degrees BETWEEN 0 AND 360),
    weather_code text,
    CONSTRAINT ck_fact_weather_has_event
        CHECK (race_id IS NOT NULL OR strava_activity_id IS NOT NULL)
);

CREATE INDEX ix_fact_weather_race_valid_at
    ON fact_weather (race_id, valid_at);
CREATE INDEX ix_fact_weather_activity_valid_at
    ON fact_weather (strava_activity_id, valid_at);

CREATE TABLE sim_predictions (
    prediction_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    race_id bigint NOT NULL REFERENCES dim_race (race_id),
    predicted_at timestamptz NOT NULL DEFAULT now(),
    model_name text NOT NULL,
    model_version text,
    historical_window_days integer CHECK (historical_window_days > 0),
    predicted_finish_seconds integer NOT NULL CHECK (predicted_finish_seconds > 0),
    lower_bound_seconds integer CHECK (lower_bound_seconds > 0),
    upper_bound_seconds integer CHECK (upper_bound_seconds > 0),
    predicted_pace_seconds_per_km numeric(8, 2)
        CHECK (predicted_pace_seconds_per_km > 0),
    input_snapshot jsonb NOT NULL DEFAULT '{}'::jsonb,
    forecast_weather_id bigint REFERENCES fact_weather (weather_id),
    actual_activity_id bigint REFERENCES fact_activities (strava_activity_id),
    evaluated_at timestamptz,
    CONSTRAINT ck_sim_predictions_bounds
        CHECK (
            lower_bound_seconds IS NULL
            OR upper_bound_seconds IS NULL
            OR lower_bound_seconds <= upper_bound_seconds
        ),
    CONSTRAINT ck_sim_predictions_actual_evaluation
        CHECK (actual_activity_id IS NOT NULL OR evaluated_at IS NULL)
);

CREATE INDEX ix_sim_predictions_race_predicted_at
    ON sim_predictions (race_id, predicted_at DESC);
CREATE INDEX ix_sim_predictions_actual_activity
    ON sim_predictions (actual_activity_id);

COMMIT;
