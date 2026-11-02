"""Audio analysis: Sendspin visualizer data -> per-channel light colors.

A simplified, stdlib-only port of the Hue Lights Sync analyzer
(``music-assistant/server`` providers/hue_entertainment/analyzer.py).

Inputs arrive timestamped (microseconds, Sendspin clock) and are queued:
spectrum frames, peak levels, beat events, and the server album palette.
:meth:`LifxAudioAnalyzer.render` is then called with the current (latency
compensated) time and returns exactly one :class:`LightColorCommand` per
channel.

Per-mode behavior is driven by :data:`MODE_PRESETS`:

- **smooth** – slow color/brightness smoothing, gentle triangular beat
  pulse, palette advances on downbeats.
- **ambient** – like smooth but calmer: no onset/transient boost at all
  and no beat pulse; designed for background listening.
- **flashing** – minimal smoothing, full-strength beat pulse and onset
  boost, palette advances on every beat.
- **energetic** – fast filters, strong spectrum response, palette
  advances on every beat, decayed onset boost (250 ms).

No Music Assistant imports: everything here is unit-testable.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Sequence

from . import constants as const
from .color import RGB, build_palette, clamp, palette_color_at, scale_rgb

# How long spectrum/beat history is retained (microseconds).
_HISTORY_US = 2_000_000
# Onset (transient) boost decay time from the Hue blueprint.
_ONSET_DECAY_US = 250_000


@dataclass(frozen=True)
class LightChannel:
    """One renderable channel of a sync target.

    ``position`` is the channel's normalized position (0.0-1.0) inside its
    target. For bulbs in an area it is their order in the group; for a
    matrix light it is the zone's column fraction, so spectrum bands map
    left-to-right across the fixture.
    """

    channel_id: str
    position: float = 0.0
    is_matrix_zone: bool = False


@dataclass(frozen=True)
class LightColorCommand:
    """A single rendered color for one channel (RGB, 0-255 per channel)."""

    channel_id: str
    rgb: RGB

    @property
    def brightness(self) -> float:
        """Overall brightness of the command as 0.0-1.0."""
        return max(self.rgb) / 255.0 if self.rgb else 0.0


@dataclass(frozen=True)
class ModePreset:
    """Tuning for one analyzer mode."""

    base_factor: float  # multiplier on the configured brightness
    spectrum_weight: float  # how much the channel's spectrum band drives brightness
    pulse_amount: float  # beat pulse strength (0 disables the pulse)
    pulse_decay_us: int  # triangular pulse length
    onset_amount: float  # transient boost strength (0 = ignore onsets)
    advance_on_downbeat_only: bool  # palette advance policy
    fast_alpha: float  # exp filter factor for the fast spectrum follower
    slow_alpha: float  # exp filter factor for the slow spectrum follower
    smoothing: float  # per-render color lerp factor (1 = no smoothing)


MODE_PRESETS: dict[str, ModePreset] = {
    const.MODE_SMOOTH: ModePreset(
        base_factor=0.85, spectrum_weight=0.35, pulse_amount=0.25,
        pulse_decay_us=600_000, onset_amount=0.15,
        advance_on_downbeat_only=True,
        fast_alpha=0.35, slow_alpha=0.05, smoothing=0.25,
    ),
    const.MODE_AMBIENT: ModePreset(
        base_factor=0.6, spectrum_weight=0.15, pulse_amount=0.0,
        pulse_decay_us=600_000, onset_amount=0.0,
        advance_on_downbeat_only=True,
        fast_alpha=0.15, slow_alpha=0.03, smoothing=0.12,
    ),
    const.MODE_FLASHING: ModePreset(
        base_factor=0.9, spectrum_weight=0.6, pulse_amount=1.0,
        pulse_decay_us=300_000, onset_amount=0.6,
        advance_on_downbeat_only=False,
        fast_alpha=0.9, slow_alpha=0.2, smoothing=0.9,
    ),
    const.MODE_ENERGETIC: ModePreset(
        base_factor=1.0, spectrum_weight=0.85, pulse_amount=0.6,
        pulse_decay_us=450_000, onset_amount=0.45,
        advance_on_downbeat_only=False,
        fast_alpha=0.65, slow_alpha=0.1, smoothing=0.55,
    ),
}


@dataclass
class _Beat:
    timestamp_us: int
    is_downbeat: bool


class LifxAudioAnalyzer:
    """Turn queued visualizer data into per-channel color commands."""

    def __init__(
        self,
        channels: Sequence[LightChannel],
        mode: str = const.DEFAULT_COLOR_MODE,
        brightness: float = 0.8,
        palette: Sequence[RGB] | None = None,
    ) -> None:
        self._channels: list[LightChannel] = list(channels)
        self._mode = mode if mode in MODE_PRESETS else const.DEFAULT_COLOR_MODE
        self._brightness = clamp(float(brightness))
        self._palette: list[RGB] = list(palette) if palette else build_palette(None)

        self._spectrum: deque[tuple[int, tuple[float, ...]]] = deque()
        self._beats: deque[_Beat] = deque()
        self._peaks: deque[tuple[int, int]] = deque()

        self._fast_bins: list[float] = []
        self._slow_bins: list[float] = []
        self._palette_index = 0
        self._last_beat_us: int | None = None
        self._last_pulse_us: int | None = None
        self._onset_us: int | None = None
        self._previous: dict[str, RGB] = {}

    # -- configuration -----------------------------------------------------

    @property
    def channels(self) -> list[LightChannel]:
        return list(self._channels)

    @property
    def mode(self) -> str:
        return self._mode

    def set_mode(self, mode: str) -> None:
        if mode in MODE_PRESETS:
            self._mode = mode

    def set_brightness(self, brightness: float) -> None:
        self._brightness = clamp(float(brightness))

    def set_palette(self, palette: Sequence[RGB]) -> None:
        if palette:
            self._palette = list(palette)
            self._palette_index %= len(self._palette)

    @property
    def _preset(self) -> ModePreset:
        return MODE_PRESETS[self._mode]

    # -- inputs ------------------------------------------------------------

    def queue_spectrum(self, timestamp_us: int, bins: Sequence[float]) -> None:
        """Queue one spectrum frame.

        Bin values may be 0.0-1.0 floats or 0-255 ints; they are
        normalized to 0.0-1.0 on arrival.
        """
        if not bins:
            return
        peak_value = max(bins)
        scale = 255.0 if peak_value > 1.5 else 1.0
        normalized = tuple(clamp(float(value) / scale) for value in bins)
        self._spectrum.append((int(timestamp_us), normalized))
        self._update_filters(normalized)
        self._prune(int(timestamp_us))

    def queue_peak(self, timestamp_us: int, peak: int) -> None:
        """Queue a peak level (0-255)."""
        self._peaks.append((int(timestamp_us), max(0, min(255, int(peak)))))
        self._prune(int(timestamp_us))

    def queue_beat(self, timestamp_us: int, is_downbeat: bool = False) -> None:
        """Queue a beat event; downbeats mark the start of a bar."""
        self._beats.append(_Beat(int(timestamp_us), bool(is_downbeat)))
        self._prune(int(timestamp_us))

    def queue_onset(self, timestamp_us: int) -> None:
        """Register an onset (transient) at the given time.

        The bridge calls this when it detects a sharp peak rise; the
        analyzer itself also derives onsets from spectrum transients.
        In ambient mode onsets have no effect (preset onset_amount = 0).
        """
        self._onset_us = int(timestamp_us)

    # -- internals ---------------------------------------------------------

    def _prune(self, now_us: int) -> None:
        cutoff = now_us - _HISTORY_US
        while self._spectrum and self._spectrum[0][0] < cutoff:
            self._spectrum.popleft()
        while self._peaks and self._peaks[0][0] < cutoff:
            self._peaks.popleft()
        while self._beats and self._beats[0].timestamp_us < cutoff:
            self._beats.popleft()

    def _update_filters(self, bins: tuple[float, ...]) -> None:
        preset = self._preset
        if len(self._fast_bins) != len(bins):
            self._fast_bins = [0.0] * len(bins)
            self._slow_bins = [0.0] * len(bins)
        for index, value in enumerate(bins):
            self._fast_bins[index] += preset.fast_alpha * (value - self._fast_bins[index])
            self._slow_bins[index] += preset.slow_alpha * (value - self._slow_bins[index])

    def _latest_spectrum(self, now_us: int) -> tuple[float, ...] | None:
        latest: tuple[float, ...] | None = None
        for timestamp_us, bins in self._spectrum:
            if timestamp_us > now_us:
                break
            latest = bins
        return latest

    def _channel_energy(self, channel: LightChannel) -> tuple[float, float]:
        """Return (level, transient) for a channel from the filter banks."""
        if not self._fast_bins:
            return 0.0, 0.0
        index = channel.position * (len(self._fast_bins) - 1)
        low = int(index)
        high = min(low + 1, len(self._fast_bins) - 1)
        fraction = index - low
        fast = self._fast_bins[low] * (1 - fraction) + self._fast_bins[high] * fraction
        slow = self._slow_bins[low] * (1 - fraction) + self._slow_bins[high] * fraction
        return fast, max(0.0, fast - slow)

    def _process_beats(self, now_us: int) -> None:
        preset = self._preset
        for beat in self._beats:
            if beat.timestamp_us > now_us:
                break
            if self._last_beat_us is not None and beat.timestamp_us <= self._last_beat_us:
                continue
            self._last_beat_us = beat.timestamp_us
            self._last_pulse_us = beat.timestamp_us
            if beat.is_downbeat or not preset.advance_on_downbeat_only:
                self._palette_index = (self._palette_index + 1) % len(self._palette)

    def _pulse_value(self, now_us: int) -> float:
        """Triangular beat pulse: 1.0 at the beat, decaying to 0."""
        preset = self._preset
        if preset.pulse_amount <= 0 or self._last_pulse_us is None:
            return 0.0
        elapsed = now_us - self._last_pulse_us
        if elapsed < 0 or elapsed > preset.pulse_decay_us:
            return 0.0
        return preset.pulse_amount * (1.0 - elapsed / preset.pulse_decay_us)

    def _onset_value(self, now_us: int, transient: float) -> float:
        preset = self._preset
        if preset.onset_amount <= 0:
            return 0.0
        boost = transient * preset.onset_amount
        if self._onset_us is not None:
            elapsed = now_us - self._onset_us
            if 0 <= elapsed <= _ONSET_DECAY_US:
                boost = max(boost, preset.onset_amount * (1.0 - elapsed / _ONSET_DECAY_US))
        return boost

    # -- rendering -----------------------------------------------------------

    def render(self, now_us: int) -> list[LightColorCommand]:
        """Render one command per channel for the given timestamp."""
        now_us = int(now_us)
        self._prune(now_us)
        self._process_beats(now_us)
        preset = self._preset
        pulse = self._pulse_value(now_us)
        has_spectrum = self._latest_spectrum(now_us) is not None

        commands: list[LightColorCommand] = []
        for channel in self._channels:
            energy, transient = self._channel_energy(channel) if has_spectrum else (0.0, 0.0)
            onset = self._onset_value(now_us, transient)

            level = (
                0.30
                + preset.spectrum_weight * energy
                + pulse
                + onset
            )
            brightness = clamp(self._brightness * preset.base_factor * level)

            # Spatial palette offset: channels further along the target
            # blend toward the next palette color.
            base = palette_color_at(
                self._palette, self._palette_index, channel.position
            )
            target_rgb = scale_rgb(base, brightness / max(0.05, _rgb_peak(base)))
            previous = self._previous.get(channel.channel_id)
            if previous is not None and preset.smoothing < 1.0:
                factor = preset.smoothing
                rendered = tuple(
                    round(old + (new - old) * factor)
                    for old, new in zip(previous, target_rgb)
                )
            else:
                rendered = target_rgb
            self._previous[channel.channel_id] = rendered  # type: ignore[assignment]
            commands.append(LightColorCommand(channel.channel_id, rendered))  # type: ignore[arg-type]
        return commands


def _rgb_peak(rgb: RGB) -> float:
    """Peak channel of an RGB color as 0.0-1.0 (its HSV value)."""
    return max(rgb) / 255.0 if rgb else 0.0


__all__ = [
    "MODE_PRESETS",
    "LightChannel",
    "LightColorCommand",
    "LifxAudioAnalyzer",
    "ModePreset",
]
