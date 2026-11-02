"""Sendspin bridge: one virtual LIGHT player per sync target.

This mirrors the structure of the Hue Lights Sync plugin
(``music-assistant/server`` providers/hue_entertainment @73c3e59):

- a manager owns one bridge per sync target;
- each bridge registers an in-process Sendspin client with the
  ``visualizer`` and ``color`` bridge roles, which makes Music Assistant
  create a stock Sendspin *LIGHT* player for the target — users join it
  to a playing Sendspin player/group exactly like a speaker;
- the visualizer role delivers spectrum/peak/beat frames, the color
  role delivers the server-side album palette;
- a 30 Hz render loop renders the analyzer at
  ``clock.now_us() + latency`` and pushes colors to the LIFX sender;
- stream end is debounced by 2 s across track gaps; stopping snapshots
  are restored (LIFX has no Hue-style firmware revert).

.. important::
    The Music Assistant / aiosendspin APIs used here mirror
    hue_entertainment @73c3e59 and are **not yet live-verified** against
    a running Music Assistant with LIFX lights. Every MA-internal call
    is marked ``# TODO(live-verify)``. All MA imports are lazy/guarded so
    importing this module outside Music Assistant does not crash.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Iterable, Mapping, Sequence

from . import constants as const
from .analyzer import LifxAudioAnalyzer
from .color import build_palette
from .discovery import SyncTarget
from .lifx_output import LifxSender
from .settings import PluginSettings

_LOGGER = logging.getLogger(__name__)


def _load_bridge_role_ids() -> tuple[str, str]:
    """Resolve the Sendspin bridge role ids, falling back to literals."""
    try:
        # TODO(live-verify): canonical source in MA @73c3e59.
        from music_assistant.providers.sendspin.bridge_role import (  # noqa: PLC0415
            COLOR_BRIDGE_ROLE_ID,
            VISUALIZER_BRIDGE_ROLE_ID,
        )

        return str(VISUALIZER_BRIDGE_ROLE_ID), str(COLOR_BRIDGE_ROLE_ID)
    except Exception:  # noqa: BLE001 - outside MA / older MA
        return const.VISUALIZER_BRIDGE_ROLE_ID, const.COLOR_BRIDGE_ROLE_ID


VISUALIZER_ROLE_ID, COLOR_ROLE_ID = _load_bridge_role_ids()


def _sanitize_client_id(target_id: str) -> str:
    safe = "".join(char if char.isalnum() or char in "-_" else "-" for char in target_id)
    return f"lifx-{safe}"


class LifxTargetBridge:
    """Bridge between one sync target and the Sendspin visualizer stream."""

    def __init__(
        self,
        mass: Any,
        target: SyncTarget,
        settings: PluginSettings,
        sender: LifxSender,
    ) -> None:
        self._mass = mass
        self._target = target
        self._settings = settings
        self._sender = sender
        self._analyzer = LifxAudioAnalyzer(
            target.channels,
            mode=settings.color_mode,
            brightness=settings.brightness,
        )
        self.client_id = _sanitize_client_id(target.target_id)
        self._visualizer_role: Any = None
        self._color_role: Any = None
        self._render_handle: Any = None
        self._stop_task: asyncio.Task[Any] | None = None
        self._streaming = False
        self._registered = False

    # -- properties ----------------------------------------------------------

    @property
    def target(self) -> SyncTarget:
        return self._target

    @property
    def analyzer(self) -> LifxAudioAnalyzer:
        return self._analyzer

    @property
    def streaming(self) -> bool:
        return self._streaming

    # -- setup / teardown ------------------------------------------------------

    async def setup(self) -> bool:
        """Register this target as a Sendspin LIGHT player.

        Returns False (and logs) when the Sendspin provider or the
        bridge APIs are unavailable, so one bad target cannot take the
        whole provider down.
        """
        try:
            sendspin = self._mass.get_provider("sendspin")
        except Exception:  # noqa: BLE001
            _LOGGER.warning("Sendspin provider not available for %s", self._target.name)
            return False
        if sendspin is None:
            _LOGGER.warning("Sendspin provider not loaded for %s", self._target.name)
            return False

        try:
            from aiosendspin.models.core import ClientHelloPayload  # noqa: PLC0415
            from aiosendspin.models.visualizer import (  # noqa: PLC0415
                ClientHelloVisualizerSpectrum,
                ClientHelloVisualizerSupport,
            )
            from music_assistant.common.models.enums import PlayerType  # noqa: PLC0415

            # TODO(live-verify): register_bridge_player_type is the
            # hue_entertainment @73c3e59 entry point that makes the
            # stock Sendspin player a LIGHT player.
            sendspin.register_bridge_player_type(self.client_id, PlayerType.LIGHT)

            hello = ClientHelloPayload(
                client_id=self.client_id,
                name=f"LIFX Lights Sync: {self._target.name}",
                supported_roles=[VISUALIZER_ROLE_ID, COLOR_ROLE_ID],
                visualizer_support=ClientHelloVisualizerSupport(
                    buffer_capacity=const.VISUALIZER_BUFFER_CAPACITY
                ),
            )
            self._hello = hello
            self._sendspin = sendspin
            # TODO(live-verify): register_external_player creates the
            # player and returns role handles in hue_entertainment;
            # the exact return shape needs confirmation on a live MA.
            roles = await self._register_player(hello)
            if roles is None:
                return False
            self._visualizer_role, self._color_role = roles
            self._wire_callbacks()
            self._registered = True
            _LOGGER.info(
                "Registered LIFX sync target %s as LIGHT player %s",
                self._target.name,
                self.client_id,
            )
            return True
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Failed to register LIFX target %s", self._target.name)
            return False

    async def _register_player(self, hello: Any) -> tuple[Any, Any] | None:
        """Create the external Sendspin player and fetch its roles."""
        server = getattr(self._sendspin, "server", None) or self._sendspin
        register = getattr(server, "register_external_player", None)
        if register is None:
            _LOGGER.warning("Sendspin server has no register_external_player")
            return None
        # TODO(live-verify): hue_entertainment passes the hello payload
        # plus stream callbacks; the role objects are then fetched from
        # the returned client. Adapt after first live run if needed.
        client = await register(
            hello,
            on_stream_start=self._on_stream_start,
            on_stream_end=self._on_stream_end,
        )
        visualizer = getattr(client, "visualizer_role", None) or getattr(
            client, "visualizer", None
        )
        color = getattr(client, "color_role", None) or getattr(client, "color", None)
        if visualizer is None:
            _LOGGER.warning("No visualizer role for %s", self._target.name)
            return None
        return visualizer, color

    def _wire_callbacks(self) -> None:
        try:
            from aiosendspin.models.visualizer import (  # noqa: PLC0415
                ClientHelloVisualizerSpectrum,
                VisualizerStatePayload,
            )

            # TODO(live-verify): in hue_entertainment @73c3e59 the roles
            # returned at registration are BridgeVisualizerRole /
            # BridgeColorRole objects exposing set_callbacks(); we call
            # the same methods duck-typed on whatever was returned.
            self._visualizer_role.set_callbacks(
                on_frame=self._on_visualizer_frame,
                on_beats=self._on_beats,
                on_beats_clear=self._on_beats_clear,
                on_stream_start=self._on_stream_start,
                on_stream_clear=self._on_stream_clear,
                on_stream_end=self._on_stream_end,
            )
            self._visualizer_role.setup_visualizer(
                VisualizerStatePayload(
                    rate_max=const.VISUALIZER_RATE_MAX,
                    types=list(const.VISUALIZER_TYPES),
                    spectrum=ClientHelloVisualizerSpectrum(
                        n_disp_bins=const.SPECTRUM_N_DISP_BINS,
                        scale=const.SPECTRUM_SCALE,
                        f_min=const.SPECTRUM_F_MIN,
                        f_max=const.SPECTRUM_F_MAX,
                    ),
                )
            )
            if self._color_role is not None:
                self._color_role.set_callbacks(on_color=self._on_color)
        except Exception:  # noqa: BLE001
            _LOGGER.debug("Callback wiring deferred for %s", self._target.name, exc_info=True)

    async def stop(self) -> None:
        """Stop rendering, restore lights and unregister the player."""
        self._cancel_render()
        if self._stop_task is not None:
            self._stop_task.cancel()
            self._stop_task = None
        if self._streaming:
            self._streaming = False
            await self._sender.restore()
        if self._registered:
            try:
                sendspin = getattr(self, "_sendspin", None)
                server = getattr(sendspin, "server", None) or sendspin
                remove = getattr(server, "remove_client", None)
                if remove is not None:
                    # TODO(live-verify): remove_client(client_id) is the
                    # hue_entertainment unload path.
                    result = remove(self.client_id)
                    if asyncio.iscoroutine(result):
                        await result
            except Exception:  # noqa: BLE001
                _LOGGER.debug("remove_client failed for %s", self.client_id, exc_info=True)
            self._registered = False

    # -- live config -----------------------------------------------------------

    def update_settings(self, settings: PluginSettings) -> None:
        """Apply live-updatable settings (brightness, mode, latency, rate)."""
        self._settings = settings
        self._analyzer.set_brightness(settings.brightness)
        self._analyzer.set_mode(settings.color_mode)
        self._sender.set_send_rate(settings.send_rate_hz)

    # -- Sendspin callbacks ------------------------------------------------------

    def _on_visualizer_frame(self, frame: Any) -> None:
        timestamp = _frame_timestamp(frame, self._now_us())
        spectrum = getattr(frame, "spectrum", None)
        if spectrum:
            self._analyzer.queue_spectrum(timestamp, list(spectrum))
        peak = getattr(frame, "peak", None)
        if peak is not None:
            self._analyzer.queue_peak(timestamp, int(peak))

    def _on_beats(self, beats: Any) -> None:
        for beat in _as_sequence(beats):
            timestamp = getattr(beat, "timestamp_us", None)
            if timestamp is None:
                timestamp = getattr(beat, "timestamp", self._now_us())
            is_downbeat = bool(
                getattr(beat, "is_downbeat", getattr(beat, "downbeat", False))
            )
            self._analyzer.queue_beat(int(timestamp), is_downbeat)

    def _on_beats_clear(self) -> None:
        _LOGGER.debug("Beats cleared for %s", self._target.name)

    def _on_color(self, color: Any) -> None:
        palette_source = _color_payload_to_dict(color)
        if palette_source:
            self._analyzer.set_palette(build_palette(palette_source))

    def _on_stream_start(self, *_args: Any) -> None:
        if self._stop_task is not None:
            self._stop_task.cancel()
            self._stop_task = None
        if self._streaming:
            return
        self._streaming = True
        self._mass.create_task(self._start_rendering())

    def _on_stream_clear(self) -> None:
        self._schedule_stop()

    def _on_stream_end(self, *_args: Any) -> None:
        self._schedule_stop()

    # -- render loop ---------------------------------------------------------------

    async def _start_rendering(self) -> None:
        await self._sender.snapshot()
        self._schedule_render()

    def _now_us(self) -> int:
        clock = getattr(self._mass, "clock", None)
        if clock is not None:
            try:
                return int(clock.now_us())
            except Exception:  # noqa: BLE001
                pass
        # TODO(live-verify): mass.clock is the Sendspin clock source in
        # hue_entertainment; time.time() is only a degraded fallback.
        import time  # noqa: PLC0415

        return int(time.time() * 1_000_000)

    def _schedule_render(self) -> None:
        if not self._streaming:
            return
        loop = self._mass.loop
        self._render_handle = loop.call_later(
            1.0 / const.RENDER_FPS, self._render_tick
        )

    def _render_tick(self) -> None:
        if not self._streaming:
            return
        self._mass.create_task(self._render_once())
        self._schedule_render()

    async def _render_once(self) -> None:
        render_at = self._now_us() + self._settings.latency_us
        commands = self._analyzer.render(render_at)
        await self._sender.send(commands, self._target)

    def _cancel_render(self) -> None:
        if self._render_handle is not None:
            self._render_handle.cancel()
            self._render_handle = None

    def _schedule_stop(self) -> None:
        if self._stop_task is not None:
            return

        async def _debounced_stop() -> None:
            await asyncio.sleep(const.STOP_DEBOUNCE_S)
            self._cancel_render()
            self._streaming = False
            await self._sender.restore()

        self._stop_task = asyncio.ensure_future(_debounced_stop())


def _as_sequence(value: Any) -> Sequence[Any]:
    if value is None:
        return []
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return value
    return [value]


def _frame_timestamp(frame: Any, fallback: int) -> int:
    for attribute in ("timestamp_us", "timestamp", "time_us"):
        value = getattr(frame, attribute, None)
        if value is not None:
            return int(value)
    return fallback


def _color_payload_to_dict(color: Any) -> dict[str, Any]:
    """Convert a Sendspin color-role payload into a plain dict.

    The server palette arrives as a model with fields such as primary,
    accent, on_dark, on_light and background_*; each color may itself be
    a model with red/green/blue attributes.
    """
    if color is None:
        return {}
    if isinstance(color, Mapping):
        return dict(color)
    result: dict[str, Any] = {}
    for field_name in ("primary", "accent", "on_dark", "on_light"):
        value = getattr(color, field_name, None)
        if value is not None:
            result[field_name] = _color_value_to_rgb(value)
    for attribute in dir(color):
        if attribute.startswith("background_"):
            value = getattr(color, attribute, None)
            if value is not None:
                result[attribute] = _color_value_to_rgb(value)
    return result


def _color_value_to_rgb(value: Any) -> Any:
    if isinstance(value, Mapping) or isinstance(value, Sequence):
        return value
    red = getattr(value, "red", None)
    if red is not None:
        return {
            "red": red,
            "green": getattr(value, "green", 0),
            "blue": getattr(value, "blue", 0),
        }
    return value


class LifxBridgeManager:
    """Owns all target bridges for one provider instance."""

    def __init__(self, mass: Any, settings: PluginSettings) -> None:
        self._mass = mass
        self._settings = settings
        self._bridges: dict[str, LifxTargetBridge] = {}

    @property
    def bridges(self) -> dict[str, LifxTargetBridge]:
        return dict(self._bridges)

    async def setup(
        self,
        targets: Iterable[SyncTarget],
        devices_by_serial: Mapping[str, Any],
    ) -> list[LifxTargetBridge]:
        """Create (and register) a bridge per target."""
        created: list[LifxTargetBridge] = []
        for target in targets:
            sender = LifxSender(
                target,
                {
                    light.serial: devices_by_serial[light.serial]
                    for light in target.lights
                    if light.serial in devices_by_serial
                },
                send_rate_hz=self._settings.send_rate_hz,
            )
            bridge = LifxTargetBridge(self._mass, target, self._settings, sender)
            self._bridges[target.target_id] = bridge
            created.append(bridge)
            await bridge.setup()
        return created

    def update_settings(self, settings: PluginSettings) -> None:
        self._settings = settings
        for bridge in self._bridges.values():
            bridge.update_settings(settings)

    async def unload(self) -> None:
        """Stop all bridges and close their senders."""
        for bridge in list(self._bridges.values()):
            await bridge.stop()
        self._bridges.clear()


__all__ = ["LifxBridgeManager", "LifxTargetBridge"]
