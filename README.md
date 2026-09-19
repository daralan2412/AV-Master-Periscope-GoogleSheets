# AV-Master-Periscope-GoogleSheets

Pulls the Sisense/Periscope shared report **"AV - Master Report - In Progress"**
into per-month Google Sheets, four times a day, on GitHub Actions.

This is a **new, independent pipeline**. It shares nothing with
`daralan2412/CM-Master-Periscope-Googlesheets` (Copa),
`daralan2412/FX-periscope-googlesheets` (FedEx REP-1901) or
`daralan2412/periscope-to-sheets` (PTY wheelchair) - different report,
different sheets, different Apps Script project, different secrets.

## What it does

| | |
|---|---|
| Source | https://app.periscopedata.com/shared/924434c7-8bb2-43c5-baf0-5ca251356bc6 (password gated; widget "Data", 46 columns) |
| Window | **D-2 to D0** (today, yesterday, the day before; America/Bogota) **plus one rotating 2-day backfill chunk** per run (D-4..D-3, D-6..D-5, D-8..D-7, D-10..D-9 by slot), so every day is re-synced again up to ten days after the fact. **Every window is scraped one day at a time** - Sisense refuses to render this widget above 10,000 rows and AV produces ~3-4k rows/day |
| Schedule | **00:07, 06:07, 12:07, 18:07 America/Bogota** (`7 5,11,17,23 * * *` UTC; :07 avoids GitHub's top-of-hour queue that delayed the CM runs by hours) + manual `workflow_dispatch` (optional `backfill_start` / `backfill_end` inputs, MM/DD/YYYY, for a one-off catch-up) |
| Target | Drive folder https://drive.google.com/drive/folders/1m7cpPLZsSEOfVL2ENSWwkq8wCbLWAo3a - one file per month, `<M>_<YYYY>_AV` (`8_2026_AV`, `9_2026_AV`, ...), tab `DATA` (the one whose A1 is `mission_sas_id`) |
| Columns | **standard 46-column schema** (v2, 2026-09-19): 21 fixed columns + `task_1..task_25`, same order as the report's CSV, mapped **by name**. Typed for dashboards: `mission_sas_id`, `arr_flt`, `dep_flt` are numbers; `date` is a real Date (`yyyy-mm-dd`); `arr_time`, `dep_time`, `assign_time`, `start_time`, `finish_time` are real datetimes (`yyyy-mm-dd HH:mm`); everything else (incl. `gate`) is text. Spreadsheet time zone America/Bogota, tab `DATA`, row 1 frozen |
| Routing | each row goes to the file matching **its own `date` column** - a run on 1 Oct that pulls 30 Sep + 1 Oct rows writes to `9_2026_AV` **and** `10_2026_AV` |
| Dedupe | **upsert** by `mission_sas_id`: an id already in the file is overwritten in place, new ids are appended (freshest scrape wins, file never grows with duplicates) |
| Cleanup | any row whose `date` is from a different month than the file it sits in is **deleted** |
| Missing file | a month file that doesn't exist yet is created in the folder with the header row |

## How it works

1. `scrape_and_upload.py` (GitHub Actions, Python 3.11 + Playwright Chromium)
   opens the report, submits the password on Sisense's `verify-password`
   page (from the `PERISCOPE_PASSWORD` secret), picks **Custom Range** in the
   Date Range filter, types the window's start/end (MM/DD/YYYY), applies,
   waits for the Data widget to settle, clicks its **Download Data** CSV
   export and polls the `/download_csv/` URL until it returns 200 - once per
   day (three primary days + two backfill days per run).
2. It maps the CSV onto the 46-column standard header by name and POSTs
   `{"rows": [[...46 cols...], ...]}` (text, as exported) to the Apps Script Web App
   (`apps-script/Code.gs`) with `?token=`. Every Web App call is retried up
   to 4 times: Apps Script answers through a one-time `script.googleusercontent.com`
   redirect that Google intermittently 404s (seen on the CM pipeline's runs
   #61/#62/#64 and on this pipeline's very first test POST).
3. The Web App groups rows by month (from column B `date`) and upserts each
   group into its monthly file (existing ids overwritten in place, new ids
   appended), then runs a cleanup over columns A:B of every touched file
   plus the current month's file: wrong-month rows and any stray duplicate
   `mission_sas_id` rows are deleted. Values are **typed on write**
   (`typeRow_` in Code.gs): dates/datetimes become real Date cells in the
   Bogota time zone, ids/flight numbers become numbers, text columns are
   written with the `@` format so nothing is ever locale-parsed.

The browser-driving code is the hardened logic from the FedEx / CM pipelines
(see the module docstring for the Sisense traps it works around: hidden
`.error-message` placeholder, datepicker ignoring `.fill()`, wrong breadcrumb
selector, slow default query at scheduled hours).

## Files

- `scrape_and_upload.py` - the scraper (v2, 2026-09-19; based on CM-Master v1.5).
- `.github/workflows/run.yml` - schedule + manual trigger; uploads
  `debug_failure.png/.html` as artifacts when a run fails.
- `apps-script/Code.gs`, `apps-script/appsscript.json` - the Web App
  (copy of what is deployed; editing here does not redeploy it).
- `requirements.txt` - `requests`, `playwright`.

## Secrets (GitHub > Settings > Secrets and variables > Actions)

- `SHEETS_WEBAPP_URL` - the Web App `/exec` URL.
- `WEBAPP_TOKEN` - must equal the Apps Script Script Property `AUTH_TOKEN`.
- `PERISCOPE_PASSWORD` - the shared report's password.

## Reading a run

A green check is **not** proof of success (the script exits 0 when the
widget genuinely has no rows). Open the "Run scrape and upload" step and
look for:

```
Windows this run (America/Bogota): primary D-2..D0 = 09/17/2026..09/17/2026; primary D-2..D0 = 09/18/2026..09/18/2026; primary D-2..D0 = 09/19/2026..09/19/2026; backfill slot 0 D-4..D-3 = 09/15/2026..09/15/2026; backfill slot 0 D-4..D-3 = 09/16/2026..09/16/2026
Pulling 'AV - Master Report' data for 09/17/2026 to 09/17/2026 (primary D-2..D0)...
Scraped 3812 rows for 09/17/2026 to 09/17/2026.
Posted 3812 rows: 3790 updated in place, 22 appended; 0 stray duplicate row(s) removed, 0 wrong-month row(s) removed, 0 row(s) had no readable date and were skipped.
  9_2026_AV: {'rows_received': 3812, 'rows_updated': 3790, 'rows_appended': 22, 'duplicates_removed': 0, 'wrong_month_removed': 0, 'total_rows': 9847}
```

(one such block per day-window). A `TooManyRowsError ... Result set too large`
line means a single day exceeded Sisense's 10,000-row widget limit - that
window is not retried; the fix is a smaller `MAX_WINDOW_DAYS` (already 1) or
splitting the report.

Large "updated in place" counts are normal - every run re-posts the last
three days plus an older 2-day chunk; "appended" is what is actually new since the previous run.
A scrape step that finishes in single-digit seconds did not do the work.
A `FAILED: Periscope password was not accepted` line means the
`PERISCOPE_PASSWORD` secret is wrong or the report's password changed.

## Admin actions (GET, token-gated)

- `?token=...` - health check.
- `?token=...&action=rebuild&file=9_2026_AV` - run the dedupe +
  wrong-month cleanup on one file without posting anything.
- `?token=...&action=inspect&file=9_2026_AV` - row/column counts, header,
  time zone and the types of the first data row.
- `?token=...&action=backup&file=9_2026_AV` - copies the file into the
  `BACKUP` subfolder as `9_2026_AV_backup_<yyyymmdd>`.
- `?token=...&action=prepare|standardize|restore|finalize&file=...` - the
  one-off migration used on 2026-09-19 to rewrite all 2026 files into the
  standard schema (`standardize` takes `start`/`count` and is driven in
  20k-row blocks; see the Code.gs header). Safe to re-run, idempotent.

## Schema history (why the files look the way they do)

Before this pipeline the monthly files were built by hand with 48 columns:
a duplicated `task_15` header at column 37 and an always-empty `USU_MES`
at 48. Column 37 held a *copy* of task_15 in the Jan-Apr files but the
*real* task_16 in May-Jul and Aug 1-5 (26 contiguous task slots), and the
v1 scraper - mapping by header name - left it blank and put task_16 one
column to the right. Dates were native Sheets dates in a Los Angeles time
zone and accented text was mojibake (`RevisiÃ³n`). On 2026-09-19 every
2026 file was backed up to `BACKUP/` and rewritten in place into the 46-column
typed schema above (per-row realignment rule in `realign48_`, UTF-8 repair
in `fixEncoding_`), so dashboards can treat all months identically.

## Known edge cases

- Around midnight Bogota time the window still covers both days, so a
  mission logged at 23:50 is picked up by the 00:07 run.
- Periscope's `date` column decides the file, not the run time.
- Rows with a blank/unreadable `date` are not written anywhere (counted as
  `rows_unroutable` in the log).
- GitHub fires scheduled runs minutes to hours late; judge a run by the
  window it logged, not by the wall clock.
- Apps Script deployments are pinned to a version: after editing `Code.gs`
  in the Apps Script editor, **Deploy > Manage deployments > Edit > New
  version**, or the live `/exec` keeps running the old code.
