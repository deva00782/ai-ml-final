(function () {
  function tick() {
    const el = document.getElementById("utcClock");
    if (el) el.textContent = new Date().toISOString().slice(11, 19) + " UTC";
  }
  tick();
  setInterval(tick, 1000);

  const stateEl = document.getElementById("fsmState");
  const targetBadge = document.getElementById("targetBadge");
  const logEl = document.getElementById("fsmLog");
  const logEl2 = document.getElementById("fsmLog2");
  const latestCmdEl = document.getElementById("latestCommand");
  const latestConfEl = document.getElementById("latestConfidence");
  const latestLevelEl = document.getElementById("latestLevel");
  const latestStateEl = document.getElementById("latestState");
  const latestActionEl = document.getElementById("latestAction");
  const latestLatencyEl = document.getElementById("latestLatency");
  const latestTargetEl = document.getElementById("latestTarget");
  const currentStateEl = document.getElementById("currentState");
  const currentStateLabelEl = document.getElementById("currentStateLabel");
  const currentTargetEl = document.getElementById("currentTarget");
  const currentLevelEl = document.getElementById("currentLevel");
  const currentActionEl = document.getElementById("currentAction");
  const historyEl = document.getElementById("commandHistory");
  const MAX_HISTORY = 30;

  function updateLevelIndicators(level) {
    ["lvl1", "lvl2", "lvl3"].forEach(function (id, i) {
      const el = document.getElementById(id);
      if (!el) return;
      const targetLevel = i + 1;
      el.classList.toggle("active", level === targetLevel);
      el.classList.toggle("done", level > targetLevel);
      const icon = el.querySelector(".level-icon");
      if (icon) {
        if (level > targetLevel) {
          icon.textContent = "✓";
        } else if (level === targetLevel) {
          icon.textContent = "●";
        } else {
          icon.textContent = "○";
        }
      }
    });
    const c1 = document.getElementById("conn1");
    const c2 = document.getElementById("conn2");
    if (c1) c1.classList.toggle("active", level >= 2);
    if (c2) c2.classList.toggle("active", level >= 3);
  }

  function getTargetBadgeClass(target) {
    if (target === "mobile") return "target-badge";
    if (target === "desktop") return "target-badge desktop";
    return "target-badge none";
  }

  function setState(s, label, target, level) {
    if (stateEl && s) stateEl.textContent = s;
    if (currentStateEl && s) currentStateEl.textContent = s;
    if (currentStateLabelEl && label) currentStateLabelEl.textContent = label;
    if (currentTargetEl) currentTargetEl.textContent = "Target: " + (target || "none");
    if (latestTargetEl) latestTargetEl.textContent = "Target Device: " + (target || "none");
    if (currentLevelEl && typeof level === "number") currentLevelEl.textContent = "Level: " + level;
    if (targetBadge) {
      targetBadge.textContent = "TARGET: " + (target ? target.toUpperCase() : "NONE");
      targetBadge.className = getTargetBadgeClass(target);
    }
    updateLevelIndicators(level);
  }

  function log(line) {
    const txt = "[" + new Date().toLocaleTimeString() + "] " + line;
    [logEl, logEl2].forEach(function (el) {
      if (!el) return;
      const d = document.createElement("div");
      d.textContent = txt;
      el.insertBefore(d, el.firstChild);
      while (el.childNodes.length > 40 && el.lastChild) {
        el.lastChild.remove();
      }
    });
  }

  function updateLatest(res) {
    if (!res) return;
    const cmd = res.raw_command || res.command_display || res.command || "--";
    const conf = typeof res.confidence === "number" ? Math.round(res.confidence * 100) + "%" : "--";
    const level = typeof res.level === "number" ? res.level : (res.level || "--");
    const state = res.new_state || res.state || "--";
    const action = res.action_label || res.message || res.action || "--";
    const latency = res.latency || (res.latency_ms ? res.latency_ms + " ms" : "--");
    const target = res.target_device || "--";
    const ts = res.timestamp || new Date().toLocaleTimeString();

    if (latestCmdEl) latestCmdEl.textContent = cmd;
    if (latestConfEl) latestConfEl.textContent = (conf === "--" ? "--" : conf + " CONFIDENCE");
    if (latestLevelEl) latestLevelEl.textContent = "Level: " + level;
    if (latestStateEl) latestStateEl.textContent = "FSM State: " + state;
    if (latestActionEl) latestActionEl.textContent = action;
    if (latestLatencyEl) latestLatencyEl.textContent = "Latency: " + latency;
    if (latestTargetEl) latestTargetEl.textContent = "Target: " + target;
    if (currentActionEl) currentActionEl.textContent = "Last Action: " + action;
    addHistory(ts, cmd, conf, level, action);
  }

  function addHistory(ts, cmd, conf, level, action) {
    if (!historyEl) return;
    const empty = historyEl.querySelector(".history-empty");
    if (empty) empty.remove();

    const item = document.createElement("div");
    item.className = "history-item";
    const td = document.createElement("div");
    td.className = "history-time";
    td.textContent = ts;
    const cd = document.createElement("div");
    cd.className = "history-cmd";
    cd.textContent = cmd;
    const md = document.createElement("div");
    md.className = "history-meta";
    md.textContent = conf + " | Level " + level;
    const ad = document.createElement("div");
    ad.className = "history-action";
    ad.textContent = "→ " + action;

    item.appendChild(td);
    item.appendChild(cd);
    item.appendChild(md);
    item.appendChild(ad);
    historyEl.insertBefore(item, historyEl.firstChild);
    while (historyEl.childNodes.length > MAX_HISTORY && historyEl.lastChild) {
      historyEl.lastChild.remove();
    }
  }

  function getDeviceStatus(j) {
    const targetText = document.getElementById("currentTarget")?.textContent || "";
    let dev = null;
    if (targetText.includes("desktop")) {
      dev = "desktop";
    } else if (targetText.includes("mobile")) {
      dev = "mobile";
    }
    const entry = dev && j?.[dev] ? j[dev] : null;
    const status = entry?.status || j?.overall || "idle";
    const msg = entry?.message || status;
    return { status: status, msg: msg };
  }

  function formatJiosaavnMessage(status, msg) {
    if (status === "opening") return "Opening...";
    if (status === "ready") return "Ready";
    if (status === "failed") return "Launch Failed - " + msg;
    if (status === "idle") return "Idle";
    return msg;
  }

  function updateJiosaavnStatus(data) {
    const j = data?.jiosaavn || data;
    if (!j) return;
    const dot = document.getElementById("jiosaavnDot");
    const txt = document.getElementById("jiosaavnText");
    if (!dot || !txt) return;

    const devStatus = getDeviceStatus(j);
    const colors = { idle: "#475569", opening: "#f59e0b", ready: "#22c55e", failed: "#ef4444" };
    dot.style.background = colors[devStatus.status] || colors.idle;
    txt.textContent = formatJiosaavnMessage(devStatus.status, devStatus.msg);

    if (data?.device) {
      log("JIOSAAVN " + data.device + " -> " + data.status + " " + (data.message || ""));
    }
  }

  const socket = io("/dashboard", { transports: ["polling"] });
  socket.on("connect", function () {
    log("Connected /dashboard — server push only");
  });
  socket.on("disconnect", function () {
    log("Disconnected, reconnecting...");
  });

  socket.on("fsm_state_update", function (data) {
    if (data) {
      setState(data.current_state, data.state_label, data.target_device, data.level);
      log("STATE " + (data.state_label || data.current_state) + " target=" + (data.target_device || "none"));
    }
  });

  socket.on("fsm_command_result", function (res) {
    if (res) {
      log("COMMAND " + (res.raw_command || res.command) + " → " + (res.action_label || res.message));
      updateLatest(res);
    }
  });

  socket.on("jiosaavn_status", function (data) {
    updateJiosaavnStatus(data);
  });

  // Single HTTP path only — no socket emit for BCI (avoids duplicate processing)
  function sendBCI(command, confidence) {
    return fetch("/api/bci/command", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ command: command, confidence: confidence || 0.95 })
    })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        if (data.status === "success") {
          log("POST /api/bci/command " + command + " accepted=" + data.accepted);
          if (data.fsm) {
            setState(data.fsm.current_state, data.fsm.state_label, data.fsm.target_device, data.fsm.level);
          }
        } else {
          log("ERROR " + data.message);
        }
        return data;
      });
  }

  // Raw single commands: send exactly one primitive through Command Window → FSM
  // Sprint 12: RIGHT Search button toggles the search panel instead of sending BCI
  document.querySelectorAll("[data-raw]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      const cmd = btn.dataset.raw;
      const text = btn.textContent || "";
      if (cmd === "RIGHT" && text.indexOf("Search") !== -1) {
        const panel = document.getElementById("searchCommandPanel");
        if (panel) {
          const isHidden = panel.style.display === "none" || !panel.style.display;
          panel.style.display = isHidden ? "block" : "none";
          if (isHidden) {
            const input = document.getElementById("searchInput");
            if (input) {
              setTimeout(function () { input.focus(); }, 50);
            }
          }
        }
        return;
      }
      log("RAW " + cmd + " → POST /api/bci/command");
      sendBCI(cmd, 0.95);
    });
  });

  function handleRawDouble(btn) {
    const raw = btn.dataset.rawDouble;
    const parts = raw.split("+");
    const c1 = parts[0].trim();
    const c2 = parts[1].trim();
    log("RAW DOUBLE " + c1 + " + " + c2 + " → POST " + c1 + " then POST " + c2);
    sendBCI(c1, 0.95).then(function () {
      setTimeout(function () {
        sendBCI(c2, 0.95);
      }, 160);
    });
  }

  // Ordered doubles: send two separate POSTs sequentially, preserving order
  document.querySelectorAll("[data-raw-double]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      handleRawDouble(btn);
    });
  });

  const resetBtn = document.getElementById("fsmReset");
  if (resetBtn) {
    resetBtn.addEventListener("click", function () {
      fetch("/api/fsm/reset", { method: "POST" })
        .then(function (r) { return r.json(); })
        .then(function (d) {
          setState(d.fsm.current_state, d.fsm.state_label, d.fsm.target_device, d.fsm.level);
          if (d.jiosaavn) updateJiosaavnStatus({ jiosaavn: d.jiosaavn });
          log("FSM reset → " + d.fsm.current_state);
          if (historyEl) historyEl.innerHTML = '<div class="history-empty">No commands yet.</div>';
          if (latestCmdEl) latestCmdEl.textContent = "Waiting...";
          if (latestActionEl) latestActionEl.textContent = "Action: --";
        });
    });
  }

  const neutralBtn = document.getElementById("fsmNeutral");
  if (neutralBtn) {
    neutralBtn.addEventListener("click", function () {
      sendBCI("NEUTRAL", 0.9).then(function () {
        log("NEUTRAL sent — should be ignored, no history");
      });
    });
  }

  fetch("/api/fsm/state")
    .then(function (r) { return r.json(); })
    .then(function (d) {
      if (d.fsm) setState(d.fsm.current_state, d.fsm.state_label, d.fsm.target_device, d.fsm.level);
      if (d.jiosaavn) updateJiosaavnStatus({ jiosaavn: d.jiosaavn });
    });

  fetch("/api/jiosaavn/status")
    .then(function (r) { return r.json(); })
    .then(function (d) {
      if (d.jiosaavn) updateJiosaavnStatus({ jiosaavn: d.jiosaavn });
    });

  // --- BCI Replay panel ---
  const replayFileEl = document.getElementById("replayFile");
  const replaySpeedEl = document.getElementById("replaySpeed");
  const replayModeEl = document.getElementById("replayMode");
  const replayBtn = document.getElementById("replayBtn");
  const replayStopBtn = document.getElementById("replayStopBtn");
  const replayText = document.getElementById("replayText");
  const replayDot = document.getElementById("replayDot");
  const replayProgressEl = document.getElementById("replayProgress");
  const replayLogEl = document.getElementById("replayLog");
  const recToggleBtn = document.getElementById("recToggleBtn");
  const recStatusEl = document.getElementById("recStatus");

  function replayLog(msg) {
    if (replayLogEl) {
      const d = document.createElement("div");
      d.textContent = "[" + new Date().toLocaleTimeString() + "] " + msg;
      replayLogEl.insertBefore(d, replayLogEl.firstChild);
      while (replayLogEl.childNodes.length > 30 && replayLogEl.lastChild) {
        replayLogEl.lastChild.remove();
      }
    }
    log(msg);
  }

  function updateReplayStatus(s) {
    if (!s) return;
    const replayObj = s.replay || s;
    const status = replayObj.status || "idle";
    const colors = { idle: "#475569", playing: "#00f6ff", completed: "#22c55e", stopped: "#f59e0b", error: "#ef4444" };
    if (replayDot) replayDot.style.background = colors[status] || colors.idle;
    if (replayText) {
      replayText.textContent = status.charAt(0).toUpperCase() + status.slice(1) + (replayObj.error ? " - " + replayObj.error : "");
    }
    if (replayProgressEl) {
      const p = replayObj.progress || {};
      replayProgressEl.textContent = p.total ? (" " + (p.sent || 0) + "/" + p.total + " sent, " + (p.skipped || 0) + " skipped") : "";
    }
  }

  function updateRecStatus(s) {
    if (!s) return;
    const r = s.recording || s;
    const on = r.is_recording;
    if (recToggleBtn) {
      recToggleBtn.textContent = on ? "● Recording: ON" : "● Recording: OFF";
      recToggleBtn.style.background = on ? "rgba(34,197,94,0.15)" : "";
      recToggleBtn.style.borderColor = on ? "rgba(34,197,94,0.4)" : "";
      recToggleBtn.style.color = on ? "#22c55e" : "";
    }
    if (recStatusEl) {
      recStatusEl.textContent = on ? (r.current_file_name + " (" + r.event_count + " events)") : "Idle";
    }
  }

  function loadReplayList() {
    fetch("/api/bci/replay/list")
      .then(function (r) { return r.json(); })
      .then(function (d) {
        const sessions = d.sessions || [];
        if (replayFileEl) {
          replayFileEl.innerHTML = "";
          if (sessions.length === 0) {
            const o = document.createElement("option");
            o.value = "";
            o.textContent = "-- no sessions --";
            replayFileEl.appendChild(o);
          } else {
            sessions.forEach(function (s) {
              const o = document.createElement("option");
              o.value = s;
              o.textContent = s;
              replayFileEl.appendChild(o);
            });
          }
        }
        if (d.replay) updateReplayStatus(d.replay);
      });

    fetch("/api/bci/recording/status")
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (d.recording) updateRecStatus(d);
      });

    fetch("/api/bci/replay/status")
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (d.replay) updateReplayStatus(d.replay);
      });
  }

  if (replayBtn) {
    replayBtn.addEventListener("click", function () {
      const f = replayFileEl?.value;
      if (!f) {
        replayLog("Select a dataset first");
        return;
      }
      const speed = Number.parseFloat(replaySpeedEl.value) || 1;
      const realtime = replayModeEl.value !== "fast";
      replayLog("Replay start " + f + " speed=" + speed + " realtime=" + realtime);
      fetch("/api/bci/replay/start", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ file: f, speed: speed, realtime: realtime })
      })
        .then(function (r) { return r.json(); })
        .then(function (d) {
          if (d.status === "success") {
            replayLog("Replay started " + f);
            updateReplayStatus(d.replay);
          } else {
            replayLog("Replay error " + (d.message || JSON.stringify(d)));
            updateReplayStatus(d.replay || d);
          }
        });
    });
  }

  if (replayStopBtn) {
    replayStopBtn.addEventListener("click", function () {
      fetch("/api/bci/replay/stop", { method: "POST" })
        .then(function (r) { return r.json(); })
        .then(function (d) {
          replayLog("Replay stop");
          if (d.replay) updateReplayStatus(d.replay);
        });
    });
  }

  if (recToggleBtn) {
    recToggleBtn.addEventListener("click", function () {
      fetch("/api/bci/recording/status")
        .then(function (r) { return r.json(); })
        .then(function (d) {
          const on = d.recording?.is_recording;
          const url = on ? "/api/bci/recording/stop" : "/api/bci/recording/start";
          fetch(url, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: "{}"
          })
            .then(function (r) { return r.json(); })
            .then(function (dd) {
              updateRecStatus(dd.recording);
              replayLog(on ? "Recording stopped " + (dd.file || "") : "Recording started " + (dd.file || ""));
            });
        });
    });
  }

  socket.on("bci_replay_status", function (s) {
    updateReplayStatus(s);
    if (s.status === "completed") {
      replayLog("Replay completed");
    }
    if (s.status === "error") {
      replayLog("Replay error " + (s.error || ""));
    }
  });

  socket.on("bci_recording_status", function (s) {
    updateRecStatus(s);
  });

  // initial load
  loadReplayList();

  // --- Volume + Window controls ---
  const volPctEl = document.getElementById("volumePct");
  const volFill = document.getElementById("volumeBarFill");
  const volSlider = document.getElementById("volumeSlider");
  const volDetail = document.getElementById("volumeDetail");
  const volSourceEl = document.getElementById("volumeSource");
  const volUpBtn = document.getElementById("volUpBtn");
  const volDownBtn = document.getElementById("volDownBtn");
  const winSlider = document.getElementById("windowSlider");
  const winValueEl = document.getElementById("windowValue");
  const winCurrentEl = document.getElementById("windowCurrent");
  const winGenEl = document.getElementById("windowGen");
  const winPlus = document.getElementById("winPlus");
  const winMinus = document.getElementById("winMinus");

  function updateVolume(data) {
    if (!data) return;
    const vCandidate = typeof data.volume === "number" ? data.volume : data.config?.volume;
    if (typeof vCandidate !== "number") return;
    const v = Math.round(vCandidate);
    if (volPctEl) volPctEl.textContent = v + "%";
    if (volFill) volFill.style.width = v + "%";
    if (volSlider) volSlider.value = v;
    if (volDetail) volDetail.textContent = "Updated " + new Date().toLocaleTimeString();
    if (volSourceEl && data.source) volSourceEl.textContent = "source: " + data.source;
  }

  function updateWindow(data) {
    if (!data) return;
    let w = data.command_window_seconds;
    if (w === undefined) w = data.window_duration;
    if (w === undefined && data.config) w = data.config.command_window_seconds;
    if (w === undefined && data.fsm) w = data.fsm.command_window_seconds;
    if (w === undefined) return;
    w = Number.parseFloat(w);
    if (winValueEl) winValueEl.textContent = w.toFixed(1) + "s";
    if (winCurrentEl) winCurrentEl.textContent = w.toFixed(1) + "s";
    if (winSlider) winSlider.value = w;
    if (data.window_generation !== undefined && winGenEl) winGenEl.textContent = data.window_generation;
    if (data.fsm?.window_generation !== undefined && winGenEl) winGenEl.textContent = data.fsm.window_generation;
  }

  socket.on("volume_update", function (d) {
    updateVolume(d);
    log("VOLUME " + d.volume + "%");
  });

  socket.on("config_update", function (d) {
    updateWindow(d);
    updateVolume(d);
    if (d.command_window_seconds) log("CONFIG window -> " + d.command_window_seconds + "s");
  });

  socket.on("fsm_state_update", function (d) {
    updateWindow(d);
  });

  function postWindow(val) {
    fetch("/api/config", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ command_window_seconds: Number.parseFloat(val) })
    })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (d.status === "success") {
          updateWindow(d.config || d);
          log("Window set -> " + (d.config?.command_window_seconds) + "s");
        } else {
          log("Window set error " + d.message);
        }
      });
  }

  function postVolume(val) {
    fetch("/api/volume", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ volume: Number.parseInt(val, 10) })
    })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (d.status === "success") {
          updateVolume(d);
        } else {
          log("Volume set error " + d.message);
        }
      });
  }

  if (winSlider) {
    winSlider.addEventListener("input", function () {
      const v = Number.parseFloat(this.value);
      if (winValueEl) winValueEl.textContent = v.toFixed(1) + "s";
    });
    winSlider.addEventListener("change", function () {
      postWindow(this.value);
    });
  }

  if (winPlus) {
    winPlus.addEventListener("click", function () {
      const cur = Number.parseFloat(winSlider.value) || 4;
      const nxt = Math.min(10, cur + 0.5);
      postWindow(nxt);
    });
  }

  if (winMinus) {
    winMinus.addEventListener("click", function () {
      const cur = Number.parseFloat(winSlider.value) || 4;
      const nxt = Math.max(0.5, cur - 0.5);
      postWindow(nxt);
    });
  }

  if (volSlider) {
    volSlider.addEventListener("change", function () {
      postVolume(this.value);
    });
  }

  if (volUpBtn) {
    volUpBtn.addEventListener("click", function () {
      const cur = Number.parseInt(volSlider.value, 10) || 50;
      postVolume(Math.min(100, cur + 10));
    });
  }

  if (volDownBtn) {
    volDownBtn.addEventListener("click", function () {
      const cur = Number.parseInt(volSlider.value, 10) || 50;
      postVolume(Math.max(0, cur - 10));
    });
  }

  // initial fetch
  fetch("/api/volume")
    .then(function (r) { return r.json(); })
    .then(function (d) {
      if (d.volume !== undefined) updateVolume(d);
    });

  fetch("/api/config")
    .then(function (r) { return r.json(); })
    .then(function (d) {
      if (d.config) {
        updateWindow(d.config);
        updateVolume(d.config);
      }
    });

  fetch("/api/fsm/state")
    .then(function (r) { return r.json(); })
    .then(function (d) {
      if (d.fsm) updateWindow(d.fsm);
    });

  // --- Sprint 12: JioSaavn Dynamic Search Panel (additive, works with Sprint 11 backend) ---
  const searchInput = document.getElementById("searchInput");
  const searchBtn = document.getElementById("searchBtn");
  const searchStatus = document.getElementById("searchStatus");
  const searchCommandPanel = document.getElementById("searchCommandPanel");
  const closeSearchPanelBtn = document.getElementById("closeSearchPanelBtn");

  if (closeSearchPanelBtn) {
    closeSearchPanelBtn.addEventListener("click", function () {
      if (searchCommandPanel) searchCommandPanel.style.display = "none";
    });
  }

  function performSearch() {
    if (!searchInput) return;
    const query = searchInput.value.trim();
    if (!query) {
      if (searchStatus) {
        searchStatus.textContent = "Please enter a song or artist name.";
        searchStatus.style.color = "#ef4444";
      }
      searchInput.focus();
      return;
    }
    // Auto-detect target from FSM state (mobile/desktop only; Sprint 11 has no ios FSM state)
    let target = "desktop";
    const curTgt = (currentTargetEl && currentTargetEl.textContent) || "";
    const curSt = (currentStateEl && currentStateEl.textContent) || "";
    if (curTgt.toLowerCase().indexOf("mobile") !== -1 || curSt.indexOf("MOBILE") !== -1) {
      target = "mobile";
    } else {
      target = "desktop";
    }
    if (searchStatus) {
      searchStatus.textContent = "Searching JioSaavn on " + target.toUpperCase() + " for '" + query + "'...";
      searchStatus.style.color = "#00f6ff";
    }
    if (searchBtn) {
      searchBtn.disabled = true;
      searchBtn.style.opacity = "0.6";
    }
    fetch("/api/mobile/search", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ query: query, target: target })
    })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (d.status === "success") {
          const title = d.title || query;
          if (searchStatus) {
            searchStatus.textContent = "Dispatched: " + title;
            searchStatus.style.color = "#22c55e";
          }
          log("SEARCH: '" + title + "' executed on " + target);
        } else {
          if (searchStatus) {
            searchStatus.textContent = "Error: " + (d.message || "Search failed");
            searchStatus.style.color = "#ef4444";
          }
          log("SEARCH error: " + (d.message || "Unknown error"));
        }
      })
      .catch(function (e) {
        if (searchStatus) {
          searchStatus.textContent = "Error: Failed to send search";
          searchStatus.style.color = "#ef4444";
        }
        log("SEARCH network error: " + e);
      })
      .finally(function () {
        if (searchBtn) {
          searchBtn.disabled = false;
          searchBtn.style.opacity = "1";
        }
      });
  }

  if (searchBtn) searchBtn.addEventListener("click", performSearch);
  if (searchInput) {
    searchInput.addEventListener("keydown", function (e) {
      if (e.key === "Enter") performSearch();
      if (e.key === "Escape") {
        if (searchCommandPanel) searchCommandPanel.style.display = "none";
      }
    });
  }

  // --- Sprint 12: Searchable Mobile Device Registry & Selector (additive) ---
  const devicePickerBtn = document.getElementById("devicePickerBtn");
  const devicePickerDropdown = document.getElementById("devicePickerDropdown");
  const deviceSearchInput = document.getElementById("deviceSearchInput");
  const deviceCountBadge = document.getElementById("deviceCountBadge");
  const deviceListContainer = document.getElementById("deviceListContainer");
  const activeDeviceLabel = document.getElementById("activeDeviceLabel");
  const activeDeviceTypeBadge = document.getElementById("activeDeviceTypeBadge");
  const devicePickerIcon = document.getElementById("devicePickerIcon");

  let cachedDevices = {};
  let currentActiveDevice = "rohan";

  function renderDeviceList(filterText) {
    if (!deviceListContainer) return;
    filterText = (filterText || "").trim().toLowerCase();
    const allUsernames = Object.keys(cachedDevices);
    const filtered = allUsernames.filter(function (usr) {
      if (!filterText) return true;
      const dev = cachedDevices[usr];
      const devType = (dev && dev.device_type ? dev.device_type : "").toLowerCase();
      return usr.toLowerCase().indexOf(filterText) !== -1 || devType.indexOf(filterText) !== -1;
    });
    if (deviceCountBadge) {
      deviceCountBadge.textContent = allUsernames.length;
    }
    deviceListContainer.innerHTML = "";
    if (filtered.length === 0) {
      const emptyEl = document.createElement("div");
      emptyEl.className = "device-item-empty";
      emptyEl.textContent = filterText ? "No matching devices" : "No devices registered";
      deviceListContainer.appendChild(emptyEl);
      return;
    }
    filtered.forEach(function (usr) {
      const dev = cachedDevices[usr] || {};
      const isIos = (dev.device_type || "").toLowerCase() === "ios";
      const isCurrentActive = usr === currentActiveDevice;
      const item = document.createElement("div");
      item.className = "device-item" + (isCurrentActive ? " active" : "");
      item.setAttribute("role", "option");
      item.setAttribute("aria-selected", isCurrentActive ? "true" : "false");
      const left = document.createElement("div");
      left.className = "device-item-left";
      const icon = document.createElement("span");
      icon.textContent = isIos ? "\u{1F4F1}" : "\u{1F4F1}";
      const name = document.createElement("span");
      name.className = "device-item-name";
      name.textContent = usr;
      left.appendChild(icon);
      left.appendChild(name);
      if (isCurrentActive) {
        const dot = document.createElement("span");
        dot.className = "device-active-dot";
        dot.textContent = "\u25CF";
        dot.title = "Currently Controlled Device";
        left.appendChild(dot);
      }
      const right = document.createElement("div");
      right.className = "device-item-right";

      const badge = document.createElement("span");
      badge.className = "device-type-badge " + (isIos ? "ios" : "android");
      badge.textContent = isIos ? "IOS" : "ANDROID";
      right.appendChild(badge);

      const delBtn = document.createElement("button");
      delBtn.className = "device-item-del-btn";
      delBtn.type = "button";
      delBtn.title = "Delete " + usr;
      delBtn.setAttribute("aria-label", "Delete device " + usr);
      delBtn.innerHTML = "🗑️";
      delBtn.addEventListener("click", function (e) {
        e.stopPropagation();
        deleteSingleDevice(usr);
      });
      right.appendChild(delBtn);

      item.appendChild(left);
      item.appendChild(right);
      item.addEventListener("click", function () {
        selectDevice(usr);
      });
      deviceListContainer.appendChild(item);
    });
  }

  function updateActiveDeviceDisplay(username, deviceType) {
    currentActiveDevice = username || "";
    const isNone = !username || username === "none";
    const isIos = (deviceType || "").toLowerCase() === "ios";
    if (activeDeviceLabel) activeDeviceLabel.textContent = isNone ? "NONE" : username;
    if (activeDeviceTypeBadge) {
      if (isNone) {
        activeDeviceTypeBadge.textContent = "OFFLINE";
        activeDeviceTypeBadge.className = "device-type-badge";
        activeDeviceTypeBadge.style.background = "rgba(100, 116, 139, 0.3)";
        activeDeviceTypeBadge.style.color = "#94a3b8";
      } else {
        activeDeviceTypeBadge.textContent = isIos ? "IOS" : "ANDROID";
        activeDeviceTypeBadge.className = "device-type-badge " + (isIos ? "ios" : "android");
        activeDeviceTypeBadge.style.background = "";
        activeDeviceTypeBadge.style.color = "";
      }
    }
    if (devicePickerIcon) {
      devicePickerIcon.textContent = "\u{1F4F1}";
    }
  }

  function selectDevice(username) {
    if (!username) return;
    fetch("/api/mobile/select_device", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username: username })
    })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        const dev = cachedDevices[username] || { device_type: "android" };
        updateActiveDeviceDisplay(username, dev.device_type);
        closeDeviceDropdown();
        renderDeviceList(deviceSearchInput ? deviceSearchInput.value : "");
        log("Controlling target device: " + username + " [" + (dev.device_type || "android").toUpperCase() + "]");
      })
      .catch(function (e) {
        log("Failed to switch target phone: " + e);
      });
  }

  function refreshMobileDevices() {
    // 1. Query in-memory MQTT device registry first (instant <1ms response)
    fetch("/api/mobile/devices")
      .then(function (r) { return r.json(); })
      .then(function (mqttData) {
        const active = mqttData.active_device || "";
        const mqttDevices = mqttData.devices || [];
        cachedDevices = {};

        mqttDevices.forEach(function (d) {
          if (d && d.username) {
            cachedDevices[d.username] = {
              username: d.username,
              device_type: d.device_type || "android",
              device_id: d.device_id,
            };
          }
        });

        if (active && !cachedDevices[active]) {
          cachedDevices[active] = { username: active, device_type: "android" };
        }
        const activeInfo = active ? (cachedDevices[active] || { device_type: "android" }) : null;
        updateActiveDeviceDisplay(active, activeInfo ? activeInfo.device_type : "none");
        renderDeviceList(deviceSearchInput ? deviceSearchInput.value : "");

        // 2. Optionally enrich with persistent PostgreSQL records in the background without blocking UI
        fetch("/api/devices")
          .then(function (r) { return r.json(); })
          .then(function (pgData) {
            const pgDevices = pgData.devices || [];
            let changed = false;
            pgDevices.forEach(function (d) {
              if (d && d.username && !cachedDevices[d.username]) {
                cachedDevices[d.username] = {
                  username: d.username,
                  device_type: d.device_type || "android",
                  device_id: d.device_id,
                  is_active: d.is_active,
                  last_seen_at: d.last_seen_at,
                };
                changed = true;
              }
            });
            if (changed) {
              renderDeviceList(deviceSearchInput ? deviceSearchInput.value : "");
            }
          })
          .catch(function () {
            // PostgreSQL offline; in-memory devices already displayed
          });
      })
      .catch(function (e) {
        console.warn("Could not load mobile devices:", e);
      });
  }

  function openDeviceDropdown() {
    if (!devicePickerDropdown) return;
    devicePickerDropdown.style.display = "block";
    if (devicePickerBtn) {
      devicePickerBtn.classList.add("open");
      devicePickerBtn.setAttribute("aria-expanded", "true");
    }
    if (deviceSearchInput) {
      deviceSearchInput.focus();
      deviceSearchInput.select();
    }
  }

  function closeDeviceDropdown() {
    if (!devicePickerDropdown) return;
    devicePickerDropdown.style.display = "none";
    if (devicePickerBtn) {
      devicePickerBtn.classList.remove("open");
      devicePickerBtn.setAttribute("aria-expanded", "false");
    }
  }

  function deleteSingleDevice(username) {
    if (!username) return;
    // 1. Optimistic UI update (0ms instant removal from DOM)
    const backupDevice = cachedDevices[username];
    const wasActive = (currentActiveDevice === username);
    delete cachedDevices[username];
    if (wasActive) {
      const remaining = Object.keys(cachedDevices);
      const nextActive = remaining.length > 0 ? remaining[0] : "";
      const nextType = nextActive ? (cachedDevices[nextActive].device_type || "android") : "none";
      updateActiveDeviceDisplay(nextActive, nextType);
    }
    renderDeviceList(deviceSearchInput ? deviceSearchInput.value : "");
    log("Deleted device: " + username);

    // 2. Background server deletion
    fetch("/api/mobile/delete_device", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username: username })
    })
      .then(function (r) { return r.json(); })
      .catch(function (e) {
        console.warn("Delete request failed:", e);
        if (backupDevice) {
          cachedDevices[username] = backupDevice;
          if (wasActive) updateActiveDeviceDisplay(username, backupDevice.device_type);
          renderDeviceList(deviceSearchInput ? deviceSearchInput.value : "");
        }
        log("Failed to delete device on server: " + e);
      });
  }

  const clearDevicesBtn = document.getElementById("clearDevicesBtn");
  if (clearDevicesBtn) {
    clearDevicesBtn.addEventListener("click", function (e) {
      e.stopPropagation();
      fetch("/api/mobile/clear_devices", {
        method: "POST",
        headers: { "Content-Type": "application/json" }
      })
        .then(function (r) { return r.json(); })
        .then(function () {
          cachedDevices = {};
          currentActiveDevice = "";
          updateActiveDeviceDisplay("", "none");
          closeDeviceDropdown();
          refreshMobileDevices();
          log("Cleared all registered devices from dashboard.");
        })
        .catch(function (e) {
          log("Failed to clear devices: " + e);
        });
    });
  }

  if (devicePickerBtn) {
    devicePickerBtn.addEventListener("click", function (e) {
      e.stopPropagation();
      const isHidden = devicePickerDropdown && devicePickerDropdown.style.display === "none";
      if (isHidden) {
        openDeviceDropdown();
      } else {
        closeDeviceDropdown();
      }
    });
    if (deviceSearchInput) {
      deviceSearchInput.addEventListener("input", function () {
        renderDeviceList(deviceSearchInput.value);
      });
      deviceSearchInput.addEventListener("click", function (e) {
        e.stopPropagation();
      });
    }
    document.addEventListener("click", function (e) {
      const wrapper = document.getElementById("mobileDeviceSelectorWrapper");
      if (wrapper && !wrapper.contains(e.target)) {
        closeDeviceDropdown();
      }
    });
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape") {
        closeDeviceDropdown();
      }
    });
    refreshMobileDevices();
    setInterval(refreshMobileDevices, 10000);
    if (typeof socket !== "undefined" && socket) {
      socket.on("mobile_device_registered", function (data) {
        if (data && data.username) {
          cachedDevices[data.username] = {
            username: data.username,
            device_type: data.device_type || "android",
            device_id: data.device_id
          };
          if (!currentActiveDevice || currentActiveDevice === "none") {
            updateActiveDeviceDisplay(data.username, data.device_type || "android");
          }
          renderDeviceList(deviceSearchInput ? deviceSearchInput.value : "");
          log("New device connected: " + data.username + " [" + (data.device_type || "android").toUpperCase() + "]");
        }
        refreshMobileDevices();
      });
      socket.on("active_mobile_device_changed", function (data) {
        if (data && data.active_device !== undefined) {
          const dev = cachedDevices[data.active_device] || null;
          updateActiveDeviceDisplay(data.active_device, dev ? dev.device_type : "none");
          renderDeviceList(deviceSearchInput ? deviceSearchInput.value : "");
        }
      });
      socket.on("mobile_devices_updated", function (data) {
        if (data && data.devices) {
          cachedDevices = {};
          data.devices.forEach(function (d) {
            if (d && d.username) {
              cachedDevices[d.username] = d;
            }
          });
          if (data.active_device !== undefined) {
            const activeInfo = cachedDevices[data.active_device] || null;
            updateActiveDeviceDisplay(data.active_device, activeInfo ? activeInfo.device_type : "none");
          }
          renderDeviceList(deviceSearchInput ? deviceSearchInput.value : "");
        } else {
          refreshMobileDevices();
        }
      });
      socket.on("mobile_devices_cleared", function () {
        cachedDevices = {};
        currentActiveDevice = "";
        updateActiveDeviceDisplay("", "none");
        renderDeviceList("");
      });
    }
  }
})();
