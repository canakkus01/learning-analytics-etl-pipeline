-- Schema for the personal study / device / notification data ecosystem.
-- All three tables key on student_name + a date, so they join directly
-- for cross-source Learning Analytics queries.

CREATE TABLE IF NOT EXISTS study_delay_sessions (
    id BIGSERIAL PRIMARY KEY,
    source_file TEXT,
    source_row_number INTEGER,
    student_name TEXT NOT NULL,
    session_date DATE NOT NULL,
    date_note TEXT,                       -- e.g. "System Offline", "Tracker Failed"
    subject TEXT NOT NULL,
    planned_start_time TIME NOT NULL,
    actual_start_time TIME NOT NULL,
    end_time TIME NOT NULL,
    planned_duration_min INTEGER,
    start_delay_min INTEGER,              -- minutes late starting the session
    actual_duration_min INTEGER,          -- actual minutes spent studying
    row_hash TEXT UNIQUE                  -- de-duplication key
);

CREATE TABLE IF NOT EXISTS device_activity (
    id BIGSERIAL PRIMARY KEY,
    source_file TEXT,
    source_row_number INTEGER,
    student_name TEXT NOT NULL,
    activity_date DATE NOT NULL,
    date_note TEXT,
    total_screen_time_min INTEGER,
    unlock_count INTEGER,
    do_not_disturb_min INTEGER,           -- NULL when missing (not the same as 0)
    row_hash TEXT UNIQUE
);

CREATE TABLE IF NOT EXISTS notifications (
    id BIGSERIAL PRIMARY KEY,
    source_file TEXT,
    source_row_number INTEGER,
    student_name TEXT NOT NULL,
    notification_date DATE NOT NULL,
    date_note TEXT,
    notification_time TIME NOT NULL,
    app_name TEXT NOT NULL,
    category TEXT NOT NULL,               -- Distractive / Non-distractive
    notification_type TEXT,               -- free-text label, e.g. "Story liked"
    response_delay_sec INTEGER,           -- seconds before the student reacted; NULL if unknown/invalid
    row_hash TEXT UNIQUE
);

-- Helpful indexes for the join keys used by the cross-source queries.
CREATE INDEX IF NOT EXISTS idx_delay_student_date  ON study_delay_sessions (student_name, session_date);
CREATE INDEX IF NOT EXISTS idx_device_student_date ON device_activity      (student_name, activity_date);
CREATE INDEX IF NOT EXISTS idx_notif_student_date  ON notifications        (student_name, notification_date);
