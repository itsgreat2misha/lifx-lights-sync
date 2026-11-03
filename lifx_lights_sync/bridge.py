"""
LIFX bridge — in-process Sendspin visualizer client.

Modeled directly on the installed Hue Lights Sync bridge
(``music_assistant.providers.hue_entertainment.bridge``): each sync
target registers as an external Sendspin client whose bridge roles run
the server's visualizer feature extraction on the group's audio and
receive color palette updates directly — no WebSocket involved. The
analyzer queues features by playback timestamp and converts them to
light colors at render time; where Hue streams over DTLS, this bridge
writes to LIFX lights over the LAN protocol via lifx-async, and —
since LIFX lights do not revert on their own — snapshots each target's
state at stream start and restores it at stream stop.

All Music Assistant / aiosendspin imports and call shapes in this
module were aligned against the installed Music Assistant in the
user's container (aiosendspin 10.2.1): ``register_external_player``
is a *synchronous* call returning a ``SendspinClient``; roles are
fetched via ``client.roles_by_family(...)`` and subscribed via
``client.attach_preinitialized_roles()``; the render clock is
``sendspin_server.clock.now_us()``; client removal is
``await sendspin_server.remove_client(client_id)``.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from typing import TYPE_CHECKING, Any, cast

from aiosendspin.models.core import ClientHelloPayload
from aiosendspin.models.core import DeviceInfo as SendspinDeviceInfo
from aiosendspin.models.types import UndefinedField
from aiosendspin.models.visualizer import (
    ClientHelloVisualizerSpectrum,
    ClientHelloVisualizerSupport,
    VisualizerStatePayload,
)
from music_assistant_models.enums import PlayerType

from music_assistant.providers.sendspin.bridge_role import (
    COLOR_BRIDGE_ROLE_ID,
    VISUALIZER_BRIDGE_ROLE_ID,
    BridgeColorRole,
    BridgeVisualizerRole,
)

from . import constants as const
from .analyzer import LifxAudioAnalyzer
from .color import build_palette
from .discovery import SyncTarget
from .lifx_output import LifxSender
from .settings import PluginSettings

if TYPE_CHECKING:
    from aiosendspin.models.core import ServerStatePayload
    from aiosendspin.models.visualizer import BeatTiming
    from aiosendspin.server import (
        ExternalStreamStartRequest,
        SendspinClient,
        SendspinServer,
    )
    from aiosendspin.server.roles.visualizer.features import ExtractedFrame

    from music_assistant.providers.sendspin.provider import SendspinProvider

    from .provider import LifxLightsSyncProvider

_LOGGER = logging.getLogger(__name__)

# Render loop rate: the analyzer renders at 30 Hz like the Hue bridge;
# the LIFX sender rate-limits actual device writes per light
# (send_rate_hz, default 20/s), so the faster render rate only smooths
# interpolation, it does not flood the lights.
_RENDER_RATE_HZ = 50
_RENDER_PERIOD_S = 1.0 / _RENDER_RATE_HZ
# Visualizer feature rate requested from the extraction pipeline
# (matches the Hue bridge; its filters are tuned for ~20 Hz input).
_VISUALIZER_RATE_HZ = 20

# Debounce before tearing a stream down, to survive track transitions.
_STOP_DEBOUNCE_S = 2.0


def _client_id_for(target_id: str) -> str:
    safe = "".join(
        char if char.isalnum() or char in "-_" else "-" for char in target_id
    )
    return f"lifx-{safe}"[:64]


class LifxTargetBridge:
    """
    Manages the Sendspin visualizer bridge for a single sync target.

    Registers with the local Sendspin server as an external visualizer
    client, receives extracted visualization features keyed to playback
    time, converts them to light colors, and writes them to the
    target's LIFX lights.
    """

    def __init__(
        self,
        provider: LifxLightsSyncProvider,
        target: SyncTarget,
        sendspin_server: SendspinServer,
        sender: LifxSender,
    ) -> None:
        """Initialize the bridge."""
        self.provider = provider
        self.mass = provider.mass
        self.target = target
        self.sendspin_server = sendspin_server
        self.logger = _LOGGER.getChild(f"bridge.{target.target_id}")

        self._sender = sender
        self._analyzer: LifxAudioAnalyzer | None = None
        self._sendspin_client: SendspinClient | None = None
        self._is_streaming = False
        self._starting = False
        self._stop_debounce_task: asyncio.Task[None] | None = None
        self._start_task: asyncio.Task[None] | None = None
        self._send_task: asyncio.Task[None] | None = None
        self._render_handle: asyncio.TimerHandle | None = None
        settings = provider.settings
        self._latency_us: int = settings.latency_us

    @property
    def streaming(self) -> bool:
        """Return True while this bridge is streaming to its lights."""
        return self._is_streaming

    async def start(self) -> None:
        """Start the bridge — register as an in-process Sendspin visualizer client."""
        settings = self.provider.settings
        self._analyzer = LifxAudioAnalyzer(
            channels=self.target.channels,
            mode=settings.color_mode,
            brightness=settings.brightness,
            brightness_floor=settings.brightness_floor,
        )

        client_id = _client_id_for(self.target.target_id)

        # Register this client as a LIGHT player type with the Sendspin
        # provider so the resulting player shows up correctly in the UI.
        sendspin_prov: SendspinProvider | None = self.mass.get_provider("sendspin")  # type: ignore[assignment]
        if sendspin_prov:
            sendspin_prov.register_bridge_player_type(client_id, PlayerType.LIGHT)

        support = ClientHelloVisualizerSupport(
            buffer_capacity=const.VISUALIZER_BUFFER_CAPACITY
        )
        request = VisualizerStatePayload(
            rate_max=_VISUALIZER_RATE_HZ,
            # Peaks requested as a fallback for when beats aren't computed yet.
            types=["beat", "peak", "spectrum"],
            spectrum=ClientHelloVisualizerSpectrum(
                n_disp_bins=const.SPECTRUM_N_DISP_BINS,
                scale=const.SPECTRUM_SCALE,
                f_min=const.SPECTRUM_F_MIN,
                f_max=const.SPECTRUM_F_MAX,
            ),
        )
        hello = ClientHelloPayload(
            client_id=client_id,
            name=f"LIFX: {self.target.name}",
            version=1,
            supported_roles=[VISUALIZER_BRIDGE_ROLE_ID, COLOR_BRIDGE_ROLE_ID],
            device_info=SendspinDeviceInfo(
                manufacturer="LIFX",
                product_name="LIFX Lights Sync",
            ),
            visualizer_support=support,
        )
        self._sendspin_client = self.sendspin_server.register_external_player(
            hello, on_stream_start=self._on_external_stream_start
        )

        if viz_roles := self._sendspin_client.roles_by_family("visualizer"):
            viz_role = cast("BridgeVisualizerRole", viz_roles[0])
            viz_role.set_callbacks(
                on_frame=self._on_visualizer_frame,
                on_beats=self._on_beats,
                on_beats_clear=self._on_beats_clear,
                on_stream_start=self._on_stream_start,
                on_stream_clear=self._on_stream_clear,
                on_stream_end=self._on_stream_end,
            )
            viz_role.setup_visualizer(request)
        if color_roles := self._sendspin_client.roles_by_family("color"):
            color_role = cast("BridgeColorRole", color_roles[0])
            color_role.set_callbacks(on_color=self._on_color)
        # Subscribes the roles to the group's visualizer/color group
        # roles, which beat schedules and palette updates flow through.
        self._sendspin_client.attach_preinitialized_roles()

        self.logger.info(
            "LIFX bridge started for target '%s' (%d channels)",
            self.target.name,
            len(self.target.channels),
        )

    async def stop(self) -> None:
        """Stop the bridge, restore the lights and remove the client."""
        self._cancel_render_loop()
        if self._stop_debounce_task and not self._stop_debounce_task.done():
            self._stop_debounce_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._stop_debounce_task
        self._stop_debounce_task = None

        if self._sendspin_client:
            await self.sendspin_server.remove_client(self._sendspin_client.client_id)
            self._sendspin_client = None

        # Cancel an in-flight start so it can't begin streaming after stop.
        if self._start_task and not self._start_task.done():
            self._start_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._start_task
        self._start_task = None

        await self._stop_streaming()
        self.logger.debug("LIFX bridge stopped for target '%s'", self.target.name)

    def update_settings(self, settings: PluginSettings) -> None:
        """Update analyzer/bridge settings without restarting the bridge."""
        if self._analyzer:
            self._analyzer.set_mode(settings.color_mode)
            self._analyzer.set_brightness(settings.brightness)
            self._analyzer.set_brightness_floor(settings.brightness_floor)
        self._sender.set_send_rate(settings.send_rate_hz)
        self._latency_us = settings.latency_us

    # -- stream lifecycle ------------------------------------------------

    async def _start_streaming(self) -> None:
        """Snapshot the lights and start the render loop."""
        try:
            await self._sender.snapshot()
            self._is_streaming = True
            self._start_render_loop()
            self.logger.info("LIFX streaming active for target '%s'", self.target.name)
        finally:
            self._starting = False

    async def _stop_streaming(self) -> None:
        """Stop rendering and restore the pre-stream light state."""
        was_streaming = self._is_streaming
        self._is_streaming = False
        self._starting = False
        self._cancel_render_loop()
        if self._analyzer is not None:
            self._analyzer.clear_beat_schedule()
        if was_streaming:
            await self._sender.restore()

    def _on_external_stream_start(self, request: ExternalStreamStartRequest) -> None:
        """Handle playback dialing this client."""
        self.logger.debug(
            "Sendspin stream start request for target '%s' (reason=%s)",
            self.target.name,
            request.connection_reason,
        )

    def _on_stream_start(self) -> None:
        """Handle stream start — snapshot lights and begin rendering."""
        # Cancel any pending stop from a previous stream end (track transition).
        if self._stop_debounce_task and not self._stop_debounce_task.done():
            self._stop_debounce_task.cancel()
            self._stop_debounce_task = None
        if not self._is_streaming and not self._starting:
            self._starting = True
            self.logger.info(
                "Stream starting for target '%s', snapshotting lights...",
                self.target.name,
            )
            self._start_task = self.mass.create_task(self._start_streaming())

    def _on_stream_end(self) -> None:
        """Handle stream end — debounce to survive track transitions."""
        # Also act while a start is still in flight: the stream can end
        # before the snapshot completes, and that late start would
        # otherwise stream forever.
        starting = self._start_task is not None and not self._start_task.done()
        if self._is_streaming or starting:
            if self._stop_debounce_task and not self._stop_debounce_task.done():
                self._stop_debounce_task.cancel()
            self._stop_debounce_task = self.mass.create_task(self._debounced_stop())

    def _on_stream_clear(self) -> None:
        """Handle seek — queued analyzer state belongs to pre-seek audio."""
        if self._analyzer is not None:
            self._analyzer.clear_queues()

    async def _debounced_stop(self) -> None:
        """Wait briefly before stopping — a new stream may start (track change)."""
        await asyncio.sleep(_STOP_DEBOUNCE_S)
        # Cancel a still-running start first; if it already completed,
        # tear the live stream down (restores the lights).
        if self._start_task is not None and not self._start_task.done():
            self._start_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._start_task
        if self._is_streaming:
            self.logger.info(
                "Visualizer stream ended for target '%s'", self.target.name
            )
            await self._stop_streaming()

    # -- visualizer / color callbacks --------------------------------------

    def _on_visualizer_frame(self, frame: ExtractedFrame) -> None:
        """Queue spectrum and onset-peak features for the renderer."""
        # Frames arrive ahead of the playhead (the extraction pipeline
        # runs with a ~30 s lookahead); the analyzer queues them by
        # timestamp and promotes each one when the render clock reaches
        # it, so rendering tracks the audible playhead. Gated so the
        # queues only fill while the lights are up or coming up.
        if self._analyzer is None or not (self._is_streaming or self._starting):
            return
        if frame.spectrum is not None:
            self._analyzer.queue_spectrum(
                frame.timestamp_us, frame.spectrum.tolist()
            )
        if frame.peak is not None:
            self._analyzer.queue_peak(frame.timestamp_us, frame.peak)

    def _on_beats(self, beats: list[BeatTiming]) -> None:
        """Append a beat schedule segment."""
        # Not gated on streaming: the schedule lands once per track and
        # may arrive while the lights are still coming up.
        if self._analyzer is not None:
            for beat in beats:
                self._analyzer.queue_beat(beat.timestamp_us, beat.is_downbeat)

    def _on_beats_clear(self) -> None:
        """Drop the beat schedule (a track change re-pushes a fresh one)."""
        if self._analyzer is not None:
            self._analyzer.clear_beat_schedule()

    def _on_color(self, payload: ServerStatePayload) -> None:
        """Forward color palette updates from the Sendspin server."""
        if self._analyzer is None or payload.color is None:
            return
        update: dict[str, Any] = {}
        for name in (
            "background_dark",
            "background_light",
            "primary",
            "accent",
            "on_dark",
            "on_light",
        ):
            value = getattr(payload.color, name)
            if isinstance(value, UndefinedField):
                continue
            update[name] = value
        if update:
            self._analyzer.set_palette(build_palette(update))

    # -- render loop ---------------------------------------------------------

    def _start_render_loop(self) -> None:
        """Begin the fixed-rate render+send loop."""
        if self._render_handle is not None:
            return
        self._render_handle = self.mass.loop.call_later(
            _RENDER_PERIOD_S, self._render_tick
        )

    def _cancel_render_loop(self) -> None:
        """Cancel the fixed-rate render+send loop."""
        if self._render_handle is not None:
            self._render_handle.cancel()
            self._render_handle = None

    def _render_tick(self) -> None:
        """One render iteration, then reschedule while streaming."""
        self._render_handle = None
        if not self._is_streaming:
            return
        try:
            if self._analyzer is not None:
                # Render at the (latency compensated) server time; the
                # analyzer promotes whichever frame is due at that
                # moment from its lookahead queue.
                server_now = self.sendspin_server.clock.now_us() + self._latency_us
                commands = self._analyzer.render(server_now)
                # The LIFX sender is async (unlike Hue's sync DTLS
                # send): hand the frame to a task, and skip this tick
                # if the previous frame is still being written so sends
                # can never pile up.
                if commands and (
                    self._send_task is None or self._send_task.done()
                ):
                    self._send_task = self.mass.create_task(
                        self._sender.send(commands, self.target)
                    )
        except Exception:
            # One bad tick must not kill the loop: log and reschedule below.
            self.logger.exception(
                "LIFX render tick failed for target '%s'", self.target.name
            )
        finally:
            if self._is_streaming:
                self._render_handle = self.mass.loop.call_later(
                    _RENDER_PERIOD_S, self._render_tick
                )


class LifxBridgeManager:
    """Manages LIFX bridges for all sync targets of one provider instance."""

    def __init__(self, provider: LifxLightsSyncProvider) -> None:
        """Initialize the bridge manager."""
        self.provider = provider
        self.mass = provider.mass
        self.logger = _LOGGER.getChild("bridge_manager")
        self._bridges: dict[str, LifxTargetBridge] = {}

    @property
    def bridges(self) -> dict[str, LifxTargetBridge]:
        """Return the current bridges, keyed by target id."""
        return dict(self._bridges)

    @property
    def sendspin_server(self) -> SendspinServer | None:
        """Get the Sendspin server if available."""
        provider = cast("SendspinProvider | None", self.mass.get_provider("sendspin"))
        if provider is not None:
            return provider.server_api
        return None

    async def setup_bridges(
        self,
        targets: list[SyncTarget],
        devices_by_serial: dict[str, Any],
    ) -> None:
        """Set up bridges for all sync targets."""
        sendspin_server = self.sendspin_server
        if sendspin_server is None:
            self.logger.warning(
                "Sendspin provider not available, cannot set up LIFX bridges"
            )
            return

        # Remove bridges for targets that no longer exist.
        current_ids = {target.target_id for target in targets}
        for target_id in list(self._bridges.keys()):
            if target_id not in current_ids:
                bridge = self._bridges.pop(target_id)
                with suppress(Exception):
                    await bridge.stop()

        settings = self.provider.settings
        for target in targets:
            if target.target_id in self._bridges:
                continue
            if not target.channels:
                continue
            sender_devices = {
                light.serial: devices_by_serial[light.serial]
                for light in target.lights
                if light.serial in devices_by_serial
            }
            if target.lights and not sender_devices:
                self.logger.warning(
                    "Target '%s' has %d lights but no reachable devices; "
                    "it will not react until discovery finds their IPs",
                    target.name,
                    len(target.lights),
                )
            elif len(sender_devices) < len(target.lights):
                self.logger.info(
                    "Target '%s': %d of %d lights reachable",
                    target.name,
                    len(sender_devices),
                    len(target.lights),
                )
            sender = LifxSender(
                target,
                sender_devices,
                send_rate_hz=settings.send_rate_hz,
            )
            bridge = LifxTargetBridge(self.provider, target, sendspin_server, sender)
            try:
                await bridge.start()
            except Exception:
                self.logger.warning(
                    "Failed to start bridge for target '%s'", target.name
                )
                with suppress(Exception):
                    await bridge.stop()
                continue
            self._bridges[target.target_id] = bridge
            self.logger.info("Bridge created for LIFX target '%s'", target.name)

    def update_settings(self, settings: PluginSettings) -> None:
        """Update settings on all bridges."""
        for bridge in self._bridges.values():
            bridge.update_settings(settings)

    async def stop_all(self) -> None:
        """Stop all bridges."""
        for bridge in list(self._bridges.values()):
            with suppress(Exception):
                await bridge.stop()
        self._bridges.clear()


__all__ = ["LifxBridgeManager", "LifxTargetBridge"]
