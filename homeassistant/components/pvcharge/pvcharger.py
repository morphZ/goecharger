"""Logic and code to run pvcharge."""
from __future__ import annotations

from datetime import timedelta
import logging
from typing import Callable

from simple_pid import PID
from transitions.extensions.asyncio import AsyncMachine

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import (
    async_call_later,
    async_track_state_change,
    async_track_time_interval,
)

_LOGGER = logging.getLogger(__name__)

REFRESH_INTERVAL = timedelta(seconds=5)
MOVING_AVERAGE_WINDOW = 10
CONF_GRID_BALANCE_ENTITY = "input_number.grid_return"
CONF_PV_THRESHOLD = 0.5
CONF_PV_HYSTERESIS = 10.0
CONF_DEFAULT_MAX_TIME = timedelta(minutes=30)


class PVCharger:
    # pylint: disable=no-member
    """Finite state machine to control the PV charging."""

    states = ["off", "idle", "pv", "max", "low", "calendar"]
    transitions = [
        ["auto", ["off", "max", "low", "calendar"], "pv", "enough_power"],
        ["auto", ["off", "max", "low", "calendar"], "idle"],
        ["start", "idle", "pv"],
        ["pause", "pv", "idle"],
        ["boost", "*", "max"],
    ]

    def __init__(self, hass: HomeAssistant) -> None:
        """Set up PVCharger instance."""

        self.hass: HomeAssistant = hass

        self.machine = AsyncMachine(
            model=self,
            states=PVCharger.states,
            transitions=PVCharger.transitions,
            initial="off",
            queued=True,
        )

        self.pid = PID(
            -1.0, -0.1, 0.0, setpoint=0.0, sample_time=1, output_limits=(2.0, 11.0)
        )
        self.control = 0.0
        self.current = float(hass.states.get(CONF_GRID_BALANCE_ENTITY).state)  # type: ignore
        self.pid_interval = REFRESH_INTERVAL
        self.pid_handle: Callable | None = None
        self.timeisup_handle: Callable | None = None

        self.watch_handle = async_track_state_change(
            self.hass, CONF_GRID_BALANCE_ENTITY, self._async_watch_balance
        )

    async def go(self) -> None:
        """Start up the Charger depending on available power."""
        if self.enough_power:
            await self.to_pv()  # type: ignore
        else:
            await self.to_idle()  # type: ignore

    @callback
    async def _async_update_pid(self, event_time) -> None:
        """Update pid controller values."""
        _LOGGER.debug("Call _async_update_pid() callback at %s", event_time)
        self.control = self.pid(self.current)  # type: ignore
        _LOGGER.debug(
            "Data is self.current=%s, self.control=%s", self.current, self.control
        )

    @property
    def enough_power(self) -> bool:
        """Check if enough power is available."""
        threshold = (
            CONF_PV_THRESHOLD - CONF_PV_HYSTERESIS
            if self.is_pv()  # type: ignore
            else CONF_PV_THRESHOLD
        )
        return self.current > threshold

    @callback
    async def _async_watch_balance(self, entity, old_state, new_state) -> None:
        """Watch for grid balance and act accordingly."""
        _LOGGER.debug("Enter async_watch_balance callback")

        self.current = float(new_state.state)

        if self.is_pv() and not self.enough_power:  # type: ignore
            await self.pause()  # type: ignore

        if self.is_idle() and self.enough_power:  # type: ignore
            await self.start()  # type: ignore

    async def on_enter_pv(self, *args, **kwargs) -> None:
        """Start control loop for pv controlled charging."""

        self.pid_handle = async_track_time_interval(
            self.hass,
            self._async_update_pid,
            self.pid_interval,
        )

    async def on_exit_pv(self) -> None:
        """Stop pid loop."""

        if self.pid_handle is not None:
            self.pid_handle()
            self.pid_handle = None

    @callback
    async def _async_time_is_up(self, *args, **kwargs) -> None:
        self.timeisup_handle = None
        await self.auto()  # type: ignore

    async def on_enter_max(self, duration: timedelta = CONF_DEFAULT_MAX_TIME) -> None:
        """Load EV battery with maximal power."""

        self.timeisup_handle = async_call_later(
            self.hass, duration, self._async_time_is_up
        )

    async def on_exit_max(self) -> None:
        """Cancel pending timeouts."""

        if self.timeisup_handle is not None:
            self.timeisup_handle()
            self.timeisup_handle = None

    def close(self):
        """Close open callback handles."""
        _LOGGER.debug("Closing %s", self)
        if self.watch_handle is not None:
            self.watch_handle()
            self.pid_handle = None