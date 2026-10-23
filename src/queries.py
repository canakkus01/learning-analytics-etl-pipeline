"""
Run the project's analysis queries against the PostgreSQL database and measure
their performance.

Implements five queries over the three loaded sources
(study_delay_sessions, device_activity, notifications):

  Q1  [MANDATORY] Average and range of daily active study minutes across all
                  students over the whole dataset.
  Q2  [MANDATORY] How a student's scheduled study activity (sessions + minutes)
                  relates to their other data sources (screen time, unlocks,
                  notifications) on the same day.
  Q3  Do days with more distractive notifications line up with bigger study
      start delays?  (notifications + study_delay_sessions)
  Q4  Per-student screen time vs. actual study time.  (device + study)
  Q5  Which apps generate the most notifications, split by distractive /
      non-distractive, and how quickly students react.  (notifications)

Queries Q2, Q3 and Q4 each combine two or more different data sources, which
satisfies the "at least two different personal data sources" requirement.

Each query is executed several times; the script reports the average and
fastest run in milliseconds plus the row count, which is what goes into the
test report.

Usage
-----
   export POSTGRES_URL="postgresql+psycopg2://postgres:password@localhost:5432/study_data"
   python queries.py                 # run all queries, print results + timings
   python queries.py --runs 10       # time each query over 10 runs
   python queries.py --save results  # also write each result + the timing table to ./results
"""

from __future__ import annotations

import argparse
import os
import time
from pathlib import Path

import pandas as pd

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass


# ---------- the five queries ----------

QUERIES = [
    {
        "id": "Q1",
        "title": "Average and range of daily active study minutes (all students)",
        "sources": "study_delay_sessions",
        "sql": """
            WITH daily AS (
                SELECT student_name,
                       session_date,
                       SUM(actual_duration_min) AS active_minutes
                FROM study_delay_sessions
                GROUP BY student_name, session_date
            )
            SELECT
                ROUND(AVG(active_minutes), 1)              AS avg_daily_active_min,
                MIN(active_minutes)                        AS min_daily_active_min,
                MAX(active_minutes)                        AS max_daily_active_min,
                MAX(active_minutes) - MIN(active_minutes)  AS range_daily_active_min,
                COUNT(*)                                   AS student_days
            FROM daily;
        """,
    },
    {
        "id": "Q2",
        "title": "Scheduled study activity vs. other data sources, per student-day",
        "sources": "study_delay_sessions + device_activity + notifications",
        "sql": """
            WITH study AS (
                SELECT student_name, session_date,
                       COUNT(*)                 AS study_sessions,
                       SUM(actual_duration_min) AS study_minutes
                FROM study_delay_sessions
                GROUP BY student_name, session_date
            ),
            notif AS (
                SELECT student_name, notification_date,
                       COUNT(*) FILTER (WHERE category = 'Distractive') AS distractive_notifs,
                       COUNT(*)                                         AS total_notifs
                FROM notifications
                GROUP BY student_name, notification_date
            )
            SELECT s.student_name,
                   s.session_date,
                   s.study_sessions,
                   s.study_minutes,
                   d.total_screen_time_min,
                   d.unlock_count,
                   COALESCE(n.distractive_notifs, 0) AS distractive_notifs,
                   COALESCE(n.total_notifs, 0)       AS total_notifs
            FROM study s
            LEFT JOIN device_activity d
                   ON d.student_name = s.student_name
                  AND d.activity_date = s.session_date
            LEFT JOIN notif n
                   ON n.student_name = s.student_name
                  AND n.notification_date = s.session_date
            ORDER BY s.student_name, s.session_date;
        """,
    },
    {
        "id": "Q3",
        "title": "Distractive notifications vs. study start delay, per student-day",
        "sources": "notifications + study_delay_sessions",
        "sql": """
            WITH notif AS (
                SELECT student_name, notification_date,
                       COUNT(*) FILTER (WHERE category = 'Distractive') AS distractive_notifs
                FROM notifications
                GROUP BY student_name, notification_date
            ),
            delay AS (
                SELECT student_name, session_date,
                       ROUND(AVG(start_delay_min), 1) AS avg_start_delay_min,
                       COUNT(*)                        AS sessions
                FROM study_delay_sessions
                GROUP BY student_name, session_date
            )
            SELECT d.student_name,
                   d.session_date,
                   COALESCE(n.distractive_notifs, 0) AS distractive_notifs,
                   d.avg_start_delay_min,
                   d.sessions
            FROM delay d
            LEFT JOIN notif n
                   ON n.student_name = d.student_name
                  AND n.notification_date = d.session_date
            ORDER BY distractive_notifs DESC, d.avg_start_delay_min DESC;
        """,
    },
    {
        "id": "Q4",
        "title": "Per-student screen time vs. actual study time",
        "sources": "device_activity + study_delay_sessions",
        "sql": """
            WITH study AS (
                SELECT student_name,
                       SUM(actual_duration_min)       AS total_study_min,
                       COUNT(DISTINCT session_date)   AS study_days
                FROM study_delay_sessions
                GROUP BY student_name
            ),
            device AS (
                SELECT student_name,
                       ROUND(AVG(total_screen_time_min), 1) AS avg_screen_min,
                       ROUND(AVG(unlock_count), 1)          AS avg_unlocks
                FROM device_activity
                GROUP BY student_name
            )
            SELECT s.student_name,
                   s.total_study_min,
                   s.study_days,
                   ROUND(s.total_study_min::numeric / s.study_days, 1) AS avg_study_min_per_day,
                   dv.avg_screen_min,
                   dv.avg_unlocks
            FROM study s
            JOIN device dv ON dv.student_name = s.student_name
            ORDER BY dv.avg_screen_min DESC;
        """,
    },
    {
        "id": "Q5",
        "title": "Notification volume by app and category, with average reaction time",
        "sources": "notifications",
        "sql": """
            SELECT app_name,
                   category,
                   COUNT(*)                              AS notif_count,
                   ROUND(AVG(response_delay_sec), 1)     AS avg_response_delay_sec
            FROM notifications
            GROUP BY app_name, category
            ORDER BY notif_count DESC, app_name
            LIMIT 10;
        """,
    },
]


# ---------- runner ----------

def get_engine(postgres_url: str):
    from sqlalchemy import create_engine
    return create_engine(postgres_url, pool_pre_ping=True)


def run_and_time(engine, sql: str, runs: int) -> tuple[pd.DataFrame, float, float]:
    """Execute a query `runs` times, returning the result plus avg and best ms."""
    durations = []
    result = None
    for _ in range(runs):
        start = time.perf_counter()
        result = pd.read_sql(sql, engine)
        durations.append((time.perf_counter() - start) * 1000.0)
    return result, sum(durations) / len(durations), min(durations)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run and time the project analysis queries.")
    parser.add_argument("--postgres-url", default=os.getenv(
                            "POSTGRES_URL",
                            "postgresql+psycopg2://postgres:password@localhost:5432/study_data",
                        ),
                        help="PostgreSQL SQLAlchemy URL. Can also be set as POSTGRES_URL. "
                             "Defaults to the docker-compose.yml credentials in this repo.")
    parser.add_argument("--runs", type=int, default=5,
                        help="How many times to run each query for timing. Default: 5")
    parser.add_argument("--max-display-rows", type=int, default=20,
                        help="Cap on rows printed per query (timing still uses the full query).")
    parser.add_argument("--save", default=None,
                        help="Optional folder to write each query result and the timing table.")
    args = parser.parse_args()

    if not args.postgres_url:
        raise SystemExit("Missing PostgreSQL URL. Set POSTGRES_URL or pass --postgres-url.")

    pd.set_option("display.width", 200)
    pd.set_option("display.max_columns", 30)

    engine = get_engine(args.postgres_url)
    save_dir = Path(args.save) if args.save else None
    if save_dir:
        save_dir.mkdir(parents=True, exist_ok=True)

    timing_rows = []

    for q in QUERIES:
        df, avg_ms, best_ms = run_and_time(engine, q["sql"], args.runs)
        timing_rows.append({
            "query": q["id"],
            "title": q["title"],
            "rows_returned": len(df),
            "avg_ms": round(avg_ms, 2),
            "best_ms": round(best_ms, 2),
        })

        print("=" * 100)
        print(f"{q['id']}: {q['title']}")
        print(f"sources: {q['sources']}  |  rows: {len(df)}  |  "
              f"avg {avg_ms:.2f} ms over {args.runs} runs  (best {best_ms:.2f} ms)")
        print("-" * 100)
        shown = df.head(args.max_display_rows)
        print(shown.to_string(index=False))
        if len(df) > len(shown):
            print(f"... ({len(df) - len(shown)} more rows)")
        print()

        if save_dir:
            df.to_csv(save_dir / f"{q['id']}_result.csv", index=False)

    timing = pd.DataFrame(timing_rows)
    print("=" * 100)
    print("PERFORMANCE SUMMARY (for the test report)")
    print("-" * 100)
    print(timing.to_string(index=False))

    if save_dir:
        timing.to_csv(save_dir / "query_performance.csv", index=False)
        print(f"\nResults and timing table written to: {save_dir}")


if __name__ == "__main__":
    main()
