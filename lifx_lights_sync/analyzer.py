"""
Audio analysis: Sendspin visualizer data -> per-channel light colors.

A stdlib-only port of the Hue Lights Sync analyzer
(``music-assistant/server`` providers/hue_entertainment/analyzer.py),
kept mechanically faithful where it matters for musical sync:

- Inputs (spectrum frames, onset peaks, beat schedule) are queued by
  their server-clock timestamps and *promoted* only when render time
  reaches them — the extraction pipeline delivers frames up to ~30 s
  ahead of the playhead, so arrival time is meaningless for alignment.
- Spectrum bins are normalized from the server's uint16 dB scale,
  noise-gated, and gamma-expanded (``** 0.5``).
- Brightness per channel is driven by spectrum *transients* only:
  a fast follower vs a slow baseline per channel band; sustained
  energy decays to the mode's floor, new hits flash toward its max.
- Onset peaks add a global brightness boost (250 ms linear decay)
  and, when no beat schedule is active, walk the palette.
- With a beat schedule, colors interpolate between beats (hold then
  crossfade per the mode's animation fraction) and a triangular
  pulse is centered on each beat.
- Bass-band transients modulate palette saturation.

No Music Assistant imports: everything here is unit-testable.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass

from . import constants as const
from .color import RGB, build_palette, clamp

# Spectrum shaping (Hue blueprint values).
_SPECTRUM_GAMMA = 0.5
_SPECTRUM_BIN_NOISE_GATE = 0.02
# Highest spectrum bin used for per-channel bands (17 mel bins total,
# 20 Hz-20 kHz; bin edges: bin 0 ends 177 Hz, bin 1 ends 369 Hz,
# bin 3 ends 887 Hz, bin 10 ends ~5.6 kHz).
_CHANNEL_BIN_MAX = 10
# Per-channel transient filters, updated once per render tick. The
# baseline is slow (~5 s) so fast-minus-baseline isolates onsets.
_CHANNEL_RISE = 0.75
_CHANNEL_DECAY = 0.10
_CHANNEL_BASELINE_RISE = 0.013
_CHANNEL_BASELINE_DECAY = 0.013
_PER_CHANNEL_MIN_MULT = 0.3
# Bass -> saturation (bins 0-1: sub-bass + kick body).
_BASS_SAT_BINS = 2
_BASS_SAT_MIN = 0.5
_BASS_SAT_MAX = 0.85
_BASS_BASELINE_SMOOTHING = 0.005
_BASS_TRANSIENT_SCALE = 10.0
_BASS_SAT_RISE = 0.8
_BASS_SAT_DECAY = 0.08
# Bass mode is a LEVEL follower, the way LedFx's Filter effect works:
# its Beat/Bass bulbs track smoothed band level, not transients. A
# fast/slow transient pair inverts on real music — a sustained heavy
# section suppresses itself as the baseline catches up while sparse
# vocal phrases keep punching through — so brightness follows the
# smoothed low-band level above
# a threshold, updated once per spectrum frame (20 Hz). Input
# contrast: the shaped low bins are cubed (effective ^1.5 on the
# dB-normalized magnitude; LedFx's peak isolation is ^1.96 on its
# linear samples) so the vocal-band residue sitting in the low bins
# is crushed while real low end survives. Bin 0 (20-177 Hz) is the
# driver; bin 1 (177-369 Hz, where sung fundamentals live) is
# half-weight and gated by bin 0's activity.
_BASS_BIN1_WEIGHT = 0.5
_BASS_BIN1_GATE_FULL = 0.20
_BASS_CONTRAST_EXP = 3
_BASS_LEVEL_GAIN = 2.5
_BASS_LEVEL_THRESHOLD = 0.20
_BASS_LEVEL_RISE = 0.95
_BASS_LEVEL_DECAY = 0.50
# Onset peaks: strength gate, boost scale, decay window.
_PEAK_MIN_STRENGTH = 0.1
_PEAK_BOOST_SCALE = 0.3
_PEAK_BOOST_DECAY_US = 250_000
# Beat pulse: triangular, half-width is this fraction of the segment.
_PULSE_HALF_FRACTION = 0.08
# Safety cap on queued spectrum frames (clock-anomaly guard).
_MAX_PENDING_SPECTRUM = 6000


@dataclass(frozen=True)
class LightChannel:
    """
    One renderable channel of a sync target.

    ``position`` is the channel's normalized position (0.0-1.0) inside
    its target: for bulbs in an area it is their order in the group;
    for a matrix light it is the zone's column fraction, so spectrum
    bands and the palette gradient map left-to-right.
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
    """Tuning for one analyzer mode (Hue blueprint values)."""

    pulse_peak: float  # beat pulse target (1.0 = no pulse)
    downbeat_pulse_peak: float
    beat_animation_fraction: float  # tail of each segment spent crossfading
    channel_transient_scale: float  # transient -> multiplier gain
    color_gradient_spread: float  # palette slots spanned across channels
    palette_advance: float  # palette slots advanced per beat / per peak
    spatial_drift_per_second: float  # palette slots drifted per second
    channel_floor: float  # per-channel multiplier at rest
    channel_max: float  # per-channel multiplier on a full transient
    onset_boost: float = 1.0  # scales the peak brightness boost
    bass_saturation_scale: float = 1.0  # scales the bass saturation swing
    # Extra global brightness lift from the bass-band transient, added
    # to every channel multiplier (room-wide kick pump). 0 = Hue stock.
    bass_brightness_scale: float = 0.0
    # Highest spectrum bin feeding the per-channel brightness bands.
    # None = the standard cap (_CHANNEL_BIN_MAX). The bass-heavy
    # Energetic tuning restricts brightness to bins 0-3 (20-887 Hz on
    # the server's mel scale).
    band_bin_max: int | None = None
    # Dedicated Bass mode: every channel shares ONE brightness
    # envelope computed from bins 0-1 only (20-369 Hz), instead of
    # per-channel band transients. Combined with pulse_peak 1.0 and
    # palette_advance 0.0 in the preset, neither the beat schedule
    # nor broadband onset peaks can move the lights — only low-end
    # transients can.
    bass_only: bool = False


MODE_PRESETS: dict[str, ModePreset] = {
    const.MODE_SMOOTH: ModePreset(
        pulse_peak=1.0,
        downbeat_pulse_peak=1.0,
        beat_animation_fraction=0.0,
        channel_transient_scale=8.0,
        color_gradient_spread=1.0,
        palette_advance=0.125,
        spatial_drift_per_second=0.1,
        channel_floor=0.60,
        channel_max=0.85,
    ),
    const.MODE_AMBIENT: ModePreset(
        pulse_peak=1.0,
        downbeat_pulse_peak=1.0,
        beat_animation_fraction=0.1,
        channel_transient_scale=0.0,
        color_gradient_spread=0.7,
        palette_advance=0.125,
        spatial_drift_per_second=0.0,
        channel_floor=0.80,
        channel_max=0.80,
        onset_boost=0.0,
    ),
    const.MODE_FLASHING: ModePreset(
        pulse_peak=1.8,
        downbeat_pulse_peak=2.2,
        beat_animation_fraction=0.0,
        channel_transient_scale=2.0,
        color_gradient_spread=0.7,
        palette_advance=0.25,
        spatial_drift_per_second=0.05,
        channel_floor=0.45,
        channel_max=0.80,
    ),
    const.MODE_ENERGETIC: ModePreset(
        # Bass-heavy tuning, in the style of LedFx Filter scenes
        # band-limited to the low end: per-channel brightness bands
        # and the kick pump use bins 0-3 (20-887 Hz). The broadband
        # onset-peak flash is off (peaks still walk the palette), but
        # the beat pulse still fires on every beat.
        pulse_peak=1.2,
        downbeat_pulse_peak=1.2,
        beat_animation_fraction=0.2,
        channel_transient_scale=8.0,
        color_gradient_spread=1.0,
        palette_advance=0.5,
        spatial_drift_per_second=0.15,
        channel_floor=0.30,
        channel_max=1.00,
        onset_boost=0.0,
        bass_brightness_scale=0.6,
        band_bin_max=3,
    ),
    const.MODE_BASS: ModePreset(
        # Dedicated Bass mode: brightness follows the smoothed LEVEL
        # of the low band, the way LedFx Filter bulbs behave — cubed
        # contrast on bins 0-1, bin 0 dominant, bin 1 half-weight and
        # gated by bin 0, above a 0.20 threshold (see the
        # _BASS_LEVEL_* constants). Kicks and drops light the room,
        # sustained bass stays lit (the LedFx behavior does the same),
        # gaps fall back to the dim floor within ~200 ms, and vocal
        # content — whose residue in the low bins the contrast
        # crushes — stays at the floor. No beat pulse (pulse_peak
        # 1.0), no peak boost and no peak palette walk
        # (palette_advance 0.0): colors only drift slowly.
        pulse_peak=1.0,
        downbeat_pulse_peak=1.0,
        beat_animation_fraction=0.2,
        channel_transient_scale=0.0,
        color_gradient_spread=0.5,
        palette_advance=0.0,
        spatial_drift_per_second=0.05,
        channel_floor=0.08,
        channel_max=1.00,
        onset_boost=0.0,
        bass_saturation_scale=0.5,
        band_bin_max=1,
        bass_only=True,
    ),
}


class _ExpFilter:
    """Exponential smoothing with separate attack and decay rates."""

    def __init__(self, alpha_rise: float, alpha_decay: float) -> None:
        self._alpha_rise = alpha_rise
        self._alpha_decay = alpha_decay
        self.value: float = 0.0

    def update(self, new_value: float) -> float:
        alpha = self._alpha_rise if new_value > self.value else self._alpha_decay
        self.value = alpha * new_value + (1.0 - alpha) * self.value
        return self.value


@dataclass
class _ScheduledBeat:
    timestamp_us: int
    beat_in_bar: int  # continuous counter (never resets at bar lines)
    is_downbeat: bool


class LifxAudioAnalyzer:
    """Turn queued visualizer data into per-channel color commands."""

    def __init__(
        self,
        channels: Sequence[LightChannel],
        mode: str = const.DEFAULT_COLOR_MODE,
        brightness: float = 0.8,
        brightness_floor: float = 0.0,
        palette: Sequence[RGB] | None = None,
    ) -> None:
        """Initialize the analyzer for a fixed channel layout."""
        self._channels: list[LightChannel] = list(channels)
        self._mode = mode if mode in MODE_PRESETS else const.DEFAULT_COLOR_MODE
        self._brightness = clamp(float(brightness))
        # Absolute resting-brightness lower bound (0.0-1.0). 0 keeps
        # each mode's built-in channel floor.
        self._brightness_floor = clamp(float(brightness_floor))
        # Palette stored as 0.0-1.0 float RGB.
        self._palette: list[tuple[float, float, float]] = [
            (r / 255.0, g / 255.0, b / 255.0)
            for r, g, b in (palette if palette else build_palette(None))
        ]

        self._pending_spectrum: deque[tuple[int, tuple[float, ...]]] = deque()
        self._spectrum: list[float] = []
        self._pending_peaks: deque[tuple[int, float]] = deque()
        self._beats: deque[_ScheduledBeat] = deque()
        self._last_beat_in_bar = -1

        self._channel_filters: list[_ExpFilter] = []
        self._channel_baselines: list[_ExpFilter] = []
        self._bass_baseline = 0.0
        self._bass_saturation = _BASS_SAT_MIN
        self._bass_swing = 0.0  # normalized bass transient, 0.0-1.0
        self._pump_baseline = 0.0  # slow baseline for the pump band
        self._bass_level = 0.0  # smoothed low-band level (Bass mode)
        self._peak_boost = 0.0
        self._peak_set_at_us: int | None = None
        self._peak_palette_position = 0.0

    # -- configuration -----------------------------------------------------

    @property
    def channels(self) -> list[LightChannel]:
        """The renderable channels, in target order."""
        return list(self._channels)

    @property
    def mode(self) -> str:
        """The active analyzer mode name."""
        return self._mode

    @property
    def bass_level(self) -> float:
        """Smoothed low-band level (Bass mode's brightness driver)."""
        return self._bass_level

    def set_mode(self, mode: str) -> None:
        """Switch the active mode (unknown names are ignored)."""
        if mode in MODE_PRESETS:
            self._mode = mode

    def set_brightness(self, brightness: float) -> None:
        """Set the overall brightness scale (0.0-1.0, clamped)."""
        self._brightness = clamp(float(brightness))

    def set_brightness_floor(self, brightness_floor: float) -> None:
        """Set the absolute resting-brightness floor (0.0-1.0)."""
        self._brightness_floor = clamp(float(brightness_floor))

    def set_palette(self, palette: Sequence[RGB]) -> None:
        """Replace the color palette (empty input keeps the current one)."""
        if palette:
            self._palette = [(r / 255.0, g / 255.0, b / 255.0) for r, g, b in palette]

    # -- inputs ------------------------------------------------------------

    def queue_spectrum(self, timestamp_us: int, bins: Sequence[float]) -> None:
        """
        Queue one spectrum frame, normalized and shaped.

        The Sendspin server encodes bins as uint16 dB-scaled values in
        [0, 65535] (-60 dB -> 0, 0 dB -> 65535). Smaller-range encodings
        (0-255, 0.0-1.0) are also accepted; the scale is picked from the
        frame's peak. Normalization, noise gate, and gamma are applied
        here (per-frame pure functions, identical to applying them at
        promotion time); the frame only *affects output* once render
        time reaches its timestamp.
        """
        if not bins:
            return
        peak_value = max(bins)
        if peak_value > 255.0:
            scale = 65535.0
        elif peak_value > 1.5:
            scale = 255.0
        else:
            scale = 1.0
        shaped = []
        for value in bins:
            norm = clamp(float(value) / scale)
            if norm < _SPECTRUM_BIN_NOISE_GATE:
                shaped.append(0.0)
            else:
                shaped.append(min(1.0, norm**_SPECTRUM_GAMMA))
        self._pending_spectrum.append((int(timestamp_us), tuple(shaped)))
        if len(self._pending_spectrum) > _MAX_PENDING_SPECTRUM:
            for _ in range(1000):
                self._pending_spectrum.popleft()

    def queue_peak(self, timestamp_us: int, peak: int) -> None:
        """Queue an onset peak (0-255); weak onsets are dropped."""
        normalized = clamp(int(peak) / 255.0)
        if normalized < _PEAK_MIN_STRENGTH:
            return
        self._pending_peaks.append((int(timestamp_us), normalized * _PEAK_BOOST_SCALE))

    def queue_beat(self, timestamp_us: int, is_downbeat: bool = False) -> None:
        """Queue a beat; positions use a continuous (never-reset) count."""
        bib = 0 if self._last_beat_in_bar < 0 else self._last_beat_in_bar + 1
        self._beats.append(_ScheduledBeat(int(timestamp_us), bib, bool(is_downbeat)))
        self._last_beat_in_bar = bib

    def queue_onset(self, timestamp_us: int) -> None:
        """Compatibility shim: onsets arrive via :meth:`queue_peak`."""
        return

    def clear_beat_schedule(self) -> None:
        """Drop the beat schedule (a track change re-pushes a fresh one)."""
        self._beats.clear()
        self._last_beat_in_bar = -1

    def clear_queues(self) -> None:
        """Drop all queued data (seek: pre-seek audio is stale)."""
        self.clear_beat_schedule()
        self._pending_spectrum.clear()
        self._spectrum = []
        self._pending_peaks.clear()
        self._peak_boost = 0.0
        self._peak_set_at_us = None
        self._peak_palette_position = 0.0
        self._bass_swing = 0.0
        self._pump_baseline = 0.0
        self._bass_level = 0.0

    # -- rendering -----------------------------------------------------------

    def render(self, now_us: int) -> list[LightColorCommand]:
        """Render one command per channel for the given timestamp."""
        now_us = int(now_us)
        if not self._channels:
            return []
        self._prune_beats(now_us)
        self._advance_spectrum(now_us)
        channel_mults = self._channel_multipliers()
        peak_factor = self._consume_peaks(now_us) * self._preset.onset_boost
        if peak_factor > 0.0:
            channel_mults = [min(1.0, m + peak_factor) for m in channel_mults]
        bass_factor = self._preset.bass_brightness_scale * self._bass_swing
        if bass_factor > 0.0:
            channel_mults = [min(1.0, m + bass_factor) for m in channel_mults]

        prior, next_beat, segment = self._current_segment(now_us)
        if prior is None or next_beat is None or segment is None:
            colors = self._peak_walk_colors(now_us)
            brightness = self._brightness
        else:
            pulse = self._compute_pulse(now_us, prior, next_beat, segment)
            colors = self._per_channel_colors(prior, next_beat, now_us, segment)
            brightness = self._brightness * pulse

        commands: list[LightColorCommand] = []
        for i, channel in enumerate(self._channels):
            scale = brightness * channel_mults[i]
            r, g, b = colors[i]
            commands.append(
                LightColorCommand(
                    channel.channel_id,
                    (
                        round(clamp(r * scale) * 255),
                        round(clamp(g * scale) * 255),
                        round(clamp(b * scale) * 255),
                    ),
                )
            )
        return commands

    def _effective_channel_floor(self) -> float:
        """
        Channel multiplier floor, honoring the user's lower bound.

        When a brightness floor is configured it replaces the mode's
        built-in floor: the floor is an absolute output level, so as a
        multiplier it is floor / brightness (capped below the max).
        """
        preset = self._preset
        if self._brightness_floor > 0.0 and self._brightness > 0.0:
            return min(
                self._brightness_floor / self._brightness,
                preset.channel_max - 0.01,
            )
        return preset.channel_floor

    @property
    def _preset(self) -> ModePreset:
        return MODE_PRESETS[self._mode]

    # -- promotion / consumption ------------------------------------------

    def _advance_spectrum(self, now_us: int) -> None:
        """Promote every frame due at ``now_us``; step bass saturation."""
        while self._pending_spectrum and self._pending_spectrum[0][0] <= now_us:
            _, bins = self._pending_spectrum.popleft()
            self._spectrum = list(bins)
            if self._preset.bass_only and bins:
                deep = bins[0] ** _BASS_CONTRAST_EXP
                energy = deep
                if len(bins) > 1:
                    gate = min(1.0, deep / _BASS_BIN1_GATE_FULL)
                    energy += _BASS_BIN1_WEIGHT * gate * bins[1] ** _BASS_CONTRAST_EXP
                energy = min(1.5, energy) * _BASS_LEVEL_GAIN
                alpha = _BASS_LEVEL_RISE if energy > self._bass_level else _BASS_LEVEL_DECAY
                self._bass_level += (energy - self._bass_level) * alpha
            bass_count = min(_BASS_SAT_BINS, len(bins))
            if not bass_count:
                continue
            bass_energy = sum(bins[:bass_count]) / bass_count
            self._bass_baseline += (bass_energy - self._bass_baseline) * _BASS_BASELINE_SMOOTHING
            bass_transient = max(0.0, bass_energy - self._bass_baseline)
            swing_raw = min(1.0, bass_transient * _BASS_TRANSIENT_SCALE)
            # The brightness pump follows the low-band transient (deep
            # bass + mid-bass when the mode restricts the band) with a
            # fast rise / slow decay per promoted frame, so kicks thump
            # then settle.
            pump_count = bass_count
            if self._preset.band_bin_max is not None:
                pump_count = min(self._preset.band_bin_max + 1, len(bins))
            if pump_count != bass_count:
                pump_energy = sum(bins[:pump_count]) / pump_count
                self._pump_baseline += (
                    pump_energy - self._pump_baseline
                ) * _BASS_BASELINE_SMOOTHING
                pump_raw = min(
                    1.0,
                    max(0.0, pump_energy - self._pump_baseline) * _BASS_TRANSIENT_SCALE,
                )
            else:
                pump_raw = swing_raw
            swing_alpha = _BASS_SAT_RISE if pump_raw > self._bass_swing else _BASS_SAT_DECAY
            self._bass_swing += (pump_raw - self._bass_swing) * swing_alpha
            swing = self._preset.bass_saturation_scale * swing_raw
            sat_target = _BASS_SAT_MIN + (_BASS_SAT_MAX - _BASS_SAT_MIN) * swing
            alpha = _BASS_SAT_RISE if sat_target > self._bass_saturation else _BASS_SAT_DECAY
            self._bass_saturation += (sat_target - self._bass_saturation) * alpha

    def _consume_peaks(self, now_us: int) -> float:
        """Promote due peaks; return the decayed global boost."""
        while self._pending_peaks and self._pending_peaks[0][0] <= now_us:
            ts, boost = self._pending_peaks.popleft()
            if self._peak_set_at_us is None or boost >= self._peak_boost:
                self._peak_boost = boost
                self._peak_set_at_us = ts
            self._peak_palette_position += self._preset.palette_advance
        if self._peak_set_at_us is None:
            return 0.0
        elapsed = now_us - self._peak_set_at_us
        if elapsed < 0:
            return 0.0
        if elapsed >= _PEAK_BOOST_DECAY_US:
            self._peak_set_at_us = None
            self._peak_boost = 0.0
            return 0.0
        return self._peak_boost * (1.0 - elapsed / _PEAK_BOOST_DECAY_US)

    # -- per-channel multipliers -------------------------------------------

    def _ensure_filters(self) -> None:
        n = len(self._channels)
        if len(self._channel_filters) != n:
            self._channel_filters = [_ExpFilter(_CHANNEL_RISE, _CHANNEL_DECAY) for _ in range(n)]
            self._channel_baselines = [
                _ExpFilter(_CHANNEL_BASELINE_RISE, _CHANNEL_BASELINE_DECAY) for _ in range(n)
            ]

    def _transient_multiplier(self, index: int, energy: float) -> float:
        preset = self._preset
        floor = self._effective_channel_floor()
        if preset.channel_transient_scale <= 0.0:
            return floor
        fast = self._channel_filters[index].update(energy)
        baseline = self._channel_baselines[index].update(energy)
        transient = max(0.0, fast - baseline)
        modulation = min(1.0, transient * preset.channel_transient_scale)
        return floor + (preset.channel_max - floor) * modulation

    def _channel_multipliers(self) -> list[float]:
        """Per-channel multipliers from spectrum-band transients."""
        n = len(self._channels)
        if n == 0:
            return []
        if not self._spectrum:
            return [1.0] * n
        self._ensure_filters()
        bins = self._spectrum
        if self._preset.bass_only:
            # Brightness = the low-band level tracked per spectrum
            # frame in _advance_spectrum (LedFx-style level following),
            # mapped from the threshold up to full. One shared value
            # for every channel, so the room moves as one. The hard
            # per-channel minimum does not apply here — it would
            # override this mode's deliberately low rest floor.
            drive = clamp(
                (self._bass_level - _BASS_LEVEL_THRESHOLD) / (1.0 - _BASS_LEVEL_THRESHOLD)
            )
            floor = self._effective_channel_floor()
            return [floor + (self._preset.channel_max - floor) * drive] * n
        bin_cap = self._preset.band_bin_max
        usable = min(_CHANNEL_BIN_MAX if bin_cap is None else bin_cap, len(bins) - 1) + 1
        # The hard minimum multiplier must not override a configured
        # brightness floor, or the floor setting would silently do
        # nothing for low values.
        min_mult = _PER_CHANNEL_MIN_MULT
        if self._brightness_floor > 0.0:
            min_mult = min(min_mult, self._effective_channel_floor())
        if n == 1:
            energy = sum(bins[:usable]) / usable
            return [max(min_mult, self._transient_multiplier(0, energy))]
        multipliers = []
        for i in range(n):
            lo = (i * usable) // n
            hi = max(lo + 1, ((i + 1) * usable) // n)
            band = bins[lo:hi]
            energy = sum(band) / len(band) if band else 0.0
            multipliers.append(max(min_mult, self._transient_multiplier(i, energy)))
        return multipliers

    # -- beats / palette walking ---------------------------------------------

    def _prune_beats(self, now_us: int) -> None:
        """Keep at most one past beat (the current segment's start)."""
        past = 0
        for beat in self._beats:
            if beat.timestamp_us <= now_us:
                past += 1
            else:
                break
        while past > 1:
            self._beats.popleft()
            past -= 1

    def _current_segment(
        self, now_us: int
    ) -> tuple[_ScheduledBeat | None, _ScheduledBeat | None, int | None]:
        if not self._beats:
            return None, None, None
        prior_idx = -1
        for i, beat in enumerate(self._beats):
            if beat.timestamp_us <= now_us:
                prior_idx = i
            else:
                break
        if prior_idx < 0 or prior_idx + 1 >= len(self._beats):
            return None, None, None
        prior = self._beats[prior_idx]
        next_beat = self._beats[prior_idx + 1]
        segment = next_beat.timestamp_us - prior.timestamp_us
        if segment <= 0:
            return None, None, None
        return prior, next_beat, segment

    def _compute_pulse(
        self, now_us: int, prior: _ScheduledBeat, next_beat: _ScheduledBeat, segment: int
    ) -> float:
        preset = self._preset
        half_width = max(1, int(segment * _PULSE_HALF_FRACTION))
        max_effect = 0.0
        for beat in (prior, next_beat):
            dist = abs(now_us - beat.timestamp_us)
            if dist >= half_width:
                continue
            peak = preset.downbeat_pulse_peak if beat.is_downbeat else preset.pulse_peak
            effect = (peak - 1.0) * (1.0 - dist / half_width)
            if abs(effect) > abs(max_effect):
                max_effect = effect
        return 1.0 + max_effect

    def _palette_color_at(self, position: float, sat_factor: float) -> tuple[float, float, float]:
        palette = self._palette
        palette_len = len(palette)
        position %= palette_len
        low = int(position) % palette_len
        high = (low + 1) % palette_len
        frac = position - int(position)
        color = _lerp(palette[low], palette[high], frac)
        if sat_factor < 0.999:
            color = _scale_saturation(color, sat_factor)
        return color

    def _per_channel_colors(
        self,
        prior: _ScheduledBeat,
        next_beat: _ScheduledBeat,
        now_us: int,
        segment: int,
    ) -> list[tuple[float, float, float]]:
        preset = self._preset
        palette_len = len(self._palette)
        if palette_len == 1:
            return [self._palette[0] for _ in self._channels]
        anim_fraction = preset.beat_animation_fraction
        t_norm = clamp((now_us - prior.timestamp_us) / segment)
        hold_until = 1.0 - anim_fraction
        if t_norm <= hold_until or anim_fraction <= 0.0:
            time_blend = 0.0
        else:
            time_blend = (t_norm - hold_until) / anim_fraction
        prior_slot = (prior.beat_in_bar * preset.palette_advance) % palette_len
        next_slot = (next_beat.beat_in_bar * preset.palette_advance) % palette_len
        delta = (next_slot - prior_slot) % palette_len
        if delta > palette_len / 2:
            delta -= palette_len
        beat_position = prior_slot + time_blend * delta
        drift = preset.spatial_drift_per_second * (now_us / 1_000_000.0)
        return [
            self._palette_color_at(
                beat_position + channel.position * preset.color_gradient_spread + drift,
                self._bass_saturation,
            )
            for channel in self._channels
        ]

    def _peak_walk_colors(self, now_us: int) -> list[tuple[float, float, float]]:
        preset = self._preset
        if len(self._palette) == 1:
            return [self._palette[0] for _ in self._channels]
        drift = preset.spatial_drift_per_second * (now_us / 1_000_000.0)
        return [
            self._palette_color_at(
                self._peak_palette_position
                + channel.position * preset.color_gradient_spread
                + drift,
                self._bass_saturation,
            )
            for channel in self._channels
        ]


def _lerp(
    a: tuple[float, float, float], b: tuple[float, float, float], frac: float
) -> tuple[float, float, float]:
    return (
        a[0] + (b[0] - a[0]) * frac,
        a[1] + (b[1] - a[1]) * frac,
        a[2] + (b[2] - a[2]) * frac,
    )


def _scale_saturation(rgb: tuple[float, float, float], factor: float) -> tuple[float, float, float]:
    """Scale HSV saturation by ``factor`` (hue and value preserved)."""
    r, g, b = rgb
    peak = max(r, g, b)
    if peak <= 0.0:
        return rgb
    low = min(r, g, b)
    if peak == low:
        return rgb
    # Rebuild from the HSV representation with scaled saturation.
    sat = (peak - low) / peak * factor
    # Keep the channel ratios that define hue; re-derive the floor.
    new_low = peak * (1.0 - sat)
    return (
        new_low + (r - low) / (peak - low) * (peak - new_low),
        new_low + (g - low) / (peak - low) * (peak - new_low),
        new_low + (b - low) / (peak - low) * (peak - new_low),
    )


__all__ = [
    "MODE_PRESETS",
    "LifxAudioAnalyzer",
    "LightChannel",
    "LightColorCommand",
    "ModePreset",
]
