"""
Clean dirty study / device / notification CSV files and load them into PostgreSQL.

This pipeline is written for the *actual* exported files used in the project:

  Delay_dirty.csv          -> study_delay_sessions
  Device_Activity_dirty.csv -> device_activity
  Notification_dirty.csv    -> notifications

All three sources are keyed on the student's NAME plus a date, so the three
tables can be joined directly (student_name + date) for cross-source queries.

Usage examples
--------------
1) Dry run only, writes cleaned CSVs and prints a summary (no database needed):
   python postgres_clean_load_pipeline.py \
     --delay Delay_dirty.csv \
     --device Device_Activity_dirty.csv \
     --notifications Notification_dirty.csv \
     --save-cleaned cleaned_output \
     --dry-run

2) Clean and load into PostgreSQL:
   export POSTGRES_URL="postgresql+psycopg2://postgres:password@localhost:5432/study_data"
   python postgres_clean_load_pipeline.py \
     --delay Delay_dirty.csv \
     --device Device_Activity_dirty.csv \
     --notifications Notification_dirty.csv \
     --schema public \
     --mode replace
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
from pathlib import Path
from typing import Iterable, Optional, Tuple

import pandas as pd

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    # dotenv is optional; environment variables still work without it.
    pass


DATE_NOTE_PATTERN = re.compile(r"^\s*(?P<date>.*?)(?:\s*\((?P<note>.*?)\))?\s*$")
VALID_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


# ---------- small reusable helpers ----------

def read_csv_clean_headers(path: str | Path) -> pd.DataFrame:
    """Read a CSV and strip surrounding whitespace from every column name.

    Several source files have headers such as 'date ' or 'Notification type '
    with trailing spaces. Stripping here means the rest of the pipeline can use
    clean, predictable column names.
    """
    raw = pd.read_csv(path)
    raw.columns = [str(col).strip() for col in raw.columns]
    raw["source_row_number"] = raw.index + 2  # +2: row 1 is the CSV header
    raw["source_file"] = Path(path).name
    return raw


def clean_text(value: object) -> Optional[str]:
    """Trim whitespace and turn empty strings / common null text into None."""
    if pd.isna(value):
        return None
    text = str(value).strip()
    if text == "" or text.lower() in {"nan", "none", "null", "n/a", "na"}:
        return None
    return re.sub(r"\s+", " ", text)


def clean_name(value: object) -> Optional[str]:
    text = clean_text(value)
    if text is None:
        return None
    return text.title()


def parse_date_and_note(value: object) -> Tuple[pd.Timestamp, Optional[str]]:
    """
    Handles values like:
    - 2026-05-17
    - 15/05/2026
    - 2026-05-17 (System Offline)
    Returns parsed date and optional note.
    """
    text = clean_text(value)
    if text is None:
        return pd.NaT, None

    match = DATE_NOTE_PATTERN.match(text)
    date_part = match.group("date").strip() if match else text
    note = clean_text(match.group("note")) if match and match.group("note") else None

    # Try explicit formats first so day/month data is not misread.
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%Y/%m/%d", "%d-%m-%Y"):
        parsed = pd.to_datetime(date_part, format=fmt, errors="coerce")
        if not pd.isna(parsed):
            return parsed.normalize(), note

    # Last fallback for unexpected but still parseable dates.
    parsed = pd.to_datetime(date_part, errors="coerce", dayfirst=True)
    return parsed.normalize() if not pd.isna(parsed) else pd.NaT, note


def parse_time(value: object):
    text = clean_text(value)
    if text is None:
        return None
    parsed = pd.to_datetime(text, errors="coerce")
    if pd.isna(parsed):
        return None
    return parsed.time()


def fraction_of_day_to_minutes(value: object) -> Optional[int]:
    """
    In the dirty delay file, fields like 0.02430555556 are Excel-style fractions
    of a day. This converts them to minutes. If a value already looks like a
    minute count (> 1), it is left as minutes.
    """
    if pd.isna(value):
        return None
    number = pd.to_numeric(value, errors="coerce")
    if pd.isna(number):
        return None
    minutes = float(number) * 1440 if abs(float(number)) <= 1 else float(number)
    return int(round(minutes))


def to_int(value: object) -> Optional[int]:
    number = pd.to_numeric(value, errors="coerce")
    if pd.isna(number):
        return None
    return int(round(float(number)))


def to_nonneg_int(value: object) -> Optional[int]:
    """Like to_int, but negative values are treated as invalid (None)."""
    number = to_int(value)
    if number is None or number < 0:
        return None
    return number


def add_row_hash(df: pd.DataFrame, cols: Iterable[str]) -> pd.DataFrame:
    def make_hash(row: pd.Series) -> str:
        raw = "|".join("" if pd.isna(row[col]) else str(row[col]) for col in cols)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    df = df.copy()
    df["row_hash"] = df.apply(make_hash, axis=1)
    return df


def ensure_valid_identifier(name: str, label: str) -> str:
    if not VALID_IDENTIFIER.match(name):
        raise ValueError(
            f"Invalid {label}: {name!r}. Use only letters, numbers and "
            f"underscores, and do not start with a number."
        )
    return name


# ---------- cleaning functions ----------

def clean_delay(path: str | Path) -> pd.DataFrame:
    raw = read_csv_clean_headers(path)
    parsed_dates = raw["date"].apply(parse_date_and_note)

    df = pd.DataFrame({
        "source_file": raw["source_file"],
        "source_row_number": raw["source_row_number"],
        "student_name": raw["Student"].apply(clean_name),
        "session_date": [d.date() if not pd.isna(d) else None for d, _ in parsed_dates],
        "date_note": [note for _, note in parsed_dates],
        "subject": raw["Subject"].apply(clean_text),
        "planned_start_time": raw["Planned Start"].apply(parse_time),
        "actual_start_time": raw["Actual Start"].apply(parse_time),
        "end_time": raw["End time"].apply(parse_time),
        "planned_duration_min": raw["Planned duration"].apply(to_int),
        "start_delay_min": raw["start delay"].apply(fraction_of_day_to_minutes),
        "actual_duration_min": raw["total time taken"].apply(fraction_of_day_to_minutes),
    })

    # Remove rows where key values could not be understood.
    df = df.dropna(subset=[
        "student_name", "session_date", "subject",
        "planned_start_time", "actual_start_time", "end_time",
    ])

    for col in ["planned_duration_min", "start_delay_min", "actual_duration_min"]:
        df[col] = df[col].astype("Int64")

    # Remove obvious duplicate study sessions, ignoring the dirty Sr.no field.
    duplicate_key = [
        "student_name", "session_date", "subject", "planned_start_time",
        "actual_start_time", "end_time", "planned_duration_min",
    ]
    df = df.drop_duplicates(subset=duplicate_key, keep="first")
    df = add_row_hash(df, duplicate_key)
    return df.reset_index(drop=True)


def clean_device_activity(path: str | Path) -> pd.DataFrame:
    raw = read_csv_clean_headers(path)
    parsed_dates = raw["date"].apply(parse_date_and_note)

    df = pd.DataFrame({
        "source_file": raw["source_file"],
        "source_row_number": raw["source_row_number"],
        "student_name": raw["Student"].apply(clean_name),
        "activity_date": [d.date() if not pd.isna(d) else None for d, _ in parsed_dates],
        "date_note": [note for _, note in parsed_dates],
        "total_screen_time_min": raw["Total_Screen_Time_Min"].apply(to_int),
        "unlock_count": raw["Unlock_Count"].apply(to_int),
        "do_not_disturb_min": raw["Do_Not_Disturb_Min"].apply(to_int),
    })

    # Range checks. Keep missing DND as NULL because missing is not the same as zero.
    df.loc[df["total_screen_time_min"].lt(0) | df["total_screen_time_min"].gt(1440), "total_screen_time_min"] = None
    df.loc[df["unlock_count"].lt(0), "unlock_count"] = None
    df.loc[df["do_not_disturb_min"].lt(0) | df["do_not_disturb_min"].gt(1440), "do_not_disturb_min"] = None

    df = df.dropna(subset=["student_name", "activity_date", "total_screen_time_min", "unlock_count"])

    for col in ["total_screen_time_min", "unlock_count", "do_not_disturb_min"]:
        df[col] = df[col].astype("Int64")

    duplicate_key = [
        "student_name", "activity_date", "total_screen_time_min",
        "unlock_count", "do_not_disturb_min",
    ]
    df = df.drop_duplicates(subset=duplicate_key, keep="first")
    df = add_row_hash(df, duplicate_key)
    return df.reset_index(drop=True)


def normalize_notification_category(value: object) -> Optional[str]:
    """Standardize the distractive / non-distractive label."""
    text = clean_text(value)
    if text is None:
        return None
    key = re.sub(r"[^a-z]", "", text.lower())
    mapping = {
        "distractive": "Distractive",
        "nondistractive": "Non-distractive",
    }
    return mapping.get(key, text.title())


def normalize_app_name(value: object) -> Optional[str]:
    """Tidy known app-name spelling variants (e.g. 'Linked In' -> 'LinkedIn')."""
    text = clean_text(value)
    if text is None:
        return None
    key = re.sub(r"[^a-z]", "", text.lower())
    mapping = {
        "linkedin": "LinkedIn",
        "whatsapp": "WhatsApp",
        "tiktok": "TikTok",
        "rainalarm": "Rain Alarm",
    }
    return mapping.get(key, text)


def clean_notifications(path: str | Path) -> pd.DataFrame:
    raw = read_csv_clean_headers(path)
    parsed_dates = raw["date"].apply(parse_date_and_note)

    df = pd.DataFrame({
        "source_file": raw["source_file"],
        "source_row_number": raw["source_row_number"],
        "student_name": raw["Student"].apply(clean_name),
        "notification_date": [d.date() if not pd.isna(d) else None for d, _ in parsed_dates],
        "date_note": [note for _, note in parsed_dates],
        "notification_time": raw["time"].apply(parse_time),
        "app_name": raw["app"].apply(normalize_app_name),
        "category": raw["category"].apply(normalize_notification_category),
        "notification_type": raw["Notification type"].apply(clean_text),
        # delay between the notification and the student reacting, in seconds.
        # Negative / NULL / None / blank become NULL.
        "response_delay_sec": raw["How many seconds delay"].apply(to_nonneg_int),
    })

    df = df.dropna(subset=[
        "student_name", "notification_date", "notification_time",
        "app_name", "category",
    ])

    df["response_delay_sec"] = df["response_delay_sec"].astype("Int64")

    duplicate_key = [
        "student_name", "notification_date", "notification_time",
        "app_name", "notification_type",
    ]
    df = df.drop_duplicates(subset=duplicate_key, keep="first")
    df = add_row_hash(df, duplicate_key)
    return df.reset_index(drop=True)


# ---------- PostgreSQL loading ----------

DDL_TEMPLATE = """
CREATE SCHEMA IF NOT EXISTS "{schema}";

CREATE TABLE IF NOT EXISTS {prefix}"study_delay_sessions" (
    id BIGSERIAL PRIMARY KEY,
    source_file TEXT,
    source_row_number INTEGER,
    student_name TEXT NOT NULL,
    session_date DATE NOT NULL,
    date_note TEXT,
    subject TEXT NOT NULL,
    planned_start_time TIME NOT NULL,
    actual_start_time TIME NOT NULL,
    end_time TIME NOT NULL,
    planned_duration_min INTEGER,
    start_delay_min INTEGER,
    actual_duration_min INTEGER,
    row_hash TEXT UNIQUE
);

CREATE TABLE IF NOT EXISTS {prefix}"device_activity" (
    id BIGSERIAL PRIMARY KEY,
    source_file TEXT,
    source_row_number INTEGER,
    student_name TEXT NOT NULL,
    activity_date DATE NOT NULL,
    date_note TEXT,
    total_screen_time_min INTEGER,
    unlock_count INTEGER,
    do_not_disturb_min INTEGER,
    row_hash TEXT UNIQUE
);

CREATE TABLE IF NOT EXISTS {prefix}"notifications" (
    id BIGSERIAL PRIMARY KEY,
    source_file TEXT,
    source_row_number INTEGER,
    student_name TEXT NOT NULL,
    notification_date DATE NOT NULL,
    date_note TEXT,
    notification_time TIME NOT NULL,
    app_name TEXT NOT NULL,
    category TEXT NOT NULL,
    notification_type TEXT,
    response_delay_sec INTEGER,
    row_hash TEXT UNIQUE
);
"""


def create_tables(engine, schema: str, mode: str) -> None:
    from sqlalchemy import text

    schema = ensure_valid_identifier(schema, "schema")
    prefix = f'"{schema}".'
    ddl = DDL_TEMPLATE.format(schema=schema, prefix=prefix)

    with engine.begin() as conn:
        if mode == "replace":
            conn.execute(text(f'DROP TABLE IF EXISTS {prefix}"notifications" CASCADE;'))
            conn.execute(text(f'DROP TABLE IF EXISTS {prefix}"device_activity" CASCADE;'))
            conn.execute(text(f'DROP TABLE IF EXISTS {prefix}"study_delay_sessions" CASCADE;'))
        conn.execute(text(ddl))


def _insert_skip_duplicates(pd_table, conn, keys, data_iter):
    """Custom pandas to_sql insert method for PostgreSQL.

    The schema's row_hash UNIQUE constraint is what makes this pipeline
    idempotent: loading the same file twice must not create duplicate rows.
    The default to_sql behaviour (method="multi") does a single bulk INSERT
    per chunk, so a single duplicate row aborts the *entire* chunk with an
    unhandled IntegrityError, even though the constraint is doing its job
    correctly. This method uses PostgreSQL's INSERT ... ON CONFLICT DO
    NOTHING instead, so already-loaded rows are silently skipped and the
    rest of the chunk still loads. Re-running the pipeline on data that is
    already loaded is then a clean, expected no-op instead of a crash.
    """
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    data = [dict(zip(keys, row)) for row in data_iter]
    if not data:
        return 0
    stmt = pg_insert(pd_table.table).values(data)
    stmt = stmt.on_conflict_do_nothing(index_elements=["row_hash"])
    result = conn.execute(stmt)
    return result.rowcount


def load_to_postgres(tables: dict[str, pd.DataFrame], postgres_url: str,
                     schema: str = "public", mode: str = "replace") -> None:
    """Create tables and append cleaned DataFrames to PostgreSQL.

    Loading is idempotent: rows whose row_hash already exists in the table
    are skipped rather than raising an error, so running this twice on the
    same input is safe and reports how many rows were actually new.
    """
    from sqlalchemy import create_engine

    schema = ensure_valid_identifier(schema, "schema")
    engine = create_engine(postgres_url, pool_pre_ping=True)
    create_tables(engine, schema=schema, mode=mode)

    for table_name, df in tables.items():
        ensure_valid_identifier(table_name, "table name")
        total_rows = len(df)
        inserted = df.to_sql(
            table_name,
            engine,
            schema=schema,
            if_exists="append",
            index=False,
            method=_insert_skip_duplicates,
            chunksize=1000,
        )
        inserted = inserted if inserted is not None else total_rows
        skipped = total_rows - inserted
        if skipped > 0:
            print(f"  {table_name}: {inserted} new row(s) inserted, "
                  f"{skipped} already present (skipped, matched by row_hash)")
        else:
            print(f"  {table_name}: {inserted} new row(s) inserted")


def save_cleaned_csvs(tables: dict[str, pd.DataFrame], output_dir: str | Path) -> None:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    for table_name, df in tables.items():
        df.to_csv(output_dir / f"{table_name}.csv", index=False)


def run_pipeline(delay_path: str, device_path: str, notification_path: str) -> dict[str, pd.DataFrame]:
    return {
        "study_delay_sessions": clean_delay(delay_path),
        "device_activity": clean_device_activity(device_path),
        "notifications": clean_notifications(notification_path),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Clean dirty CSVs and load them into PostgreSQL.")
    parser.add_argument("--delay", required=True, help="Path to Delay_dirty.csv")
    parser.add_argument("--device", required=True, help="Path to Device_Activity_dirty.csv")
    parser.add_argument("--notifications", required=True, help="Path to Notification_dirty.csv")
    parser.add_argument("--postgres-url", default=os.getenv(
                            "POSTGRES_URL",
                            "postgresql+psycopg2://postgres:password@localhost:5432/study_data",
                        ),
                        help="PostgreSQL SQLAlchemy URL. Can also be set as POSTGRES_URL. "
                             "Defaults to the docker-compose.yml credentials in this repo.")
    parser.add_argument("--schema", default=os.getenv("POSTGRES_SCHEMA", "public"),
                        help="PostgreSQL schema name. Default: public")
    parser.add_argument("--mode", choices=["replace", "append"], default="replace",
                        help="replace drops/recreates tables; append adds to existing tables.")
    parser.add_argument("--save-cleaned", default=None,
                        help="Optional folder where cleaned CSV files will be written.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Only clean and summarize the files. Do not load PostgreSQL.")
    args = parser.parse_args()

    tables = run_pipeline(args.delay, args.device, args.notifications)

    print("Cleaned table summary:")
    for name, df in tables.items():
        print(f"- {name}: {len(df)} rows, {len(df.columns)} columns")

    if args.save_cleaned:
        save_cleaned_csvs(tables, args.save_cleaned)
        print(f"Cleaned CSVs written to: {args.save_cleaned}")

    if args.dry_run:
        print("Dry run complete. PostgreSQL load skipped.")
        return

    if not args.postgres_url:
        raise SystemExit("Missing PostgreSQL URL. Set POSTGRES_URL or pass --postgres-url.")

    load_to_postgres(tables, postgres_url=args.postgres_url, schema=args.schema, mode=args.mode)
    print(f"Loaded cleaned data into PostgreSQL schema '{args.schema}' using mode '{args.mode}'.")


if __name__ == "__main__":
    main()
