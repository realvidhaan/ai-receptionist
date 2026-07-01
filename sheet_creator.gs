/**
 * Sheet-creator web app for the multi-tenant AI receptionist.
 *
 * WHY: a service account on a personal Google account has ZERO Drive storage, so it can't create
 * spreadsheets. This tiny web app runs as YOU, so it creates the Sheet under your Gmail (which has
 * storage), adds the Customers + Call Log tabs, and shares it with the service account for runtime
 * read/write. The Python server POSTs to this app during /provision and gets back the new sheet id.
 *
 * DEPLOY (one time, ~2 min):
 *   1. Go to https://script.google.com  ->  New project.  Paste this whole file.
 *   2. Set SECRET below to the SAME value as PROVISION_SECRET in your .env.
 *   3. Deploy -> New deployment -> type "Web app".
 *        Execute as: Me (your Gmail).   Who has access: Anyone.
 *   4. Authorize when prompted (it needs Sheets + Drive to create/share files).
 *   5. Copy the Web app URL (ends in /exec) into your .env as SHEET_CREATOR_URL.
 */

var SECRET = 'CHANGE-ME-to-match-PROVISION_SECRET';

function doPost(e) {
  try {
    var body = JSON.parse(e.postData.contents);
    if (body.secret !== SECRET) return _json({ error: 'forbidden' });

    var ss = SpreadsheetApp.create(body.title || 'Receptionist DB');

    var customers = ss.getSheets()[0];
    customers.setName('Customers');
    customers.appendRow(body.customers_headers || ['phone']);

    var log = ss.insertSheet('Call Log');
    log.appendRow(body.log_headers || ['timestamp']);

    if (body.share_with) ss.addEditor(body.share_with);  // grant the service account read/write

    return _json({ spreadsheet_id: ss.getId(), url: ss.getUrl() });
  } catch (err) {
    return _json({ error: String(err) });
  }
}

function _json(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj))
                       .setMimeType(ContentService.MimeType.JSON);
}
