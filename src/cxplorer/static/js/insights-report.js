import {
  BrowserCache, CacheFault, MAX_RECORD_CHARS, dateLabel, responseText,
  serverRequest, setText, show, validateReport, wireCacheControls,
} from "./workspace-cache.js";

function errorMessage(error) {
  return error instanceof CacheFault ? error.message
    : "The server or browser cache could not be reached. No existing copy was deleted. Check your connection, retry saving, or download JSON before leaving.";
}

function wireReport(root) {
  const cache = new BrowserCache(root);
  const id = root.dataset.reportId;
  const retry = root.querySelector("[data-report-retry]");
  const remove = root.querySelector("[data-report-delete]");
  const print = root.querySelector("[data-print-report]");
  let operation = 0;
  let expiryTimer;
  const status = (message) => setText(root, "[data-report-save-status]", message);
  wireCacheControls(cache);
  print.hidden = false;
  print.addEventListener("click", () => window.print());

  function saved(record) {
    status(`Saved in this browser temporarily until ${dateLabel(record.expires_at)}.`);
    remove.hidden = false;
    remove.disabled = false;
    retry.hidden = true;
    window.clearTimeout(expiryTimer);
    expiryTimer = window.setTimeout(async () => {
      try {
        if (!await cache.readReport(id)) {
          unsaved("This browser copy expired. Download JSON if it is still available.");
        }
      } catch (error) {
        cache.warn(errorMessage(error));
      }
    }, Math.max(1000, Date.parse(record.expires_at) - Date.now() + 100));
  }

  function unsaved(message) {
    operation += 1;
    window.clearTimeout(expiryTimer);
    status(message);
    remove.hidden = true;
    retry.hidden = false;
    retry.disabled = !cache.active;
  }

  function currentSave(request, revision) {
    // Superseding actions set their own explicit state; an old response cannot replace it.
    if (request !== operation) return false;
    if (!cache.active) {
      unsaved("Browser saving stopped because this page’s session is no longer active. Reload or sign in again; existing copies were kept.");
      return false;
    }
    if (revision !== cache.reportRevision(id)) {
      unsaved("This report’s browser copy changed while saving. Saving stopped without overwriting it. Retry saving or download JSON.");
      return false;
    }
    return true;
  }

  async function save() {
    const request = ++operation;
    retry.hidden = false;
    retry.disabled = true;
    status("Preparing the server’s signed browser copy. Keep this page open or download JSON until saving is confirmed.");
    try {
      const revision = cache.reportRevision(id);
      const existing = await cache.readReport(id);
      if (!currentSave(request, revision)) return;
      remove.hidden = !existing;
      const response = await serverRequest(root.dataset.cachePayloadUrl, {
        headers: { "Accept": "application/json" },
      });
      if (!currentSave(request, revision)) return;
      if (response.status === 401 || response.status === 403) {
        show(root, "[data-session-link]");
        cache.pause("Your session expired or changed. Browser saving is paused. Sign in again; existing saved copies were kept.");
        return;
      }
      if (response.status === 413) {
        throw new CacheFault("full", "This report is too large for browser caching. Download JSON before leaving. Any existing saved copy was kept.");
      }
      if (response.status === 404 || response.status === 410) {
        throw new CacheFault("unavailable", "The temporary server copy or its cache payload is no longer available. Any existing browser copy was kept. Return to the workspace to reopen it; generation was not restarted.");
      }
      if (!response.ok || !response.headers.get("content-type")?.includes("application/json")) throw new Error();
      let record;
      try {
        record = JSON.parse(await responseText(response, MAX_RECORD_CHARS));
      } catch (error) {
        if (error instanceof CacheFault) throw error;
        throw new CacheFault("response", "The server’s cache response was unreadable. This report was not saved; any existing browser copy was kept. Retry or download JSON.");
      }
      validateReport(record, id);
      if (!currentSave(request, revision)) return;
      await cache.saveReport(record, id, revision);
      if (!currentSave(request, revision)) return;
      cache.warn("");
      saved(record);
    } catch (error) {
      if (request !== operation) return;
      status("Browser saving is not confirmed. Keep this page open or download JSON. Any existing saved copy was kept.");
      cache.warn(errorMessage(error));
    } finally {
      if (request === operation) retry.disabled = !cache.active;
    }
  }

  retry.addEventListener("click", save);
  remove.addEventListener("click", async () => {
    if (!window.confirm("Delete this report’s saved browser copy? Downloaded files are not deleted.")) return;
    operation += 1;
    window.clearTimeout(expiryTimer);
    status("Browser saving stopped while the saved copy is being deleted.");
    retry.hidden = false;
    retry.disabled = true;
    remove.disabled = true;
    try {
      await cache.removeReport(id);
      cache.warn("");
      unsaved("Saved browser copy deleted. This page is not saved. Download JSON or choose Retry saving report to keep it again.");
      cache.notice("Only this report’s browser entry was deleted. Other saved copies were kept.");
      retry.focus();
    } catch (error) {
      status("The browser copy could not be deleted. Saving this page was stopped; existing copies were kept. Retry saving or download JSON.");
      cache.warn(errorMessage(error));
      remove.disabled = !cache.active;
    } finally {
      retry.disabled = !cache.active;
    }
  });
  root.addEventListener("cache:cleared", () => {
    unsaved("Browser cache cleared for this account. This report is not saved. Download JSON or explicitly retry saving to keep it again.");
  });
  root.addEventListener("cache:external", async (event) => {
    if (event.detail.key !== null && event.detail.key !== `${cache.prefix}:report:${id}`) return;
    unsaved("This report’s browser copy changed in another tab. Saving this page stopped without overwriting it. Retry saving or download JSON.");
    const request = operation;
    try {
      const record = await cache.readReport(id);
      if (request !== operation || !cache.active) return;
      if (record) {
        remove.hidden = false;
        remove.disabled = false;
        cache.notice(`The changed browser copy was kept until its original expiry, ${dateLabel(record.expires_at)}. This server-rendered report has not changed.`);
      } else {
        unsaved("The saved copy was removed or expired in another tab. This page is not saved; download JSON or explicitly retry saving.");
      }
    } catch (error) {
      cache.warn(errorMessage(error));
    }
  });
  root.addEventListener("cache:paused", () => {
    operation += 1;
    window.clearTimeout(expiryTimer);
    retry.disabled = true;
    remove.disabled = true;
    status("Browser saving is paused because the session changed. Existing saved copies were kept.");
    show(root, "[data-session-link]");
  });
  save();
}

function wireRestore(root) {
  const cache = new BrowserCache(root);
  const id = root.dataset.reportId;
  const form = root.querySelector("[data-restore-form]");
  const blob = form.elements.namedItem("cache_blob");
  const submit = root.querySelector("[data-restore-submit]");
  const retry = root.querySelector("[data-restore-retry]");
  const status = (message) => setText(root, "[data-restore-status]", message);
  let record = null;
  let checking = 0;
  let submitted = false;
  retry.hidden = false;

  async function check(autoSubmit = false) {
    const request = ++checking;
    const revision = cache.revision;
    record = null;
    blob.value = "";
    submit.hidden = true;
    retry.disabled = true;
    status("Checking this account’s browser copy…");
    try {
      const candidate = await cache.readReport(id);
      if (request !== checking || revision !== cache.revision || !cache.active) return;
      if (!candidate) {
        status("No readable, unexpired copy was found for this account in this browser. Nothing was regenerated. Check your original browser or return to the workspace.");
        return;
      }
      record = candidate;
      // Transport only. No decoding, decompression or business/report HTML in JavaScript.
      blob.value = record.blob;
      submit.hidden = false;
      status(`A saved copy is available until ${dateLabel(record.expires_at)}. The server must verify it before any report is displayed.`);
      if (autoSubmit) form.requestSubmit(submit);
    } catch (error) {
      status("The saved copy could not be opened. No new report was generated.");
      cache.warn(errorMessage(error));
    } finally {
      retry.disabled = !cache.active || submitted;
    }
  }

  form.addEventListener("submit", (event) => {
    if (submitted || !cache.active || !record || Date.parse(record.expires_at) <= Date.now()) {
      event.preventDefault();
      blob.value = "";
      if (!submitted) status("The copy or session changed. Check the browser again before opening it.");
      return;
    }
    submitted = true;
    submit.disabled = true;
    retry.disabled = true;
    status("Sending the signed copy for server verification. This opens the existing report; it does not regenerate it.");
    // An actual same-origin POST form carries the blob and current CSRF token.
    // Network/401/503 failures do not remove the local entry.
  });
  retry.addEventListener("click", () => check(false));
  root.addEventListener("cache:paused", () => {
    checking += 1;
    record = null;
    blob.value = "";
    submit.disabled = true;
    retry.disabled = true;
    status("Opening is paused because the session changed. Reload or sign in again; the saved copy was kept.");
  });
  root.addEventListener("cache:external", (event) => {
    if (event.detail.key !== null && event.detail.key !== `${cache.prefix}:report:${id}`) return;
    checking += 1;
    record = null;
    blob.value = "";
    submit.hidden = true;
    retry.disabled = false;
    status("This browser copy changed in another tab. Check the browser again before opening it.");
  });

  if (root.dataset.invalidCache === "true") {
    // Only an explicit server rejection is evidence of signature/schema/blob corruption.
    // Do not auto-submit a rejection shell, even if another tab later saves a copy.
    status("The server rejected this saved copy. Automatic opening has stopped.");
    cache.removeReport(id).then((removed) => {
      cache.notice(removed
        ? "Only this rejected report’s browser entry was removed. Other reports and the draft were kept."
        : "No matching browser entry remained to remove. Other saved data was not changed.");
    }).catch((error) => cache.warn(errorMessage(error)));
  } else {
    // A POST error shell never retries itself. A normal authenticated GET may restore once.
    const isPostShell = window.location.pathname === new URL(form.action).pathname;
    check(root.dataset.hasErrors !== "true" && !isPostShell);
  }
}

const report = document.querySelector("[data-insight-report]");
const restore = document.querySelector("[data-insight-restore]");
if (report) wireReport(report);
if (restore) wireRestore(restore);
