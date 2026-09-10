/* Browser storage is transport, never authorization or a report renderer. */
const APP_PREFIX = "cxplorer:";
const NAMESPACE = /^cxplorer:[a-f0-9]{64}$/;
const REPORT_ID = /^[a-f0-9]{32}$/;
const FINGERPRINT = /^[a-f0-9]{64}$/;
const SEVEN_DAYS = 7 * 24 * 60 * 60 * 1000;
const MAX_BYTES = 2 * 1024 * 1024;
const MAX_REPORTS = 50;
const MAX_BLOB_CHARS = Math.ceil((512 * 1024) / 3) * 4 + 65;
export const MAX_RECORD_CHARS = MAX_BLOB_CHARS + 8192;
const DRAFT_FIELDS = Object.freeze({
  homepage_url: 2048,
  about_url: 2048,
  products_url: 2048,
  source_url_4: 2048,
  source_url_5: 2048,
  source_url_6: 2048,
  source_purpose_4: 16,
  source_purpose_5: 16,
  source_purpose_6: 16,
  seller_context: 4000,
});
const AUDIENCES = ["ceo", "cto", "cio", "cfo", "ciso"];
const PURPOSES = ["", "investors", "newsroom", "trust", "industry", "careers", "other"];
const REPORT_FIELDS = [
  "report_id", "company_name", "generated_at", "expires_at", "input_fingerprint", "blob",
];

export class CacheFault extends Error {
  constructor(code, message) {
    super(message);
    this.code = code;
  }
}

export function setText(root, selector, message) {
  const element = root.querySelector(selector);
  if (element) element.textContent = message;
}

export function show(root, selector, visible = true) {
  const element = root.querySelector(selector);
  if (element) element.hidden = !visible;
}

export function dateLabel(value) {
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: "medium", timeStyle: "short",
  }).format(new Date(value));
}

function exactKeys(value, keys) {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    && Object.keys(value).length === keys.length
    && keys.every((key) => Object.hasOwn(value, key));
}

function text(value, max, min = 0) {
  return typeof value === "string" && value.length >= min && value.length <= max;
}

function timestamp(value) {
  if (!text(value, 40) || !/^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])T([01]\d|2[0-3]):[0-5]\d:[0-5]\d(?:\.\d{1,6})?(Z|[+-]([01]\d|2[0-3]):[0-5]\d)$/.test(value)) {
    return NaN;
  }
  const year = Number(value.slice(0, 4));
  const month = Number(value.slice(5, 7));
  const day = Number(value.slice(8, 10));
  if (day > new Date(Date.UTC(year, month, 0)).getUTCDate()) return NaN;
  return Date.parse(value);
}

function checkDates(start, end) {
  const generated = timestamp(start);
  const expires = timestamp(end);
  if (!Number.isFinite(generated) || !Number.isFinite(expires)
      || expires <= generated || expires - generated > SEVEN_DAYS) {
    throw new CacheFault("invalid", "The saved copy has invalid dates.");
  }
  // A device clock disagreement is not proof that a signed report is corrupt.
  if (generated > Date.now() + 60_000) {
    throw new CacheFault("clock", "A saved copy is dated in the future. Check this device’s clock and retry; the copy was kept.");
  }
  if (expires <= Date.now()) throw new CacheFault("expired", "The saved copy has expired.");
}

function validateDraft(record) {
  if (!exactKeys(record, ["fields", "audiences", "updated_at", "expires_at"])
      || !exactKeys(record.fields, Object.keys(DRAFT_FIELDS))
      || !Object.entries(DRAFT_FIELDS).every(([key, max]) => text(record.fields[key], max))
      || ![4, 5, 6].every((slot) => PURPOSES.includes(record.fields[`source_purpose_${slot}`]))
      || !Array.isArray(record.audiences) || record.audiences.length > 5
      || !record.audiences.every((role) => AUDIENCES.includes(role))
      || new Set(record.audiences).size !== record.audiences.length) {
    throw new CacheFault("invalid", "The saved draft is unreadable.");
  }
  checkDates(record.updated_at, record.expires_at);
  return record;
}

export function validateReport(record, expectedId) {
  if (!REPORT_ID.test(expectedId) || !exactKeys(record, REPORT_FIELDS)
      || record.report_id !== expectedId
      || !text(record.company_name, 240, 1) || !record.company_name.trim()
      || !text(record.input_fingerprint, 64) || !FINGERPRINT.test(record.input_fingerprint)
      || !text(record.blob, MAX_BLOB_CHARS, 1)) {
    throw new CacheFault("invalid", "The server’s signed cache record could not be accepted. Any existing saved copy was kept.");
  }
  checkDates(record.generated_at, record.expires_at);
  // The blob is opaque. Only the server verifies, decompresses and renders it.
  return record;
}

function storageFault(error) {
  if (error instanceof CacheFault) return error;
  if (error?.name === "QuotaExceededError" || error?.name === "NS_ERROR_DOM_QUOTA_REACHED") {
    return new CacheFault("full", "Browser storage is full. Existing reports were kept. Delete an unwanted saved copy, then retry, or download your report.");
  }
  return new CacheFault("unavailable", "Browser cache is unavailable. Changes are not saved. Allow local storage and retry, or download your report before leaving.");
}

export async function serverRequest(value, options = {}) {
  let url;
  try {
    url = new URL(value, window.location.href);
    if (url.origin !== window.location.origin || !["http:", "https:"].includes(url.protocol)
        || url.username || url.password) throw new Error();
  } catch {
    throw new CacheFault("network", "The server address is unavailable. Reload this page to retry.");
  }
  const controller = new AbortController();
  const timeout = window.setTimeout(() => controller.abort(), 15_000);
  try {
    return await fetch(url, {
      ...options, credentials: "same-origin", cache: "no-store",
      redirect: "error", signal: controller.signal,
    });
  } finally {
    window.clearTimeout(timeout);
  }
}

export async function responseText(response, limit) {
  if (!response.body) throw new CacheFault("network", "The server returned an empty response.");
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let result = "";
  let timedOut = false;
  const timeout = window.setTimeout(() => {
    timedOut = true;
    reader.cancel().catch(() => {});
  }, 15_000);
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (timedOut) throw new CacheFault("network", "The server response timed out. Existing browser copies were kept; check your connection and retry.");
      result += decoder.decode(value, { stream: !done });
      if (result.length > limit) {
        await reader.cancel();
        throw new CacheFault("response", "The server response is too large for browser caching. Download the report instead.");
      }
      if (done) return result;
    }
  } finally {
    window.clearTimeout(timeout);
    reader.releaseLock();
  }
}

export class BrowserCache {
  constructor(root) {
    this.root = root;
    // Immutable, server-rendered context: never derived from a form, URL or stored identity.
    Object.defineProperty(this, "prefix", { value: root.dataset.cachePrefix || "" });
    this.active = NAMESPACE.test(this.prefix);
    this.revision = 0;
    this.clearRevision = 0;
    this.keyRevisions = new Map();
    this.removals = { expired: 0, invalid: 0, clock: 0, ignored: 0 };
    this.channel = null;
    if (!this.active) {
      this.warn("The authenticated browser workspace is unavailable. Reload or sign in again; no saved data was changed.");
      return;
    }
    // Ephemeral account-switch coordination. No session or profile is persisted.
    try {
      if ("BroadcastChannel" in window) {
        this.channel = new BroadcastChannel("cxplorer:workspace-context");
        this.channel.onmessage = ({ data }) => {
          if (!this.active) return;
          if (data?.type === "signed-out"
              || (data?.type === "account" && NAMESPACE.test(data.prefix) && data.prefix !== this.prefix)) {
            this.pause("Your account or session changed in another tab. Reload this page before saving or opening copies. Saved data was kept.");
          } else if (data?.type === "cache-cleared" && data.prefix === this.prefix) {
            this.invalidate();
            this.notice("This account’s browser cache was cleared in another tab.");
            this.emit("cleared");
          }
        };
        this.broadcast("account");
      }
    } catch {
      // Keys remain namespace-bound even when cross-tab notifications are unavailable.
    }
    window.addEventListener("storage", (event) => {
      if (!this.active || (event.key !== null && !this.owns(event.key))) return;
      this.invalidate(event.key);
      this.emit("external", { key: event.key });
    });
    window.addEventListener("pagehide", () => {
      this.active = false;
      this.invalidate();
      this.channel?.close();
    });
    window.addEventListener("pageshow", (event) => {
      if (event.persisted) window.location.reload();
    });
  }

  assertActive() {
    if (!this.active) throw new CacheFault("paused", "Reload this page to use the current authenticated workspace. Saved copies were kept.");
  }

  get storage() {
    this.assertActive();
    try {
      return window.localStorage;
    } catch (error) {
      throw storageFault(error);
    }
  }

  owns(key) {
    return key === `${this.prefix}:draft` || key.startsWith(`${this.prefix}:report:`);
  }

  reportKey(id) {
    this.assertActive();
    if (!REPORT_ID.test(id)) throw new CacheFault("invalid", "This report address is invalid. No saved copies were changed.");
    return `${this.prefix}:report:${id}`;
  }

  invalidate(key = null) {
    this.revision += 1;
    if (key === null) this.clearRevision = this.revision;
    else this.keyRevisions.set(key, this.revision);
  }

  reportRevision(id) {
    // Report work is invalidated by this key or a clear/session change, not draft edits.
    return Math.max(this.clearRevision, this.keyRevisions.get(this.reportKey(id)) || 0);
  }

  broadcast(type) {
    try {
      this.channel?.postMessage({ type, prefix: this.prefix });
    } catch {
      // A closed/unavailable notification channel must never block native sign-out.
    }
  }

  emit(name, detail = {}) {
    this.root.dispatchEvent(new CustomEvent(`cache:${name}`, { detail }));
  }

  notice(message) {
    setText(this.root, "[data-cache-notice]", message);
  }

  warn(message) {
    setText(this.root, "[data-cache-warning]", message);
    show(this.root, "[data-cache-warning]", Boolean(message));
  }

  pause(message) {
    if (!this.active) return;
    this.active = false;
    this.invalidate();
    this.warn(message);
    this.emit("paused");
  }

  async exclusive(action, writing = false) {
    this.assertActive();
    try {
      if (navigator.locks?.request) {
        // All namespaces share a lock because the 2 MiB budget is app-wide.
        return await navigator.locks.request("cxplorer:cache-write", () => {
          this.assertActive();
          return action(true);
        });
      }
      if (writing) {
        throw new CacheFault("unavailable", "This browser cannot safely coordinate cache writes across tabs. Use a current browser over HTTPS (or localhost), then retry. Download reports before leaving.");
      }
      return action(false);
    } catch (error) {
      throw storageFault(error);
    }
  }

  keys(storage) {
    if (storage.length > 10_000) {
      throw new CacheFault("unavailable", "There are too many browser storage entries to safely check capacity. Nothing was overwritten; download your report instead.");
    }
    const keys = [];
    for (let index = 0; index < storage.length; index += 1) {
      const key = storage.key(index);
      if (key !== null) keys.push(key);
    }
    return keys;
  }

  readRecord(storage, key, canPrune, removed) {
    const raw = storage.getItem(key);
    if (raw === null) return null;
    try {
      const isDraft = key === `${this.prefix}:draft`;
      if (raw.length > (isDraft ? 120_000 : MAX_RECORD_CHARS)) throw new CacheFault("invalid", "");
      let record;
      try {
        record = JSON.parse(raw);
      } catch {
        throw new CacheFault("invalid", "");
      }
      return isDraft ? validateDraft(record) : validateReport(record, key.slice(`${this.prefix}:report:`.length));
    } catch (error) {
      if (!["invalid", "expired", "clock"].includes(error.code)) throw error;
      if (error.code === "clock") {
        removed.clock += 1;
      } else if (canPrune) {
        storage.removeItem(key);
        removed[error.code] += 1;
      } else {
        removed.ignored += 1;
      }
      return null;
    }
  }

  removalNotice(removed) {
    if (!Object.values(removed).some(Boolean)) return;
    for (const key of Object.keys(this.removals)) this.removals[key] += removed[key];
    const total = this.removals;
    const messages = [];
    if (total.expired) messages.push(`${total.expired} expired browser ${total.expired === 1 ? "copy was" : "copies were"} removed.`);
    if (total.invalid) messages.push(`${total.invalid} unreadable or invalid browser ${total.invalid === 1 ? "entry was" : "entries were"} removed. Other copies were kept.`);
    if (total.clock) messages.push("A future-dated copy was kept but not opened. Check this device’s clock.");
    if (total.ignored) messages.push("Invalid or expired entries were ignored. This browser cannot safely coordinate automatic removal.");
    if (messages.length) this.notice(messages.join(" "));
  }

  sweepOwn(storage, canPrune) {
    const removed = { expired: 0, invalid: 0, clock: 0, ignored: 0 };
    const reports = [];
    for (const key of this.keys(storage).filter((entry) => this.owns(entry))) {
      const record = this.readRecord(storage, key, canPrune, removed);
      if (record && key !== `${this.prefix}:draft`) reports.push(record);
    }
    this.removalNotice(removed);
    return reports;
  }

  async read(key) {
    if (!this.owns(key)) throw new CacheFault("invalid", "The saved copy is outside this account’s workspace.");
    return this.exclusive((canPrune) => {
      const removed = { expired: 0, invalid: 0, clock: 0, ignored: 0 };
      const record = this.readRecord(this.storage, key, canPrune, removed);
      this.removalNotice(removed);
      return record;
    });
  }

  readDraft() {
    return this.read(`${this.prefix}:draft`);
  }

  readReport(id) {
    return this.read(this.reportKey(id));
  }

  async metadata() {
    return this.exclusive((canPrune) => {
      const reports = this.sweepOwn(this.storage, canPrune);
      reports.sort((a, b) => timestamp(b.generated_at) - timestamp(a.generated_at));
      if (reports.length > MAX_REPORTS) this.notice("Only the newest 50 saved reports are listed. Additional valid copies were kept.");
      return reports.slice(0, MAX_REPORTS).map(({ report_id, company_name, generated_at, expires_at }) => ({
        report_id, company_name, generated_at, expires_at,
      }));
    });
  }

  async save(key, record, revision) {
    return this.exclusive(() => {
      const isDraft = key === `${this.prefix}:draft`;
      const currentRevision = isDraft ? this.revision : this.reportRevision(record.report_id);
      if (revision !== currentRevision) throw new CacheFault("changed", "Browser copies changed while saving. Nothing was overwritten. Retry to save this page’s copy.");
      const storage = this.storage;
      // Remove this account's expired/definitely invalid entries first. Other accounts are not touched.
      const reports = this.sweepOwn(storage, true);
      if (isDraft) validateDraft(record);
      else {
        validateReport(record, key.slice(`${this.prefix}:report:`.length));
        const previous = reports.find((entry) => entry.report_id === record.report_id);
        if (!previous && storage.getItem(key) !== null) {
          throw new CacheFault("changed", "An existing copy could not be checked, possibly because of this device’s clock. It was kept unchanged. Check the clock or download this report.");
        }
        if (previous && (previous.generated_at !== record.generated_at || previous.expires_at !== record.expires_at)) {
          throw new CacheFault("changed", "The saved copy has different original dates. It was kept unchanged; download this report instead.");
        }
        if (!previous && reports.length >= MAX_REPORTS) {
          throw new CacheFault("full", "The 50-report browser limit is reached. Valid reports were kept. Delete an unwanted saved copy and retry, or download this report.");
        }
      }
      const value = JSON.stringify(record);
      let total = 0;
      for (const entry of this.keys(storage)) {
        if (!entry.startsWith(APP_PREFIX) || entry === key) continue;
        const existing = storage.getItem(entry);
        // JS string length counts UTF-16 code units, including surrogate pairs.
        if (existing !== null) total += 2 * (entry.length + existing.length);
      }
      total += 2 * (key.length + value.length);
      if (total > MAX_BYTES) {
        throw new CacheFault("full", "CXplorer’s total 2 MiB browser limit is reached, including other accounts’ copies. No valid report was removed. Delete an unwanted saved copy and retry, or download your report.");
      }
      // setItem is atomic: a quota/security failure leaves the old value intact.
      storage.setItem(key, value);
      this.emit("saved", { kind: isDraft ? "draft" : "report" });
      return record;
    }, true);
  }

  saveDraft(record, revision = this.revision) {
    // Never persist FormData: hidden CSRF/submission fields and identity are excluded.
    validateDraft(record);
    return this.save(`${this.prefix}:draft`, record, revision);
  }

  saveReport(record, id, revision = this.reportRevision(id)) {
    validateReport(record, id);
    return this.save(this.reportKey(id), record, revision);
  }

  async removeReport(id) {
    const key = this.reportKey(id);
    this.invalidate(key);
    const removed = await this.exclusive(() => {
      const storage = this.storage;
      const exists = storage.getItem(key) !== null;
      storage.removeItem(key);
      return exists;
    });
    this.emit("deleted", { id });
    return removed;
  }

  async clearOwn() {
    this.invalidate();
    const count = await this.exclusive(() => {
      const storage = this.storage;
      const keys = this.keys(storage).filter((key) => this.owns(key));
      for (const key of keys) storage.removeItem(key);
      return keys.length;
    });
    this.warn("");
    this.notice(`Cleared ${count} browser ${count === 1 ? "entry" : "entries"} for this account. Other accounts, downloads and your sign-in session are unchanged.`);
    // Also cancel pending saves in tabs where no report key existed to emit a storage event.
    this.broadcast("cache-cleared");
    this.emit("cleared");
  }
}

export function wireCacheControls(cache) {
  const button = cache.root.querySelector("[data-cache-clear]");
  if (!button) return;
  button.hidden = false;
  button.addEventListener("click", async () => {
    if (!window.confirm("Delete this account’s draft and all its saved reports from this browser? This cannot be undone.")) return;
    button.disabled = true;
    try {
      await cache.clearOwn();
    } catch (error) {
      cache.warn(storageFault(error).message);
    } finally {
      button.disabled = !cache.active;
    }
  });
  cache.root.addEventListener("cache:paused", () => { button.disabled = true; });
}

function wireHistory(cache) {
  const root = cache.root;
  const list = root.querySelector("[data-saved-list]");
  const retry = root.querySelector("[data-history-retry]");
  if (!list || !retry) return;
  let sequence = 0;
  let expiryTimer;
  retry.hidden = false;
  async function refresh() {
    const request = ++sequence;
    const revision = cache.revision;
    window.clearTimeout(expiryTimer);
    list.setAttribute("aria-busy", "true");
    retry.disabled = true;
    setText(root, "[data-history-status]", "Checking this account’s saved browser reports…");
    try {
      const entries = await cache.metadata();
      const response = await serverRequest(root.dataset.cachedListUrl, {
        method: "POST",
        headers: { "Content-Type": "application/json", "Accept": "text/html", "X-CSRF-Token": root.dataset.csrfToken },
        body: JSON.stringify({ entries }),
      });
      if (response.status === 401 || response.status === 403) {
        cache.pause("Your session expired or changed. Sign in again or reload to list saved reports. Browser copies were kept.");
        throw new CacheFault("session", "Your session expired or changed. Reload or sign in again; saved copies were kept.");
      }
      if (!response.ok || !response.headers.get("content-type")?.includes("text/html")) throw new Error();
      const html = await responseText(response, 200_000);
      if (request !== sequence || revision !== cache.revision || !cache.active) return;
      const documentFragment = new DOMParser().parseFromString(html, "text/html");
      const fragment = documentFragment.querySelector("[data-saved-report-fragment]");
      if (!fragment) throw new Error();
      // Business labels and links are rendered/escaped by Jinja, not built from local data.
      list.replaceChildren(document.importNode(fragment, true));
      setText(root, "[data-history-status]", entries.length
        ? `${entries.length} saved ${entries.length === 1 ? "report" : "reports"} in this browser. Opening verifies the signed copy.`
        : "No unexpired saved reports were found for this account in this browser.");
      if (entries.length) {
        const nextExpiry = Math.min(...entries.map((entry) => timestamp(entry.expires_at)));
        expiryTimer = window.setTimeout(refresh, Math.max(1000, nextExpiry - Date.now() + 100));
      }
    } catch (error) {
      if (request !== sequence) return;
      setText(root, "[data-history-status]", error instanceof CacheFault ? error.message
        : "Saved history could not be loaded. Local copies have not been deleted. Check your connection and refresh saved reports.");
    } finally {
      if (request === sequence) {
        list.setAttribute("aria-busy", "false");
        retry.disabled = !cache.active;
      }
    }
  }
  retry.addEventListener("click", refresh);
  list.addEventListener("click", async (event) => {
    const button = event.target.closest("[data-delete-saved]");
    if (!button || !list.contains(button) || !REPORT_ID.test(button.dataset.deleteSaved)) return;
    if (!window.confirm("Delete this saved browser copy? Downloads and the temporary server copy are not deleted.")) return;
    button.disabled = true;
    try {
      await cache.removeReport(button.dataset.deleteSaved);
      cache.notice("Saved browser copy deleted. Other reports were kept.");
      await refresh();
      retry.focus();
    } catch (error) {
      cache.warn(storageFault(error).message);
      button.disabled = false;
    }
  });
  root.addEventListener("cache:cleared", refresh);
  root.addEventListener("cache:external", (event) => {
    if (event.detail.key !== `${cache.prefix}:draft`) refresh();
  });
  root.addEventListener("cache:paused", () => {
    sequence += 1;
    window.clearTimeout(expiryTimer);
    retry.disabled = true;
    list.setAttribute("aria-busy", "false");
  });
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden && cache.active) refresh();
  });
  refresh();
}

async function wireDraft(cache, form) {
  const root = cache.root;
  const retry = root.querySelector("[data-draft-retry]");
  let pending = null;
  let timer;
  let editedAt = 0;
  let interacted = false;
  let submitting = false;
  const field = (name) => form.elements.namedItem(name);
  const status = (message) => setText(root, "[data-draft-status]", message);

  function inputValues() {
    const fields = {};
    for (const name of Object.keys(DRAFT_FIELDS)) fields[name] = field(name).value;
    return {
      fields,
      audiences: Array.from(form.querySelectorAll('input[name="audiences"]:checked'), (input) => input.value),
    };
  }

  let lastValues = JSON.stringify(inputValues());
  function snapshot(values = inputValues()) {
    return {
      ...values,
      updated_at: new Date(editedAt).toISOString(),
      expires_at: new Date(editedAt + SEVEN_DAYS).toISOString(),
    };
  }

  async function savePending() {
    window.clearTimeout(timer);
    const change = pending;
    if (!change) return;
    try {
      await cache.saveDraft(change.record, change.revision);
      if (pending !== change) return;
      pending = null;
      cache.warn("");
      status(`Draft saved in this browser. Expires ${dateLabel(change.record.expires_at)}, seven days after your last edit.`);
      retry.hidden = true;
    } catch (error) {
      if (pending !== change) return;
      status("Draft changes are not saved. Your inputs remain on this page.");
      cache.warn(storageFault(error).message);
      retry.hidden = false;
    }
  }

  async function flushPending() {
    while (pending) {
      const change = pending;
      await savePending();
      // A failure keeps this snapshot for retry; do not trap the user in a sign-out loop.
      if (pending === change) break;
    }
  }

  function edited(event) {
    if (!Object.hasOwn(DRAFT_FIELDS, event.target.name) && event.target.name !== "audiences") return;
    const values = inputValues();
    const serialized = JSON.stringify(values);
    // Blur/change after an earlier input event is not a new edit and cannot renew expiry.
    if (serialized === lastValues) return;
    lastValues = serialized;
    interacted = true;
    editedAt = Date.now();
    pending = { record: snapshot(values), revision: cache.revision };
    status("Draft changes not saved yet…");
    window.clearTimeout(timer);
    timer = window.setTimeout(savePending, 250);
  }

  form.addEventListener("input", edited);
  form.addEventListener("change", edited);
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (submitting) return;
    submitting = true;
    await savePending();
    // Native POST; caching failure must not prevent server-side generation/validation.
    HTMLFormElement.prototype.submit.call(form);
  });
  const logout = root.querySelector("[data-sign-out]");
  logout?.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (submitting) return;
    submitting = true;
    logout.setAttribute("aria-busy", "true");
    if (event.submitter) event.submitter.disabled = true;
    try {
      // Await only the whitelisted draft, retaining the actual last edit/expiry timestamps.
      await flushPending();
    } catch (error) {
      status("Draft changes are not saved. Signing out will still continue.");
      cache.warn(storageFault(error).message);
    } finally {
      // A different account may have signed in while the write was waiting. Never adopt
      // its namespace/CSRF token or announce its sign-out on behalf of this stale form.
      if (cache.active) cache.broadcast("signed-out");
      HTMLFormElement.prototype.submit.call(logout);
    }
  });
  retry.addEventListener("click", () => {
    if (!editedAt) {
      status("Edit a source or audience to save a new draft.");
      return;
    }
    pending = { record: snapshot(), revision: cache.revision };
    savePending();
  });
  root.addEventListener("cache:cleared", () => {
    window.clearTimeout(timer);
    pending = null;
    editedAt = 0;
    retry.hidden = true;
    status("Browser draft cleared. Inputs on this page are unchanged; edit them to save a new draft.");
  });
  root.addEventListener("cache:external", (event) => {
    if (event.detail.key !== null && event.detail.key !== `${cache.prefix}:draft`) return;
    window.clearTimeout(timer);
    status("The browser draft changed in another tab. Your inputs were not replaced. Reload to use that draft, or edit/retry to save these inputs.");
    retry.hidden = !editedAt;
  });
  root.addEventListener("cache:paused", () => {
    window.clearTimeout(timer);
    pending = null;
    retry.disabled = true;
    status("Draft saving paused because the session changed. Reload this page; saved copies were kept.");
  });
  // Flush the most recent edit when a tab is hidden, without extending its edit time.
  document.addEventListener("visibilitychange", () => {
    if (document.hidden && cache.active) savePending();
  });

  try {
    const revision = cache.revision;
    const record = await cache.readDraft();
    if (revision !== cache.revision || !cache.active) return;
    if (form.dataset.hasErrors === "true") {
      if (!interacted) {
        status("Your submitted inputs were kept. The saved browser draft was not restored over the errors.");
        root.querySelector("[data-form-errors]")?.focus();
      }
      return;
    }
    if (interacted || !cache.active) return;
    if (record) {
      for (const name of Object.keys(DRAFT_FIELDS)) field(name).value = record.fields[name];
      for (const input of form.querySelectorAll('input[name="audiences"]')) {
        input.checked = record.audiences.includes(input.value);
      }
      lastValues = JSON.stringify(inputValues());
      if ([4, 5, 6].some((slot) => record.fields[`source_url_${slot}`] || record.fields[`source_purpose_${slot}`])) {
        form.querySelector("[data-optional-sources]").open = true;
      }
      editedAt = timestamp(record.updated_at);
      status(`Browser draft restored. Expires ${dateLabel(record.expires_at)}. Viewing has not extended its expiry.`);
    } else {
      status("Ready to save your draft in this browser when you edit. No sources are fetched until you generate.");
    }
    if (!navigator.locks?.request) {
      cache.warn("Browser draft saving is unavailable because safe cross-tab cache writes are not supported here. Use a current browser over HTTPS (or localhost). Existing copies can still be read.");
    }
  } catch (error) {
    status("Browser draft could not be restored. Your inputs are still usable, but changes may not be saved.");
    cache.warn(storageFault(error).message);
    retry.hidden = false;
  }
}

const draftForm = document.querySelector("[data-draft-form]");
if (draftForm) {
  const cache = new BrowserCache(draftForm.closest("[data-cache-root]"));
  wireCacheControls(cache);
  wireDraft(cache, draftForm);
  wireHistory(cache);
}
