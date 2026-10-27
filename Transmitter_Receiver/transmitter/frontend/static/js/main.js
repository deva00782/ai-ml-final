(function () {
  function tick() {
    const now = new Date();
    const utc = now.toISOString().slice(11, 19);
    const el = document.getElementById("utcClock");
    if (el) el.textContent = utc + " UTC";
  }

  tick();
  setInterval(tick, 1000);

  // -------------------------------------------------------------------
  // FSM command panel (hierarchical command interpretation)
  // -------------------------------------------------------------------
  const stateEl = document.getElementById("fsmState");
  const logEl = document.getElementById("fsmLog");
  const latestCmdEl = document.getElementById("latestCommand");
  const latestConfEl = document.getElementById("latestConfidence");
  const latestLevelEl = document.getElementById("latestLevel");
  const latestStateEl = document.getElementById("latestState");
  const latestActionEl = document.getElementById("latestAction");
  const latestLatencyEl = document.getElementById("latestLatency");
  const currentStateEl = document.getElementById("currentState");
  const currentStateLabelEl = document.getElementById("currentStateLabel");
  const historyEl = document.getElementById("commandHistory");
  let connected = false;
  let pending = []; // commands clicked before the socket connected
  let historyCount = 0;
  const MAX_HISTORY = 30;

  function setState(state) {
    if (stateEl && state) stateEl.textContent = state;
    if (currentStateEl && state) currentStateEl.textContent = state;
  }

  function setStateLabel(label) {
    if (currentStateLabelEl && label) currentStateLabelEl.textContent = label;
  }

  function log(line) {
    if (!logEl) return;
    const div = document.createElement("div");
    div.textContent = line;
    logEl.insertBefore(div, logEl.firstChild);
    while (logEl.childNodes.length > 40) {
      if (logEl.lastChild) {
        logEl.lastChild.remove();
      }
    }
  }

  function updateLatestBCI(res) {
    if (!res) return;
    // Use enriched fields from backend: raw_command/command_display, confidence, level, timestamp, latency, action_label
    const cmd = res.raw_command || res.command_display || res.command || "--";
    const conf = typeof res.confidence === "number" ? Math.round(res.confidence * 100) + "%" : "--";
    const level = typeof res.level === "number" ? res.level : (res.level || "--");
    const state = res.new_state || res.state || "--";
    const action = res.action_label || res.message || res.action || "--";
    const latency = res.latency || (res.latency_ms ? res.latency_ms + " ms" : "--");
    const timestamp = res.timestamp || new Date().toLocaleTimeString();

    if (latestCmdEl) latestCmdEl.textContent = cmd;
    if (latestConfEl) latestConfEl.textContent = "Confidence: " + conf;
    if (latestLevelEl) latestLevelEl.textContent = "Level: " + level;
    if (latestStateEl) latestStateEl.textContent = "State: " + state;
    if (latestActionEl) latestActionEl.textContent = "Action: " + action;
    if (latestLatencyEl) latestLatencyEl.textContent = "Latency: " + latency;

    // Add to history (only for accepted commands – backend dedup ensures repeated PUSH only once)
    addToHistory(timestamp, cmd, conf, level, action);
  }

  function addToHistory(timestamp, cmd, conf, level, action) {
    if (!historyEl) return;
    // Remove empty placeholder
    const empty = historyEl.querySelector(".history-empty");
    if (empty) empty.remove();

    const item = document.createElement("div");
    item.className = "history-item";
    const timeDiv = document.createElement("div");
    timeDiv.className = "history-time";
    timeDiv.textContent = timestamp;
    const cmdDiv = document.createElement("div");
    cmdDiv.className = "history-cmd";
    cmdDiv.textContent = cmd;
    const metaDiv = document.createElement("div");
    metaDiv.className = "history-meta";
    metaDiv.textContent = conf + " | Level " + level;
    const actionDiv = document.createElement("div");
    actionDiv.className = "history-action";
    actionDiv.textContent = "→ " + action;

    item.appendChild(timeDiv);
    item.appendChild(cmdDiv);
    item.appendChild(metaDiv);
    item.appendChild(actionDiv);

    historyEl.insertBefore(item, historyEl.firstChild);
    historyCount++;
    while (historyEl.childNodes.length > MAX_HISTORY) {
      if (historyEl.lastChild) {
        historyEl.lastChild.remove();
      }
      historyCount--;
    }
  }

  // The Werkzeug dev server cannot upgrade to WebSocket, so python-engineio
  // intentionally answers the upgrade with a 500 and the browser falls back to
  // long-polling. Using polling-only avoids that 500 and its silent command
  // loss window during page load.
  const socket = io("/master", {
    transports: ["polling"],
    reconnection: true,
    reconnectionDelay: 500,
    reconnectionDelayMax: 2000,
  });

  function setConnected(state) {
    connected = state;
    const panel = document.querySelector(".fsm-panel");
    if (panel) {
      if (state) {
        panel.classList.remove("fsm-offline");
      } else {
        panel.classList.add("fsm-offline");
      }
    }
    const btn = document.querySelectorAll(".fsm-btn[data-cmd]");
    btn.forEach(function (b) {
      if (state) {
        b.classList.remove("fsm-disabled");
      } else {
        b.classList.add("fsm-disabled");
      }
    });
  }

  socket.on("connect", function () {
    setConnected(true);
    log("[CONNECTED] FSM panel linked to /master namespace");
    // Flush anything clicked while the socket was still connecting.
    pending.forEach(function (cmd) {
      socket.emit("manual_command", { raw_command: cmd, confidence: 0.95 });
      log("[QUEUED] flushed command: " + cmd);
    });
    pending = [];
  });

  socket.on("disconnect", function () {
    setConnected(false);
    log("[DISCONNECTED] reconnecting...");
  });

  socket.on("fsm_state_update", function (data) {
    if (data) {
      setState(data.current_state);
      setStateLabel(data.state_label || data.current_state);
      log("[STATE] " + (data.state_label || data.current_state) + (data.target_device ? " · target=" + data.target_device : ""));
      // Update Current FSM State card
      if (currentStateEl) currentStateEl.textContent = data.current_state;
      if (currentStateLabelEl) currentStateLabelEl.textContent = data.state_label || data.current_state;
    }
  });

  socket.on("fsm_command_result", function (res) {
    if (res) {
      log("[COMMAND] " + (res.raw_command || res.command) + " -> " + (res.action_label || res.message));
      // Update Latest BCI Command and History (only for accepted commands, dedup already handled backend)
      updateLatestBCI(res);
      // An "open" action means the dashboard was activated: navigate to it.
      if (res.action === "open_mobile_dashboard") {
        globalThis.location.href = "/mobile";
      } else if (res.action === "open_desktop_dashboard") {
        globalThis.location.href = "/desktop";
      }
    }
  });

  const buttons = document.querySelectorAll(".fsm-btn[data-cmd]");
  buttons.forEach(function (btn) {
    btn.addEventListener("click", function () {
      const cmd = btn.dataset.cmd;
      if (connected) {
        socket.emit("manual_command", { raw_command: cmd, confidence: 0.95 });
      } else {
        pending.push(cmd);
        log("[QUEUED] socket not ready, holding: " + cmd);
      }
    });
  });

  const startBtn = document.getElementById("fsmStart");
  if (startBtn) {
    startBtn.addEventListener("click", function () {
      fetch("/api/fsm/start_automation", { method: "POST" })
        .then(function (r) { return r.json(); })
        .then(function (data) {
          if (data.status === "success") {
            log("[ACTION] Start Automation -> " + data.device + " dashboard opened");
            setState(data.fsm.current_state);
            globalThis.location.href = data.url;
          } else {
            log("[ERROR] " + data.message);
          }
        });
    });
  }

  const resetBtn = document.getElementById("fsmReset");
  if (resetBtn) {
    resetBtn.addEventListener("click", function () {
      fetch("/api/fsm/reset", { method: "POST" })
        .then(function (r) { return r.json(); })
        .then(function (data) {
          setState(data.fsm.current_state);
          log("[RESET] FSM -> " + data.fsm.current_state);
        });
    });
  }

  // Load initial FSM state on page load.
  fetch("/api/fsm/state")
    .then(function (r) { return r.json(); })
    .then(function (data) {
      setState(data.fsm.current_state);
      setStateLabel(data.fsm.state_label || data.fsm.current_state);
      if (currentStateEl) currentStateEl.textContent = data.fsm.current_state;
      if (currentStateLabelEl) currentStateLabelEl.textContent = data.fsm.state_label || data.fsm.current_state;
    });
})();