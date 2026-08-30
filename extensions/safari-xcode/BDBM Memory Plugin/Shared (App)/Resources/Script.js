function show(platform) {
    document.body.classList.add(`platform-${platform}`);
}

function setStatus(elementId, state, labels) {
    const element = document.getElementById(elementId);
    element.className = `status status-${state}`;
    element.textContent = labels[state] || labels.unavailable;
}

function setExtensionState(state) {
    setStatus("extension-status", state, {
        enabled: "Enabled",
        disabled: "Not enabled",
        unavailable: "Unavailable"
    });

    const guidance = document.getElementById("guidance");
    if (state === "enabled") {
        guidance.textContent = "The extension is enabled. Keep the local Biomem service running while you browse.";
    } else if (state === "disabled") {
        guidance.textContent = "Enable the extension in Safari Settings to use Biomem on supported sites.";
    } else {
        guidance.textContent = "Safari could not read the extension state. Open Safari Settings to finish setup.";
    }
}

function setLocalServiceState(state) {
    setStatus("service-status", state, {
        checking: "Checking…",
        running: "Running",
        offline: "Not running",
        unavailable: "Unavailable"
    });
}

function postCommand(command) {
    window.webkit.messageHandlers.controller.postMessage(command);
}

document.querySelector("button.open-preferences").addEventListener("click", () => postCommand("open-preferences"));
document.querySelector("button.refresh-status").addEventListener("click", () => postCommand("refresh-status"));
