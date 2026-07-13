"""
card_sync.py
────────────
Keeps HA thermostat cards (item 7b) in sync with the resolved schedule/away
band, detects user edits into manual overrides (items 7/7b), and polls the
user-controllable inputs during the inter-tick sleep so an app edit takes
effect within poll_seconds instead of waiting for the next full tick.
"""

import time

from thermal_control.ha_bridge import controller as ha


def input_signature(house, control):
    """
    Cheap snapshot of the user-controllable inputs the HA app changes — the
    per-room thermostat targets (items 7/7b) and the away toggle (item 8).
    Compared during the inter-tick sleep so the loop can wake early when the user
    drags a card instead of waiting up to a full tick. The baseline is captured
    at the end of a tick (after the scheduler's own write-backs), so a steady
    state reads stable and only a real user edit trips it. Fails safe so a
    transient HA glitch at most causes one harmless extra tick.
    """
    try:
        targets = ha.get_room_targets(house)
    except Exception:
        targets = {}
    return (tuple(sorted(targets.items())), ha.get_away_mode(control))


def responsive_sleep(sleep_s, poll_s, house, control, baseline):
    """
    Sleep up to sleep_s, waking early (within poll_s) if a user input changes
    vs `baseline`. Returns True when woken early by a change, False on timeout.
    """
    deadline = time.monotonic() + sleep_s
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(poll_s, remaining))
        if time.monotonic() >= deadline:
            return False
        if input_signature(house, control) != baseline:
            return True


def plan_card_detection(raw_targets, display_target, card_synced, away):
    """
    Reconcile per-room thermostat cards (item 7b) against what the scheduler last
    wrote (``card_synced``) and detect user edits. Pure: returns the writes/edits
    to act on; the caller performs the HA writes and updates ``card_synced``.

      raw_targets    : {room: target_f} read from the cards this tick (absent =
                       unavailable).
      display_target : {room: upper_bound_f} the card should show — the away band
                       max when away, else the scheduled band max.
      card_synced    : {room: last_value_the_scheduler_wrote}; room absent = card
                       not yet seeded.

    Returns (detected, writes):
      detected : {room: target_f} home-mode user edits (candidate overrides).
      writes   : {room: target_f} cards to (re)write now — first-tick seeding and
                 away-mode pinning (away beats override, so edits are ignored and
                 the card is held on the away target).
    """
    detected, writes = {}, {}
    for room, S in display_target.items():
        T    = raw_targets.get(room)
        last = card_synced.get(room)
        if last is None:                       # first sight → seed to schedule/away
            writes[room] = S
        elif away:                             # away pins the card; ignore edits
            if T != S:
                writes[room] = S
        elif T is not None and T != last:      # user dragged the card → override
            detected[room] = T
    return detected, writes


def plan_card_revert(raw_targets, display_target, card_synced, effective):
    """
    Home-mode follow-up to plan_card_detection: keep cards for rooms *not* under
    an active override pinned to the schedule — resyncing on a schedule
    transition and reverting on override expiry/cancel. Returns {room: target_f}
    writes to apply (caller updates ``card_synced``).
    """
    writes = {}
    for room, S in display_target.items():
        if room in effective:                  # leave the user's value showing
            continue
        T = raw_targets.get(room)
        if card_synced.get(room) != S or (T is not None and T != S):
            writes[room] = S
    return writes
