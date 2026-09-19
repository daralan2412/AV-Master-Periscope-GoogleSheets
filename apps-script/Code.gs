/**
 * AV-Master-Periscope-GoogleSheets - Apps Script Web App  (v2, 2026-09-19)
 *
 * NEW, INDEPENDENT PROJECT. It shares nothing (no Script Properties, no
 * deployment, no sheet) with the Copa pipeline
 * (daralan2412/CM-Master-Periscope-Googlesheets), the FedEx REP-1901 pipeline
 * (daralan2412/FX-periscope-googlesheets) or the old PTY wheelchair pipeline
 * (daralan2412/periscope-to-sheets). Those are left completely alone.
 *
 * Source:  Sisense/Periscope shared report "AV - Master Report - In Progress"
 *          https://app.periscopedata.com/shared/924434c7-8bb2-43c5-baf0-5ca251356bc6
 *          (password gated - the scraper handles that; this script never
 *          sees the password).
 * Target:  Google Drive folder 1m7cpPLZsSEOfVL2ENSWwkq8wCbLWAo3a, which holds one
 *          spreadsheet per month named "<M>_<YYYY>_AV" (no zero padding:
 *          "8_2026_AV", "9_2026_AV", "12_2026_AV"). The data tab is the one
 *          whose A1 reads mission_sas_id (named "DATA").
 *
 * ---------------------------------------------------------------------------
 * STANDARD SCHEMA (v2) - every monthly file, every row, dashboard-ready
 * ---------------------------------------------------------------------------
 *   46 columns: 21 fixed + task_1 .. task_25 (see HEADERS / COL_TYPES).
 *     mission_sas_id, arr_flt, dep_flt        -> numbers        (format 0)
 *     date                                    -> real Date      (yyyy-mm-dd)
 *     arr_time, dep_time, assign_time,
 *     start_time, finish_time                 -> real datetime  (yyyy-mm-dd HH:mm)
 *     everything else (gate included)         -> text           (@)
 *   Spreadsheet time zone: America/Bogota. Tab name: DATA. Row 1 frozen.
 *   Text is proper UTF-8 ("Revisión 360°", not "RevisiÃ³n 360Â°").
 *
 *   History: the files built before this pipeline (Jan-Jul 2026, Aug 1-5)
 *   had 48 columns: a duplicated "task_15" header at column 37 and an
 *   always-empty "USU_MES" at 48. Column 37 held a copy of task_15 in the
 *   Jan-Apr files but the real task_16 in May-Jul/Aug 1-5 (26 contiguous
 *   task slots); the pipeline's v1 rows mapped by header name and left it
 *   blank, shifting task 16+ one column right. The one-off "standardize"
 *   action below re-aligned all of that into this 46-column layout
 *   (2026-09-19); see realign48_ for the exact per-row rule.
 *
 * Contract with the scraper (scrape_and_upload.py):
 *   GET  ?token=...                -> {success:true} health check
 *   POST ?token=...  {"rows":[[...46 cols (text, as in the CSV)...], ...]}
 *        Rows are TYPED on the way in (typeRow_): the scraper keeps posting
 *        the CSV's text values, this script parses dates/numbers. A 48-column
 *        row from the v1 scraper is accepted too (realigned, see realign48_).
 *        Every posted row is routed to the monthly file that matches the
 *        row's own "date" column (column B) - 09/30/2026 -> 9_2026_AV,
 *        10/01/2026 -> 10_2026_AV - regardless of when the run happened.
 *        A monthly file that does not exist yet is created in the folder
 *        with the header row and the column formats.
 *        Rows are UPSERTED: a mission_sas_id already in the file has its
 *        row overwritten in place (freshest scrape wins), new ids are
 *        appended - so re-posting D-2..D0 four times a day never grows the
 *        file with duplicates. Then every touched file (plus the current
 *        month's file) gets a cleanup pass over columns A:B only:
 *          1. rows whose "date" belongs to a DIFFERENT month/year than the
 *             file are DELETED;
 *          2. stray duplicate mission_sas_id rows are collapsed to the
 *             LAST occurrence.
 *        The cleanup only writes when something has to be removed.
 *
 * Admin (GET, token-gated):
 *   ?action=rebuild&file=9_2026_AV        dedupe + wrong-month cleanup
 *   ?action=backup&file=9_2026_AV         copy the file into <folder>/BACKUP
 *   ?action=prepare&file=9_2026_AV        one-off: tz, header, column formats
 *   ?action=standardize&file=..&start=2&count=15000
 *                                         one-off: rewrite a block of rows
 *                                         into the standard schema
 *   ?action=restore&file=..&start=2&count=1000
 *                                         copy rows back from today's backup
 *   ?action=finalize&file=9_2026_AV       one-off: drop cols 47-48, tab name
 *   ?action=inspect&file=9_2026_AV        row/col counts + first data row types
 *
 * Both endpoints require ?token=<AUTH_TOKEN> (Script Property, Project
 * Settings > Script Properties). Put the same value in the GitHub secret
 * WEBAPP_TOKEN.
 *
 * Deploy: Deploy > New deployment > Web app, Execute as: Me, Who has access:
 * Anyone. The /exec URL goes in the GitHub secret SHEETS_WEBAPP_URL.
 * Remember: editing this code does NOT change the live /exec until you
 * Deploy > Manage deployments > Edit > Version: New version.
 */

var FOLDER_ID = '1m7cpPLZsSEOfVL2ENSWwkq8wCbLWAo3a';
var BACKUP_FOLDER_NAME = 'BACKUP';
var FILE_SUFFIX = '_AV';           // file name = <month>_<year> + FILE_SUFFIX
var DATA_SHEET_NAME = 'DATA';
var MISSION_ID_COL = 1;            // column A = mission_sas_id (1-based)
var DATE_COL = 2;                  // column B = date
var TZ = 'America/Bogota';         // script + spreadsheet time zone

var HEADERS = [
  'mission_sas_id', 'date', 'station', 'airline_code', 'tail_number', 'vessel_description',
  'gate', 'job_name', 'mission_name', 'arr_flt', 'dep_flt', 'org_city', 'dest_city',
  'arr_time', 'dep_time', 'disp_name', 'agent_name', 'mission_notes', 'assign_time',
  'start_time', 'finish_time',
  'task_1', 'task_2', 'task_3', 'task_4', 'task_5', 'task_6', 'task_7', 'task_8', 'task_9',
  'task_10', 'task_11', 'task_12', 'task_13', 'task_14', 'task_15', 'task_16', 'task_17',
  'task_18', 'task_19', 'task_20', 'task_21', 'task_22', 'task_23', 'task_24', 'task_25'
];
// n = number, d = date, t = datetime, s = text  (one per HEADERS entry)
var COL_TYPES = (
  'n d s s s s s s s n n s s t t s s s t t t' + // 21 fixed
  ' s s s s s s s s s s s s s s s s s s s s s s s s s'  // 25 tasks
).trim().split(/\s+/);
var FORMAT_BY_TYPE = { n: '0', d: 'yyyy-mm-dd', t: 'yyyy-mm-dd HH:mm', s: '@' };
var COL_FORMATS = COL_TYPES.map(function (t) { return FORMAT_BY_TYPE[t]; });

function doGet(e) {
  if (!checkToken_(e)) return jsonOut_({ success: false, error: 'unauthorized' });
  try {
    var action = e.parameter.action || '';
    if (!action) {
      var folder = DriveApp.getFolderById(FOLDER_ID);
      return jsonOut_({ success: true, message: 'ok', version: 2, folder: folder.getName() });
    }
    var m = String(e.parameter.file || '').match(/^(\d{1,2})_(\d{4})_AV$/);
    if (!m) return jsonOut_({ success: false, error: 'file must look like 9_2026_AV' });
    var ym = m[1] + '_' + m[2];
    var result;
    // Mutating admin actions take the same lock as doPost so a scheduled
    // upload can never interleave with a rewrite of the same file.
    var lock = null;
    if (action !== 'inspect') {
      lock = LockService.getScriptLock();
      if (!lock.tryLock(120000)) return jsonOut_({ success: false, error: 'another upload is in progress (lock timeout)' });
    }
    try {
    if (action === 'rebuild') {
      var sh = findMonthSheet_(DriveApp.getFolderById(FOLDER_ID), ym);
      if (!sh) return jsonOut_({ success: false, error: 'file not found' });
      result = rebuildSheet_(sh, ym);
    } else if (action === 'backup') {
      result = backupFile_(ym + FILE_SUFFIX);
    } else if (action === 'prepare') {
      result = prepareFile_(ym + FILE_SUFFIX);
    } else if (action === 'standardize') {
      result = standardizeBlock_(ym + FILE_SUFFIX, parseInt(e.parameter.start || '2', 10), parseInt(e.parameter.count || '15000', 10));
    } else if (action === 'restore') {
      result = restoreBlock_(ym + FILE_SUFFIX, parseInt(e.parameter.start || '2', 10), parseInt(e.parameter.count || '1000', 10));
    } else if (action === 'finalize') {
      result = finalizeFile_(ym + FILE_SUFFIX);
    } else if (action === 'inspect') {
      result = inspectFile_(ym + FILE_SUFFIX);
    } else {
      return jsonOut_({ success: false, error: 'unknown action ' + action });
    }
    } finally {
      if (lock) lock.releaseLock();
    }
    return jsonOut_({ success: true, action: action, result: result });
  } catch (err) {
    return jsonOut_({ success: false, error: 'doGet failed: ' + err });
  }
}

function doPost(e) {
  if (!checkToken_(e)) return jsonOut_({ success: false, error: 'unauthorized' });

  var body;
  try {
    body = JSON.parse(e.postData.contents);
  } catch (err) {
    return jsonOut_({ success: false, error: 'invalid JSON body: ' + err });
  }
  var rows = body.rows || [];

  // Serialize concurrent runs (a late-firing cron overlapping a manual run)
  // so two rebuilds never interleave on the same file.
  var lock = LockService.getScriptLock();
  if (!lock.tryLock(120000)) {
    return jsonOut_({ success: false, error: 'another upload is in progress (lock timeout)' });
  }

  try {
    var folder = DriveApp.getFolderById(FOLDER_ID);

    // 1. Normalize + type every row, then route it to its month by its own
    //    "date" column.
    var groups = {};           // "9_2026" -> [rows]
    var unroutable = 0;
    rows.forEach(function (row) {
      row = typeRow_(normalizeWidth_(row));
      var ym = monthKeyFromCell_(row[DATE_COL - 1]);
      if (!ym) { unroutable++; return; }
      (groups[ym] = groups[ym] || []).push(row);
    });

    // 2. UPSERT each group into its monthly file (creating the file if needed).
    var touched = {};          // "9_2026" -> Sheet
    var perFile = {};          // "9_2026_AV" -> stats
    Object.keys(groups).forEach(function (ym) {
      var sheet = getOrCreateMonthSheet_(folder, ym);
      var u = upsertRows_(sheet, groups[ym]);
      touched[ym] = sheet;
      perFile[ym + FILE_SUFFIX] = {
        rows_received: groups[ym].length,
        rows_updated: u.updated,
        rows_appended: u.appended
      };
    });

    // 3. Always also check the current month's file (Bogota time), so the
    //    cross-month cleanup runs even on a run that posted zero rows there.
    var nowYm = monthKeyFromDate_(new Date());
    if (!touched[nowYm]) {
      var cur = findMonthSheet_(folder, nowYm);
      if (cur) touched[nowYm] = cur;
    }

    // 4. Cleanup pass on every touched file.
    var totalDupes = 0, totalWrongMonth = 0;
    Object.keys(touched).forEach(function (ym) {
      var r = rebuildSheet_(touched[ym], ym);
      var name = ym + FILE_SUFFIX;
      perFile[name] = perFile[name] || { rows_received: 0, rows_updated: 0, rows_appended: 0 };
      perFile[name].duplicates_removed = r.duplicates;
      perFile[name].wrong_month_removed = r.wrongMonth;
      perFile[name].total_rows = r.total;
      totalDupes += r.duplicates;
      totalWrongMonth += r.wrongMonth;
    });

    var totalUpdated = 0, totalAppended = 0;
    Object.keys(perFile).forEach(function (k) {
      totalUpdated += perFile[k].rows_updated || 0;
      totalAppended += perFile[k].rows_appended || 0;
    });

    return jsonOut_({
      success: true,
      rows_received: rows.length,
      rows_unroutable: unroutable,
      rows_updated: totalUpdated,
      rows_appended: totalAppended,
      duplicates_removed: totalDupes,
      wrong_month_removed: totalWrongMonth,
      files: perFile
    });
  } catch (err) {
    return jsonOut_({ success: false, error: 'doPost failed: ' + err });
  } finally {
    lock.releaseLock();
  }
}

// ---------------------------------------------------------------------------
// Typing: text (from the CSV / old files) -> Date / number / clean text
// ---------------------------------------------------------------------------

function isDate_(v) {
  return Object.prototype.toString.call(v) === '[object Date]' && !isNaN(v);
}

// "MM/dd/yyyy", "M/d/yy", "yyyy-MM-dd", each optionally followed by
// " HH:mm[:ss[.fff]]". Interpreted as Bogota wall-clock (script tz).
function parseDateTime_(s) {
  s = String(s).trim();
  var m = s.match(/^(\d{1,2})\/(\d{1,2})\/(\d{2,4})(?:[ T](\d{1,2}):(\d{2})(?::(\d{2})(?:\.\d+)?)?)?$/);
  var y, mo, d, h = 0, mi = 0, se = 0;
  if (m) {
    mo = +m[1]; d = +m[2]; y = +m[3]; if (y < 100) y += 2000;
    if (m[4] !== undefined) { h = +m[4]; mi = +m[5]; se = m[6] ? +m[6] : 0; }
  } else {
    m = s.match(/^(\d{4})-(\d{1,2})-(\d{1,2})(?:[ T](\d{1,2}):(\d{2})(?::(\d{2})(?:\.\d+)?)?)?$/);
    if (!m) return null;
    y = +m[1]; mo = +m[2]; d = +m[3];
    if (m[4] !== undefined) { h = +m[4]; mi = +m[5]; se = m[6] ? +m[6] : 0; }
  }
  var dt = new Date(y, mo - 1, d, h, mi, se);
  return isNaN(dt) ? null : dt;
}

// Repairs UTF-8 text that was decoded as Latin-1 ("RevisiÃ³n" -> "Revisión").
function fixEncoding_(s) {
  if (!/[ÂÃ]/.test(s)) return s;
  try {
    var fixed = decodeURIComponent(escape(s));
    return fixed || s;
  } catch (err) {
    return s;
  }
}

function typeCell_(v, type) {
  if (v === null || v === undefined) return '';
  if (type === 'd' || type === 't') {
    if (isDate_(v)) return v;
    if (v === '') return '';
    var dt = parseDateTime_(v);
    if (!dt) return fixEncoding_(String(v));           // unreadable: keep as text
    if (type === 'd') dt = new Date(dt.getFullYear(), dt.getMonth(), dt.getDate());
    return dt;
  }
  if (type === 'n') {
    if (typeof v === 'number') return v;
    var s = String(v).trim();
    if (s === '') return '';
    return /^-?\d+(\.\d+)?$/.test(s) ? Number(s) : fixEncoding_(s);
  }
  // text
  if (isDate_(v)) return Utilities.formatDate(v, TZ, 'yyyy-MM-dd HH:mm');
  if (typeof v === 'number') return String(v);
  return fixEncoding_(String(v));
}

function typeRow_(row) {
  var out = new Array(HEADERS.length);
  for (var c = 0; c < HEADERS.length; c++) out[c] = typeCell_(row[c], COL_TYPES[c]);
  return out;
}

// A 48-column row in the pre-v2 layout -> 46 columns.
//   old layout: 0-20 fixed | 21-35 task_1..15 | 36 dup "task_15" | 37-46 "task_16..25" | 47 USU_MES
//   Three row flavours were found in the files (2026-09-19 audit):
//     - Jan-Apr files: col 36 is a verbatim COPY of col 35 (task_15) and
//       37-46 really are task_16..25            -> drop col 36
//     - May-Jul files + Aug 1-5: 26 contiguous task slots, col 36 is the
//       real task_16 and 37-46 are task_17..26   -> keep 36..45, drop slot 26
//       (slot 26 was never used in those files)
//     - v1 scraper rows (Aug 6+): col 36 blank, 37-46 = task_16..25 -> drop col 36
//   Rows already in the 46-column layout (padded to 48 with blanks) map
//   onto themselves under the same rule, so the conversion is idempotent.
function realign48_(row) {
  var fixed = row.slice(0, 21);
  var first15 = row.slice(21, 36);
  var col35 = row[35], col36 = row[36];
  var hasCol36 = col36 !== '' && col36 !== null && col36 !== undefined;
  var rest = (hasCol36 && String(col36) !== String(col35))
    ? row.slice(36, 46)     // contiguous legacy: tasks 16..25
    : row.slice(37, 47);    // duplicated col 36 or blank col 36: tasks 16..25
  return fixed.concat(first15, rest);
}

function normalizeWidth_(row) {
  row = row || [];
  if (row.length === 48) row = realign48_(row);
  if (row.length < HEADERS.length) return row.concat(new Array(HEADERS.length - row.length).fill(''));
  if (row.length > HEADERS.length) return row.slice(0, HEADERS.length);
  return row;
}

function formatsFor_(nRows) {
  var out = new Array(nRows);
  for (var i = 0; i < nRows; i++) out[i] = COL_FORMATS;
  return out;
}

// ---------------------------------------------------------------------------
// Month routing helpers
// ---------------------------------------------------------------------------

function monthKeyFromCell_(v) {
  if (v === null || v === undefined || v === '') return null;
  if (isDate_(v)) return monthKeyFromDate_(v);
  var s = String(v).trim();
  var m = s.match(/^(\d{1,2})\/(\d{1,2})\/(\d{4})/);          // MM/DD/YYYY
  if (m) return parseInt(m[1], 10) + '_' + m[3];
  m = s.match(/^(\d{4})-(\d{1,2})-(\d{1,2})/);                // YYYY-MM-DD
  if (m) return parseInt(m[2], 10) + '_' + m[1];
  return null;
}

function monthKeyFromDate_(d) {
  return Utilities.formatDate(d, TZ, 'M') + '_' + Utilities.formatDate(d, TZ, 'yyyy');
}

function findMonthFile_(folder, name) {
  var files = folder.getFilesByName(name);
  while (files.hasNext()) {
    var f = files.next();
    if (f.getMimeType() === MimeType.GOOGLE_SHEETS) return f;
  }
  return null;
}

function findMonthSheet_(folder, ym) {
  var f = findMonthFile_(folder, ym + FILE_SUFFIX);
  return f ? firstDataSheet_(SpreadsheetApp.openById(f.getId())) : null;
}

function getOrCreateMonthSheet_(folder, ym) {
  var existing = findMonthSheet_(folder, ym);
  if (existing) return existing;

  var name = ym + FILE_SUFFIX;
  var ss = SpreadsheetApp.create(name);
  ss.setSpreadsheetTimeZone(TZ);
  var file = DriveApp.getFileById(ss.getId());
  folder.addFile(file);
  DriveApp.getRootFolder().removeFile(file); // move out of My Drive root
  var sheet = ss.getSheets()[0];
  sheet.setName(DATA_SHEET_NAME);
  applySchema_(sheet);
  return sheet;
}

// Header row, frozen row, column formats for the whole tab.
function applySchema_(sheet) {
  if (sheet.getMaxColumns() < HEADERS.length) {
    sheet.insertColumnsAfter(sheet.getMaxColumns(), HEADERS.length - sheet.getMaxColumns());
  }
  sheet.getRange(1, 1, 1, HEADERS.length).setNumberFormat('@').setValues([HEADERS]);
  sheet.setFrozenRows(1);
  var n = sheet.getMaxRows() - 1;
  if (n > 0) {
    for (var c = 0; c < HEADERS.length; c++) {
      sheet.getRange(2, c + 1, n, 1).setNumberFormat(COL_FORMATS[c]);
    }
  }
}

// The data tab is the one whose A1 is mission_sas_id; fall back to the first
// tab, and write the header if the tab is empty.
function firstDataSheet_(ss) {
  var sheets = ss.getSheets();
  for (var i = 0; i < sheets.length; i++) {
    if (String(sheets[i].getRange(1, 1).getValue()).trim() === HEADERS[0]) return sheets[i];
  }
  var sheet = sheets[0];
  if (sheet.getLastRow() === 0) applySchema_(sheet);
  return sheet;
}

// ---------------------------------------------------------------------------
// Upsert: overwrite rows whose mission_sas_id already exists, append the rest
// ---------------------------------------------------------------------------

function upsertRows_(sheet, batch) {
  var lastRow = sheet.getLastRow();

  var rowById = {};
  if (lastRow >= 2) {
    var ids = sheet.getRange(2, MISSION_ID_COL, lastRow - 1, 1).getValues();
    for (var i = 0; i < ids.length; i++) {
      var id = String(ids[i][0]).trim();
      if (id) rowById[id] = i + 2;
    }
  }

  var updates = {};      // sheet row number -> row values
  var appendsById = {};  // id -> row values (dedupe within the batch)
  var appendOrder = [];
  var blankIdRows = [];
  batch.forEach(function (row) {
    var id = String(row[MISSION_ID_COL - 1]).trim();
    if (!id) { blankIdRows.push(row); return; }
    if (rowById[id]) { updates[rowById[id]] = row; return; }
    if (!appendsById.hasOwnProperty(id)) appendOrder.push(id);
    appendsById[id] = row;
  });

  var rowNums = Object.keys(updates).map(Number).sort(function (a, b) { return a - b; });
  var updated = 0;
  var b = 0;
  while (b < rowNums.length) {
    var e = b;
    while (e + 1 < rowNums.length && rowNums[e + 1] === rowNums[e] + 1) e++;
    var block = [];
    for (var r = b; r <= e; r++) block.push(updates[rowNums[r]]);
    var rng = sheet.getRange(rowNums[b], 1, block.length, HEADERS.length);
    rng.setNumberFormats(formatsFor_(block.length));
    rng.setValues(block);
    updated += block.length;
    b = e + 1;
  }

  var appends = appendOrder.map(function (id) { return appendsById[id]; }).concat(blankIdRows);
  if (appends.length > 0) {
    var target = sheet.getRange(lastRow + 1, 1, appends.length, HEADERS.length);
    target.setNumberFormats(formatsFor_(appends.length));
    target.setValues(appends);
  }
  return { updated: updated, appended: appends.length };
}

// ---------------------------------------------------------------------------
// Cleanup: remove wrong-month rows + stray duplicate mission_sas_id rows
// ---------------------------------------------------------------------------

function rebuildSheet_(sheet, ym) {
  var lastRow = sheet.getLastRow();
  var lastCol = Math.max(sheet.getLastColumn(), HEADERS.length);
  if (lastRow < 2) return { duplicates: 0, wrongMonth: 0, total: 0 };

  var ab = sheet.getRange(2, 1, lastRow - 1, 2).getValues();

  var wrong = {};
  var wrongMonth = 0;
  for (var w = 0; w < ab.length; w++) {
    var key = monthKeyFromCell_(ab[w][1]);
    if (key && key !== ym) { wrong[w] = true; wrongMonth++; }
  }

  var lastIndexById = {};
  for (var j = 0; j < ab.length; j++) {
    if (wrong[j]) continue;
    var id = String(ab[j][0]).trim();
    if (id) lastIndexById[id] = j;
  }

  var toDelete = [];
  var duplicates = 0;
  for (var i = 0; i < ab.length; i++) {
    if (wrong[i]) { toDelete.push(i); continue; }
    var idI = String(ab[i][0]).trim();
    if (idI && lastIndexById[idI] !== i) { duplicates++; toDelete.push(i); }
  }

  var total = ab.length - toDelete.length;
  if (toDelete.length === 0) return { duplicates: 0, wrongMonth: 0, total: total };

  var blocks = [];
  for (var k = 0; k < toDelete.length; k++) {
    if (blocks.length && toDelete[k] === blocks[blocks.length - 1].end + 1) {
      blocks[blocks.length - 1].end = toDelete[k];
    } else {
      blocks.push({ start: toDelete[k], end: toDelete[k] });
    }
  }

  if (blocks.length <= 50) {
    for (var bi = blocks.length - 1; bi >= 0; bi--) {
      sheet.deleteRows(blocks[bi].start + 2, blocks[bi].end - blocks[bi].start + 1);
    }
  } else {
    var data = sheet.getRange(2, 1, lastRow - 1, lastCol).getValues();
    var drop = {};
    toDelete.forEach(function (x) { drop[x] = true; });
    var kept = [];
    for (var d = 0; d < data.length; d++) if (!drop[d]) kept.push(data[d]);
    sheet.getRange(2, 1, lastRow - 1, lastCol).clearContent();
    if (kept.length > 0) {
      sheet.getRange(2, 1, kept.length, lastCol).setValues(kept);
    }
  }
  return { duplicates: duplicates, wrongMonth: wrongMonth, total: total };
}

// ---------------------------------------------------------------------------
// One-off standardization (2026-09-19) - driven from outside, file by file:
//   backup -> prepare -> standardize (start=2, count=N, repeat) -> finalize
// ---------------------------------------------------------------------------

function openFile_(fileName) {
  var f = findMonthFile_(DriveApp.getFolderById(FOLDER_ID), fileName);
  if (!f) throw new Error('not found: ' + fileName);
  return { file: f, ss: SpreadsheetApp.openById(f.getId()) };
}

function backupFile_(fileName) {
  var folder = DriveApp.getFolderById(FOLDER_ID);
  var o = openFile_(fileName);
  var subs = folder.getFoldersByName(BACKUP_FOLDER_NAME);
  var backup = subs.hasNext() ? subs.next() : folder.createFolder(BACKUP_FOLDER_NAME);
  var stamp = Utilities.formatDate(new Date(), TZ, 'yyyyMMdd');
  var name = fileName + '_backup_' + stamp;
  var existing = backup.getFilesByName(name);
  if (existing.hasNext()) return { file: name, id: existing.next().getId(), skipped: 'already exists' };
  var copy = o.file.makeCopy(name, backup);
  return { file: name, id: copy.getId() };
}

// Time zone, header row (46 names, cols 47-48 header cleared), column formats.
// Must run BEFORE the standardize blocks: legacy Date cells are read and
// written back in the spreadsheet's time zone, so it has to be Bogota for
// both the read and the write.
function prepareFile_(fileName) {
  var o = openFile_(fileName);
  var ss = o.ss;
  ss.setSpreadsheetTimeZone(TZ);
  var sheet = firstDataSheet_(ss);
  var maxCols = sheet.getMaxColumns();
  applySchema_(sheet);
  if (maxCols > HEADERS.length) {
    sheet.getRange(1, HEADERS.length + 1, 1, maxCols - HEADERS.length).clearContent();
  }
  return { file: fileName, tz: ss.getSpreadsheetTimeZone(), lastRow: sheet.getLastRow(), maxCols: maxCols, sheet: sheet.getName() };
}

// Rewrites rows [start, start+count) into the 46-column typed layout and
// clears columns 47+ for those rows. Idempotent for already-converted rows
// (tasks are contiguous, so the realign rule maps them onto themselves).
function standardizeBlock_(fileName, start, count) {
  var o = openFile_(fileName);
  var sheet = firstDataSheet_(o.ss);
  var lastRow = sheet.getLastRow();
  var maxCols = sheet.getMaxColumns();
  var readCols = Math.max(maxCols, HEADERS.length);
  if (start < 2) start = 2;
  if (start > lastRow) return { file: fileName, start: start, done: 0, lastRow: lastRow, finished: true };
  var n = Math.min(count, lastRow - start + 1);
  var t0 = Date.now();
  var data = sheet.getRange(start, 1, n, readCols).getValues();
  var out = new Array(n);
  var fixedEnc = 0, typedDates = 0, realigned = 0;
  for (var i = 0; i < n; i++) {
    var row = data[i];
    if (readCols >= 48 && row[36] !== '' && row[36] !== null) realigned++;
    var norm = normalizeWidth_(readCols >= 48 ? row.slice(0, 48) : row);
    var typed = typeRow_(norm);
    for (var c = 0; c < HEADERS.length; c++) {
      if (typeof norm[c] === 'string' && typeof typed[c] === 'string' && norm[c] !== typed[c]) fixedEnc++;
      if (typeof norm[c] === 'string' && isDate_(typed[c])) typedDates++;
    }
    out[i] = typed;
  }
  // Column formats were already applied to the whole tab by prepareFile_,
  // so only the values are written here (saves a full pass per block).
  sheet.getRange(start, 1, n, HEADERS.length).setValues(out);
  if (readCols > HEADERS.length) {
    sheet.getRange(start, HEADERS.length + 1, n, readCols - HEADERS.length).clearContent();
  }
  var next = start + n;
  return {
    file: fileName, start: start, done: n, next: next, lastRow: lastRow,
    finished: next > lastRow, legacy_rows: realigned, cells_reencoded: fixedEnc,
    cells_typed_from_text: typedDates, ms: Date.now() - t0
  };
}

// Copies rows [start, start+count) verbatim from today's backup copy back
// into the live file (all 48 columns). Both spreadsheets are put in the same
// time zone first so Date cells round-trip without shifting.
function restoreBlock_(fileName, start, count) {
  var folder = DriveApp.getFolderById(FOLDER_ID);
  var subs = folder.getFoldersByName(BACKUP_FOLDER_NAME);
  if (!subs.hasNext()) throw new Error('no BACKUP folder');
  var stamp = Utilities.formatDate(new Date(), TZ, 'yyyyMMdd');
  var bf = findMonthFile_(subs.next(), fileName + '_backup_' + stamp);
  if (!bf) throw new Error('no backup for ' + fileName);
  var bss = SpreadsheetApp.openById(bf.getId());
  bss.setSpreadsheetTimeZone(TZ);
  var bsheet = firstDataSheet_(bss);
  var o = openFile_(fileName);
  o.ss.setSpreadsheetTimeZone(TZ);
  var sheet = firstDataSheet_(o.ss);
  var cols = Math.min(bsheet.getMaxColumns(), sheet.getMaxColumns());
  var n = Math.min(count, bsheet.getLastRow() - start + 1);
  if (n <= 0) return { file: fileName, restored: 0 };
  var vals = bsheet.getRange(start, 1, n, cols).getValues();
  var fmtRow = COL_FORMATS.slice(0, cols);
  while (fmtRow.length < cols) fmtRow.push('@');
  var fmts = new Array(n);
  for (var i = 0; i < n; i++) fmts[i] = fmtRow;
  var rng = sheet.getRange(start, 1, n, cols);
  rng.setNumberFormats(fmts);
  rng.setValues(vals);
  return { file: fileName, start: start, restored: n, cols: cols, backup: bf.getName() };
}

// Drops the now-empty columns 47-48, names the tab DATA, freezes the header.
function finalizeFile_(fileName) {
  var o = openFile_(fileName);
  var sheet = firstDataSheet_(o.ss);
  var maxCols = sheet.getMaxColumns();
  var extra = maxCols - HEADERS.length;
  var dropped = 0;
  if (extra > 0) {
    // Refuse to drop columns that still hold anything.
    var lastRow = sheet.getLastRow();
    if (lastRow >= 1) {
      var vals = sheet.getRange(1, HEADERS.length + 1, lastRow, extra).getValues();
      for (var i = 0; i < vals.length; i++) for (var j = 0; j < vals[i].length; j++) {
        if (vals[i][j] !== '' && vals[i][j] !== null) throw new Error('column ' + (HEADERS.length + 1 + j) + ' still has data at row ' + (i + 1));
      }
    }
    sheet.deleteColumns(HEADERS.length + 1, extra);
    dropped = extra;
  }
  if (sheet.getName() !== DATA_SHEET_NAME) sheet.setName(DATA_SHEET_NAME);
  sheet.setFrozenRows(1);
  return { file: fileName, columns: sheet.getMaxColumns(), dropped: dropped, sheet: sheet.getName(), rows: sheet.getLastRow() - 1, tz: o.ss.getSpreadsheetTimeZone() };
}

function inspectFile_(fileName) {
  var o = openFile_(fileName);
  var sheet = firstDataSheet_(o.ss);
  var lastRow = sheet.getLastRow();
  var header = sheet.getRange(1, 1, 1, sheet.getMaxColumns()).getValues()[0];
  var sample = lastRow >= 2 ? sheet.getRange(2, 1, 1, sheet.getMaxColumns()).getValues()[0] : [];
  var types = sample.map(function (v) { return isDate_(v) ? 'date' : typeof v; });
  return {
    file: fileName, sheet: sheet.getName(), tz: o.ss.getSpreadsheetTimeZone(), rows: lastRow - 1,
    maxCols: sheet.getMaxColumns(), header: header, row2_types: types,
    row2: sample.map(function (v) { return isDate_(v) ? Utilities.formatDate(v, TZ, 'yyyy-MM-dd HH:mm') : v; })
  };
}

// ---------------------------------------------------------------------------
// Misc
// ---------------------------------------------------------------------------

function checkToken_(e) {
  var token = PropertiesService.getScriptProperties().getProperty('AUTH_TOKEN');
  return !!token && e && e.parameter && e.parameter.token === token;
}

function jsonOut_(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj)).setMimeType(ContentService.MimeType.JSON);
}

// Manual helper: run once from the editor to grant Drive/Sheets scopes and
// confirm the folder is reachable and which monthly files exist.
function debugListFolder() {
  var folder = DriveApp.getFolderById(FOLDER_ID);
  var files = folder.getFiles();
  var names = [];
  while (files.hasNext()) names.push(files.next().getName());
  Logger.log(folder.getName() + ': ' + names.sort().join(', '));
  Logger.log('current month key (' + TZ + '): ' + monthKeyFromDate_(new Date()));
}

// Manual helper: run the cleanup/dedupe on every monthly file in the folder.
function debugRebuildAll() {
  var folder = DriveApp.getFolderById(FOLDER_ID);
  var files = folder.getFilesByType(MimeType.GOOGLE_SHEETS);
  while (files.hasNext()) {
    var f = files.next();
    var m = f.getName().match(/^(\d{1,2})_(\d{4})_AV$/);
    if (!m) continue;
    var r = rebuildSheet_(firstDataSheet_(SpreadsheetApp.openById(f.getId())), m[1] + '_' + m[2]);
    Logger.log(f.getName() + ': ' + JSON.stringify(r));
  }
}
