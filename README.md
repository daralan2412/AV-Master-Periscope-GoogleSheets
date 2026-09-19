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
| Window | **D-2 to D0** (today, yesterday, the day before; America/Bogota) **plus one rotating 3-day backfill chunk** per run (D-5..D-3, D-8..D-6, D-11..D-9, D-14..D-12 by slot), so every day is re-synced again up to two weeks after the fact |
| Schedule | **00:07, 06:07, 12:07, 18:07 America/Bogota** (`7 5,11,17,23 * * *` UTC; :07 avoids GitHub's top-of-hour queue that delayed the CM runs by hours) + manual `workflow_dispatch` (optional `backfill_start` / `backfill_end` inputs, MM/DD/YYYY, for a one-off catch-up) |
| Target | Drive folder https://drive.google.com/drive/folders/1m7cpPLZsSEOfVL2ENSWwkq8wCbLWAo3a - one file per month, `<M>_<YYYY>_AV` (`8_2026_AV`, `9_2026_AV`, ...), tab whose A1 is `mission_sas_id` |
| Columns | the files' 48-column header (which has a duplicated `task_15` and a trailing `USU_MES`); the report's 46 CSV columns are mapped onto it **by name**, the two extra columns stay blank |
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
   export and polls the `/download_csv/` URL until it returns 200 - once for
   the primary D-2..D0 window and once for the backfill chunk.
2. It maps the CSV onto the 48-column sheet header by name and POSTs
   `{"rows": [[...48 cols...], ...]}` to the Apps Script Web App
   (`apps-script/Code.gs`) with `?token=`. Every Web App call is retried up
   to 4 times: Apps Script answers through a one-time `script.googleusercontent.com`
   redirect that Google intermittently 404s (seen on the CM pipeline's runs
   #61/#62/#64 and on this pipeline's very first test POST).
3. The Web App groups rows by month (from column B `date`) and upserts each
   group into its monthly file (existing ids overwritten in place, new ids
   appended), then runs a cleanup over columns A:B of every touched file
   plus the current month's file: wrong-month rows and any stray duplicate
   `mission_sas_id` rows are deleted. Every write is preceded by
   `setNumberFormat('@')` so Sheets never turns `09/03/2026` into a Date.

The browser-driving code is the hardened logic from the FedEx / CM pipelines
(see the module docstring for the Sisense traps it works around: hidden
`.error-message` placeholder, datepicker ignoring `.fill()`, wrong breadcrumb
selector, slow default query at scheduled hours).

## Files

- `scrape_and_upload.py` - the scraper (v1, 2026-09-18; based on CM-Master v1.5).
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
Windows this run (America/Bogota): primary D-2..D0 = 09/16/2026..09/18/2026; backfill slot 3 D-14..D-12 = 09/04/2026..09/06/2026
Pulling 'AV - Master Report' data for 09/16/2026 to 09/18/2026 (primary D-2..D0)...
Scraped 1234 rows for 09/16/2026 to 09/18/2026.
Posted 1234 rows: 1100 updated in place, 134 appended; 0 stray duplicate row(s) removed, 0 wrong-month row(s) removed, 0 row(s) had no readable date and were skipped.
  9_2026_AV: {'rows_received': 1234, 'rows_updated': 1100, 'rows_appended': 134, 'duplicates_removed': 0, 'wrong_month_removed': 0, 'total_rows': 5678}
```

Large "updated in place" counts are normal - every run re-posts the last
three days plus an older 3-day chunk; "appended" is what is actually new since the previous run.
A scrape step that finishes in single-digit seconds did not do the work.
A `FAILED: Periscope password was not accepted` line means the
`PERISCOPE_PASSWORD` secret is wrong or the report's password changed.

## Admin actions (GET, token-gated)

- `?token=...` - health check.
- `?token=...&action=rebuild&file=9_2026_AV` - run the dedupe +
  wrong-month cleanup on one file without posting anything.
- `?token=...&action=restore_text&file=9_2026_AV` - one-off repair that
  turns any Date/number cells back into the text form (`MM/dd/yyyy`,
  `MM/dd/yyyy HH:mm`) and marks the data range as plain text.

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
# AV-Master-Periscope-GoogleSheets
Periscope AV Master Report to monthly Google Sheets (M_YYYY_AV), 4x daily via GitHub Actions and Apps Script
