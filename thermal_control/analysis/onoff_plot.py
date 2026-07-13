"""
onoff_plot.py
─────────────
Shared rendering of the "AC on/off + room temperatures" figure used both by the
`live-analysis` skill (ac_onoff.png) and by the web dashboard (served inline).

Kept in `thermal_control/` proper — not in the skill — so the dashboard image
(which never copies the dev `.claude/` tree in spirit) has a stable import.
All temperatures are °F; timestamps are tz-aware America/New_York Timestamps.

The figure: three stacked panels (one per AC zone). Each panel shows the MPC
on/off decision as a step plot on the left axis and, on the right axis, every
room that AC directly covers, with red/blue markers where a room breaches its
schedule-resolved comfort band.
"""

AC_UNITS = ["bedroom_ac", "living_ac", "extension_ac"]

AC_ROOMS = {
    "bedroom_ac":   ["master_bedroom", "kids_bedroom", "anna_office"],
    "living_ac":    ["dining_room", "family_room", "kitchen"],
    "extension_ac": ["tv_room", "nicolas_office"],
}

AC_COLORS = {
    "bedroom_ac":   "#1f77b4",
    "living_ac":    "#d62728",
    "extension_ac": "#2ca02c",
}

GREY_SHADES = ["#555555", "#888888", "#aaaaaa", "#cccccc"]
WARM_SHADES = ["#cc7733", "#dd9955", "#bb5511", "#eeaa77"]  # all-off projection


# ── Schedule resolution ───────────────────────────────────────────────────────

def _hhmm(s):
    h, m = map(int, s.split(":"))
    return h * 60 + m


def resolve_schedule(cfg, ts_local):
    """Return (schedule_name, {room: (min_f, max_f)}, default_band) for a local timestamp."""
    default  = (cfg["targets"]["default"]["min_f"], cfg["targets"]["default"]["max_f"])
    schedule = cfg["targets"].get("schedule", [])
    if not schedule:
        return "default", {}, default

    is_weekend = ts_local.weekday() >= 5  # Mon=0 … Sat=5, Sun=6
    applicable = []
    for e in schedule:
        days = e.get("days")
        if days is None:
            applicable.append(e)
        elif days == "weekend" and is_weekend:
            applicable.append(e)
        elif days == "weekday" and not is_weekend:
            applicable.append(e)

    if not applicable:
        return "default", {}, default

    by_time    = sorted(applicable, key=lambda e: _hhmm(e["time"]))
    now_min    = ts_local.hour * 60 + ts_local.minute
    candidates = [e for e in by_time if _hhmm(e["time"]) <= now_min]
    active     = candidates[-1] if candidates else by_time[-1]

    rooms = active.get("rooms") or {}
    bands = {r: (b["min_f"], b["max_f"]) for r, b in rooms.items()}
    return active["name"], bands, default


def get_band(bands, default, room):
    return bands.get(room, default)


def _row_band(cfg, row, room):
    _, bands, default = resolve_schedule(cfg, row["timestamp"])
    return get_band(bands, default, room)


def _active_schedule_name(cfg, ts_local):
    name, _, _ = resolve_schedule(cfg, ts_local)
    return name


# ── Shared plot helpers ───────────────────────────────────────────────────────

def _draw_ac_left_axis(ax, t, on_vals, ac_color, label_fontsize=7, tick_labels=True):
    """Step plot of on/off on the left axis, optionally with per-tick ON/OFF text."""
    ax.step(t, on_vals, where="post", color=ac_color, linewidth=2, zorder=3)
    ax.fill_between(t, 0, on_vals, step="post", alpha=0.15, color=ac_color, zorder=2)
    if tick_labels:
        for ts, val in zip(t, on_vals):
            ax.text(ts, val + 0.06, "ON" if val else "OFF",
                    ha="center", va="bottom", fontsize=label_fontsize,
                    color=ac_color, fontweight="bold")
    ax.set_ylim(-0.3, 1.5)
    ax.set_yticks([0, 1])
    ax.set_yticklabels(["OFF", "ON"], fontsize=9, color=ac_color)
    ax.tick_params(axis="y", colors=ac_color)
    ax.grid(axis="x", alpha=0.3)
    ax.spines["top"].set_visible(False)
    ax.spines["left"].set_color(ac_color)


def _scatter_breaches(ax2, t_vals, temp_vals, lo_vals, hi_vals,
                      marker="o", size=22):
    """Scatter red/blue breach markers."""
    above = temp_vals > hi_vals
    below = temp_vals < lo_vals
    if above.any():
        ax2.scatter(t_vals[above], temp_vals[above],
                    color="tab:red", zorder=5, s=size, linewidths=0, marker=marker)
    if below.any():
        ax2.scatter(t_vals[below], temp_vals[below],
                    color="tab:blue", zorder=5, s=size, linewidths=0, marker=marker)


# ── Plot: full-history on/off + temperatures ──────────────────────────────────

def make_onoff_plot(df, cfg, plot_path, tick_labels=True):
    """Render the 3-panel on/off + room-temps figure.

    `plot_path` is anything matplotlib's savefig accepts — a filesystem path or a
    file-like object (e.g. a BytesIO for serving the PNG over HTTP).
    Set tick_labels=False to suppress the per-tick ON/OFF text annotations.
    """
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates

    span_start = df["timestamp"].iloc[0].strftime("%Y-%m-%d %H:%M")
    span_end   = df["timestamp"].iloc[-1].strftime("%H:%M")
    t = df["timestamp"].dt.tz_localize(None)

    fig, axes = plt.subplots(3, 1, figsize=(14, 9), sharex=True)
    fig.suptitle(
        f"AC on/off & room temps — MPC live  |  {span_start} → {span_end} ET",
        fontsize=11, fontweight="bold",
    )

    for ax, ac in zip(axes, AC_UNITS):
        ac_color = AC_COLORS[ac]
        on = df[f"on_{ac}"].values.astype(float)
        _draw_ac_left_axis(ax, t, on, ac_color, tick_labels=tick_labels)
        ax.set_ylabel(ac.replace("_", "\n"), fontsize=9, rotation=0,
                      labelpad=52, va="center", color=ac_color)

        ax2 = ax.twinx()
        panel_rooms = [r for r in AC_ROOMS[ac] if f"T_{r}" in df.columns]
        all_vals = []

        for room, grey in zip(panel_rooms, GREY_SHADES):
            vals  = df[f"T_{room}"]
            lo_s  = df.apply(lambda r, rm=room: _row_band(cfg, r, rm)[0], axis=1)
            hi_s  = df.apply(lambda r, rm=room: _row_band(cfg, r, rm)[1], axis=1)
            all_vals.extend(vals.tolist())
            ax2.plot(t, vals, color=grey, linewidth=1.6, marker="none",
                     label=room.replace("_", " "), zorder=3)
            _scatter_breaches(ax2, t, vals, lo_s, hi_s)

        if all_vals:
            ax2.set_ylim(min(all_vals) - 0.5, max(all_vals) + 0.5)

        ax2.set_ylabel("°F", fontsize=9)
        ax2.tick_params(axis="y", labelsize=8)
        ax2.legend(fontsize=8, loc="upper left", ncol=len(panel_rooms), framealpha=0.7)

    axes[-1].xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
    axes[-1].xaxis.set_major_locator(mdates.AutoDateLocator(minticks=6, maxticks=14))
    axes[-1].tick_params(axis="x", rotation=0)
    axes[-1].set_xlabel("Time (ET)", fontsize=9)
    plt.tight_layout()
    fig.savefig(plot_path, dpi=130, bbox_inches="tight")
    plt.close(fig)


def make_placeholder_plot(plot_path, message="No data in the selected time range"):
    """Small PNG shown in place of the on/off figure when a selected time
    window has no decision-log rows (e.g. a custom range with no overlap)."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(14, 3))
    ax.text(0.5, 0.5, message, ha="center", va="center", fontsize=13, color="#888888")
    ax.axis("off")
    fig.savefig(plot_path, dpi=130, bbox_inches="tight")
    plt.close(fig)
