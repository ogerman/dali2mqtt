"""Keep the lamp levels and colours dali2mqtt reports in sync with the bus.

Two sources feed one model:

* bus traffic, i.e. our own commands and, on drivers that can sniff, the
  commands of other masters (wall switches, sensors, other controllers).
  They give immediate optimistic updates and mark the affected lamps for a
  confirming ``QueryActualLevel`` once their fade is over;
* a slow periodic poll of every lamp, which catches whatever the bus never
  showed us: missed frames, power cycles, time spent offline.

Colour gear is re-read in the same way. Colour commands dirty its colour,
and so do level commands when the gear activates colour automatically with
them (IEC 62386-209, 9.12.5), or a scene recall, which always carries colour.

Groups are never queried, their level and colour are derived from the members.

Gear can miss a command: on the real bus living-room-rgb ignored a DAPC
sent while the poll was busy. When the confirming query after one of our
own level commands reads something else, the command is sent again, up to
MAX_RESENDS times, instead of taking the old level as the truth. A command
from another master in between cancels that.
"""
import logging
import threading
import time

import dali.gear.general as gear
from dali.address import GearBroadcast, GearBroadcastUnaddressed, GearGroup, GearShort

from dali2mqtt import colour

logger = logging.getLogger(__name__)

# IEC 62386-102 fade time codes in seconds; 0 means no fade.
FADE_TIME_SECONDS = {
    0: 0.0, 1: 0.7, 2: 1.0, 3: 1.4, 4: 2.0, 5: 2.8, 6: 4.0, 7: 5.7,
    8: 8.0, 9: 11.3, 10: 16.0, 11: 22.6, 12: 32.0, 13: 45.3, 14: 64.0, 15: 90.5,
}
# Fades that make the lamp move for an unknown time are re-checked after this.
STEP_SETTLE = 0.5

# Commands that change a lamp's level.
LEVEL_COMMANDS = frozenset(
    [
        "DAPC", "Off", "Up", "Down", "StepUp", "StepDown",
        "RecallMaxLevel", "RecallMinLevel", "StepDownAndOff", "OnAndStepUp",
        "EnableDAPCSequence", "GoToLastActiveLevel", "GoToScene",
        "ContinuousUp", "ContinuousDown",
    ]
)
# Commands that change a colour gear's colour on their own.
COLOUR_COMMANDS = frozenset(
    [
        "Activate",
        "XCoordinateStepUp", "XCoordinateStepDown",
        "YCoordinateStepUp", "YCoordinateStepDown",
        "ColourTemperatureTcStepCooler", "ColourTemperatureTcStepWarmer",
    ]
)
MASK = 255
# Own level commands confirmed by a query: how often to resend a missed one,
# and how long the expectation lasts.
MAX_RESENDS = 2
EXPECT_WINDOW = 15.0


class LevelSync:
    """Level model fed by bus traffic and polling.

    ``publish(lamp)`` is called whenever a lamp's or group's level changed.
    """

    def __init__(self, bus, publish=None, poll_interval=10, fade_margin=0.5):
        self._bus = bus
        self.publish = publish or (lambda lamp: None)
        self.poll_interval = poll_interval
        self.fade_margin = fade_margin
        self._lock = threading.RLock()
        self._wakeup = threading.Condition(self._lock)
        self._lamps = {}  # short address -> Lamp
        self._groups = {}  # group number -> Lamp
        self._dirty = {}  # short address -> monotonic time of the confirming query
        self._colour_dirty = set()  # short addresses whose colour is re-read too
        # short address -> [level, resends so far, monotonic deadline] of our own
        # last level command, until a confirming query agrees.
        self._expected = {}
        self._next_poll = None
        self._thread = None
        self._stopping = False
        bus.add_listener(self.on_command)

    # Lamp set

    def set_lamps(self, all_lamps):
        """Replace the lamps being tracked, typically after a bus scan."""
        with self._lock:
            lamps = list(all_lamps.values())
            self._lamps = {
                lamp.short_address.address: lamp for lamp in lamps if not lamp.is_group
            }
            self._groups = {
                lamp.short_address.group: lamp for lamp in lamps if lamp.is_group
            }
            self._dirty.clear()
            self._colour_dirty.clear()
            self._expected.clear()
            for group in self._groups.values():
                self._update_group(group)
                self._update_group_colour(group)
            self._wakeup.notify_all()
        logger.debug(
            "Level sync tracking %d lamps and %d groups", len(self._lamps), len(self._groups)
        )

    # Lifecycle

    def start(self):
        """Start the background poller."""
        with self._lock:
            self._stopping = False
            if self.poll_interval:
                self._next_poll = time.monotonic() + self.poll_interval
        self._thread = threading.Thread(target=self._run, name="level-sync", daemon=True)
        self._thread.start()

    def stop(self):
        """Stop the background poller."""
        with self._lock:
            self._stopping = True
            self._wakeup.notify_all()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    # Bus traffic

    def on_command(self, command, own):
        """Apply a command seen on the bus to the model.

        ``command=None`` means a frame went by unread: every lamp is re-checked.
        """
        if command is None:
            with self._lock:
                self._mark_dirty(self._lamps.values(), STEP_SETTLE, with_colour=True)
            logger.debug("Unreadable frame on the bus, re-checking all lamps")
            return
        name = type(command).__name__
        if name in COLOUR_COMMANDS:
            with self._lock:
                targets = [
                    lamp
                    for lamp in self._resolve(getattr(command, "destination", None))
                    if lamp.colour is not None
                ]
                self._mark_dirty(targets, with_colour=True)
        elif name in LEVEL_COMMANDS:
            with self._lock:
                targets = self._resolve(getattr(command, "destination", None))
                if not targets:
                    return
                for lamp in targets:
                    level = self._optimistic_level(command, lamp)
                    if level is not None:
                        self._set_level(lamp, level)
                    self._expect(lamp, level if own else None)
                self._mark_dirty(targets)
                # A level command also applies pending colour on gear that
                # activates automatically; a scene always carries a colour.
                self._colour_dirty.update(
                    lamp.short_address.address
                    for lamp in targets
                    if lamp.colour is not None
                    and (lamp.colour.auto_activation or name == "GoToScene")
                )
        else:
            return
        if not targets:
            return
        logger.debug(
            "%s %s affects %s", "Sent" if own else "Sniffed", command,
            [lamp.friendly_name for lamp in targets],
        )

    def _expect(self, lamp, level):
        """Remember the level our own command should leave the lamp at.

        ``level=None`` forgets it: another master changed the lamp, or the
        command's outcome can't be predicted.
        """
        addr = lamp.short_address.address
        if level is None:
            self._expected.pop(addr, None)
            return
        deadline = time.monotonic() + EXPECT_WINDOW
        known = self._expected.get(addr)
        if known is not None and known[0] == level:
            known[2] = deadline  # a resend, or the same level again
        else:
            self._expected[addr] = [level, 0, deadline]

    def _check_expected(self, lamp, level):
        """Return True when ``level`` read back means our command was missed.

        Counts the resend; gives up after MAX_RESENDS and accepts the reading.
        """
        addr = lamp.short_address.address
        known = self._expected.get(addr)
        if known is None:
            return False
        wanted, resends, deadline = known
        if level == wanted or time.monotonic() > deadline:
            del self._expected[addr]
            return False
        if resends >= MAX_RESENDS:
            del self._expected[addr]
            logger.warning(
                "%s stays at level %s after %d resends of level %s",
                lamp.friendly_name, level, resends, wanted,
            )
            return False
        known[1] += 1
        logger.info(
            "%s missed level %s (reads %s), sending it again (%d/%d)",
            lamp.friendly_name, wanted, level, known[1], MAX_RESENDS,
        )
        return True

    def _mark_dirty(self, lamps, settle=None, with_colour=False):
        """Schedule a confirming query once the lamps' fades are over."""
        now = time.monotonic()
        for lamp in lamps:
            wait = settle
            if wait is None:
                wait = lamp.fade_time if lamp.fade_time is not None else STEP_SETTLE
            self._dirty[lamp.short_address.address] = now + wait + self.fade_margin
            if with_colour and lamp.colour is not None:
                self._colour_dirty.add(lamp.short_address.address)
        self._wakeup.notify_all()

    def _resolve(self, destination):
        if isinstance(destination, GearShort):
            lamp = self._lamps.get(destination.address)
            return [lamp] if lamp is not None else []
        if isinstance(destination, GearGroup):
            group = self._groups.get(destination.group)
            if group is None:
                return []
            return [m for m in group.members if m.short_address.address in self._lamps]
        if isinstance(destination, (GearBroadcast, GearBroadcastUnaddressed)):
            return list(self._lamps.values())
        return []

    @staticmethod
    def _optimistic_level(command, lamp):
        """Level the lamp will end up at, when it follows from the command alone."""
        name = type(command).__name__
        if name == "DAPC":
            level = command.power
            if level == MASK:
                return None  # stops a fade wherever it is
            if level == 0:
                return 0
            return min(max(level, lamp.min_level), lamp.max_level)
        if name == "Off":
            return 0
        if name == "RecallMaxLevel":
            return lamp.max_level
        if name == "RecallMinLevel":
            return lamp.min_level
        return None

    # Model

    def _set_level(self, lamp, level):
        if lamp.update_level(level):
            self.publish(lamp)
        for group in self._groups.values():
            if any(member is lamp for member in group.members):
                self._update_group(group)

    def _update_group(self, group):
        """A group is on when any member is on; its level is the mean of those."""
        levels = [m.level for m in group.members if isinstance(m.level, int) and m.level > 0]
        level = round(sum(levels) / len(levels)) if levels else 0
        if group.update_level(level):
            self.publish(group)

    def _set_colour(self, lamp, state):
        if lamp.update_colour(state):
            self.publish(lamp)
        for group in self._groups.values():
            if any(member is lamp for member in group.members):
                self._update_group_colour(group)

    def _update_group_colour(self, group):
        """A group shows a colour when all its members show the same one."""
        if group.colour is None or not group.members:
            return
        states = [member.colour_state() for member in group.members]
        if any(state != states[0] for state in states):
            return
        if group.update_colour(states[0]):
            self.publish(group)

    # Bus queries (never called with the model lock held)

    def refresh(self, lamp, with_colour=False):
        """Query the lamp's actual level, and colour if asked, and apply them.

        Returns the level or None.
        """
        if lamp.is_group:
            return None
        try:
            response = self._bus.send(gear.QueryActualLevel(lamp.short_address))
        except Exception as err:  # pylint: disable=broad-except
            logger.warning("Can't query level of %s: %s", lamp.friendly_name, err)
            return None
        level = getattr(response, "value", None)
        resend = None
        if not isinstance(level, int) or level == MASK:
            logger.debug("No usable level from %s: %s", lamp.friendly_name, response)
            level = None
        else:
            with self._lock:
                if self._check_expected(lamp, level):
                    resend = self._expected[lamp.short_address.address][0]
                else:
                    self._set_level(lamp, level)
        if resend is not None:
            if resend:
                command = gear.DAPC(lamp.short_address, resend)
            else:
                command = gear.Off(lamp.short_address)
            try:
                self._bus.send(command)  # on_command schedules the next check
            except Exception as err:  # pylint: disable=broad-except
                logger.warning("Can't resend level to %s: %s", lamp.friendly_name, err)
        if with_colour and lamp.colour is not None:
            state = colour.read_colour_state(self._bus, lamp.short_address)
            with self._lock:
                self._set_colour(lamp, state)
        return level

    def _load_fade_time(self, lamp):
        try:
            response = self._bus.send(gear.QueryFadeTimeFadeRate(lamp.short_address))
            code = response.fade_time
        except Exception as err:  # pylint: disable=broad-except
            logger.debug("Can't read fade time of %s: %s", lamp.friendly_name, err)
            code = 0
        lamp.fade_time = FADE_TIME_SECONDS.get(code, 0.0)
        logger.debug("%s fade time %.1fs", lamp.friendly_name, lamp.fade_time)

    # Worker

    def _due(self):
        """Return (lamps needing their fade time, [(lamp, with_colour)], full poll?).

        Waits for the next deadline when there is nothing to do.
        """
        now = time.monotonic()
        unknown_fade = [lamp for lamp in self._lamps.values() if lamp.fade_time is None]
        if self.poll_interval and self._next_poll is not None and now >= self._next_poll:
            self._next_poll = now + self.poll_interval
            self._dirty.clear()
            self._colour_dirty.clear()
            return unknown_fade, [(lamp, True) for lamp in self._lamps.values()], True
        due = [addr for addr, when in self._dirty.items() if when <= now]
        for addr in due:
            del self._dirty[addr]
        lamps = [
            (self._lamps[addr], addr in self._colour_dirty) for addr in due if addr in self._lamps
        ]
        self._colour_dirty.difference_update(due)
        if lamps or unknown_fade:
            return unknown_fade, lamps, False
        deadlines = list(self._dirty.values())
        if self.poll_interval and self._next_poll is not None:
            deadlines.append(self._next_poll)
        timeout = max(0.0, min(deadlines) - now) if deadlines else None
        self._wakeup.wait(timeout)
        return [], [], False

    def _run(self):
        while True:
            with self._lock:
                if self._stopping:
                    return
                unknown_fade, lamps, full_poll = self._due()
            for lamp in unknown_fade:
                self._load_fade_time(lamp)
            started = time.monotonic()
            for lamp, with_colour in lamps:
                with self._lock:
                    if self._stopping:
                        return
                self.refresh(lamp, with_colour)
            if full_poll:
                logger.debug(
                    "Polled %d lamps in %.1fs", len(lamps), time.monotonic() - started
                )
            elif lamps:
                logger.debug("Re-checked %s", [lamp.friendly_name for lamp, _ in lamps])
