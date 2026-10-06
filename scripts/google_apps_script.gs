/**
 * Kopi Calf Portal - Google Sheets export (Apps Script web app, used by app/gsheets.py).
 *
 * The portal POSTs the export (.xlsx, base64); this script saves it in the Drive of the
 * account that deployed it as a Google Sheet, in the folder FOLDER_NAME, and shares it with
 * the user who exported it.
 *
 * Setup: paste this file into script.google.com, Services (+) > Drive API > Add,
 * Run "authorize" once (grant access), Deploy > Web app (Execute as: Me, Who has access:
 * Anyone). The portal server registers its secret key on first contact; only the key's
 * SHA-256 is kept (Project Settings > Script properties: PORTAL_KEY_SHA256). To pair with a
 * new key, delete that property.
 */
var FOLDER_NAME = 'Kopi Calf Portal Exports';
var XLSX = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet';

/** Run once from the editor so Google asks for the Drive permissions. */
function authorize() {
  DriveApp.getRootFolder();
  Drive.Files.list({ pageSize: 1 });
  Logger.log('Authorized. Deploy as a web app (Execute as: Me, Who has access: Anyone).');
}

function doGet() {
  var props = PropertiesService.getScriptProperties();
  return json_({ ok: true, service: 'kopicalf-portal-export', paired: Boolean(props.getProperty('PORTAL_KEY_SHA256')) });
}

function doPost(e) {
  try {
    var req = JSON.parse(e.postData.contents);
    var props = PropertiesService.getScriptProperties();
    var stored = props.getProperty('PORTAL_KEY_SHA256');
    var given = sha256_(String(req.key || ''));
    if (!req.key || String(req.key).length < 32) return json_({ ok: false, error: 'missing key' });

    if (req.action === 'pair') {
      // first contact wins; later pairs only succeed with the same key
      var lock = LockService.getScriptLock();
      lock.waitLock(10000);
      try {
        stored = props.getProperty('PORTAL_KEY_SHA256');
        if (!stored) props.setProperty('PORTAL_KEY_SHA256', given);
        else if (stored !== given) return json_({ ok: false, error: 'already paired with another key' });
      } finally {
        lock.releaseLock();
      }
      return json_({ ok: true, folderUrl: folder_().getUrl() });
    }

    if (!stored || stored !== given) return json_({ ok: false, error: 'unauthorized' });

    var blob = Utilities.newBlob(Utilities.base64Decode(req.data), XLSX, req.name + '.xlsx');
    var file = Drive.Files.create({ name: req.name, mimeType: MimeType.GOOGLE_SHEETS, parents: [folder_().getId()] }, blob);
    var shared = req.shareWith ? share_(file.id, req.shareWith, req.role === 'reader' ? 'reader' : 'writer') : null;
    return json_({ ok: true, id: file.id, url: 'https://docs.google.com/spreadsheets/d/' + file.id + '/edit', sharedWith: shared });
  } catch (err) {
    return json_({ ok: false, error: String(err && err.message || err) });
  }
}

function share_(fileId, email, role) {
  // without a notification first; an address that is not a Google account needs the e-mail invite
  var notify = [false, true];
  for (var i = 0; i < notify.length; i++) {
    try {
      Drive.Permissions.create({ type: 'user', role: role, emailAddress: email }, fileId, { sendNotificationEmail: notify[i] });
      return email;
    } catch (err) { /* try the next way */ }
  }
  return null;
}

function folder_() {
  var props = PropertiesService.getScriptProperties();
  var id = props.getProperty('FOLDER_ID');
  if (id) {
    try {
      var f = DriveApp.getFolderById(id);
      if (!f.isTrashed()) return f;
    } catch (err) { /* deleted: create again */ }
  }
  var folder = DriveApp.createFolder(FOLDER_NAME);
  props.setProperty('FOLDER_ID', folder.getId());
  return folder;
}

function sha256_(text) {
  return Utilities.computeDigest(Utilities.DigestAlgorithm.SHA_256, text, Utilities.Charset.UTF_8)
    .map(function (b) { return ('0' + (b & 0xff).toString(16)).slice(-2); }).join('');
}

function json_(body) {
  return ContentService.createTextOutput(JSON.stringify(body)).setMimeType(ContentService.MimeType.JSON);
}
