"""Constants for the Govee H601E integration.

All domain-wide constants, configuration keys, and default values are
centralised here so that other modules can import them without creating
circular-import problems.
"""

from __future__ import annotations

# ── Integration identity ───────────────────────────────────────────────────────

DOMAIN = "govee_h601e"
"""The HA domain / unique identifier for this integration."""

MANUFACTURER = "Govee"
MODEL = "H601E"

# ── Config-entry / options keys ────────────────────────────────────────────────

CONF_MAC = "mac"
"""BLE MAC address (or CoreBluetooth UUID on macOS) of the lamp."""

CONF_CONNECTION_MODE = "connection_mode"
"""Persistent vs. on-demand connection mode, stored in the config entry."""

# ── Connection mode values ─────────────────────────────────────────────────────

CONNECTION_MODE_PERSISTENT = "persistent"
"""Keep a permanent BLE connection open; notifications and heartbeats are
processed continuously."""

CONNECTION_MODE_ON_DEMAND = "on_demand"
"""Open a BLE connection only while sending a command, then disconnect."""

CONNECTION_MODE_DEFAULT = CONNECTION_MODE_PERSISTENT

# ── Entity unique-ID suffixes ──────────────────────────────────────────────────

SUFFIX_CENTER = "_center"
SUFFIX_RING = "_ring"
SUFFIX_PERSIST = "_persist"

# ── HA platform names forwarded from this integration ─────────────────────────

PLATFORMS: list[str] = ["light", "switch"]

# ── BLE / protocol parameters ──────────────────────────────────────────────────

HEARTBEAT_INTERVAL = 8
"""Seconds between keep-alive frames sent in persistent mode.
The device itself sends a heartbeat every ~3 s; we send every 8 s to stay
well within the device's inactivity timeout."""

RECONNECT_INTERVAL = 5
"""Base seconds for the first reconnect attempt after a connection loss.
Subsequent attempts use exponential backoff: 5 → 10 → 20 → … → 600 s."""

CONNECT_TIMEOUT = 20.0
"""Maximum seconds for the initial BLE connection attempt."""

HANDSHAKE_TIMEOUT = 6.0
"""Maximum seconds to wait for each handshake notification."""

COMMAND_TIMEOUT = 3.0
"""Maximum seconds to wait for a command acknowledgement notification."""

ON_DEMAND_IDLE_TIMEOUT = 2.0
"""Seconds an on-demand BLE connection is kept open after the last command.

Commands that arrive within this window reuse the existing connection and
session key instead of performing a new connect + handshake cycle."""

# ── Govee-specific BLE UUIDs (from APK reverse engineering) ───────────────────

GOVEE_SERVICE_UUID = "00010203-0405-0607-0809-0a0b0c0d1910"
"""Primary GATT service UUID used by the H601E (and related H604x family)."""

GOVEE_WRITE_UUID = "00010203-0405-0607-0809-0a0b0c0d2b11"
"""Write characteristic – commands are written here (write without response)."""

GOVEE_NOTIFY_UUID = "00010203-0405-0607-0809-0a0b0c0d2b10"
"""Notify characteristic – device sends handshake responses and state updates."""

# ── Local-name patterns for BLE advertisement scanning ────────────────────────

GOVEE_LOCAL_NAME_PREFIXES: tuple[str, ...] = ("GVH", "Govee", "govee", "ihoment")
"""Known prefixes in the BLE advertisement local-name field for Govee lamps."""

# ── Ring light effects ─────────────────────────────────────────────────────────

RING_EFFECT_SOLID = "Solid"
"""Solid colour (default, subEffectType=0x01, KmpSubEffectCommon)."""

RING_EFFECT_BREATHE = "Breathe"
"""Breathing/pulse effect (subEffectType=0x0B, KmpSubEffectBreathe)."""

RING_EFFECT_STROBE = "Strobe"
"""Strobe/flash effect (subEffectType=0x0A, KmpSubEffectDuiJi)."""

RING_EFFECT_CHASE = "Chase"
"""Streamer/chase effect (subEffectType=0x07, KmpSubEffectStreamer)."""

RING_EFFECT_GRADIENT = "Gradient"
"""Two-colour gradient (subEffectType=0x06, KmpSubEffectSpeedColor).
The second colour is the complementary hue of the primary colour."""

RING_EFFECTS: list[str] = [
    RING_EFFECT_SOLID,
    RING_EFFECT_BREATHE,
    RING_EFFECT_STROBE,
    RING_EFFECT_CHASE,
    RING_EFFECT_GRADIENT,
]
"""All supported ring light effect names, in display order."""

# ── Colour-temperature range ───────────────────────────────────────────────────

KELVIN_MIN: int = 2200
"""Minimum supported colour temperature in Kelvin (H604a / H601E hardware limit)."""

KELVIN_MAX: int = 6500
"""Maximum supported colour temperature in Kelvin."""
