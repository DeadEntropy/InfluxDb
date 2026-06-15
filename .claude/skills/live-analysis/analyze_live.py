"""
analyze_live.py
───────────────
Analysis of the live MPC decisions log (mpc_decision_log.csv) produced by
thermal_control/scheduler.py.

Reports:
  1. Log coverage (span, tick count, outdoor range, write failures)
  2. Per-AC: % ON time, decision flips with timestamps
  3. Per-room: temp range, current reading, comfort band status
  4. Cost trend and chosen-vs-runner-up margin
  5. Plot 1 (ac_onoff.png):    on/off step + room temperatures (secondary axis)
  6. Plot 2 (ac_projection.png): last 3h history + 3h projection under MPC vs all-off

Usage (from the repo root):
    python .claude/skills/live-analysis/analyze_live.py [path/to/mpc_decision_log.csv]
        [--plot ac_onoff.png] [--plot2 ac_projection.png] [--no-plot]
"""

import argparse
import sys
from pathlib import Path

import pandas as pd
import yaml

REPO_ROOT    = Path(__file__).resolve().parents[3]
CONTROL_YAML = REPO_ROOT / "thermal_control" / "config" / "control.yaml"
DEFAULT_CSV  = REPO_ROOT / "remote_logs" / "mpc_decision_log.csv"
DEFAULT_PLOT  = REPO_ROOT / "ac_onoff.png"
DEFAULT_PLOT2 = REPO_ROOT / "ac_projection.png"

# The on/off figure + its schedule/plot helpers live in thermal_control proper so
# the dashboard can reuse them; only plot 2 (the projection) stays skill-local.
sys.path.insert(0, str(REPO_ROOT))
from thermal_control.analysis.onoff_plot import (   # noqa: E402
    AC_UNITS, AC_ROOMS, AC_COLORS, GREY_SHADES, WARM_SHADES,
    resolve_schedule, get_band, _row_band, _active_schedule_name,
    _draw_ac_left_axis, _scatter_breaches, make_onoff_plot,
)


def load_config():
    with open(CONTROL_YAML) as f:
        return yaml.safe_load(f)


# ── Plot 2: last 3h history + 3h projection (MPC vs all-off) ─────────────────

def make_projection_plot(df, cfg, plot_path):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates
    from matplotlib.lines import Line2D

    sys.path.insert(0, str(REPO_ROOT))
    try:
        from thermal_control.model.simulate import HouseSimulator
    except ImportError as exc:
        print(f"projection plot skipped — cannot import HouseSimulator: {exc}")
        return

    HOUSE_YAML  = REPO_ROOT / "thermal_control" / "config" / "house.yaml"
    WEIGHTS_DIR = REPO_ROOT / "thermal_control" / "model" / "weights"
    with open(HOUSE_YAML) as f:
        house_cfg = yaml.safe_load(f)
    sim = HouseSimulator(WEIGHTS_DIR, house_cfg)

    HIST_STEPS = 18
    hist    = df.tail(HIST_STEPS).copy().reset_index(drop=True)
    t_hist  = hist["timestamp"].dt.tz_localize(None)
    last    = hist.iloc[-1]
    t_now   = t_hist.iloc[-1]

    current_state = {
        r: float(last[f"T_{r}"])
        for r in sim.rooms if f"T_{r}" in hist.columns
    }
    T_outdoor = float(last["T_outdoor"])

    mpc_sp = {ac: float(last[f"sp_{ac}"]) for ac in AC_UNITS}
    off_sp = {ac: 84.0 for ac in AC_UNITS}

    HORIZON = 18
    outdoor_series = [T_outdoor] * HORIZON
    mpc_states = sim.rollout(current_state, [mpc_sp] * HORIZON, outdoor_series)
    off_states = sim.rollout(current_state, [off_sp] * HORIZON, outdoor_series)

    # Projection time axis (t+10min … t+180min), tz-naive
    t_proj = pd.date_range(
        start=t_now + pd.Timedelta(minutes=10),
        periods=HORIZON, freq="10min",
    )

    span_start = hist["timestamp"].iloc[0].strftime("%Y-%m-%d %H:%M")
    proj_end   = t_proj[-1].strftime("%H:%M")

    fig, axes = plt.subplots(3, 1, figsize=(14, 9), sharex=True)
    fig.suptitle(
        f"3h history + 3h projection — MPC live  |  "
        f"{span_start} → {proj_end} ET  (▏= now: {t_now.strftime('%H:%M')})",
        fontsize=11, fontweight="bold",
    )

    for ax, ac in zip(axes, AC_UNITS):
        ac_color  = AC_COLORS[ac]
        on_hist   = hist[f"on_{ac}"].values.astype(float)

        # ── Left axis: on/off history + projected decision (dashed) ───────
        _draw_ac_left_axis(ax, t_hist, on_hist, ac_color, label_fontsize=6.5)
        future_on = 1.0 if mpc_sp.get(ac, 84.0) == 65.0 else 0.0
        ax.plot([t_now, t_proj[-1]], [future_on, future_on],
                color=ac_color, lw=1.5, ls="--", alpha=0.45, zorder=3)
        ax.axvline(t_now, color="#444444", lw=1.0, ls=":", alpha=0.55, zorder=4)
        ax.set_ylabel(ac.replace("_", "\n"), fontsize=9, rotation=0,
                      labelpad=52, va="center", color=ac_color)

        # ── Right axis: temperatures ───────────────────────────────────────
        ax2 = ax.twinx()
        panel_rooms = [r for r in AC_ROOMS[ac] if f"T_{r}" in hist.columns]
        all_vals = []

        for room, grey, warm in zip(panel_rooms, GREY_SHADES, WARM_SHADES):
            # ── History ────────────────────────────────────────────────────
            vals_h = hist[f"T_{room}"]
            lo_h   = hist.apply(lambda r, rm=room: _row_band(cfg, r, rm)[0], axis=1)
            hi_h   = hist.apply(lambda r, rm=room: _row_band(cfg, r, rm)[1], axis=1)
            all_vals.extend(vals_h.tolist())

            ax2.plot(t_hist, vals_h, color=grey, linewidth=1.6, marker="none",
                     label=room.replace("_", " "), zorder=3)
            _scatter_breaches(ax2, t_hist, vals_h, lo_h, hi_h)

            # ── MPC projection (grey dashed) ───────────────────────────────
            mpc_proj = pd.Series(
                [mpc_states[i + 1].get(room, float("nan")) for i in range(HORIZON)],
                index=t_proj,
            )
            all_vals.extend(mpc_proj.dropna().tolist())
            ax2.plot(t_proj, mpc_proj, color=grey, lw=1.4, ls="--",
                     marker="none", zorder=3, alpha=0.9)

            # ── All-off projection (warm dashed) ───────────────────────────
            off_proj = pd.Series(
                [off_states[i + 1].get(room, float("nan")) for i in range(HORIZON)],
                index=t_proj,
            )
            all_vals.extend(off_proj.dropna().tolist())
            ax2.plot(t_proj, off_proj, color=warm, lw=1.4, ls="--",
                     marker="none", zorder=3, alpha=0.9)

            # ── Breach markers on projections ──────────────────────────────
            proj_lo = pd.Series(
                [get_band(*resolve_schedule(cfg, tp)[1:], room) [0] for tp in t_proj],
                index=t_proj,
            )
            proj_hi = pd.Series(
                [get_band(*resolve_schedule(cfg, tp)[1:], room) [1] for tp in t_proj],
                index=t_proj,
            )
            _scatter_breaches(ax2, t_proj, mpc_proj, proj_lo, proj_hi,
                              marker="o", size=22)
            _scatter_breaches(ax2, t_proj, off_proj, proj_lo, proj_hi,
                              marker="D", size=18)

        ax2.axvline(t_now, color="#444444", lw=1.0, ls=":", alpha=0.55, zorder=4)

        if all_vals:
            _, bands, default = resolve_schedule(cfg, hist["timestamp"].iloc[-1])
            blo = min(get_band(bands, default, r)[0] for r in panel_rooms)
            bhi = max(get_band(bands, default, r)[1] for r in panel_rooms)
            ax2.set_ylim(min(all_vals) - 0.5, max(all_vals) + 0.5)

        handles, labels = ax2.get_legend_handles_labels()
        handles += [
            Line2D([0], [0], color="#777777", lw=1.4, ls="--"),
            Line2D([0], [0], color=WARM_SHADES[0], lw=1.4, ls="--"),
        ]
        labels += ["MPC proj.", "all-off proj."]
        ax2.set_ylabel("°F", fontsize=9)
        ax2.tick_params(axis="y", labelsize=8)
        ax2.legend(handles, labels, fontsize=8, loc="upper left",
                   ncol=len(panel_rooms) + 2, framealpha=0.7)

    axes[-1].xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
    axes[-1].xaxis.set_major_locator(mdates.AutoDateLocator(minticks=7, maxticks=15))
    axes[-1].tick_params(axis="x", rotation=0)
    axes[-1].set_xlabel("Time (ET)", fontsize=9)
    plt.tight_layout()
    fig.savefig(plot_path, dpi=130, bbox_inches="tight")
    plt.close(fig)
    print(f"plot 2 saved → {plot_path}")


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("csv",     nargs="?", default=str(DEFAULT_CSV))
    ap.add_argument("--plot",  default=str(DEFAULT_PLOT))
    ap.add_argument("--plot2", default=str(DEFAULT_PLOT2))
    ap.add_argument("--no-plot", action="store_true")
    args = ap.parse_args()

    df = pd.read_csv(args.csv)
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True).dt.tz_convert("America/New_York")
    df = df.sort_values("timestamp").reset_index(drop=True)

    rooms = [c[2:] for c in df.columns if c.startswith("T_") and c != "T_outdoor"]
    cfg   = load_config()

    span_start  = df["timestamp"].iloc[0].strftime("%Y-%m-%d %H:%M")
    span_end    = df["timestamp"].iloc[-1].strftime("%H:%M")
    write_fails = (~df["write_ok"].astype(bool)).sum() if "write_ok" in df.columns else "n/a"

    # ── 1. Coverage ───────────────────────────────────────────────────────────
    print("=" * 68)
    print(f"Log: {args.csv}")
    print(f"  ticks  : {len(df)}  ({span_start} → {span_end} ET)")
    print(f"  outdoor: {df['T_outdoor'].min():.1f} → {df['T_outdoor'].max():.1f} °F")
    print(f"  write failures: {write_fails}")

    # ── 2. Per-AC decisions ───────────────────────────────────────────────────
    print("\n" + "=" * 68)
    print(f"  {'AC':14s} {'% ON':>6s}  {'flips':>5s}")
    for ac in AC_UNITS:
        on    = df[f"on_{ac}"]
        flips = int(on.diff().abs().fillna(0).sum())
        print(f"  {ac:14s} {on.mean():>5.0%}   {flips:>4d}")
        for _, row in df[on.diff().fillna(0) != 0].iterrows():
            state = "ON " if row[f"on_{ac}"] else "OFF"
            sched = _active_schedule_name(cfg, row["timestamp"])
            print(f"      flip → {state} at {row['timestamp'].strftime('%H:%M')}  [{sched}]")

    # ── 3. Room temperatures vs comfort bands ─────────────────────────────────
    print("\n" + "=" * 68)
    print(f"  {'room':16s} {'now':>6s} {'min':>6s} {'max':>6s}  {'band':>9s}  status")
    for room in rooms:
        T    = df[f"T_{room}"]
        last = T.iloc[-1]
        _, bands, default = resolve_schedule(cfg, df["timestamp"].iloc[-1])
        blo, bhi = get_band(bands, default, room)
        if last < blo:
            status = f"too cold  ({last - blo:+.1f}°F)"
        elif last > bhi:
            status = f"TOO HOT   ({last - bhi:+.1f}°F)"
        else:
            status = "ok"
        print(f"  {room:16s} {last:>6.1f} {T.min():>6.1f} {T.max():>6.1f}  "
              f"{blo}–{bhi}°F   {status}")

    # ── 4. Cost ───────────────────────────────────────────────────────────────
    cost_cols = [c for c in df.columns if c.startswith("cost_") and c != "cost_chosen"]
    if cost_cols and "cost_chosen" in df.columns:
        margin = df[cost_cols].apply(lambda r: sorted(r)[1] - sorted(r)[0], axis=1)
        print(f"\n  cost_chosen: first={df['cost_chosen'].iloc[0]:.1f}  "
              f"last={df['cost_chosen'].iloc[-1]:.1f}")
        print(f"  chosen vs runner-up: median={margin.median():.2f}  max={margin.max():.1f}")

    if args.no_plot:
        return 0

    make_onoff_plot(df, cfg, args.plot)
    print(f"plot 1 saved → {args.plot}")
    make_projection_plot(df, cfg, args.plot2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
