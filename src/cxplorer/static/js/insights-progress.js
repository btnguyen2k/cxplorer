import { BrowserCache, CacheFault, responseText, serverRequest, setText, show } from "./workspace-cache.js";

const progressByStage = Object.freeze({
  preflight: 10,
  verify_sources: 20,
  discover_news: 30,
  verify_news: 38,
  collect_sources: 46,
  extract_evidence: 58,
  consolidate_evidence: 68,
  synthesize_strategy: 76,
  generate_talk_points: 84,
  repair: 94,
  review_report: 94,
  complete: 100,
});

function stageLabel(stage) {
  const label = stage.replaceAll("_", " ");
  return label.charAt(0).toUpperCase() + label.slice(1);
}

const root = document.querySelector("[data-insight-progress]");
if (root) {
  const context = new BrowserCache(root);
  const activeStates = ["queued", "running"];
  const finishedStates = ["completed", "partial", "failed", "cancelled"];
  const retry = root.querySelector("[data-poll-retry]");
  const meter = root.querySelector("[data-progress-meter]");
  const initialProgress = Number.parseInt(meter?.getAttribute("aria-valuenow") || "0", 10);
  let highestProgress = Number.isFinite(initialProgress) ? initialProgress : 0;
  let state = root.dataset.jobState;
  let timer;
  let checking = false;
  let paused = false;
  let leaving = false;

  function stop() {
    window.clearTimeout(timer);
    paused = true;
  }

  function warning(message, canRetry = true) {
    setText(root, "[data-progress-warning]", message);
    show(root, "[data-progress-warning]");
    show(root, "[data-poll-retry]", canRetry);
  }

  function updateProgress(jobState, stage) {
    if (!meter) return;
    const label = stageLabel(stage);
    let value = null;
    if (["completed", "partial"].includes(jobState)) {
      value = 100;
    } else if (jobState === "running" && Object.hasOwn(progressByStage, stage)) {
      value = progressByStage[stage];
      // A repair pass can revisit an earlier stage; never make the visible estimate run backward.
      value = Math.max(value, highestProgress);
    }
    if (value !== null) highestProgress = Math.max(highestProgress, value);

    meter.dataset.progressActive = activeStates.includes(jobState) ? "true" : "false";
    meter.dataset.progressLevel = value === null ? "indeterminate" : String(value);
    if (value === null) {
      meter.removeAttribute("aria-valuenow");
    } else {
      meter.setAttribute("aria-valuenow", String(value));
    }

    let visibleValue = "Working…";
    let valueText = `In progress — ${label}.`;
    if (value !== null) {
      visibleValue = value === 100 ? "100%" : `About ${value}%`;
      valueText = value === 100 ? `Complete — ${label}.` : `About ${value} percent. ${label}.`;
    } else if (jobState === "queued") {
      visibleValue = "Waiting…";
      valueText = "Queued — waiting for generation capacity.";
    } else if (jobState === "failed") {
      visibleValue = "Stopped";
      valueText = `Generation stopped during ${label}.`;
    } else if (jobState === "cancelled") {
      visibleValue = "Cancelled";
      valueText = `Generation cancelled during ${label}.`;
    }
    meter.setAttribute("aria-valuetext", valueText);
    setText(root, "[data-progress-value]", visibleValue);
  }

  async function poll() {
    if (checking || paused || leaving || !context.active || document.hidden || !activeStates.includes(state)) return;
    checking = true;
    try {
      const response = await serverRequest(root.dataset.statusUrl, {
        headers: { "Accept": "application/json" },
      });
      if (!context.active || leaving) return;
      if (response.status === 401 || response.status === 403) {
        stop();
        context.pause("Your session expired or changed. Status checks have stopped. Sign in again; saved browser data was kept.");
        show(root, "[data-session-link]");
        warning("Sign in again before checking this generation. No automatic retry or new generation will run.", false);
        return;
      }
      if (response.status === 404) {
        stop();
        warning("This temporary job is missing or expired, possibly after a server restart. Use Refresh status to look for a saved report, or return to the workspace. Nothing will restart automatically.", false);
        return;
      }
      if (!response.ok || !response.headers.get("content-type")?.includes("application/json")) throw new Error();
      const payload = JSON.parse(await responseText(response, 12_000));
      if (!context.active || leaving) return;
      if (!payload || ![...activeStates, ...finishedStates].includes(payload.state)
          || typeof payload.stage !== "string" || payload.stage.length > 160
          || typeof payload.message !== "string" || payload.message.length > 2000
          || (payload.error != null && (typeof payload.error !== "string" || payload.error.length > 2000))) {
        throw new CacheFault("response", "The status response could not be read. Your last known stage is still shown. Retry the status check.");
      }
      state = payload.state;
      updateProgress(state, payload.stage);
      if (finishedStates.includes(state)) {
        stop();
        leaving = true;
        // The authenticated server renders completion, partial coverage and failures.
        // Never use an API-supplied URL to navigate to another origin.
        window.location.reload();
        return;
      }
      setText(root, "[data-progress-stage]", stageLabel(payload.stage));
      setText(root, "[data-progress-state]", state.charAt(0).toUpperCase() + state.slice(1));
      setText(root, "[data-progress-message]", payload.message);
      show(root, "[data-progress-warning]", false);
      show(root, "[data-poll-retry]", false);
    } catch (error) {
      if (leaving || !context.active) return;
      stop();
      warning(error instanceof CacheFault ? error.message
        : "The status check could not reach the server. Generation may still be running; the last known stage is shown. Check your connection and retry. No work has been restarted.");
    } finally {
      checking = false;
      if (!paused && !leaving && context.active && !document.hidden && activeStates.includes(state)) {
        timer = window.setTimeout(poll, 2000);
      }
    }
  }

  retry?.addEventListener("click", () => {
    if (!context.active) return;
    paused = false;
    show(root, "[data-progress-warning]", false);
    poll();
  });
  root.querySelector("[data-cancel-form]")?.addEventListener("submit", () => {
    leaving = true;
    stop();
  });
  root.addEventListener("cache:paused", () => {
    stop();
    show(root, "[data-poll-retry]", false);
    show(root, "[data-session-link]");
    const cancel = root.querySelector("[data-cancel-form] button");
    if (cancel) cancel.disabled = true;
  });
  document.addEventListener("visibilitychange", () => {
    window.clearTimeout(timer);
    if (!document.hidden && !paused) poll();
  });
  window.addEventListener("pagehide", () => { leaving = true; stop(); });
  if (activeStates.includes(state)) timer = window.setTimeout(poll, 2000);
}
