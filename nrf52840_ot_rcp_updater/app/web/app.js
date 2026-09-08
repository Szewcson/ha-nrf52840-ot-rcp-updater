"use strict";

const state = {
  source: "release",
  artifactId: null,
  releases: [],
};

const elements = {
  headline: document.querySelector("#headline-status"),
  badge: document.querySelector("#operation-badge"),
  device: document.querySelector("#device-details"),
  firmware: document.querySelector("#firmware-details"),
  policyWarning: document.querySelector("#policy-warning"),
  otbr: document.querySelector("#otbr-details"),
  release: document.querySelector("#release-select"),
  url: document.querySelector("#url-input"),
  file: document.querySelector("#file-input"),
  validate: document.querySelector("#validate-button"),
  flash: document.querySelector("#flash-button"),
  message: document.querySelector("#action-message"),
  artifact: document.querySelector("#artifact-details"),
  preflight: document.querySelector("#preflight-checks"),
  operationStage: document.querySelector("#operation-stage"),
  progress: document.querySelector("#progress-bar"),
  operationError: document.querySelector("#operation-error"),
  operationLog: document.querySelector("#operation-log"),
};

function endpoint(path) {
  return new URL(path, document.baseURI).toString();
}

async function request(path, options = {}) {
  const headers = new Headers(options.headers || {});
  headers.set("X-Requested-With", "XMLHttpRequest");
  const response = await fetch(endpoint(path), { ...options, headers, credentials: "same-origin" });
  const body = await response.json().catch(() => ({ error: "Server returned an invalid response" }));
  if (!response.ok) {
    throw new Error(body.error || `Request failed with HTTP ${response.status}`);
  }
  return body;
}

function jsonRequest(path, body) {
  return request(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

function clear(element) {
  while (element.firstChild) {
    element.removeChild(element.firstChild);
  }
}

function details(element, rows) {
  clear(element);
  for (const [label, value] of rows) {
    const term = document.createElement("dt");
    term.textContent = label;
    const definition = document.createElement("dd");
    definition.textContent = value || "Unknown";
    element.append(term, definition);
  }
}

function setMessage(message = "", error = false) {
  elements.message.textContent = message;
  elements.message.classList.toggle("error", error);
}

function renderStatus(status) {
  const device = status.device || {};
  const firmware = status.firmware || {};
  const installed = firmware.installed || {};
  const latest = firmware.latest || {};
  const otbr = status.otbr || {};
  details(elements.device, [
    ["Hardware", device.hardware],
    ["Port", device.port],
    ["USB VID:PID", device.usb_vid_pid],
    ["USB serial", device.usb_serial],
    ["State", device.state],
  ]);
  details(elements.firmware, [
    ["Installed NCS", installed.ncs_version],
    ["Installed Zephyr", installed.zephyr_version],
    ["Configured NCS", latest.ncs_version],
    ["Configured Zephyr", latest.zephyr_version],
    ["Target direction", firmware.policy_target_direction],
  ]);
  details(elements.otbr, [
    ["State", otbr.state],
    ["Status", otbr.error || "Available"],
  ]);
  const targetLabel = latest.ncs_version ? `Configured NCS ${latest.ncs_version}` : "No release manifest";
  elements.headline.textContent = `${device.state || "Unknown"} RCP. ${targetLabel}.`;
  const isDowngrade = firmware.policy_target_direction === "downgrade";
  elements.policyWarning.hidden = !isDowngrade;
  elements.policyWarning.textContent = isDowngrade
    ? "The current prerelease or minor-line policy selects an older firmware. Flash it explicitly from this panel; the Secure DFU bootloader may still reject a rollback."
    : "";
  renderOperation(status.operation || {});
}

function renderOperation(operation) {
  const operationState = operation.state || "idle";
  const busy = Boolean(operation.busy);
  elements.badge.dataset.state = operationState;
  elements.badge.textContent = operationState;
  elements.operationStage.textContent = operation.stage || "Idle";
  const hasExactProgress = Number.isInteger(operation.progress);
  const progress = hasExactProgress ? operation.progress : 0;
  elements.progress.classList.toggle("indeterminate", busy && !hasExactProgress);
  elements.progress.style.width = hasExactProgress
    ? `${Math.max(0, Math.min(100, progress))}%`
    : busy
      ? "100%"
      : "0%";
  elements.operationError.hidden = !operation.error;
  elements.operationError.textContent = operation.error || "";
  elements.validate.disabled = busy;
  elements.flash.disabled = busy || !state.artifactId;
  clear(elements.operationLog);
  const events = Array.isArray(operation.events) ? operation.events : [];
  if (events.length === 0) {
    const item = document.createElement("li");
    item.textContent = "No operation has run in this app session.";
    elements.operationLog.append(item);
    return;
  }
  for (const event of events) {
    const item = document.createElement("li");
    const timestamp = document.createElement("time");
    timestamp.textContent = formatTime(event.time);
    const message = document.createElement("span");
    message.textContent = event.message || "";
    item.append(timestamp, message);
    elements.operationLog.append(item);
  }
}

function formatTime(value) {
  const date = new Date(value);
  return Number.isNaN(date.valueOf()) ? "" : date.toLocaleTimeString();
}

function renderReleases(payload) {
  state.releases = Array.isArray(payload.releases) ? payload.releases : [];
  clear(elements.release);
  for (const release of state.releases) {
    const option = document.createElement("option");
    option.value = release.ncs_version;
    option.textContent = `NCS ${release.ncs_version} / Zephyr ${release.zephyr_version}`;
    if (payload.automatic_release && release.ncs_version === payload.automatic_release.ncs_version) {
      option.selected = true;
    }
    elements.release.append(option);
  }
  if (state.releases.length === 0) {
    const option = document.createElement("option");
    option.textContent = payload.manifest_error || "No release is currently available";
    option.disabled = true;
    option.selected = true;
    elements.release.append(option);
    elements.validate.disabled = true;
  }
}

function renderValidation(result) {
  state.artifactId = result.artifact_id || null;
  const artifact = result.artifact || {};
  const rows = [
    ["Source", artifact.source],
    ["Size", artifact.size ? `${artifact.size} bytes` : null],
    ["SHA-256", artifact.sha256],
    ["Hardware", artifact.hardware],
    ["NCS", artifact.ncs_version],
    ["Zephyr", artifact.zephyr_version],
    ["Authentication", artifact.authenticated ? "Signed release" : "Embedded tags and structural checks only"],
  ];
  if (artifact.url) {
    rows.push(["URL", artifact.url]);
  }
  details(elements.artifact, rows);
  elements.artifact.classList.remove("empty");
  clear(elements.preflight);
  const checks = Array.isArray(result.preflight?.checks) ? result.preflight.checks : [];
  for (const check of checks) {
    const item = document.createElement("li");
    item.dataset.state = check.state || "pending";
    item.textContent = `${check.name}: ${check.message}`;
    elements.preflight.append(item);
  }
  if (checks.length === 0) {
    const item = document.createElement("li");
    item.textContent = "Preflight did not return any checks.";
    elements.preflight.append(item);
  }
  elements.flash.disabled = !state.artifactId;
  if (state.artifactId) {
    setMessage("Validation passed. Flashing will stop OTBR temporarily and verify Spinel afterward.");
  } else {
    setMessage("Validation completed, but flashing is blocked until all required checks pass.", true);
  }
}

function selectSource(source) {
  state.source = source;
  state.artifactId = null;
  elements.flash.disabled = true;
  setMessage();
  document.querySelectorAll(".source-tab").forEach((tab) => {
    tab.classList.toggle("active", tab.dataset.source === source);
  });
  document.querySelectorAll(".source-panel").forEach((panel) => {
    const active = panel.id === `source-${source}`;
    panel.classList.toggle("active", active);
    panel.hidden = !active;
  });
}

async function validate() {
  state.artifactId = null;
  elements.flash.disabled = true;
  setMessage("Validating firmware...");
  try {
    let result;
    if (state.source === "release") {
      if (!elements.release.value) {
        throw new Error("Choose a known release first.");
      }
      result = await jsonRequest("api/validate/release", { ncs_version: elements.release.value });
    } else if (state.source === "url") {
      result = await jsonRequest("api/validate/url", { url: elements.url.value.trim() });
    } else {
      const file = elements.file.files[0];
      if (!file) {
        throw new Error("Choose a local ELF file first.");
      }
      result = await request("api/validate/upload", {
        method: "POST",
        headers: { "Content-Type": "application/octet-stream" },
        body: file,
      });
    }
    renderValidation(result);
  } catch (error) {
    setMessage(error.message || "Firmware validation failed.", true);
  }
}

async function flash() {
  if (!state.artifactId) {
    return;
  }
  if (!window.confirm("Flash the validated firmware? OTBR will be stopped temporarily.")) {
    return;
  }
  elements.flash.disabled = true;
  try {
    const result = await jsonRequest("api/flash", { artifact_id: state.artifactId });
    state.artifactId = null;
    setMessage(`Firmware operation ${result.state}. Progress is shown below.`);
    await refreshStatus();
  } catch (error) {
    setMessage(error.message || "Firmware flash could not be queued.", true);
  }
}

async function refreshStatus() {
  try {
    renderStatus(await request("api/status", { method: "GET" }));
  } catch (error) {
    elements.headline.textContent = error.message || "Unable to read app status.";
  }
}

async function initialize() {
  document.querySelectorAll(".source-tab").forEach((tab) => {
    tab.addEventListener("click", () => selectSource(tab.dataset.source));
  });
  elements.validate.addEventListener("click", validate);
  elements.flash.addEventListener("click", flash);
  await Promise.all([
    refreshStatus(),
    request("api/releases", { method: "GET" }).then(renderReleases).catch((error) => setMessage(error.message, true)),
  ]);
  window.setInterval(refreshStatus, 1500);
}

initialize();
