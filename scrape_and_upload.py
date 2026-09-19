#!/usr/bin/env python3
"""
scrape_and_upload.py - AV-Master-Periscope-GoogleSheets

NEW pipeline, independent of daralan2412/CM-Master-Periscope-Googlesheets
(Copa), daralan2412/FX-periscope-googlesheets (FedEx REP-1901) and
daralan2412/periscope-to-sheets (PTY wheelchair). Those are left untouched;
this repo shares no secrets, sheet or Apps Script with them.

Source: Periscope/Sisense shared report "AV - Master Report - In Progress"
        https://app.periscopedata.com/shared/924434c7-8bb2-43c5-baf0-5ca251356bc6
        This report is PASSWORD GATED: the first load redirects to
        .../verify-password with a single <input name="password"> and a
        "VIEW DASHBOARD" submit button. The password comes from the
        PERISCOPE_PASSWORD GitHub secret - it is never written in this repo.
Target: one Google Sheet per month in Drive folder
        https://drive.google.com/drive/folders/1m7cpPLZsSEOfVL2ENSWwkq8wCbLWAo3a
        named "<M>_<YYYY>_AV" (8_2026_AV, 9_2026_AV, ...). The scraper does
        NOT pick the file - it posts every row to the Apps Script Web App
        (SHEETS_WEBAPP_URL / WEBAPP_TOKEN secrets) and the Web App routes
        each row by the row's own "date" column, so a run on 2026-10-01 that
        pulls 09/30 and 10/01 rows lands them in 9_2026_AV and 10_2026_AV.

Flow (runs 4x a day: 00:07, 06:07, 12:07, 18:07 America/Bogota - see run.yml):
  1. Open the report, submit the password, set the Date Range filter to a
     rolling "D-2 to D0" window (America/Bogota) plus one older 3-day backfill
     chunk (see BACKFILL_*) via Custom Range with computed Start/End dates, then use the Data widget's own "Download
     Data" CSV export (NOT DOM scraping - the grid is virtualized, only the
     rows and columns near the viewport exist in the DOM; the CSV is
     generated server-side and is complete).
  2. Map the CSV's 46 columns onto the monthly files' 48-column header BY
     NAME (see SHEET_HEADERS / map_csv_to_sheet) and POST
     {"rows": [[...48 cols...], ...]} to the Web App. The Web App upserts
     each row into its monthly file by mission_sas_id, then deletes any row
     whose "date" belongs to another month. Re-pulling D-2..D0 four times a
     day is therefore safe and expected - large "updated in place" counts
     in the log are the normal steady state.

The browser-driving code is the hardened v4.1 logic from the FedEx / CM
pipelines (2026-09-02 / 2026-09-04), verbatim except for the password step,
URL, timezone and column mapping, because the reports are the same Sisense
template with the same DOM (confirmed live 2026-09-18 on this report):
  - do NOT wait for the grid / "networkidle" before applying our filter (the
    default "All Dates" query is slow at scheduled hours and we replace it);
  - "Custom Range" lives in .custom-date-option, NOT inside .radio-button-group;
  - type the dates with press_sequentially (the datepicker ignores .fill());
    no Escape (it can clear the field), no Tab; click-into-next-field commits;
  - wait for .apply-button to lose "disabled" before clicking it;
  - the real breadcrumb is ".filters-bar .filter-group .filter .label"
    (".filters-bar-label" only ever reads "Filters (N)");
  - ".error-message" is ALWAYS in the DOM (display:none) - check visibility,
    never existence, or every run silently reports "no rows";
  - whole-scrape retry with a fresh browser (SCRAPE_ATTEMPTS);
  - every Web App call retried (webapp_request) because Apps Script's
    googleusercontent "echo" redirect intermittently 404s (CM runs #61/#62/#64);
  - ONE DAY PER WINDOW (MAX_WINDOW_DAYS = 1). Unlike CM/FedEx, this report
    is big: Sisense refuses to render the widget above 10,000 rows ("Result
    set too large. Data displayed in web browser is limited to 5MB, please
    select fewer than 10,000 rows.") and then offers no "Download Data".
    Run #1 (2026-09-19) pulled 9,825 rows for a 3-day window and its 3-day
    backfill chunk hit the limit three times. AV is ~3-4k rows/day, so every
    range is split into single-day scrapes, and the limit message is
    detected and raised as a non-retryable error (see TooManyRowsError).
"""

import csv
import io
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests
from playwright.sync_api import sync_playwright

PERISCOPE_URL = "https://app.periscopedata.com/shared/924434c7-8bb2-43c5-baf0-5ca251356bc6"
LOCAL_TZ = ZoneInfo("America/Bogota")  # BOG station time; UTC-5 all year (no DST).
# PRIMARY window = today back through LOOKBACK_DAYS days ago, inclusive.
# Same as the CM-Master pipeline: the Sisense source publishes rows with a lag
# of hours, D0 is often thin and D-1 keeps filling during D0. Also, the
# report's Date Range filter is on a UTC finish timestamp, not the "date"
# column, so a mission dated D-1 that finished after 19:00 local only shows
# up in a window that starts on D0. D-2 covers both.
LOOKBACK_DAYS = 2  # "D-2 to D0": the day before yesterday, yesterday and today, inclusive.

# BACKFILL window - one extra, older 3-day chunk per run, rotating by slot.
# WHY (CM-Master audit, 2026-09-19): ~1% of rows only appear in the source
# 6-7 days after their date, i.e. after the D-2..D0 window has moved past
# them. Each run therefore also re-pulls one older chunk chosen by the run's
# 6-hour slot (00:07 -> D-4..D-3, 06:07 -> D-6..D-5, 12:07 -> D-8..D-7,
# 18:07 -> D-10..D-9), so every day is re-synced again at 3-4, 5-6, 7-8
# and 9-10 days of age. Every range is scraped one day at a time
# (MAX_WINDOW_DAYS) because of the 10,000-row widget limit. Rows are
# upserted, so this only ever adds/refreshes.
BACKFILL_CHUNK_DAYS = 2  # days of backfill per run (scraped one day at a time)
BACKFILL_SLOTS = 4  # = number of runs per day -> D-3..D-10 re-synced daily
# Sisense will not render (and cannot export) more than 10,000 rows in the
# Data widget, and this report produces ~3-4k rows per day. Every window is
# therefore cut into MAX_WINDOW_DAYS-day pieces before scraping.
MAX_WINDOW_DAYS = 1
TOO_MANY_ROWS_MARKER = "Result set too large"
# Manual override for a one-off catch-up (workflow_dispatch inputs or env):
# BACKFILL_START="09/01/2026" BACKFILL_END="09/12/2026" replaces the rotating
# chunk with that exact range (scraped in BACKFILL_CHUNK_DAYS pieces).
SCRAPE_ATTEMPTS = 3  # whole-scrape retries with a fresh browser.
SCRAPE_RETRY_DELAY_S = 60
WEBAPP_URL = os.environ["SHEETS_WEBAPP_URL"]
WEBAPP_TOKEN = os.environ["WEBAPP_TOKEN"]
PERISCOPE_PASSWORD = os.environ["PERISCOPE_PASSWORD"]

# Header row EXACTLY as it exists in the monthly "<M>_<YYYY>_AV" files
# (48 columns). It carries two quirks the report's CSV does not have: a
# duplicated "task_15" column (position 37) and a trailing "USU_MES"
# column. Rows are mapped onto this header BY NAME (first occurrence of a
# name wins; the second "task_15" and "USU_MES" are left blank), so the
# report's 46 CSV columns can never be shifted by those extra columns.
SHEET_HEADERS = [
    "mission_sas_id", "date", "station", "airline_code", "tail_number", "vessel_description",
    "gate", "job_name", "mission_name", "arr_flt", "dep_flt", "org_city", "dest_city",
    "arr_time", "dep_time", "disp_name", "agent_name", "mission_notes", "assign_time",
    "start_time", "finish_time",
    "task_1", "task_2", "task_3", "task_4", "task_5", "task_6", "task_7", "task_8", "task_9",
    "task_10", "task_11", "task_12", "task_13", "task_14", "task_15",
    "task_15",  # duplicated in the files' header - left blank
    "task_16", "task_17", "task_18", "task_19", "task_20", "task_21", "task_22", "task_23",
    "task_24", "task_25",
    "USU_MES",  # not in the report - left blank
]

# Column ORDER of the report's Data widget / CSV export (confirmed live
# 2026-09-18 against the grid header: mission sas id, date, station, airline
# code, tail number, vessel description, gate, job name, mission name, arr
# flt, dep flt, org city, dest city, arr time, dep time, disp name, agent
# name, mission notes, assign time, start time, finish time, task 1..task 25).
CSV_HEADERS = SHEET_HEADERS[:36] + SHEET_HEADERS[37:47]  # 46 columns
assert len(CSV_HEADERS) == 46 and len(SHEET_HEADERS) == 48


def _norm(h: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", h.strip().lower()).strip("_")


WEBAPP_ATTEMPTS = 4
WEBAPP_RETRY_DELAY_S = 45


class PermanentWebAppError(RuntimeError):
    """The Web App answered in a way a retry cannot fix (bad token, doPost error)."""


def webapp_request(method: str, timeout: int, **kwargs) -> dict:
    """GET/POST the Apps Script Web App and return its JSON, retrying transients.

    WHY THIS EXISTS. Three of the four scheduled runs of 2026-09-18/19
    (#61, #62, #64) went red with exactly this:
        FAILED: 404 Client Error: Not Found for url:
        https://script.googleusercontent.com/macros/echo?user_content_key=...
    Apps Script does not answer on the /exec URL itself - it 302-redirects
    to a one-time googleusercontent "echo" URL that carries the reply, and
    Google intermittently 404s that redirect. #61 and #62 died on the
    tiny health-check GET before scraping anything; #64 died on the POST
    *after* the Web App had already written all 6,505 rows (the sheet's
    modified time matches). Twelve back-to-back GETs from another machine
    all returned 200, so it is a transient, not a broken deployment.

    Both calls are safe to repeat: the GET is read-only and the POST is an
    upsert by mission_sas_id, so a retry after an ambiguous failure can at
    worst rewrite identical rows.

    Retryable: connection errors, timeouts, HTTP 5xx / 429 / 408 / 404, a
    non-JSON body, and the Web App's own "another upload is in progress"
    lock timeout. Not retryable (raised as PermanentWebAppError): 401/403
    and any other 4xx, and an explicit success:false from doGet/doPost.
    """
    last_err = None
    for attempt in range(1, WEBAPP_ATTEMPTS + 1):
        try:
            resp = requests.request(
                method, WEBAPP_URL, params={"token": WEBAPP_TOKEN}, timeout=timeout, **kwargs
            )
            if resp.status_code >= 500 or resp.status_code in (404, 408, 429):
                raise RuntimeError(
                    f"HTTP {resp.status_code} from Web App ({resp.url[:60]}...): {resp.text[:160]!r}"
                )
            if resp.status_code >= 400:
                raise PermanentWebAppError(
                    f"Web App {method} rejected with HTTP {resp.status_code}: {resp.text[:160]!r}"
                )
            try:
                data = resp.json()
            except ValueError:
                raise RuntimeError(f"non-JSON reply from Web App (HTTP {resp.status_code}): {resp.text[:160]!r}")
            if not data.get("success"):
                err = str(data.get("error", ""))
                if "lock timeout" in err or "in progress" in err:
                    raise RuntimeError(f"Web App busy: {err}")
                raise PermanentWebAppError(f"Web App {method} failed: {data}")
            if method == "POST" and "rows_received" not in data:
                # Seen on AV run #3 (2026-09-19, 09/18 window): the reply was
                # the doGet health-check body ({"success":true,"message":"ok"})
                # instead of doPost's counts - the googleusercontent redirect
                # was answered as a GET. Whether the rows were written is
                # unknown, and the upsert is idempotent, so post again.
                raise RuntimeError(f"Web App POST answered without counts (treated as not written): {data}")
            return data
        except PermanentWebAppError:
            raise
        except Exception as exc:  # noqa: BLE001 - the transient classes listed above
            last_err = exc
            print(f"Web App {method} attempt {attempt}/{WEBAPP_ATTEMPTS} failed: {exc}", file=sys.stderr)
            if attempt < WEBAPP_ATTEMPTS:
                print(f"Retrying in {WEBAPP_RETRY_DELAY_S}s...", file=sys.stderr)
                time.sleep(WEBAPP_RETRY_DELAY_S)
    raise RuntimeError(f"Web App {method} failed after {WEBAPP_ATTEMPTS} attempts: {last_err}")


def check_token():
    """Cheap pre-flight auth check before paying for a headless-browser scrape.

    doGet() no longer decides what to pull (there's no watermark anymore),
    it's just a token/connectivity health check now. Retried like the POST
    (see webapp_request): runs #61/#62 died right here on the echo-URL 404.
    """
    webapp_request("GET", timeout=30)


def _fmt(d):
    return d.strftime("%m/%d/%Y")


def compute_date_range_mmddyyyy():
    """Primary window: D-LOOKBACK_DAYS to D0 (America/Bogota), MM/DD/YYYY for
    Periscope's Custom Range Start/End Date inputs. Computed fresh on every
    call so the window is always "as of right now".
    """
    today = datetime.now(LOCAL_TZ).date()
    return _fmt(today - timedelta(days=LOOKBACK_DAYS)), _fmt(today)


def compute_windows():
    """All (label, start, end) windows this run must pull, primary first.

    - primary: D-2..D0 (see LOOKBACK_DAYS)
    - backfill: BACKFILL_START/BACKFILL_END if set (manual catch-up, cut into
      BACKFILL_CHUNK_DAYS pieces), else the rotating chunk for this run's
      slot (see BACKFILL_CHUNK_DAYS). Slot = Bogota hour // 6, which still
      lands right when GitHub starts a scheduled run a couple of hours late.
    """
    now = datetime.now(LOCAL_TZ)
    today = now.date()
    windows = [("primary D-%d..D0" % LOOKBACK_DAYS, _fmt(today - timedelta(days=LOOKBACK_DAYS)), _fmt(today))]

    bf_start, bf_end = os.environ.get("BACKFILL_START", "").strip(), os.environ.get("BACKFILL_END", "").strip()
    if bf_start and bf_end:
        a = datetime.strptime(bf_start, "%m/%d/%Y").date()
        b = datetime.strptime(bf_end, "%m/%d/%Y").date()
        while a <= b:
            c = min(a + timedelta(days=BACKFILL_CHUNK_DAYS - 1), b)
            windows.append(("manual backfill", _fmt(a), _fmt(c)))
            a = c + timedelta(days=1)
        return split_windows(windows)

    slot = (now.hour // (24 // BACKFILL_SLOTS)) % BACKFILL_SLOTS
    end = today - timedelta(days=LOOKBACK_DAYS + 1 + slot * BACKFILL_CHUNK_DAYS)
    start = end - timedelta(days=BACKFILL_CHUNK_DAYS - 1)
    age_hi = (today - start).days
    age_lo = (today - end).days
    windows.append(("backfill slot %d D-%d..D-%d" % (slot, age_hi, age_lo), _fmt(start), _fmt(end)))
    return split_windows(windows)


def split_windows(windows):
    """Cut every (label, start, end) range into MAX_WINDOW_DAYS-day pieces.
    Labels keep their prefix ("primary ..." / "backfill ...") so main() still
    knows which failures are fatal. A 3-day primary window becomes three
    single-day windows, each scraped and posted on its own.
    """
    out = []
    for label, start_str, end_str in windows:
        a = datetime.strptime(start_str, "%m/%d/%Y").date()
        b = datetime.strptime(end_str, "%m/%d/%Y").date()
        while a <= b:
            c = min(a + timedelta(days=MAX_WINDOW_DAYS - 1), b)
            out.append((label, _fmt(a), _fmt(c)))
            a = c + timedelta(days=1)
    return out


class TooManyRowsError(RuntimeError):
    """Sisense refused to render the window (>10,000 rows). Retrying the same
    window can never help - the window has to be smaller."""


def unlock_report(page):
    """Submit the shared-report password if Sisense redirected us to the
    /verify-password page. Confirmed live 2026-09-18: the page is a plain
    POST form with <input type="password" name="password" id="password"> and
    a <button type="submit" class="button action-medium disabled">VIEW
    DASHBOARD</button> that loses "disabled" once the field has text.
    A wrong password re-renders the same page, which is why we check the
    URL afterwards instead of assuming success.
    """
    if "verify-password" not in page.url:
        # Also cover the case where the form is rendered without a redirect.
        if page.locator("input[name='password']").count() == 0:
            return
    pw = page.locator("input[name='password']")
    pw.wait_for(state="visible", timeout=60_000)
    pw.click()
    pw.press_sequentially(PERISCOPE_PASSWORD, delay=20)
    page.wait_for_function(
        """() => {
            const btn = document.querySelector('button[type=submit]');
            return btn && !btn.classList.contains('disabled');
        }""",
        timeout=15_000,
    )
    page.locator("button[type='submit']").click()
    page.wait_for_load_state("domcontentloaded", timeout=90_000)
    # Give a wrong-password re-render a moment to show up before deciding.
    page.wait_for_timeout(1500)
    if "verify-password" in page.url or page.locator("input[name='password']").count() > 0:
        raise RuntimeError(
            "Periscope password was not accepted (still on the verify-password page). "
            "Check the PERISCOPE_PASSWORD secret."
        )


def scrape_window_csv(start_str, end_str):
    """Filter the report's Data widget to a rolling D0-to-D-1 window and pull
    its CSV export.

    Uses Periscope's "Custom Range" Date Range filter with Start/End Date
    computed fresh on every run (see compute_date_range_mmddyyyy), rather
    than a built-in preset - this pins down the exact semantics ("today back
    through yesterday, inclusive") instead of relying on unclear/undocumented
    behavior of a preset like "7 Days". Verified live: filling Start/End Date
    with explicit MM/DD/YYYY values and clicking Apply correctly narrows the
    Data widget and the resulting breadcrumb to that exact range.

    Uses the widget's built-in "Download Data" export instead of scraping the
    DOM: the Data widget is a virtualized grid (rows AND columns are only
    rendered near the viewport), so a DOM scrape would silently miss most of
    a real week's rows/columns. The CSV export is generated server-side and
    is complete regardless of what happened to be scrolled into view.

    Returns the CSV text, or None if the widget shows "Query returned no
    matching rows" - Sisense doesn't even offer a "Download Data" menu item
    when there's nothing to export, so this has to be checked for explicitly
    rather than treated as a scrape failure.
    """
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1600, "height": 1000})

        # Capture the export URL from the first matching request, then poll
        # it ourselves rather than relying on the page's own retry behavior.
        # Registered before any interaction so we can't race the request.
        export_url = {"value": None}

        def on_response(resp):
            if export_url["value"] is None and "/download_csv/" in resp.url:
                export_url["value"] = resp.url

        page.on("response", on_response)

        try:
            # "domcontentloaded" rather than "networkidle": the report's
            # default Date Range is "All Dates", so the page kicks off a
            # full-history query the moment it loads. On the scheduled
            # 7am/7pm runs that query is slow enough that "networkidle"
            # (and the old up-front wait for the grid, see below) never
            # settled in time. We don't need that unfiltered query at all
            # - we're about to replace it with our own Custom Range - so
            # don't wait on it.
            page.goto(PERISCOPE_URL, wait_until="domcontentloaded", timeout=90_000)
            unlock_report(page)

            # Only wait for the filters bar - that's all the next step
            # needs. NOTE: deliberately NOT waiting for ".ninja-grid" here.
            # An earlier version did, with a 30s timeout, and that is
            # exactly what every scheduled run failed on (runs #22, #23 on
            # 2026-09-01/02: "waiting for locator('.ninja-grid') to be
            # visible - Timeout 30000ms exceeded") while manual runs at
            # other times of day passed: the grid only renders once the
            # default "All Dates" query finishes, which at those hours
            # takes longer than 30s. The grid IS waited for further down,
            # after our filter is applied, with a much longer timeout.
            page.wait_for_selector(".filters-bar-label", timeout=90_000)

            # Open the report-level filters panel.
            page.locator(".filters-bar-label").first.click()
            page.wait_for_selector(".radio-button-group", timeout=30_000)
            page.wait_for_timeout(300)

            # Date Range column: select "Custom Range". Unlike the preset
            # options (Current Week, 7 Days, etc.), which live inside
            # .radio-button-group > .small-radio-button, "Custom Range" is
            # rendered in its own sibling container - .custom-date-option -
            # under .options (confirmed live via DOM inspection: a selector
            # scoped to .radio-button-group never matches it, which is why
            # an earlier version of this selector timed out in CI). It's
            # always the first item in the Date Range column, so no scroll
            # is needed to reach it. force=True because a plain click here
            # can hit a transient overlap issue while the panel settles.
            custom_range_option = page.locator(".custom-date-option .small-radio-button").first
            custom_range_option.click(force=True)
            page.wait_for_timeout(800)

            # Fill Start/End Date with the freshly computed D-1/D0 window.
            # force=True for the same transient-overlap reason as above.
            #
            # This is a jQuery UI-style datepicker (class "hasDatepicker")
            # that only registers a value in its own real filter state in
            # response to real keystrokes - a bulk .fill() (optionally
            # followed by dispatching synthetic "change"/"blur"/"focusout"
            # events) leaves the field SHOWING the right text but the
            # underlying filter state stays unset, so Apply stays disabled
            # and/or silently applies nothing (confirmed both ways in CI:
            # a bare .fill() and a .fill() + dispatch_event() combo both
            # left the Apply button with class "...apply-button disabled").
            # press_sequentially() sends one real keydown/keypress/keyup
            # per character, which is what a datepicker actually listens
            # for, and is what worked reliably in manual verification.
            start_input = page.locator(".range-start")
            end_input = page.locator(".range-end")

            # NOTE: no Escape/Tab here. Confirmed live (twice, 2026-08-31)
            # that clicking straight into the End field reliably commits
            # the Start field's typed value (it visibly reformats
            # "08/25/2026" -> "2026-08-25" the moment End gets focus), and
            # clicking Apply directly afterwards - without blurring End
            # first - still commits correctly. An earlier version of this
            # code pressed Escape after each field to close the
            # datepicker's calendar popup; that turned out to be the wrong
            # call - one live repro showed Escape leaving BOTH fields
            # empty after Apply (breadcrumb read just "Custom Range", no
            # dates, and the widget genuinely had no rows), most likely
            # because Escape is also this datepicker's "clear/cancel the
            # pending edit" shortcut, not just "close the popup".
            start_input.click(force=True)
            start_input.clear()
            start_input.press_sequentially(start_str, delay=40)

            end_input.click(force=True)
            end_input.clear()
            end_input.press_sequentially(end_str, delay=40)

            # Don't click Apply on a fixed delay - wait for the actual
            # signal that the datepicker has validated both typed dates:
            # the Apply button loses its "disabled" class. Confirmed live
            # in CI that even with real keystrokes, a short fixed wait
            # isn't always enough - the button can still read "disabled"
            # for a bit while the widget's own validation catches up, and
            # clicking (even with force=True) while it's disabled is a
            # no-op in the app's own click handler, silently applying
            # nothing. NOTE: this only proves the *button* thinks the
            # inputs are non-empty/well-formed - see below, it is NOT
            # sufficient proof the Custom Range actually got applied.
            page.wait_for_function(
                """() => {
                    const btn = document.querySelector('.apply-button');
                    return btn && !btn.classList.contains('disabled');
                }""",
                timeout=15_000,
            )

            # Apply the filter, then POLL the breadcrumb (not a fixed
            # delay) until it shows the committed "<date> to <date>" text.
            #
            # IMPORTANT: ".filters-bar-label" (used above to *open* the
            # panel) is NOT the per-filter breadcrumb - confirmed live via
            # DOM inspection that it only ever contains the generic
            # "Filters (N)" toggle text (a <div class="filters-bar-label
            # bold">Filters<span id="filter-count">(N)</span>...</div>).
            # This selector bug is why every prior CI run failed this
            # check even when the date range genuinely committed (run
            # #18, 2026-08-31: last breadcrumb text was literally
            # 'Filters (1)'). The actual per-filter readout lives in
            # ".filters-bar .filter-group .filter .label" (sibling of a
            # ".dimension-name" span reading "DateRange") - confirmed live
            # it shows "2026-08-25 to 2026-09-01" (computed dates,
            # YYYY-MM-DD) once both inputs hold a valid date, and keeps
            # showing it after Apply is clicked and the panel closes.
            apply_button = page.locator(".apply-button")
            date_range_label = page.locator(".filters-bar .filter-group .filter .label").first
            apply_button.click(force=True)
            try:
                page.wait_for_function(
                    """() => {
                        const el = document.querySelector('.filters-bar .filter-group .filter .label');
                        return !!el && el.textContent.includes(' to ');
                    }""",
                    timeout=15_000,
                )
            except Exception:
                raise RuntimeError(
                    "Custom Range Start/End Date did not commit - filter breadcrumb never "
                    "showed '<date> to <date>' after Apply (last breadcrumb text: "
                    f"{date_range_label.text_content()!r})"
                )

            # Find the "Data" widget specifically (report may gain more
            # widgets later).
            widget = page.locator(
                ".widget-container",
                has=page.locator(".widget-title", has_text=re.compile(r"^\s*Data\s*$")),
            ).first
            widget.scroll_into_view_if_needed()

            # Wait for the query that Apply just triggered to finish. The
            # widget shows a transient ".widget-loader" overlay ON TOP OF
            # its *previous* results while requerying - checking the
            # widget's contents before this resolves can observe stale
            # state. Poll (not a fixed delay) for either real grid rows or
            # a genuinely visible "no matching rows" message; also bail
            # out if the loader itself never appears/disappears within
            # the timeout, since a network hiccup here should be a clear
            # failure, not a silent "no rows".
            #
            # IMPORTANT: the ".error-message" node is ALWAYS present in
            # the widget's DOM, even when it's showing real data - it's a
            # hidden placeholder (confirmed live: display:none,
            # offsetParent:null while a fully-loaded grid with rows sat
            # right next to it). A bare `.count() > 0` check on it is
            # true unconditionally, which is why CI run #19 (2026-08-31)
            # reported "no rows" for a range that actually had data. Must
            # check visibility, not just presence.
            widget_handle = widget.element_handle()
            page.wait_for_function(
                """(el) => {
                    const loader = el.querySelector('.widget-loader');
                    if (loader && loader.offsetParent !== null) return false;
                    const err = el.querySelector('.error-message');
                    const errVisible = !!err && err.offsetParent !== null;
                    const grid = el.querySelector('.ninja-grid');
                    // "Result set too large ... fewer than 10,000 rows" is a
                    // third terminal state: no grid, no .error-message.
                    const tooBig = (el.innerText || '').includes('Result set too large');
                    return errVisible || !!grid || tooBig;
                }""",
                arg=widget_handle,
                # Generous: on the scheduled 7am/7pm runs Sisense has been
                # measurably slower than during ad-hoc manual runs (see
                # the ".ninja-grid" note near page.goto above), and this
                # query may also be queued behind the still-in-flight
                # default "All Dates" query the page fired on load.
                timeout=180_000,
            )

            # No rows in this date range - Sisense shows this in place of
            # the grid and doesn't offer a "Download Data" menu item at
            # all, so check for it (now that the query above has settled)
            # instead of timing out waiting for a menu that will never
            # appear.
            if widget.locator(".error-message", has_text="no matching rows").is_visible():
                browser.close()
                return None

            if TOO_MANY_ROWS_MARKER in (widget.inner_text() or ""):
                browser.close()
                raise TooManyRowsError(
                    f"Sisense refused to render {start_str}-{end_str}: more than 10,000 rows "
                    "in the window (widget says 'Result set too large'). Lower MAX_WINDOW_DAYS "
                    "or the report has grown past ~10k rows/day."
                )

            # Open the per-widget menu.
            widget.hover()
            page.wait_for_timeout(500)
            widget.locator(".controls .expand.button").click(force=True)
            page.wait_for_selector("text=Download Data", timeout=30_000)
            page.get_by_text("Download Data", exact=True).click()

            deadline = time.time() + 60
            while export_url["value"] is None and time.time() < deadline:
                page.wait_for_timeout(250)
            if export_url["value"] is None:
                raise RuntimeError("Did not observe a download_csv request after clicking Download Data")
        except Exception:
            try:
                page.screenshot(path="debug_failure.png", full_page=True)
                with open("debug_failure.html", "w", encoding="utf-8") as f:
                    f.write(page.content())
            except Exception as diag_err:
                print(f"(could not capture debug artifacts: {diag_err})", file=sys.stderr)
            browser.close()
            raise

        csv_text = None
        deadline = time.time() + 180
        while time.time() < deadline:
            resp = page.context.request.get(export_url["value"])
            if resp.status == 200:
                csv_text = resp.text()
                break
            page.wait_for_timeout(2000)

        browser.close()

        if csv_text is None:
            raise RuntimeError("Timed out waiting for the CSV export to become ready")
        return csv_text


def map_csv_to_sheet(csv_text: str):
    """Parse the CSV and re-shape every row onto SHEET_HEADERS (48 columns) by
    header NAME. Unknown CSV columns are dropped with a warning; sheet columns
    the CSV doesn't provide (second task_15, USU_MES) stay blank. Falls back
    to positional mapping onto CSV_HEADERS if the CSV has no usable header.
    """
    reader = csv.reader(io.StringIO(csv_text))
    try:
        header = next(reader)
    except StopIteration:
        return []

    csv_norm = [_norm(h) for h in header]
    expected = [_norm(h) for h in CSV_HEADERS]
    if csv_norm != expected:
        print(
            f"WARNING: CSV header differs from the expected 46 columns.\n"
            f"  got      ({len(csv_norm)}): {csv_norm}\n"
            f"  expected ({len(expected)}): {expected}",
            file=sys.stderr,
        )
    known = set(csv_norm)
    if "mission_sas_id" not in known or "date" not in known:
        print("WARNING: CSV header unusable - falling back to positional mapping.", file=sys.stderr)
        csv_norm = expected

    # sheet column index -> csv column index (first occurrence of each name)
    first_csv_idx = {}
    for i, n in enumerate(csv_norm):
        first_csv_idx.setdefault(n, i)
    seen_sheet = set()
    sheet_src = []
    for name in SHEET_HEADERS:
        n = _norm(name)
        if n in seen_sheet:
            sheet_src.append(None)      # duplicated header column -> blank
        else:
            seen_sheet.add(n)
            sheet_src.append(first_csv_idx.get(n))
    unmapped = [csv_norm[i] for i in range(len(csv_norm)) if i not in set(x for x in sheet_src if x is not None)]
    if unmapped:
        print(f"WARNING: CSV columns not present in the sheet header were dropped: {unmapped}", file=sys.stderr)

    rows = []
    for row in reader:
        if not any(c.strip() for c in row):
            continue
        rows.append([row[i] if (i is not None and i < len(row)) else "" for i in sheet_src])
    return rows


def post_rows(rows: list):
    """POST the scraped rows to the Web App (retried, see webapp_request).

    Generous timeout: the Web App upserts into a month file that grows to
    ~40k rows; Apps Script's own hard limit is 6 minutes, so wait for it
    rather than declaring failure while it is still writing.
    """
    return webapp_request(
        "POST",
        timeout=360,
        data=json.dumps({"rows": rows}),
        headers={"Content-Type": "application/json"},
    )


def scrape_with_retry(start_str, end_str):
    """Whole-scrape retry with a fresh browser. Every failure seen on the
    scheduled runs so far has been Sisense being slow/unresponsive at that
    hour rather than anything wrong with the page or the code, and a second
    attempt a minute later is cheap compared to losing a whole sync window.
    The debug screenshot/HTML from the LAST failed attempt is what ends up
    in the workflow's artifacts.
    """
    last_exc = None
    for attempt in range(1, SCRAPE_ATTEMPTS + 1):
        try:
            return scrape_window_csv(start_str, end_str)
        except TooManyRowsError:
            raise  # a smaller window is the only fix; a fresh browser is not
        except Exception as exc:  # noqa: BLE001 - deliberately broad, see above
            last_exc = exc
            print(f"Scrape attempt {attempt}/{SCRAPE_ATTEMPTS} for {start_str}-{end_str} failed: {exc}", file=sys.stderr)
            if attempt < SCRAPE_ATTEMPTS:
                print(f"Retrying in {SCRAPE_RETRY_DELAY_S}s with a fresh browser...", file=sys.stderr)
                time.sleep(SCRAPE_RETRY_DELAY_S)
    raise last_exc


def print_result(result):
    print(
        f"Posted {result.get('rows_received')} rows: "
        f"{result.get('rows_updated')} updated in place, {result.get('rows_appended')} appended; "
        f"{result.get('duplicates_removed')} stray duplicate row(s) removed, "
        f"{result.get('wrong_month_removed')} wrong-month row(s) removed, "
        f"{result.get('rows_unroutable')} row(s) had no readable date and were skipped."
    )
    for name, stats in sorted((result.get("files") or {}).items()):
        print(f"  {name}: {stats}")


def main():
    check_token()

    windows = compute_windows()
    print("Windows this run (America/Bogota): " + "; ".join(f"{lbl} = {a}..{b}" for lbl, a, b in windows))

    failures = []
    for label, start_str, end_str in windows:
        print(f"Pulling 'AV - Master Report' data for {start_str} to {end_str} ({label})...")
        try:
            csv_text = scrape_with_retry(start_str, end_str)
        except Exception as exc:  # noqa: BLE001
            # The primary window is what the pipeline exists for - a failure
            # there fails the run. A backfill chunk is best-effort: it comes
            # around again on the next day's same slot, so log and go on.
            if label.startswith("primary"):
                raise
            print(f"Backfill window {start_str}-{end_str} skipped after retries: {exc}", file=sys.stderr)
            failures.append(label)
            continue

        if csv_text is None:
            print(f"No rows for {start_str} to {end_str} - nothing to post for this window.")
            continue

        rows = map_csv_to_sheet(csv_text)
        print(f"Scraped {len(rows)} rows for {start_str} to {end_str}.")
        print_result(post_rows(rows))

    if failures:
        print(f"Note: {len(failures)} backfill window(s) skipped this run: {', '.join(failures)}", file=sys.stderr)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        sys.exit(1)
