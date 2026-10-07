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
 * new key, delete that property. The portal's daily housekeeping sends action "cleanup":
 * sheets (and export sub-folders) older than N days go to the Drive trash (restorable for 30 days).
 * Uploads may name a sub-folder (the parts of one large export) and are shared with anyone who
 * has the link when linkAccess is "view" or "edit", so nobody has to request access.
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

    if (req.action === 'cleanup') {
      var days = Number(req.days);
      if (!(days >= 1)) return json_({ ok: false, error: 'days must be at least 1' });
      var cutoff = new Date(Date.now() - days * 86400000);
      var root = folder_();
      var files = root.getFilesByType(MimeType.GOOGLE_SHEETS);
      var trashed = 0;
      while (files.hasNext()) {
        var file = files.next();
        if (file.getDateCreated() < cutoff) {
          file.setTrashed(true);
          trashed++;
        }
      }
      var subs = root.getFolders();  // the parts of large exports
      while (subs.hasNext()) {
        var sub = subs.next();
        if (sub.getDateCreated() < cutoff) {
          sub.setTrashed(true);
          trashed++;
        }
      }
      return json_({ ok: true, trashed: trashed });
    }

    var target = req.folder ? subFolder_(String(req.folder)) : folder_();
    var blob = Utilities.newBlob(Utilities.base64Decode(req.data), XLSX, req.name + '.xlsx');
    var file = Drive.Files.create({ name: req.name, mimeType: MimeType.GOOGLE_SHEETS, parents: [target.getId()] }, blob);
    var shared = req.shareWith ? share_(file.id, req.shareWith, req.role === 'reader' ? 'reader' : 'writer') : null;
    var link = shareLink_(DriveApp.getFileById(file.id), req.linkAccess);
    if (req.folder) shareLink_(target, req.linkAccess);
    return json_({ ok: true, id: file.id, url: 'https://docs.google.com/spreadsheets/d/' + file.id + '/edit', sharedWith: shared,
                   linkAccess: link, folderUrl: req.folder ? target.getUrl() : null });
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

/** Anyone with the link can open it ("view" or "edit"); "none" or nothing = only who it is shared with. */
function shareLink_(item, access) {
  if (access !== 'view' && access !== 'edit') return 'none';
  try {
    item.setSharing(DriveApp.Access.ANYONE_WITH_LINK, access === 'edit' ? DriveApp.Permission.EDIT : DriveApp.Permission.VIEW);
    return access;
  } catch (err) {
    return 'none';  // e.g. a Workspace policy that forbids link sharing
  }
}

/** The sub-folder of one large export (created once, reused for its next parts). */
function subFolder_(name) {
  var root = folder_();
  var found = root.getFoldersByName(name);
  return found.hasNext() ? found.next() : root.createFolder(name);
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
