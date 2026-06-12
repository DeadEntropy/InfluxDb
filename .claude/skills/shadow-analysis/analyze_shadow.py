"""
analyze_shadow.py
─────────────────
Reproducible analysis of a shadow-run log (shadow.csv) produced by
thermal_control/shadow_run.py.

Reports:
  1. Log coverage (span, tick count, gaps, missing sensor data)
  2. Per-AC: MPC-on %, actual-cooling %, tick-level agreement, decision flips
  3. Per-room temperature ranges and time outside the comfort band
     (bands resolved per row from active_schedule + config/control.yaml)
  4. Cost evolution by hour and chosen-vs-runner-up margins
  5. A 3-panel plot: MPC desired state vs actual cooling state per AC

Usage (from the repo root):
    python .claude/skills/shadow-analysis/analyze_shadow.py [shadow.csv]
        [--plot shadow_mpc_vs_actual.png] [--no-plot]
"""

import argparse
import sys
from pathlib import Path

import pandas as pd
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
CONTROL_YAML = REPO_ROOT / "thermal_control" / "config" / "control.yaml"

AC_UNITS = ["bedroom_ac", "living_ac", "extension_ac"]


def load_bands():
    """name -> {room: (min_f, max_f)} with default fallback per schedule entry."""
    with open(CONTROL_YAML) as f:
        cfg = yaml.safe_load(f)
    targets = cfg["targets"]
    default = (targets["default"]["min_f"], targets["default"]["max_f"])
    bands = {}
    for entry in targets["schedule"]:
        rooms = entry.get("rooms") or {}
        bands[entry["name"]] = {
            room: (band["min_f"], band["max_f"]) for room, band in rooms.items()
        }
    return bands, default


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("csv", nargs="?", default="shadow.csv", help="path to shadow log")
    ap.add_argument("--plot", default="shadow_mpc_vs_actual.png", help="plot output path")
    ap.add_argument("--no-plot", action="store_true")
    args = ap.parse_args()

    df = pd.read_csv(args.csv, parse_dates=["timestamp"])
    rooms = [c[2:] for c in df.columns if c.startswith("T_") and c != "T_outdoor"]

    # ── 1. Coverage ───────────────────────────────────────────────────────
    print("=" * 70)
    print(f"Log: {args.csv}")
    print(f"  rows: {len(df)}  span: {df.timestamp.min()} -> {df.timestamp.max()}")
    gaps = (df.timestamp.diff().dt.total_seconds() > 120).sum()
    print(f"  gaps > 2 min: {gaps}")
    print(f"  ticks with missing rooms: {df.missing_rooms.notna().sum()}")
    print(f"  outdoor: {df.T_outdoor.min():.1f} -> {df.T_outdoor.max():.1f} °F")
    print(f"  schedule blocks: {df.active_schedule.value_counts().to_dict()}")

    # ── 2. Per-AC agreement and stability ─────────────────────────────────
    print("\n" + "=" * 70)
    print("MPC vs actual (tick-level):")
    print(f"  {'AC':14s} {'MPC on':>7s} {'actual':>7s} {'agree':>6s} {'MPC flips':>10s}")
    for ac in AC_UNITS:
        mpc = df[f"mpc_on_{ac}"]
        act = (df[f"actual_action_{ac}"] == "cooling").astype(int)
        flips = int(mpc.diff().abs().sum())
        print(f"  {ac:14s} {mpc.mean():>6.0%} {act.mean():>7.0%} "
              f"{(mpc == act).mean():>6.0%} {flips:>10d}")
        chg = df[mpc.diff().fillna(0) != 0]
        for _, row in chg.iterrows():
            state = "ON" if row[f"mpc_on_{ac}"] else "OFF"
            print(f"      flip -> {state:3s} at {row.timestamp:%H:%M} "
                  f"({row.active_schedule})")
    print("\n  Note: real units duty-cycle in short bursts while MPC holds")
    print("  multi-hour decisions, so tick agreement understates alignment.")

    # ── 3. Room temps vs comfort bands ────────────────────────────────────
    bands, default = load_bands()
    print("\n" + "=" * 70)
    print("Room temperatures (actual, under REAL thermostat control)")
    print("and time outside the active comfort band:")
    print(f"  {'room':16s} {'min':>6s} {'max':>6s} {'end':>6s} "
          f"{'below':>7s} {'above':>7s}")
    for room in rooms:
        T = df["T_" + room]
        lo = df.active_schedule.map(
            lambda s: bands.get(s, {}).get(room, default)[0])
        hi = df.active_schedule.map(
            lambda s: bands.get(s, {}).get(room, default)[1])
        below, above = (T < lo).mean(), (T > hi).mean()
        flag = "  <-- check" if max(below, above) > 0.25 else ""
        print(f"  {room:16s} {T.min():>6.1f} {T.max():>6.1f} {T.iloc[-1]:>6.1f} "
              f"{below:>7.0%} {above:>7.0%}{flag}")

    # ── 4. Costs ──────────────────────────────────────────────────────────
    print("\n" + "=" * 70)
    cost_cols = [c for c in df.columns if c.startswith("cost_") and c != "cost_chosen"]
    hourly = df.set_index("timestamp").cost_chosen.resample("1h").agg(["first", "max"])
    print("cost_chosen by hour (a steady climb = too-cold discomfort the")
    print("cooling-only MPC cannot fix; constant offset, harmless to decisions):")
    print(hourly.round(1).to_string())
    margin = df[cost_cols].apply(
        lambda r: r.nsmallest(2).iloc[1] - r.nsmallest(2).iloc[0], axis=1)
    print(f"\nchosen vs runner-up margin: median={margin.median():.3f} "
          f"max={margin.max():.1f}")
    print("(median near energy_weight scale => comfort ties broken by energy)")

    # ── 5. Plot ───────────────────────────────────────────────────────────
    if args.no_plot:
        return
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.dates as mdates
    import matplotlib.pyplot as plt

    t = df["timestamp"]
    fig, axes = plt.subplots(3, 1, figsize=(14, 9), sharex=True)
    for ax, ac in zip(axes, AC_UNITS):
        mpc = df[f"mpc_on_{ac}"]
        act = (df[f"actual_action_{ac}"] == "cooling").astype(int)
        ax.fill_between(t, 0, act, step="post", alpha=0.35, color="tab:red",
                        label="actual (cooling)")
        ax.step(t, mpc, where="post", color="tab:blue", lw=1.8,
                label="MPC wants ON")
        ax.set_title(f"{ac} — agreement {(mpc == act).mean():.0%}",
                     loc="left", fontsize=11, fontweight="bold")
        ax.set_yticks([0, 1]); ax.set_yticklabels(["OFF", "ON"])
        ax.set_ylim(-0.08, 1.15)
        ax.legend(loc="center right", fontsize=9)
        sched_change = df[df.active_schedule.ne(df.active_schedule.shift())][1:]
        for _, row in sched_change.iterrows():
            ax.axvline(row.timestamp, color="gray", ls="--", lw=0.8)
            ax.text(row.timestamp, 1.07, " " + row.active_schedule,
                    fontsize=8, color="gray")
    axes[-1].xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
    axes[-1].set_xlabel(
        f"time (local), {t.min():%Y-%m-%d %H:%M} -> {t.max():%Y-%m-%d %H:%M}")
    fig.suptitle("Shadow run: MPC decision vs actual thermostat behavior",
                 fontsize=13)
    fig.tight_layout()
    fig.savefig(args.plot, dpi=120)
    print(f"\nplot saved to {args.plot}")


if __name__ == "__main__":
    sys.exit(main())
