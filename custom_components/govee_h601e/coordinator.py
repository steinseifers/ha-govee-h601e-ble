"""Govee H601E coordinator.

The coordinator is the central hub for each physical lamp registered in Home
Assistant.  It owns the BLE connection lifecycle, performs the cryptographic
handshake, sends commands and distributes state changes to the registered
light and switch entities.

Connection modes
----------------
``persistent``
    One BLE connection stays open indefinitely.  Notifications from the device
    (heartbeats, state-push frames) are processed in real time.  A keep-alive
    frame is sent every :data:`~const.HEARTBEAT_INTERVAL` seconds to prevent
    the device from timing out the connection.  Suitable for fast response
    times and proactive state tracking.

``on_demand``
    The BLE connection is opened only when a command needs to be sent.  After
    the handshake and command the connection is immediately closed.  Suitable
    for battery-constrained setups or when the Bluetooth radio is shared with
    many devices.

The active mode can be changed at runtime via the companion switch entity; the
coordinator tears down the existing connection and re-establishes it in the new
mode.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import timedelta
from time import monotonic
from typing import TYPE_CHECKING, Any

# BleakError is bleak's base exception class.  bleak is a hard dependency of
# homeassistant.components.bluetooth, so importing it here is acceptable in HA
# custom integrations – we are NOT creating BleakClient instances directly
# (that is done via bleak_retry_connector.establish_connection).
from bleak.exc import BleakError
from bleak_retry_connector import (
    BleakAbortedError,
    BleakClientWithServiceCache,
    BleakConnectionError,
    BleakNotFoundError,
    establish_connection,
)

if TYPE_CHECKING:
    # Only needed for type annotations; bleak_retry_connector returns a
    # BleakClientWithServiceCache (a BleakClient subclass) at runtime.
    from bleak import BleakClient

from homeassistant.components import bluetooth
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import async_track_time_interval

from .const import (
    CONNECTION_MODE_ON_DEMAND,
    CONNECTION_MODE_PERSISTENT,
    DOMAIN,
    GOVEE_NOTIFY_UUID,
    GOVEE_WRITE_UUID,
    HANDSHAKE_TIMEOUT,
    HEARTBEAT_INTERVAL,
    ON_DEMAND_IDLE_TIMEOUT,
    RECONNECT_INTERVAL,
    RING_EFFECT_BREATHE,
    RING_EFFECT_CHASE,
    RING_EFFECT_GRADIENT,
    RING_EFFECT_SOLID,
    RING_EFFECT_STROBE,
)
from .govee.device import (
    GoveeDeviceState,
    LightColorMode,
    NotificationType,
    StateUpdate,
    cmd_brightness,
    cmd_color_rgb_panel,
    cmd_color_temp,
    cmd_keepalive,
    cmd_power,
    cmd_ring_breathe,
    cmd_ring_chase,
    cmd_ring_diy,
    cmd_ring_gradient,
    cmd_ring_strobe,
    encrypt_command,
    make_hs1_frame,
    make_hs2_frame,
    parse_hs1_response,
    parse_notification,
    snap_kelvin,
)

_LOGGER = logging.getLogger(__name__)


class GoveeCoordinator:
    """Manages the BLE connection and state for a single Govee H601E lamp.

    Attributes:
        address:          BLE address of the lamp (MAC or CoreBluetooth UUID).
        connection_mode:  Active connection mode (``persistent`` / ``on_demand``).
        state:            Current known state of the lamp.
    """

    def __init__(self, hass: HomeAssistant, address: str, connection_mode: str) -> None:
        """Initialise the coordinator.

        Args:
            hass:            Home Assistant instance.
            address:         BLE address (MAC on Linux/Windows, UUID on macOS).
            connection_mode: Initial connection mode.
        """
        self._hass = hass
        self.address = address
        self.connection_mode = connection_mode

        # Device state – updated optimistically when commands are sent and will
        # be updated from notifications once state-push parsing is implemented.
        self.state: GoveeDeviceState = GoveeDeviceState()

        # Active BLE client and session key
        self._client: BleakClient | None = None
        self._session_key: bytes | None = None

        # Callbacks registered by entity classes
        self._listeners: list[Callable[[], None]] = []

        # Keep-alive / reconnect tracking
        self._heartbeat_unsub: Callable[[], None] | None = None
        self._reconnect_task: asyncio.Task[None] | None = None
        self._connect_lock = asyncio.Lock()
        self._write_lock = asyncio.Lock()
        self._shutdown = False
        # Exponential backoff counter: reset to 0 on successful connection,
        # incremented on each failed attempt.  Delay = min(30 * 2^n, 600)s.
        self._reconnect_attempt: int = 0
        # On-demand idle-disconnect timer
        self._od_idle_timer: asyncio.TimerHandle | None = None
        # Timestamp of the last successfully written command (monotonic seconds).
        # Used to suppress stale heartbeat echoes that carry pre-command state.
        self._last_cmd_sent_at: float = 0.0

    @property
    def _repair_issue_id(self) -> str:
        """Stable issue ID for the 'device unreachable' repair entry."""
        return f"unreachable_{self.address.lower().replace(':', '_').replace('-', '_')}"

    # ── Public API ─────────────────────────────────────────────────────────────

    def register_update_callback(self, cb: Callable[[], None]) -> Callable[[], None]:
        """Register a callback that is invoked whenever the device state changes.

        Args:
            cb: Zero-argument callable (typically ``entity.async_write_ha_state``).

        Returns:
            A remove function; call it in ``async_will_remove_from_hass``.
        """
        self._listeners.append(cb)

        def _remove() -> None:
            try:
                self._listeners.remove(cb)
            except ValueError:
                pass

        return _remove

    @property
    def available(self) -> bool:
        """Return ``True`` while the coordinator has a valid session key.

        In ``on_demand`` mode this is ``True`` even when not currently connected
        because connections are transient.
        """
        if self.connection_mode == CONNECTION_MODE_ON_DEMAND:
            return True
        return self._session_key is not None

    # ── Connection lifecycle ────────────────────────────────────────────────────

    async def async_start(self) -> None:
        """Start the coordinator.

        In ``persistent`` mode a background connection task is fired immediately
        so that HA setup is not blocked by the BLE connect + handshake latency.
        Entities start as *unavailable* and transition to *available* once the
        first connection succeeds.

        In ``on_demand`` mode nothing is done until the first command arrives.
        """
        self._shutdown = False
        if self.connection_mode == CONNECTION_MODE_PERSISTENT:
            self._hass.async_create_task(self._async_connect())

    async def async_stop(self) -> None:
        """Shut down the coordinator and close any open BLE connection."""
        self._shutdown = True
        self._cancel_heartbeat()
        self._cancel_od_idle_timer()
        if self._reconnect_task and not self._reconnect_task.done():
            self._reconnect_task.cancel()
        await self._async_disconnect()

    async def async_set_connection_mode(self, mode: str) -> None:
        """Switch the connection mode at runtime.

        Tears down the current connection (if any), updates the mode and
        re-initialises accordingly.

        Args:
            mode: ``CONNECTION_MODE_PERSISTENT`` or ``CONNECTION_MODE_ON_DEMAND``.
        """
        if mode == self.connection_mode:
            return
        _LOGGER.info(
            "[%s] Switching connection mode: %s → %s",
            self.address, self.connection_mode, mode,
        )
        self._cancel_heartbeat()
        self._cancel_od_idle_timer()
        await self._async_disconnect()
        self.connection_mode = mode
        if mode == CONNECTION_MODE_PERSISTENT:
            # Non-blocking: connect in background so the switch entity returns
            # immediately.  Entities will transition from unavailable → available
            # once the handshake completes.
            self._hass.async_create_task(self._async_connect())

    # ── Command API ─────────────────────────────────────────────────────────────

    async def async_turn_on(self) -> None:
        """Turn the lamp on (master power)."""
        if await self._send_command(cmd_power(True), label="POWER_ON"):
            self.state.is_on = True
            self.state.center.is_on = True
            self.state.ring.is_on = True
            self._notify_listeners()

    async def async_turn_off(self) -> None:
        """Turn the lamp off (master power)."""
        if await self._send_command(cmd_power(False), label="POWER_OFF"):
            self.state.is_on = False
            self.state.center.is_on = False
            self.state.ring.is_on = False
            self._notify_listeners()

    async def async_set_brightness(self, brightness_pct: int) -> None:
        """Set the global brightness (affects both centre and ring).

        Args:
            brightness_pct: Govee brightness percentage 0–100.
        """
        pct = max(0, min(100, brightness_pct))
        if await self._send_command(cmd_brightness(pct), label="BRIGHTNESS"):
            self.state.is_on = True
            self.state.center.is_on = True
            self.state.ring.is_on = True
            self.state.brightness_pct = pct
            self.state.center.brightness_pct = pct
            self.state.ring.brightness_pct = pct
            self._notify_listeners()

    async def async_set_center_color_temp(self, kelvin: int) -> None:
        """Set centre-panel colour temperature.

        Args:
            kelvin: Colour temperature in Kelvin (2 200–6 500).
        """
        k = snap_kelvin(kelvin)
        if await self._send_command(cmd_color_temp(k), label="COLOR_TEMP"):
            self.state.is_on = True
            self.state.center.is_on = True
            self.state.center.color_mode = LightColorMode.COLOR_TEMP
            self.state.center.color_temp_kelvin = k
            self._notify_listeners()

    async def async_set_center_rgb(self, r: int, g: int, b: int) -> None:
        """Set centre-panel RGB colour.

        Args:
            r, g, b: Red, green, blue components (0–255).
        """
        if await self._send_command(cmd_color_rgb_panel(r, g, b), label="COLOR_PANEL"):
            self.state.is_on = True
            self.state.center.is_on = True
            self.state.center.color_mode = LightColorMode.RGB
            self.state.center.rgb = (r & 0xFF, g & 0xFF, b & 0xFF)
            self._notify_listeners()

    async def async_set_ring_rgb(self, r: int, g: int, b: int) -> None:
        """Set outer-ring RGB colour via the H601E DIY protocol (solid colour).

        Args:
            r, g, b: Red, green, blue components (0–255).
        """
        if await self._send_command(cmd_ring_diy(r, g, b), label="COLOR_RING"):
            self.state.is_on = True
            self.state.ring.is_on = True
            self.state.ring.color_mode = LightColorMode.RGB
            self.state.ring.rgb = (r & 0xFF, g & 0xFF, b & 0xFF)
            self.state.ring.effect = None  # Solid colour clears any active effect
            self._notify_listeners()

    async def async_set_ring_effect(self, effect: str, r: int, g: int, b: int) -> None:
        """Set an animated effect on the outer ring.

        Args:
            effect: One of the :data:`~const.RING_EFFECTS` names.
            r, g, b: Primary colour for the effect (0–255).
        """
        if effect == RING_EFFECT_BREATHE:
            frame = cmd_ring_breathe(r, g, b)
        elif effect == RING_EFFECT_STROBE:
            frame = cmd_ring_strobe(r, g, b)
        elif effect == RING_EFFECT_CHASE:
            frame = cmd_ring_chase(r, g, b)
        elif effect == RING_EFFECT_GRADIENT:
            frame = cmd_ring_gradient(r, g, b)
        else:  # SOLID or unrecognised → plain solid colour
            frame = cmd_ring_diy(r, g, b)
            effect = RING_EFFECT_SOLID

        if await self._send_command(frame, label=f"RING_EFFECT_{effect.upper()}"):
            self.state.is_on = True
            self.state.ring.is_on = True
            self.state.ring.color_mode = LightColorMode.RGB
            self.state.ring.rgb = (r & 0xFF, g & 0xFF, b & 0xFF)
            self.state.ring.effect = effect
            self._notify_listeners()

    # ── Internal connection helpers ────────────────────────────────────────────

    async def _async_connect(self) -> bool:
        """Establish a BLE connection and perform the cryptographic handshake.

        Returns:
            ``True`` on success, ``False`` on any failure.
        """
        async with self._connect_lock:
            if self._client and self._client.is_connected:
                return True  # Already connected

            ble_device = bluetooth.async_ble_device_from_address(
                self._hass, self.address, connectable=True
            )
            if ble_device is None:
                _LOGGER.warning(
                    "[%s] Device not found in Bluetooth cache – "
                    "is Bluetooth active and the device in range?",
                    self.address,
                )
                return False

            _LOGGER.debug("[%s] Connecting…", self.address)
            try:
                client = await establish_connection(
                    client_class=BleakClientWithServiceCache,
                    device=ble_device,
                    name=self.address,
                    disconnected_callback=self._on_disconnected,
                    max_attempts=3,
                )
            except (BleakAbortedError, BleakConnectionError, BleakNotFoundError) as exc:
                _LOGGER.warning("[%s] Connection failed: %s", self.address, exc)
                return False
            except BleakError as exc:
                _LOGGER.error("[%s] Unexpected BLE error: %s", self.address, exc)
                return False

            self._client = client
            _LOGGER.debug("[%s] Connected", self.address)

            session_key = await self._perform_handshake(client)
            if session_key is None:
                _LOGGER.error("[%s] Handshake failed", self.address)
                await client.disconnect()
                self._client = None
                return False

            self._session_key = session_key

            # Subscribe to ongoing notifications
            try:
                await client.start_notify(GOVEE_NOTIFY_UUID, self._on_notification)
            except BleakError as exc:
                _LOGGER.warning(
                    "[%s] Could not subscribe to notifications: %s", self.address, exc
                )
                # Non-fatal: continue without notifications

            # Start keep-alive timer in persistent mode
            if self.connection_mode == CONNECTION_MODE_PERSISTENT:
                self._start_heartbeat()

            _LOGGER.info("[%s] Handshake complete – connection ready", self.address)
            self._reconnect_attempt = 0  # Reset backoff on successful connection
            # Dismiss any outstanding "unreachable" repair issue
            ir.async_delete_issue(self._hass, DOMAIN, self._repair_issue_id)
            # Request current device state to sync HA with physical reality
            await self._async_request_state()
            self._notify_listeners()
            return True

    async def _async_disconnect(self) -> None:
        """Close the BLE connection and clear the session key."""
        self._cancel_heartbeat()
        client = self._client
        self._client = None
        self._session_key = None
        if client and client.is_connected:
            try:
                await client.disconnect()
                _LOGGER.debug("[%s] Disconnected", self.address)
            except BleakError as exc:
                _LOGGER.debug("[%s] Disconnect error (ignored): %s", self.address, exc)
        self._notify_listeners()

    @callback
    def _on_disconnected(self, _client: BleakClient) -> None:
        """Called by bleak when the connection drops unexpectedly."""
        _LOGGER.warning("[%s] Connection lost", self.address)
        self._client = None  # Clear stale reference so _async_connect doesn't re-use it
        self._session_key = None
        self._cancel_heartbeat()
        self._notify_listeners()

        if self._shutdown:
            return

        if self.connection_mode == CONNECTION_MODE_PERSISTENT:
            self._schedule_reconnect()

    def _schedule_reconnect(self) -> None:
        """Schedule a reconnect attempt with exponential backoff.

        Delay = min(RECONNECT_INTERVAL * 2^attempt, 600) seconds.
        The attempt counter is reset to 0 by a successful connection so that
        a device that was temporarily unreachable reconnects quickly once it
        returns, rather than staying stuck at the maximum delay.
        """
        if self._reconnect_task and not self._reconnect_task.done():
            return  # Already scheduled

        self._reconnect_attempt += 1
        delay = min(RECONNECT_INTERVAL * (2 ** (self._reconnect_attempt - 1)), 600)

        # Surface a HA Repairs issue once the backoff has hit its ceiling so
        # the user sees an actionable notification in the UI.
        if delay >= 600:
            ir.async_create_issue(
                self._hass,
                DOMAIN,
                self._repair_issue_id,
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key="device_unreachable",
                translation_placeholders={"address": self.address},
            )

        async def _reconnect() -> None:
            _LOGGER.info("[%s] Reconnecting in %d s (attempt %d)…",
                         self.address, delay, self._reconnect_attempt)
            await asyncio.sleep(delay)
            if not self._shutdown:
                await self._async_connect()

        self._reconnect_task = self._hass.async_create_task(_reconnect())

    # ── Handshake ──────────────────────────────────────────────────────────────

    async def _perform_handshake(self, client: BleakClient) -> bytes | None:
        """Execute the two-step BLE handshake and return the 16-byte session key.

        Uses a temporary local notification queue so that the handshake bytes
        are not mixed up with subsequent state-push notifications.

        Args:
            client: Open BleakClient already connected to the device.

        Returns:
            16-byte session key, or ``None`` on failure.
        """
        # Bounded queue – a misbehaving device cannot fill memory by flooding
        # notifications.  Handshake only needs 2 messages (HS1 response + HS2 echo).
        notify_queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=8)

        def _queue_callback(_char: Any, data: bytearray) -> None:
            try:
                notify_queue.put_nowait(bytes(data))
            except asyncio.QueueFull:
                _LOGGER.debug("[%s] Handshake notify queue full – dropping frame", self.address)

        # Subscribe with a dedicated callback for the handshake phase
        try:
            await client.start_notify(GOVEE_NOTIFY_UUID, _queue_callback)
        except BleakError as exc:
            _LOGGER.error("[%s] Cannot subscribe to notify char: %s", self.address, exc)
            return None

        await asyncio.sleep(0.05)  # Brief settle time

        # HS1 ──────────────────────────────────────────────────────────────────
        hs1 = make_hs1_frame()
        _LOGGER.debug("[%s] → HS1: %s", self.address, hs1.hex())
        try:
            await client.write_gatt_char(GOVEE_WRITE_UUID, hs1, response=False)
        except BleakError as exc:
            _LOGGER.error("[%s] HS1 write failed: %s", self.address, exc)
            await client.stop_notify(GOVEE_NOTIFY_UUID)
            return None

        try:
            resp1 = await asyncio.wait_for(notify_queue.get(), timeout=HANDSHAKE_TIMEOUT)
        except asyncio.TimeoutError:
            _LOGGER.error("[%s] Timeout waiting for HS1 response", self.address)
            await client.stop_notify(GOVEE_NOTIFY_UUID)
            return None

        _LOGGER.debug("[%s] ← HS1 response: %s", self.address, resp1.hex())
        session_key = parse_hs1_response(resp1)
        if session_key is None:
            await client.stop_notify(GOVEE_NOTIFY_UUID)
            return None

        # HS2 ──────────────────────────────────────────────────────────────────
        hs2 = make_hs2_frame()
        _LOGGER.debug("[%s] → HS2: %s", self.address, hs2.hex())
        try:
            await client.write_gatt_char(GOVEE_WRITE_UUID, hs2, response=False)
        except BleakError as exc:
            _LOGGER.warning("[%s] HS2 write failed (non-fatal): %s", self.address, exc)

        # HS2 echo – optional; short timeout
        try:
            resp2 = await asyncio.wait_for(notify_queue.get(), timeout=1.0)
            _LOGGER.debug("[%s] ← HS2 echo: %s", self.address, resp2.hex())
        except asyncio.TimeoutError:
            _LOGGER.debug("[%s] No HS2 echo (non-fatal)", self.address)

        # Hand back the notify characteristic to the persistent callback
        await client.stop_notify(GOVEE_NOTIFY_UUID)
        return session_key

    # ── Post-connect state sync ────────────────────────────────────────────────

    async def _async_request_state(self) -> None:
        """Trigger a state sync immediately after (re)connecting.

        Sends a keepalive frame (0xAA/0x36).  The device echoes it back with
        per-zone power states in bytes[2..3], which :meth:`_on_notification`
        parses via ``_parse_heartbeat()`` and applies to :attr:`state`.

        This gives us accurate on/off state for each zone right after a
        (re)connect, without requiring any destructive write command.
        Brightness, colour and ring-effect state are still tracked optimistically
        because the protocol offers no non-destructive query for those values.
        """
        await self._send_persistent(cmd_keepalive(), "STATE_QUERY")

    # ── Command sending ────────────────────────────────────────────────────────

    async def _send_command(self, plain_frame: bytes, label: str = "CMD") -> bool:
        """Encrypt and send a command frame to the device.

        Handles both connection modes: in ``on_demand`` mode a transient
        connection is opened, the command sent and the connection closed.

        Args:
            plain_frame: 20-byte plaintext frame (from a ``cmd_*`` builder).
            label:       Log label for debug output.

        Returns:
            ``True`` if the command was sent successfully.
        """
        if self.connection_mode == CONNECTION_MODE_ON_DEMAND:
            return await self._send_on_demand(plain_frame, label)
        return await self._send_persistent(plain_frame, label)

    async def _send_persistent(self, plain_frame: bytes, label: str) -> bool:
        """Send a command over the persistent connection; reconnect if needed."""
        if self._client is None or not self._client.is_connected:
            _LOGGER.info("[%s] Not connected – reconnecting before %s", self.address, label)
            if not await self._async_connect():
                return False

        if self._session_key is None:
            _LOGGER.error("[%s] No session key for %s", self.address, label)
            return False

        # Capture atomically before async suspension points (Fix #8 – race condition)
        session_key = self._session_key
        client = self._client
        if session_key is None or client is None:
            # Disconnected between the checks above and here (race window)
            _LOGGER.warning("[%s] Connection lost just before %s – retrying", self.address, label)
            return False

        encrypted = encrypt_command(session_key, plain_frame)
        _LOGGER.debug("[%s] → %s: %s", self.address, label, encrypted.hex())
        try:
            async with self._write_lock:
                await client.write_gatt_char(
                    GOVEE_WRITE_UUID, encrypted, response=False
                )
            self._last_cmd_sent_at = monotonic()
            return True
        except BleakError as exc:
            _LOGGER.warning("[%s] Write failed for %s: %s", self.address, label, exc)
            return False

    async def _send_on_demand(self, plain_frame: bytes, label: str) -> bool:
        """Send a command via a reused or freshly opened transient connection.

        Commands that arrive within ``ON_DEMAND_IDLE_TIMEOUT`` seconds of the
        previous one share the same BLE connection and session key, avoiding a
        full connect + handshake cycle for every attribute in a multi-attribute
        service call.  The connection is closed automatically by an idle timer
        once the burst is over.
        """
        # Cancel any pending idle-disconnect so the connection stays alive
        # while we send this command.
        self._cancel_od_idle_timer()

        # Connect (or reuse an existing session) via the shared connect path.
        if not (self._client and self._client.is_connected and self._session_key):
            _LOGGER.debug("[%s] On-demand connect for %s", self.address, label)
            if not await self._async_connect():
                return False

        # Reuse the persistent-send path, which handles session key capture
        # and the write lock.
        result = await self._send_persistent(plain_frame, label)

        # Schedule idle disconnect; the next command will cancel and reschedule
        # this timer, keeping the connection open for the entire command burst.
        self._schedule_od_disconnect()
        return result

    # ── Notification handler ───────────────────────────────────────────────────

    @callback
    def _on_notification(self, _char: Any, data: bytearray) -> None:
        """Process an inbound BLE notification from the device.

        Called from the bleak event loop callback.  Parses the notification,
        merges any state delta into :attr:`state`, and notifies listeners.
        """
        raw = bytes(data)
        _LOGGER.debug("[%s] ← NOTIFY: %s", self.address, raw.hex())

        # Capture session_key once; it can be set to None concurrently by
        # _on_disconnected, so we must not read self._session_key twice.
        session_key = self._session_key
        parsed = parse_notification(raw, session_key)

        if parsed.type == NotificationType.HEARTBEAT:
            _LOGGER.debug("[%s] Heartbeat received", self.address)
            if parsed.state_update is not None:
                self._apply_state_update(parsed.state_update, from_heartbeat=True)

        elif parsed.type == NotificationType.STATE_UPDATE:
            if parsed.state_update is not None:
                _LOGGER.debug("[%s] State update: %s", self.address, parsed.state_update)
                self._apply_state_update(parsed.state_update, from_heartbeat=False)
            else:
                _LOGGER.debug(
                    "[%s] Unrecognised state notification: %s",
                    self.address, parsed.plain.hex() if parsed.plain else raw.hex(),
                )

        elif parsed.type == NotificationType.UNKNOWN:
            _LOGGER.debug("[%s] Unknown notification: %s", self.address, raw.hex())

    @callback
    def _apply_state_update(self, update: StateUpdate, from_heartbeat: bool = False) -> None:
        """Merge a :class:`StateUpdate` delta into :attr:`state`.

        Only non-``None`` fields in *update* are written.  Notifies registered
        listeners if any field actually changed.

        ``from_heartbeat`` suppresses stale "off" power-state values that arrive
        in a keepalive echo shortly after a command was sent.  The echo reflects
        the device state *before* the command was processed, which would
        incorrectly override the optimistic update we set on send.  Command
        echoes (0x33/0x01 etc.) are always applied unconditionally.
        """
        changed = False
        s = self.state

        # Suppress stale "off" from heartbeat if a command was sent recently.
        _CMD_HOLD_SECS = 2.0
        suppress_off = from_heartbeat and (monotonic() - self._last_cmd_sent_at) < _CMD_HOLD_SECS

        if update.is_on is not None:
            new_val = update.is_on if (update.is_on or not suppress_off) else s.is_on
            if s.is_on != new_val:
                s.is_on = new_val
                changed = True

        if update.center_is_on is not None:
            new_val = update.center_is_on if (update.center_is_on or not suppress_off) else s.center.is_on
            if s.center.is_on != new_val:
                s.center.is_on = new_val
                changed = True

        if update.ring_is_on is not None:
            new_val = update.ring_is_on if (update.ring_is_on or not suppress_off) else s.ring.is_on
            if s.ring.is_on != new_val:
                s.ring.is_on = new_val
                changed = True

        if update.brightness_pct is not None and s.brightness_pct != update.brightness_pct:
            pct = update.brightness_pct
            s.brightness_pct = pct
            s.center.brightness_pct = pct
            s.ring.brightness_pct = pct
            changed = True

        if update.center_color_mode is not None and s.center.color_mode != update.center_color_mode:
            s.center.color_mode = update.center_color_mode
            changed = True

        if update.center_color_temp_kelvin is not None and s.center.color_temp_kelvin != update.center_color_temp_kelvin:
            s.center.color_temp_kelvin = update.center_color_temp_kelvin
            changed = True

        if update.center_rgb is not None and s.center.rgb != update.center_rgb:
            s.center.rgb = update.center_rgb
            changed = True

        if update.ring_present:
            if update.ring_rgb is not None and s.ring.rgb != update.ring_rgb:
                s.ring.rgb = update.ring_rgb
                changed = True
            if s.ring.effect != update.ring_effect:
                s.ring.effect = update.ring_effect
                changed = True

        if changed:
            self._notify_listeners()

    # ── Heartbeat ──────────────────────────────────────────────────────────────

    def _start_heartbeat(self) -> None:
        """Schedule a periodic keep-alive frame in persistent connection mode."""
        self._cancel_heartbeat()

        @callback
        def _send_keepalive(now: Any) -> None:  # noqa: ANN401
            """Fire-and-forget keep-alive command."""
            if self._client and self._client.is_connected and self._session_key:
                self._hass.async_create_task(
                    self._send_persistent(cmd_keepalive(), "KEEPALIVE")
                )

        self._heartbeat_unsub = async_track_time_interval(
            self._hass,
            _send_keepalive,
            timedelta(seconds=HEARTBEAT_INTERVAL),
        )
        _LOGGER.debug("[%s] Heartbeat timer started (%ds)", self.address, HEARTBEAT_INTERVAL)

    def _cancel_heartbeat(self) -> None:
        """Cancel the keep-alive timer if active."""
        if self._heartbeat_unsub is not None:
            self._heartbeat_unsub()
            self._heartbeat_unsub = None

    # ── On-demand idle timer ────────────────────────────────────────────────────

    def _cancel_od_idle_timer(self) -> None:
        """Cancel the on-demand idle-disconnect timer if active."""
        if self._od_idle_timer is not None:
            self._od_idle_timer.cancel()
            self._od_idle_timer = None

    def _schedule_od_disconnect(self) -> None:
        """Schedule an on-demand connection teardown after the idle timeout.

        Each new command cancels and reschedules this timer, so the connection
        stays open for the duration of a command burst (e.g. the three writes
        emitted by a single multi-attribute ``turn_on`` service call) and is
        cleanly closed once the device goes quiet.
        """
        def _do_disconnect() -> None:
            self._od_idle_timer = None
            _LOGGER.debug("[%s] On-demand idle timeout – disconnecting", self.address)
            self._hass.async_create_task(self._async_disconnect())

        self._od_idle_timer = self._hass.loop.call_later(
            ON_DEMAND_IDLE_TIMEOUT, _do_disconnect
        )

    # ── Listener notification ──────────────────────────────────────────────────

    @callback
    def notify_listeners(self) -> None:
        """Invoke all registered entity update callbacks (public alias)."""
        self._notify_listeners()

    @callback
    def _notify_listeners(self) -> None:
        """Invoke all registered entity update callbacks."""
        for cb in list(self._listeners):
            try:
                cb()
            except Exception:  # noqa: BLE001
                _LOGGER.exception("[%s] Error in update callback", self.address)
