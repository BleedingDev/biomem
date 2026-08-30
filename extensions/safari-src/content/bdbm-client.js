class biomemError extends Error {
  constructor(code, message, response = null) {
    super(message);
    this.name = "biomemError";
    this.code = code;
    this.response = response;
  }
}

class biomemClient {
  constructor(options = {}) {
    this.commandTimeout = options.commandTimeout || 30000;
    this.connected = false;
    this.onDisconnect = null;
    this.onConnect = null;
  }

  async connect() {
    try {
      const response = await this._sendLocal("health", Math.min(this.commandTimeout, 3000));
      const health = response && response.data;
      if (!response || !response.ok || !health ||
          health.status !== "success" || health.product !== "biomem" ||
          health.protocol_version !== 1 || health.ready !== true ||
          health.transport !== "http" || typeof health.version !== "string" ||
          !health.version.trim()) {
        throw this._errorFromResponse(response, "INVALID_HEALTH_RESPONSE", "Invalid biomem health response");
      }
      this.connected = true;
      if (typeof this.onConnect === "function") this.onConnect();
    } catch (err) {
      this.connected = false;
      throw err instanceof biomemError
        ? err
        : new biomemError("SERVICE_UNAVAILABLE", err && err.message ? err.message : "biomem unavailable");
    }
  }

  disconnect() {
    this.connected = false;
  }

  async sendCommand(command, timeout = null) {
    if (!this.connected) {
      try {
        await this.connect();
      } catch (err) {
        throw new biomemError("DISCONNECTED", "biomem not connected");
      }
    }

    const effectiveTimeout = timeout || this.commandTimeout;
    const response = await this._sendLocal({ ...command }, effectiveTimeout);
    if (!response || !response.ok) {
      if (response && (response.code === "SERVICE_UNAVAILABLE" ||
          (response.data && response.data.code === "SERVICE_UNAVAILABLE"))) {
        this._notifyDisconnect(response);
      }
      throw this._errorFromResponse(response, "SERVICE_UNAVAILABLE", "biomem command failed");
    }
    const data = response.data;
    if (!data || typeof data !== "object") {
      throw new biomemError("INVALID_COMMAND_RESPONSE", "Invalid biomem command response", response);
    }
    return data;
  }

  async retrieve(query, sessionId, topK = 5) {
    return this.sendCommand({
      command: "retrieve",
      query,
      session_id: sessionId,
      top_k: topK
    });
  }

  async store(userSummary, modelSummary, sessionId, responseText) {
    const origin = window.location && typeof window.location.hostname === "string"
      ? window.location.hostname
      : "unknown";
    return this.sendCommand({
      command: "store",
      user_summary: userSummary,
      model_summary: modelSummary,
      response_text: responseText,
      session_id: sessionId,
      provenance: {
        source_class: "browser",
        origin,
        session_id: sessionId
      }
    });
  }

  async status() {
    return this.sendCommand({ command: "status" });
  }

  _sendLocal(command, timeout) {
    return new Promise((resolve, reject) => {
      chrome.runtime.sendMessage({ type: "localCommand", command, timeout }, (response) => {
        const runtimeError = chrome.runtime.lastError;
        if (runtimeError) {
          const message = runtimeError.message || "Extension background unavailable";
          const failure = {
            ok: false,
            status: 0,
            error: message,
            code: "SERVICE_UNAVAILABLE",
            data: { code: "SERVICE_UNAVAILABLE", error: message }
          };
          this._notifyDisconnect(failure);
          reject(new biomemError("SERVICE_UNAVAILABLE", message, failure));
          return;
        }
        resolve(response || null);
      });
    });
  }

  _errorFromResponse(response, fallbackCode, fallbackMessage) {
    const data = response && response.data && typeof response.data === "object"
      ? response.data
      : null;
    const code = (response && response.code) || (data && data.code) || fallbackCode;
    const message = (response && response.error) || (data && data.error) || fallbackMessage;
    return new biomemError(code, message, response || null);
  }

  _notifyDisconnect(response) {
    if (!this.connected) return;
    this.connected = false;
    if (typeof this.onDisconnect === "function") {
      this.onDisconnect(response && response.status ? response.status : 0,
        response && response.error ? response.error : "Local transport unavailable");
    }
  }

}

window.biomemClient = biomemClient;
window.biomemError = biomemError;
