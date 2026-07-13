/*
 * plots.js — client-side rendering for the three "AC on/off & room temps"
 * panels. Fetches JSON from /plots/onoff_data.json and draws it with Plotly.
 * Range switches update the charts in place — no page reload, so there's no
 * scroll jump. Click a room's legend entry to hide/show its temp line and
 * target-range band together.
 */
(function () {
  "use strict";

  const DEFAULT_RANGE = "1d";
  const TOGGLE_KEY = "dashboard.plotToggles";
  const SCROLL_KEY = "dashboard.scrollY";
  const AUTO_REFRESH_KEY = "dashboard.autoRefresh";
  const AUTO_REFRESH_MS = 30000;

  // Restore scroll position lost to the page's 30s auto-refresh (a full
  // navigation reload) as early as possible — the rest of the DOM is already
  // parsed by the time this deferred script runs, so heights are accurate.
  (function restoreScroll() {
    try {
      const y = sessionStorage.getItem(SCROLL_KEY);
      if (y !== null) window.scrollTo(0, parseInt(y, 10));
    } catch (e) { /* sessionStorage unavailable — nothing to restore */ }
  })();
  ["beforeunload", "pagehide"].forEach((evt) => {
    window.addEventListener(evt, () => {
      try { sessionStorage.setItem(SCROLL_KEY, String(window.scrollY)); } catch (e) {}
    });
  });

  // Whole-page auto-refresh (was a <meta http-equiv="refresh">) — now an
  // opt-out checkbox, since a full reload is what causes the scroll jump.
  function autoRefreshEnabled() {
    try {
      const v = localStorage.getItem(AUTO_REFRESH_KEY);
      return v === null ? true : v === "1";      // default on, matches prior behavior
    } catch (e) {
      return true;
    }
  }

  let autoRefreshTimer = null;
  function scheduleAutoRefresh() {
    if (autoRefreshTimer) clearTimeout(autoRefreshTimer);
    autoRefreshTimer = autoRefreshEnabled()
      ? setTimeout(() => window.location.reload(), AUTO_REFRESH_MS)
      : null;
  }

  const autoRefreshCheckbox = document.getElementById("auto-refresh-toggle");
  if (autoRefreshCheckbox) {
    autoRefreshCheckbox.checked = autoRefreshEnabled();
    autoRefreshCheckbox.addEventListener("change", () => {
      try {
        localStorage.setItem(AUTO_REFRESH_KEY, autoRefreshCheckbox.checked ? "1" : "0");
      } catch (e) {}
      scheduleAutoRefresh();
    });
    scheduleAutoRefresh();
  }

  const state = readStateFromUrl();
  const toggles = loadToggles();

  function readStateFromUrl() {
    const p = new URLSearchParams(window.location.search);
    return {
      range: p.get("range") || DEFAULT_RANGE,
      start: p.get("start") || "",
      end: p.get("end") || "",
    };
  }

  function writeStateToUrl() {
    const p = new URLSearchParams(window.location.search);
    p.set("range", state.range);
    if (state.range === "custom" && state.start && state.end) {
      p.set("start", state.start);
      p.set("end", state.end);
    } else {
      p.delete("start");
      p.delete("end");
    }
    history.replaceState(null, "", window.location.pathname + "?" + p.toString());

    // Keep the (full-page-nav) weekday/weekend links in sync so switching
    // day doesn't drop the currently selected plot range.
    document.querySelectorAll(".toggle a[data-day]").forEach((a) => {
      const q = new URLSearchParams(p);
      q.set("day", a.dataset.day);
      a.href = "?" + q.toString();
    });
  }

  function dataUrl(ac) {
    const p = new URLSearchParams();
    p.set("ac", ac);
    p.set("range", state.range);
    if (state.range === "custom" && state.start && state.end) {
      p.set("start", state.start);
      p.set("end", state.end);
    }
    return "/plots/onoff_data.json?" + p.toString();
  }

  function loadToggles() {
    try {
      return JSON.parse(localStorage.getItem(TOGGLE_KEY)) || {};
    } catch (e) {
      return {};
    }
  }

  function saveToggles() {
    try {
      localStorage.setItem(TOGGLE_KEY, JSON.stringify(toggles));
    } catch (e) { /* localStorage unavailable — toggles just won't persist */ }
  }

  // Per-room visibility (shared by a room's temp line and its two band
  // lines — they always show/hide together) persisted across reloads.
  function roomVisible(ac, roomId) {
    if (!toggles[ac]) toggles[ac] = {};
    if (!(roomId in toggles[ac])) toggles[ac][roomId] = true;
    return toggles[ac][roomId];
  }

  function setRoomVisible(ac, roomId, visible) {
    if (!toggles[ac]) toggles[ac] = {};
    toggles[ac][roomId] = visible;
    saveToggles();
  }

  function hexToRgba(hex, alpha) {
    const n = parseInt(hex.replace("#", ""), 16);
    return `rgba(${(n >> 16) & 255},${(n >> 8) & 255},${n & 255},${alpha})`;
  }

  const AC_ON_COLOR = "#2f81f7";
  const AC_OFF_COLOR = "#f0483e";

  // A thick two-color strip hugging the bottom of the chart (y=0 on a hidden
  // axis) instead of a full-height trace — keeps the on/off state visible
  // without dominating the room-temp lines. One 2-point trace per contiguous
  // on/off run, each spanning exactly to where the next run starts, so
  // adjacent segments touch with no gap (a single trace with null-gapped
  // points doesn't work here: a lone point with mode "lines" right before a
  // gap has no neighbor to draw to, leaving a hole at every transition).
  function buildAcStateTraces(timestamps, on) {
    const traces = [];
    const n = timestamps.length;
    let i = 0;
    while (i < n) {
      const state = on[i];
      let j = i;
      while (j + 1 < n && on[j + 1] === state) j++;
      const xEnd = j + 1 < n ? timestamps[j + 1] : timestamps[j];
      traces.push({
        x: [timestamps[i], xEnd], y: [0, 0],
        type: "scatter", mode: "lines", yaxis: "y",
        hoverinfo: "skip", showlegend: false, meta: { kind: "ac" },
        line: { color: state ? AC_ON_COLOR : AC_OFF_COLOR, width: 6 },
      });
      i = j + 1;
    }
    return traces;
  }

  function buildTraces(payload) {
    const traces = buildAcStateTraces(payload.timestamps, payload.on);
    payload.rooms.forEach((room) => {
      const visible = roomVisible(payload.ac, room.id);
      const bandColor = hexToRgba(room.color, 0.6);
      [room.band_hi, room.band_lo].forEach((y) => {
        traces.push({
          x: payload.timestamps, y: y, name: room.label + " band",
          type: "scatter", mode: "lines",
          line: { color: bandColor, width: 1.5, dash: "dot", shape: "hv" },
          showlegend: false, hoverinfo: "skip", yaxis: "y2",
          visible: visible, meta: { kind: "band", room: room.id },
        });
      });
      traces.push({
        x: payload.timestamps, y: room.temp, name: room.label,
        type: "scatter", mode: "lines", line: { color: room.color, width: 1.6 },
        yaxis: "y2", visible: visible ? true : "legendonly",
        meta: { kind: "temp", room: room.id },
      });
    });
    return traces;
  }

  function layoutFor() {
    return {
      margin: { t: 10, r: 48, b: 32, l: 56 },
      height: 300,
      showlegend: true,
      legend: { orientation: "h", y: 1.18, font: { size: 10 }, bgcolor: "rgba(0,0,0,0)" },
      paper_bgcolor: "transparent",
      plot_bgcolor: "transparent",
      font: { color: "#d6dde6", size: 11 },
      xaxis: { gridcolor: "#2a323d", showgrid: true },
      yaxis: { visible: false, range: [-0.2, 1], fixedrange: true },
      yaxis2: {
        title: "°F", overlaying: "y", side: "right", gridcolor: "transparent",
      },
      annotations: [{
        xref: "paper", x: -0.005, xanchor: "right",
        yref: "y", y: 0, yanchor: "middle",
        text: "AC", showarrow: false,
        font: { size: 10, color: "#8a95a3" },
      }],
    };
  }

  const PLOTLY_CONFIG = { responsive: true, displaylogo: false };

  function showEmpty(el) {
    if (el.dataset.plotted) {
      Plotly.purge(el);
      delete el.dataset.plotted;
    }
    el.innerHTML = '<p class="muted plot-empty">No data in the selected time range.</p>';
  }

  // Clicking a room's legend entry toggles its temp line (Plotly's default
  // behavior); this also toggles that room's two band lines in lockstep so
  // temp and target range always show/hide together.
  function linkBandsToLegend(el, ac) {
    el.on("plotly_legendclick", (evt) => {
      const trace = evt.data[evt.curveNumber];
      if (!trace.meta || trace.meta.kind !== "temp") return true;
      const nowVisible = trace.visible === true || trace.visible === undefined;
      const newVisible = nowVisible ? "legendonly" : true;
      setRoomVisible(ac, trace.meta.room, newVisible === true);
      const idx = [];
      el.data.forEach((tr, i) => {
        if (tr.meta && tr.meta.kind === "band" && tr.meta.room === trace.meta.room) idx.push(i);
      });
      if (idx.length) Plotly.restyle(el, { visible: newVisible }, idx);
      return true;   // let Plotly's default handling toggle the clicked temp trace too
    });
  }

  function renderPanel(panel) {
    const ac = panel.dataset.ac;
    const el = document.getElementById("plot-" + ac);
    return fetch(dataUrl(ac))
      .then((r) => r.json())
      .then((payload) => {
        if (!payload.timestamps.length) {
          showEmpty(el);
          return;
        }
        const traces = buildTraces(payload);
        if (el.dataset.plotted) {
          Plotly.react(el, traces, layoutFor(), PLOTLY_CONFIG);
        } else {
          el.innerHTML = "";
          Plotly.newPlot(el, traces, layoutFor(), PLOTLY_CONFIG);
          el.dataset.plotted = "1";
          linkBandsToLegend(el, ac);
        }
      });
  }

  function renderAll() {
    document.querySelectorAll(".ac-panel").forEach(renderPanel);
  }

  function setActiveRangeControls() {
    document.querySelectorAll(".plot-controls a[data-range]").forEach((a) => {
      a.classList.toggle("active", a.dataset.range === state.range);
    });
    const form = document.querySelector(".custom-range");
    if (!form) return;
    form.querySelector(".apply-btn").classList.toggle("active", state.range === "custom");
    if (state.start) form.querySelector('[name="start"]').value = state.start;
    if (state.end) form.querySelector('[name="end"]').value = state.end;
  }

  function applyRange(range, start, end) {
    state.range = range;
    state.start = start || "";
    state.end = end || "";
    writeStateToUrl();
    setActiveRangeControls();
    renderAll();
  }

  document.querySelectorAll(".plot-controls a[data-range]").forEach((a) => {
    a.addEventListener("click", (e) => {
      e.preventDefault();
      applyRange(a.dataset.range, "", "");
    });
  });

  const customForm = document.querySelector(".custom-range");
  if (customForm) {
    customForm.addEventListener("submit", (e) => {
      e.preventDefault();
      applyRange(
        "custom",
        customForm.querySelector('[name="start"]').value,
        customForm.querySelector('[name="end"]').value
      );
    });
  }

  writeStateToUrl();
  setActiveRangeControls();
  renderAll();
})();
