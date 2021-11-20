"""Logic and code to run pvcharge."""
from __future__ import annotations

from datetime import timedelta
import logging
from typing import Any

from simple_pid import PID
from transitions.extensions.asyncio import AsyncMachine

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.helpers.event import (
    async_call_later,
    async_track_state_change,
    async_track_time_interval,
)

from .models import GoeStatus

_LOGGER = logging.getLogger(__name__)

AMP_MIN = 6.0
AMP_MAX = 32.0
POWER_MIN = 2.0
POWER_MAX = 11.0
SOC_MIN = 0.25
SOC_MID = 0.5


class PVCharger:
    # pylint: disable=no-member
    """Finite state machine to control the PV charging."""

    states = ["off", "loading", "boosting", "idle"]
    transitions = [
        ["run", ["off", "max", "idle"], "loading"],
        ["pause", "loading", "idle"],
        ["boost", ["load", "idle"], "boosting"],
        ["halt", "*", "off"],
    ]

    def __init__(
        self,
        hass: HomeAssistant,
        charger_host,
        duration,
        soc_entity,
        low_value,
        pid_interval,
    ) -> None:
        """Set up PVCharger instance."""

        self.hass = hass

        self._host = charger_host
        self.duration = duration
        self.soc_entity = soc_entity
        self.low_value = low_value
        self.pid_interval = pid_interval
        self.amp_min = AMP_MIN
        self.amp_max = AMP_MAX
        self._power_limits = (POWER_MIN, POWER_MAX)
        self._soc_limits = (SOC_MIN, SOC_MID)

        # Initialize basic controller variables
        self.control = self.amp_min
        self._session = async_create_clientsession(hass)
        self._base_url = f"http://{charger_host}"
        self._charge_power = 0.0
        self._status: GoeStatus | None = None

        try:
            self.soc = float(self.hass.states.get(self.soc_entity).state) / 100.0  # type: ignore
        except ValueError:
            self.soc = 0.48

        self._handles: dict[str, Any] = {}

        self.machine = AsyncMachine(
            model=self,
            states=PVCharger.states,
            transitions=PVCharger.transitions,
            initial="off",
            queued=True,
        )

        self.pid = PID(
            0.7,
            0.05,
            0.0,
            setpoint=7.5,
            sample_time=1,
            output_limits=(self.amp_min, self.amp_max),
        )

    @property
    def enough_power(self) -> bool:
        """Check if enough power is available."""

        if self.soc < 0.5:
            return True

        return False
        # threshold = (
        #     self.pv_threshold - self.pv_hysteresis
        #     if self.is_pv()  # type: ignore
        #     else self.pv_threshold
        # )

        # try:
        #     value = mean(self._balance_store)
        # except StatisticsError:
        #     value = self.current

        # return value > threshold

    def _calculate_setpoint(self):
        """Calculate power setpoint from soc."""

        soc_min, soc_mid = self._soc_limits
        p_min, p_max = self._power_limits
        grad = (p_max - p_min) / (soc_mid - soc_min)

        # Calculate setpoint with respect to SOC and state
        if self.is_boosting():
            power = p_max
        elif self.soc < soc_min:
            power = p_max
        elif self.soc < soc_mid:
            power = p_max - grad * (self.soc - soc_min)
        else:
            power = p_min

        return power

    async def _async_update_status(self):
        """Read power value of charger from API."""

        async with self._session.get(self._base_url + "/status") as res:
            status = await res.text()

        self._status = GoeStatus.parse_raw(status)
        self._charge_power = round(0.01 * self._status.nrg[11], 2)

    async def _async_watch_soc(
        self,
        entity,
        old_state,
        new_state,
    ) -> None:
        """Update internal soc variable."""
        self.soc = float(new_state.state) / 100.0

    async def _async_switch_charger(self, on: bool = True) -> None:
        """Switch go-e charger on or off via API call."""
        alw = 1 if on else 0

        async with self._session.get(f"{self._base_url}/mqtt?payload=alw={alw}") as res:
            _LOGGER.debug("Response of alw update request: %s", res)

    async def _async_update_control(self) -> None:
        """Update charging current via API call."""
        amp = round(self.control)
        async with self._session.get(f"{self._base_url}/mqtt?payload=amx={amp}") as res:
            _LOGGER.debug("Response of amp update request: %s", res)

    async def _async_update_pid(self, event_time) -> None:
        """Update pid controller values."""
        _LOGGER.debug("Call _async_update_pid() callback at %s", event_time)

        await self._async_update_status()

        # Check if state change is necessary
        if self.is_idle():  # type: ignore
            if self.enough_power:
                await self.run()  # type: ignore

            return  # in case of idle state we can exit here

        if self.is_loading() and not self.enough_power:  # type: ignore
            await self.pause()  # type: ignore
            return

        self.pid.setpoint = self._calculate_setpoint()
        self.control = self.pid(self._charge_power)  # type: ignore
        _LOGGER.debug(
            "Data is self._charge_power=%s, self.control=%s, self.pid.setpoint=%s",
            self._charge_power,
            self.control,
            self.pid.setpoint,
        )
        await self._async_update_control()

    async def on_exit_off(self) -> None:
        """Register callbacks when leaving off mode."""

        self._handles["soc"] = async_track_state_change(
            self.hass,
            [self.soc_entity],
            self._async_watch_soc,
        )

        self._handles["pid"] = async_track_time_interval(
            self.hass,
            self._async_update_pid,
            timedelta(seconds=self.pid_interval),
        )

        await self._async_switch_charger(True)

    async def on_enter_off(self) -> None:
        """Cancel callbacks when entering off mode."""
        for unsub in self._handles.values():
            unsub()

        self._handles = {}

        await self._async_switch_charger(False)

    async def on_enter_idle(self) -> None:
        """Switch off charger and PID controller."""
        await self._async_switch_charger(False)
        self.pid.set_auto_mode(False)

    async def on_exit_idle(self) -> None:
        """Switch on charger and PID controller."""
        await self._async_switch_charger(True)
        self.pid.set_auto_mode(True, self._power_limits[0])

    async def _async_time_is_up(self, *args, **kwargs) -> None:
        self._handles.pop("timer", None)
        await self.auto()  # type: ignore

    async def on_enter_boosting(
        self, duration: timedelta = timedelta(minutes=30)
    ) -> None:
        """Load EV battery with maximal power."""

        self._handles["timer"] = async_call_later(
            self.hass, duration, self._async_time_is_up
        )
        self.control = self.amp_max
        # await self._async_update_control()

        # if self.charge_switch:
        #     await self._async_turn_charge_switch(True)

    async def on_exit_boosting(self) -> None:
        """Cancel pending timeouts."""

        timer_handle = self._handles.pop("timer", None)
        if timer_handle is not None:
            timer_handle()
            timer_handle = None

        # if self.charge_switch:
        #     await self._async_turn_charge_switch(False)