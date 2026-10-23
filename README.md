# Learning Analytics ETL Pipeline

An ETL pipeline that takes three messy, hand-collected student datasets (study sessions, phone screen time and notifications), cleans them, loads them into PostgreSQL and answers analytical questions about how phone use relates to studying.

Built as a team project for the Introduction to Data Engineering course in the Applied Data Science & AI program at Zuyd University of Applied Sciences, together with Aarya, Alvaro and Calin.

> **About the data:** the original datasets contain personal study and phone-usage data from students, so they are not included in this repository. The code, schema and query logic are complete; the expected input format is described below so the pipeline can be run on your own data.

## What it does

1. **Extract**: reads three raw CSV exports.
2. **Clean**: fixes header whitespace, parses mixed date formats, converts Excel day-fractions to minutes, sets invalid values (negative delays, impossible screen times) to NULL, normalises app and category labels and removes duplicate rows with a SHA-256 row hash.
3. **Load**: writes the cleaned tables into PostgreSQL 16 (run locally with Docker), with a schema and indexes in `sql/schema.sql`.
4. **Analyse**: runs five SQL queries, three of which join several sources on student + date, and benchmarks each one.

## Expected input files

| File | Table | Columns |
|---|---|---|
| Study sessions | `study_delay_sessions` | `Sr.no, Student, date, Subject, Planned Start, Actual Start, End time, Planned duration, start delay, total time taken` |
| Device activity | `device_activity` | `Sr.no, Student, date, Total_Screen_Time_Min, Unlock_Count, Do_Not_Disturb_Min` |
| Notifications | `notifications` | `Sr.no, Student, date, time, app, category, Notification type, How many seconds delay` |

All three sources are keyed on student + date, so they can be joined for cross-source analysis.

## Queries and performance

Average over 5 runs on a local PostgreSQL 16 instance with the original dataset:

| Query | Question | Rows | Avg ms |
|---|---|---|---|
| Q1 | Average and range of daily active study minutes (all students) | 1 | 5.12 |
| Q2 | Scheduled study activity vs. other data sources, per student-day | 34 | 2.10 |
| Q3 | Distractive notifications vs. study start delay, per student-day | 34 | 1.91 |
| Q4 | Per-student screen time vs. actual study time | 10 | 1.68 |
| Q5 | Notification volume by app and category, with average reaction time | 10 | 1.41 |

## Tech stack

Python, Pandas, SQLAlchemy, PostgreSQL 16, Docker, SQL

## How to run

```bash
pip install -r requirements.txt
docker compose up -d
export POSTGRES_URL="postgresql+psycopg2://postgres:password@localhost:5432/study_data"

python src/postgres_clean_load_pipeline.py \
  --delay path/to/study_sessions.csv \
  --device path/to/device_activity.csv \
  --notifications path/to/notifications.csv \
  --schema public --mode replace

python src/queries.py --runs 5 --save results
```

Add `--save-cleaned data/processed --dry-run` to the pipeline command to produce cleaned CSVs without a database.

## Project structure

```
sql/schema.sql    table definitions and indexes
src/              pipeline and query scripts
results/          query timings
```
