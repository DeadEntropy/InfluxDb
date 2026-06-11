"""
ac_power_status_check.py
────────────────────────
Cross-checks AC power consumption (InfluxDB, Emporia Vue circuits) against
the thermostats' reported status (Home Assistant, hvac_action) and reports
where the two disagree.

For each AC unit:
  1. pulls instantaneous power (W) for its circuits from InfluxDB,
  2. pulls hvac_action history (cooling vs idle) from the HA REST API,
  3. aligns both on a common time grid and compares
     "power above threshold" vs "thermostat says cooling".

All dates/times are US/Eastern: CLI arguments are interpreted as Eastern
and every timestamp printed or written to CSV is Eastern. UTC is used only
internally when talking to InfluxDB / HA.

Usage (from repo root):
    python ac_power_status_check.py --start-date 2026-06-04 --end-date 2026-06-10
    python ac_power_status_check.py --start-date 2026-06-04 --csv mismatches.csv

Expects INFLUX_* in ./.env and HA_URL/HA_TOKEN in thermal_control/.env.
"""

import argparse
import os
from datetime import datetime, timedelta

import pandas as pd
import pytz
import requests
from dotenv import load_dotenv

from influx_kwh import get_influx_client

EASTERN = pytz.timezone('US/Eastern')

# Zone-to-circuit mapping, established empirically (Jun 2026) by correlating
# circuit watts against each thermostat's hvac_action:
#   - air_handler_1/compressor_1 track thermostat_central
#   - air_handler_2/compressor_2 track bedroom_thermostat
#   - the extension unit is not on a dedicated circuit; it shows up as
#     ~700-800 W on EACH sub-panel leg when cooling (240 V unit), so its
#     power is sub_panel_1 + sub_panel_2 (which also carry other small loads).
AC_UNITS = {
    'living_ac': {
        'climate_entity': 'climate.thermostat_central',
        'power_entities': ['air_compressor_1_12_1min', 'air_handler_1_16_1min'],
        'on_threshold_w': 400,
    },
    'bedroom_ac': {
        'climate_entity': 'climate.bedroom_thermostat',
        'power_entities': ['air_compressor_2_14_1min', 'air_handler_2_2_1min'],
        'on_threshold_w': 400,
    },
    'extension_ac': {
        'climate_entity': 'climate.thermostat_extension',
        'power_entities': ['sub_panel_1_10_1min', 'sub_panel_2_9_1min'],
        # higher threshold: sub-panels carry other loads (~200 W baseline)
        'on_threshold_w': 600,
    },
}


# ── Extract: power from InfluxDB ──────────────────────────────────────────

# HA only writes a point to InfluxDB on state change, so the 1-min Emporia
# entities go silent both when the value is constant (idle circuit stuck at
# ~0 W — a valid reading) and when the sensor drops out (gaps up to 11+ h
# observed with the compressor mid-run). Short gaps are always forward-
# filled. Long gaps are forward-filled only when the last logged value was
# near zero — a running compressor fluctuates and keeps logging, so a long
# silence after a high reading means the sensor went away, not the load.
_MAX_FFILL_SLOTS = 3   # × resample interval (15 min at the 5min default)
_GAP_TRUST_W = 100.0   # long gaps are trusted only below this wattage


def _fill_gaps(col: pd.Series) -> pd.Series:
    filled = col.ffill()
    gap = col.isna()
    gap_run_len = gap.groupby((~gap).cumsum()).transform('sum')
    distrusted = gap & (gap_run_len > _MAX_FFILL_SLOTS) & (filled > _GAP_TRUST_W)
    return filled.mask(distrusted)


def fetch_power(start_utc: datetime, end_utc: datetime, resample: str) -> pd.DataFrame:
    """
    Returns a DataFrame (Eastern-tz index, one column per power entity, watts)
    resampled to `resample`. NaN where the sensor went silent mid-run for
    more than _MAX_FFILL_SLOTS intervals.
    """
    client = get_influx_client(df=True)
    entities = sorted({e for ac in AC_UNITS.values() for e in ac['power_entities']})
    s = start_utc.strftime('%Y-%m-%dT%H:%M:%SZ')
    e = end_utc.strftime('%Y-%m-%dT%H:%M:%SZ')
    query = (
        f'SELECT "value" FROM "W" WHERE time >= \'{s}\' AND time < \'{e}\' '
        f'GROUP BY "entity_id"'
    )
    results = client.query(query)
    series = {}
    for (measurement, tags) in results:
        entity_id = dict(tags)['entity_id']
        if entity_id in entities:
            series[entity_id] = results[(measurement, tags)]['value']
    missing = set(entities) - set(series)
    if missing:
        raise RuntimeError(f"No power data in InfluxDB for: {sorted(missing)}")
    df = pd.DataFrame(series).resample(resample).mean().apply(_fill_gaps)
    return df.tz_convert(EASTERN)


# ── Extract: hvac_action from Home Assistant ──────────────────────────────

def fetch_hvac_action(climate_entity: str, start_utc: datetime, end_utc: datetime,
                      batch_days: int = 7) -> pd.Series:
    """
    Returns the raw hvac_action event series ('cooling'/'idle'/...) for one
    climate entity, Eastern-tz index. Queries the HA history REST API in
    batches to keep responses small.
    """
    headers = {'Authorization': f"Bearer {os.environ['HA_TOKEN']}"}
    frames = []
    batch_start = start_utc
    while batch_start < end_utc:
        batch_end = min(batch_start + timedelta(days=batch_days), end_utc)
        url = f"{os.environ['HA_URL']}/api/history/period/{batch_start.isoformat()}"
        r = requests.get(
            url,
            params={'filter_entity_id': climate_entity,
                    'end_time': batch_end.isoformat()},
            headers=headers,
            timeout=60,
        )
        r.raise_for_status()
        data = r.json()
        if data and data[0]:
            rows = data[0]
            frames.append(pd.Series(
                [row.get('attributes', {}).get('hvac_action') for row in rows],
                index=pd.to_datetime([row['last_changed'] for row in rows],
                                     format='ISO8601'),
            ))
        batch_start = batch_end
    if not frames:
        raise RuntimeError(f"No HA history for {climate_entity}")
    s = pd.concat(frames)
    s = s[~s.index.duplicated(keep='last')].sort_index()
    return s.tz_convert(EASTERN)


def cooling_flag(actions: pd.Series, grid: pd.DatetimeIndex, resample: str) -> pd.Series:
    """hvac_action events → 0/1 cooling flag on the comparison grid."""
    flag = (actions == 'cooling').astype(int)
    return flag.resample(resample).last().ffill().reindex(grid).ffill()


# ── Compare ───────────────────────────────────────────────────────────────

def mismatch_episodes(mask: pd.Series, min_slots: int) -> list:
    """Consecutive runs of True lasting >= min_slots, longest first."""
    groups = (mask != mask.shift()).cumsum()
    episodes = []
    for _, chunk in mask[mask].groupby(groups):
        if len(chunk) >= min_slots:
            episodes.append((chunk.index[0], chunk.index[-1], len(chunk)))
    return sorted(episodes, key=lambda x: -x[2])


def compare_unit(ac_id: str, cfg: dict, power: pd.DataFrame,
                 actions: pd.Series, resample: str) -> pd.DataFrame:
    df = pd.DataFrame(index=power.index)
    # all circuits must have data for the slot, otherwise total is undercounted
    df['power_w'] = power[cfg['power_entities']].dropna().sum(axis=1)
    df['cooling'] = cooling_flag(actions, power.index, resample)
    df = df[df['cooling'].notna()]
    df['cooling'] = df['cooling'].astype(int)
    # slots with no power data are kept for coverage reporting but never
    # counted as mismatches
    df['power_gap'] = df['power_w'].isna().astype(int)
    df['power_on'] = (df['power_w'] > cfg['on_threshold_w']).astype(int)
    # slots adjacent to a status transition: disagreement there is just
    # compressor spin-up/down lag, not a real inconsistency
    transition = df['cooling'].diff().abs().fillna(0)
    df['near_transition'] = ((transition + transition.shift(-1).fillna(0)) > 0).astype(int)
    df['mismatch'] = ((df['power_on'] != df['cooling'])
                      & (df['power_gap'] == 0)).astype(int)
    return df


def report_unit(ac_id: str, cfg: dict, df: pd.DataFrame, resample: str,
                episode_min: int) -> pd.DataFrame:
    n = len(df)
    valid = df[df['power_gap'] == 0]
    steady = valid[valid['near_transition'] == 0]
    agree_all = 1 - valid['mismatch'].mean() if len(valid) else float('nan')
    agree_steady = 1 - steady['mismatch'].mean() if len(steady) else float('nan')

    cooling_no_power = steady[(steady['cooling'] == 1) & (steady['power_on'] == 0)]
    power_no_cooling = steady[(steady['cooling'] == 0) & (steady['power_on'] == 1)]

    print(f"\n{'─' * 68}")
    print(f"{ac_id}  ({cfg['climate_entity']})")
    print(f"  power circuits : {', '.join(cfg['power_entities'])}"
          f"  (ON if > {cfg['on_threshold_w']} W)")
    print(f"  slots compared : {len(valid)} × {resample} "
          f"({df['power_gap'].mean():.0%} of {n} dropped: no power data)   "
          f"cooling duty: {df['cooling'].mean():.0%}")
    print(f"  agreement      : {agree_all:.1%} overall, "
          f"{agree_steady:.1%} excluding transition slots")
    print(f"  inconsistencies (steady-state only):")
    print(f"    status=cooling but power off : {len(cooling_no_power):4d} slots "
          f"({len(cooling_no_power) / max(len(steady), 1):.1%})")
    print(f"    power on but status=idle     : {len(power_no_cooling):4d} slots "
          f"({len(power_no_cooling) / max(len(steady), 1):.1%})")
    mean_on = df.loc[df['cooling'] == 1, 'power_w'].mean()
    mean_off = df.loc[df['cooling'] == 0, 'power_w'].mean()
    print(f"  mean power     : {mean_on:6.0f} W while cooling, "
          f"{mean_off:6.0f} W while idle")

    steady_mismatch = (df['mismatch'] == 1) & (df['near_transition'] == 0)
    episodes = mismatch_episodes(steady_mismatch, episode_min)
    if episodes:
        print(f"  longest mismatch episodes (>= {episode_min} consecutive slots):")
        for start, end, slots in episodes[:5]:
            kind = ('cooling/no-power' if df.loc[start, 'cooling'] == 1
                    else 'power/idle')
            print(f"    {start:%Y-%m-%d %H:%M} → {end:%H:%M %Z}  "
                  f"({slots} slots, {kind})")
    else:
        print(f"  no mismatch episode of >= {episode_min} consecutive slots — "
              f"power and status are in line")

    out = df[df['mismatch'] == 1].copy()
    out.insert(0, 'ac_id', ac_id)
    return out


# ── Main ──────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description='Compare AC power draw (InfluxDB) with thermostat '
                    'cooling status (Home Assistant). Dates are US/Eastern.')
    parser.add_argument('--start-date', required=True,
                        help='Start date YYYY-MM-DD (US/Eastern)')
    parser.add_argument('--end-date', default=None,
                        help='End date YYYY-MM-DD exclusive (default: now)')
    parser.add_argument('--resample', default='5min',
                        help='Comparison grid (default: 5min)')
    parser.add_argument('--episode-min-slots', type=int, default=3,
                        help='Min consecutive mismatch slots to report as an '
                             'episode (default: 3)')
    parser.add_argument('--csv', default=None,
                        help='Optional path to write all mismatch slots as CSV')
    args = parser.parse_args()

    load_dotenv('.env')
    load_dotenv('thermal_control/.env')

    start_local = EASTERN.localize(datetime.strptime(args.start_date, '%Y-%m-%d'))
    if args.end_date:
        end_local = EASTERN.localize(datetime.strptime(args.end_date, '%Y-%m-%d'))
    else:
        end_local = datetime.now(EASTERN)
    start_utc = start_local.astimezone(pytz.UTC)
    end_utc = end_local.astimezone(pytz.UTC)

    print(f"Window: {start_local:%Y-%m-%d %H:%M %Z} → {end_local:%Y-%m-%d %H:%M %Z}"
          f"  (grid: {args.resample})")

    power = fetch_power(start_utc, end_utc, args.resample)
    print(f"Power: {len(power)} rows from InfluxDB "
          f"({power.index[0]:%Y-%m-%d %H:%M} → {power.index[-1]:%H:%M %Z})")

    all_mismatches = []
    for ac_id, cfg in AC_UNITS.items():
        actions = fetch_hvac_action(cfg['climate_entity'], start_utc, end_utc)
        df = compare_unit(ac_id, cfg, power, actions, args.resample)
        all_mismatches.append(report_unit(ac_id, cfg, df, args.resample,
                                          args.episode_min_slots))

    if args.csv:
        out = pd.concat(all_mismatches).sort_index()
        out.index.name = 'time_eastern'
        out.to_csv(args.csv)
        print(f"\nWrote {len(out)} mismatch slots to {args.csv}")


if __name__ == '__main__':
    main()
