"""Light platform for the Govee H601E integration.

Three :class:`~homeassistant.components.light.LightEntity` instances are
created per physical lamp:

``GoveeMainLight``  (``light.<name>``)
    Master power switch for the entire lamp.  Turning it on/off affects both
    the centre panel and the outer ring.

``GoveeCenterLight``  (``light.<name>_center``)
    Controls the centre diffuser panel.  Supports:
    * Brightness     (0–100 %, mapped from HA 0–255)
    * Colour temperature  (2 200–6 500 K)
    * RGB colour

``GoveeRingLight``  (``light.<name>_ring``)
    Controls the outer RGB ring via the H601E DIY protocol (cmdType=0x50).
    Supports:
    * Brightness     (global, same hardware register as centre)
    * RGB colour     (colour temperature is not supported by the ring protocol)
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_COLOR_TEMP_KELVIN,
    ATTR_EFFECT,
    ATTR_RGB_COLOR,
    ColorMode,
    LightEntity,
    LightEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    CONF_MAC,
    DOMAIN,
    KELVIN_MAX,
    KELVIN_MIN,
    MANUFACTURER,
    MODEL,
    RING_EFFECT_SOLID,
    RING_EFFECTS,
    SUFFIX_CENTER,
    SUFFIX_RING,
)
from .coordinator import GoveeCoordinator
from .govee.device import (
    LightColorMode,
    brightness_ha_to_pct,
    brightness_pct_to_ha,
    snap_kelvin,
)

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the three light entities for a config entry.

    Called by HA when the platform is set up.  Retrieves the coordinator from
    ``hass.data`` and creates the three entity objects.

    Args:
        hass:               Home Assistant instance.
        entry:              Config entry for this lamp.
        async_add_entities: Callback to register the new entities with HA.
    """
    coordinator: GoveeCoordinator = hass.data[DOMAIN][entry.entry_id]
    name: str = entry.data.get("name", "Govee H601E")

    async_add_entities(
        [
            GoveeMainLight(coordinator, entry, name),
            GoveeCenterLight(coordinator, entry, name),
            GoveeRingLight(coordinator, entry, name),
        ]
    )


# ── Shared device-registry info ────────────────────────────────────────────────

def _device_info(entry: ConfigEntry, name: str) -> DeviceInfo:
    """Build the shared :class:`DeviceInfo` used by all three entities.

    All three lights (main, centre, ring) belong to the same HA device so that
    they appear together on one device card.

    Args:
        entry: Config entry.
        name:  User-assigned display name.

    Returns:
        DeviceInfo for the device registry.
    """
    return DeviceInfo(
        identifiers={(DOMAIN, entry.data[CONF_MAC])},
        name=name,
        manufacturer=MANUFACTURER,
        model=MODEL,
        sw_version=None,
    )


# ═════════════════════════════════════════════════════════════════════════════
# Base entity
# ═════════════════════════════════════════════════════════════════════════════

class _GoveeBaseLight(LightEntity):
    """Shared base class for all three Govee H601E light entities.

    Handles coordinator subscription and availability tracking.
    """

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(
        self,
        coordinator: GoveeCoordinator,
        entry: ConfigEntry,
        name: str,
    ) -> None:
        """Initialise the base entity.

        Args:
            coordinator: Coordinator managing this lamp's BLE connection.
            entry:       Config entry.
            name:        User-assigned display name of the lamp.
        """
        self._coordinator = coordinator
        self._entry = entry
        self._lamp_name = name
        self._remove_callback: Callable[[], None] | None = None

    @property
    def available(self) -> bool:
        """Return ``True`` when the coordinator has an active session."""
        return self._coordinator.available

    @property
    def device_info(self) -> DeviceInfo:
        """Return device registry info shared by all three entities."""
        return _device_info(self._entry, self._lamp_name)

    async def async_added_to_hass(self) -> None:
        """Subscribe to coordinator state updates when the entity is added."""
        self._remove_callback = self._coordinator.register_update_callback(
            self.async_write_ha_state
        )

    async def async_will_remove_from_hass(self) -> None:
        """Unsubscribe from coordinator state updates."""
        if self._remove_callback is not None:
            self._remove_callback()
            self._remove_callback = None


# ═════════════════════════════════════════════════════════════════════════════
# Main light  (on/off only)
# ═════════════════════════════════════════════════════════════════════════════

class GoveeMainLight(_GoveeBaseLight):
    """Master on/off light entity for the Govee H601E.

    Turning this light on or off sends the global power command which affects
    both the centre panel and the outer ring simultaneously.
    """

    _attr_color_mode = ColorMode.ONOFF
    _attr_supported_color_modes = {ColorMode.ONOFF}

    def __init__(
        self,
        coordinator: GoveeCoordinator,
        entry: ConfigEntry,
        name: str,
    ) -> None:
        """Initialise the main-light entity."""
        super().__init__(coordinator, entry, name)
        self._attr_unique_id = entry.data[CONF_MAC]
        self._attr_name = None  # Uses the device name directly

    @property
    def is_on(self) -> bool:
        """Return ``True`` when the lamp is powered on."""
        return self._coordinator.state.is_on

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the lamp on.

        Any extra keyword arguments (brightness, colour) are ignored here;
        those are handled by the centre/ring entities.
        """
        await self._coordinator.async_turn_on()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the lamp off."""
        await self._coordinator.async_turn_off()


# ═════════════════════════════════════════════════════════════════════════════
# Centre panel light
# ═════════════════════════════════════════════════════════════════════════════

class GoveeCenterLight(_GoveeBaseLight):
    """Light entity for the Govee H601E centre diffuser panel.

    Supports brightness (0–255 mapped from 0–100 % Govee scale),
    colour temperature (2 200–6 500 K) and RGB colour.

    Notes
    -----
    Brightness is physically shared with the outer ring on the H601E hardware.
    Setting brightness via this entity updates the global brightness register
    and is reflected on the ring as well.
    """

    _attr_supported_color_modes = {ColorMode.COLOR_TEMP, ColorMode.RGB}
    _attr_min_color_temp_kelvin = KELVIN_MIN
    _attr_max_color_temp_kelvin = KELVIN_MAX

    def __init__(
        self,
        coordinator: GoveeCoordinator,
        entry: ConfigEntry,
        name: str,
    ) -> None:
        """Initialise the centre-panel light entity."""
        super().__init__(coordinator, entry, name)
        self._attr_unique_id = f"{entry.data[CONF_MAC]}{SUFFIX_CENTER}"
        self._attr_name = "Centre"

    # ── State properties ───────────────────────────────────────────────────────

    @property
    def is_on(self) -> bool:
        """Return ``True`` when the centre panel is on."""
        return self._coordinator.state.center.is_on

    @property
    def brightness(self) -> int | None:
        """Return brightness in HA scale (0–255)."""
        pct = self._coordinator.state.center.brightness_pct
        return brightness_pct_to_ha(pct)

    @property
    def color_mode(self) -> ColorMode:
        """Return the active colour mode."""
        mode = self._coordinator.state.center.color_mode
        if mode == LightColorMode.COLOR_TEMP:
            return ColorMode.COLOR_TEMP
        if mode == LightColorMode.RGB:
            return ColorMode.RGB
        return ColorMode.COLOR_TEMP  # Sensible default

    @property
    def color_temp_kelvin(self) -> int | None:
        """Return active colour temperature in Kelvin."""
        return self._coordinator.state.center.color_temp_kelvin

    @property
    def rgb_color(self) -> tuple[int, int, int] | None:
        """Return active RGB colour tuple."""
        return self._coordinator.state.center.rgb

    # ── Command handlers ───────────────────────────────────────────────────────

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the centre panel on, optionally setting brightness and colour.

        Args:
            **kwargs: Standard HA light service call attributes:

                * ``ATTR_BRIGHTNESS`` (0–255) – maps to Govee 0–100 %.
                * ``ATTR_COLOR_TEMP_KELVIN`` (2200–6500) – switches to CT mode.
                * ``ATTR_RGB_COLOR`` (R, G, B) – switches to RGB mode.
        """
        # If the lamp is off, power it on first (sets center.is_on via coordinator)
        if not self._coordinator.state.is_on:
            await self._coordinator.async_turn_on()

        # Mark centre as on unconditionally – it may have been False if HA was
        # restarted while the lamp was already on (optimistic state initialises
        # to False).  async_turn_on above covers the power-off case; this line
        # covers the "already on, just change a parameter" case.
        if not self._coordinator.state.center.is_on:
            self._coordinator.state.center.is_on = True

        if ATTR_BRIGHTNESS in kwargs:
            pct = brightness_ha_to_pct(kwargs[ATTR_BRIGHTNESS])
            await self._coordinator.async_set_brightness(pct)

        if ATTR_COLOR_TEMP_KELVIN in kwargs:
            kelvin = snap_kelvin(kwargs[ATTR_COLOR_TEMP_KELVIN])
            await self._coordinator.async_set_center_color_temp(kelvin)

        elif ATTR_RGB_COLOR in kwargs:
            r, g, b = kwargs[ATTR_RGB_COLOR]
            await self._coordinator.async_set_center_rgb(r, g, b)

        else:
            # No colour attribute: just propagate the is_on state to listeners
            self._coordinator.notify_listeners()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the entire lamp off (shared power register)."""
        await self._coordinator.async_turn_off()


# ═════════════════════════════════════════════════════════════════════════════
# Ring light
# ═════════════════════════════════════════════════════════════════════════════

class GoveeRingLight(_GoveeBaseLight):
    """Light entity for the Govee H601E outer RGB ring.

    The ring is controlled via the H601E DIY protocol (cmdType=0x50) and
    supports only RGB colour.  Colour temperature is not available for the
    ring segment.

    Brightness is physically shared with the centre panel; setting it here
    updates the global brightness register and affects both zones.
    """

    _attr_supported_color_modes = {ColorMode.RGB}
    _attr_color_mode = ColorMode.RGB
    _attr_supported_features = LightEntityFeature.EFFECT

    def __init__(
        self,
        coordinator: GoveeCoordinator,
        entry: ConfigEntry,
        name: str,
    ) -> None:
        """Initialise the ring-light entity."""
        super().__init__(coordinator, entry, name)
        self._attr_unique_id = f"{entry.data[CONF_MAC]}{SUFFIX_RING}"
        self._attr_name = "Ring"

    # ── State properties ───────────────────────────────────────────────────────

    @property
    def is_on(self) -> bool:
        """Return ``True`` when the ring is on (follows master power state)."""
        return self._coordinator.state.ring.is_on

    @property
    def brightness(self) -> int | None:
        """Return brightness in HA scale (0–255).

        Reflects the global brightness register shared with the centre panel.
        """
        pct = self._coordinator.state.ring.brightness_pct
        return brightness_pct_to_ha(pct)

    @property
    def rgb_color(self) -> tuple[int, int, int] | None:
        """Return the active RGB colour of the ring."""
        return self._coordinator.state.ring.rgb

    @property
    def effect_list(self) -> list[str]:
        """Return the list of supported ring effects."""
        return RING_EFFECTS

    @property
    def effect(self) -> str | None:
        """Return the currently active ring effect, or ``None`` for solid colour."""
        return self._coordinator.state.ring.effect

    # ── Command handlers ───────────────────────────────────────────────────────

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the ring on, optionally setting brightness, colour, and effect.

        Args:
            **kwargs: Standard HA light service call attributes:

                * ``ATTR_BRIGHTNESS`` (0–255) – maps to Govee 0–100 %.
                * ``ATTR_RGB_COLOR`` (R, G, B) – sets ring colour.
                * ``ATTR_EFFECT`` – activates a named ring effect.
        """
        if not self._coordinator.state.is_on:
            await self._coordinator.async_turn_on()

        if ATTR_BRIGHTNESS in kwargs:
            pct = brightness_ha_to_pct(kwargs[ATTR_BRIGHTNESS])
            await self._coordinator.async_set_brightness(pct)

        effect = kwargs.get(ATTR_EFFECT)
        rgb = kwargs.get(ATTR_RGB_COLOR)

        if effect is not None:
            r, g, b = rgb if rgb is not None else (self._coordinator.state.ring.rgb or (255, 255, 255))
            await self._coordinator.async_set_ring_effect(effect, r, g, b)
        else:
            # Always send a ring colour command – the ring has no simple on/off
            # and needs an explicit 0x50 DIY frame to activate / confirm its colour.
            r, g, b = rgb if rgb is not None else (self._coordinator.state.ring.rgb or (255, 255, 255))
            await self._coordinator.async_set_ring_rgb(r, g, b)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the entire lamp off (shared power register)."""
        await self._coordinator.async_turn_off()
