"""Tests for lifx_lights_sync.analyzer (pure stdlib module)."""

from __future__ import annotations

from lifx_lights_sync.analyzer import (
    LifxAudioAnalyzer,
    LightChannel,
    LightColorCommand,
)

CHANNELS = [
    LightChannel("aaaa00000001", 0.0),
    LightChannel("aaaa00000002", 0.33),
    LightChannel("aaaa00000003", 0.66),
    LightChannel("aaaa00000004", 1.0),
]
FLAT_PALETTE = [(200, 200, 200)]


def _total(commands: list[LightColorCommand]) -> float:
    """Return the summed RGB brightness over all commands."""
    return sum(sum(command.rgb) for command in commands)


class TestRenderBasics:
    """Tests for Render Basics."""

    def test_render_returns_one_command_per_channel(self) -> None:
        """Render returns one command per channel."""
        analyzer = LifxAudioAnalyzer(CHANNELS, mode="smooth", brightness=0.8)
        analyzer.queue_spectrum(1_000_000, [0.4] * 17)
        commands = analyzer.render(1_000_000)
        assert (len(commands)) == (len(CHANNELS))
        assert ([command.channel_id for command in commands]) == (
            [channel.channel_id for channel in CHANNELS]
        )
        for command in commands:
            assert all(0 <= value <= 255 for value in command.rgb)

    def test_render_without_inputs_still_renders(self) -> None:
        """Render without inputs still renders."""
        analyzer = LifxAudioAnalyzer(CHANNELS)
        commands = analyzer.render(500_000)
        assert (len(commands)) == (len(CHANNELS))

    def test_matrix_zone_channels_render(self) -> None:
        """Matrix zone channels render."""
        channels = [
            LightChannel("bbbb00000001:zone:0", 0.0, is_matrix_zone=True),
            LightChannel("bbbb00000001:zone:1", 1.0, is_matrix_zone=True),
        ]
        analyzer = LifxAudioAnalyzer(channels, mode="energetic")
        analyzer.queue_spectrum(0, [0.9] * 17)
        commands = analyzer.render(0)
        assert (len(commands)) == (2)
        assert commands[0].channel_id.endswith(":zone:0")

    def test_peak_values_in_255_range_are_accepted(self) -> None:
        """Peak values in 255 range are accepted."""
        analyzer = LifxAudioAnalyzer(CHANNELS, mode="energetic", palette=FLAT_PALETTE)
        analyzer.queue_spectrum(0, [200] * 17)  # 0-255 style bins
        commands = analyzer.render(0)
        assert (_total(commands)) > (0)


class TestBeatPulse:
    """Tests for Beat Pulse."""

    def test_beat_pulse_raises_brightness_near_a_beat(self) -> None:
        """Beat pulse raises brightness near a beat."""
        analyzer = LifxAudioAnalyzer(
            CHANNELS, mode="energetic", brightness=0.8, palette=FLAT_PALETTE
        )
        analyzer.queue_spectrum(1_000_000, [0.5] * 17)
        analyzer.queue_beat(1_000_000, is_downbeat=False)
        analyzer.queue_beat(2_000_000, is_downbeat=True)
        analyzer.queue_beat(3_000_000, is_downbeat=False)
        mid_segment = _total(analyzer.render(1_500_000))
        at_beat = _total(analyzer.render(2_000_000))
        assert (at_beat) > (mid_segment)

    def test_pulse_decays_after_the_beat(self) -> None:
        """Pulse decays after the beat."""
        analyzer = LifxAudioAnalyzer(
            CHANNELS, mode="energetic", brightness=0.8, palette=FLAT_PALETTE
        )
        analyzer.queue_spectrum(0, [0.5] * 17)
        analyzer.queue_beat(1_000_000, is_downbeat=False)
        analyzer.queue_beat(2_000_000, is_downbeat=False)
        analyzer.queue_beat(3_000_000, is_downbeat=False)
        at_beat = _total(analyzer.render(2_000_000))
        mid_next = _total(analyzer.render(2_500_000))
        assert (at_beat) > (mid_next)


class TestAmbientIgnoresOnset:
    """Tests for Ambient Ignores Onset."""

    def test_ambient_does_not_spike_on_a_transient(self) -> None:
        # Sustained loud spectrum in one analyzer...
        """Ambient does not spike on a transient."""
        sustained = LifxAudioAnalyzer(
            CHANNELS, mode="ambient", brightness=0.8, palette=FLAT_PALETTE
        )
        for step in range(10):
            sustained.queue_spectrum(step * 50_000, [0.9] * 17)
        sustained_total = _total(sustained.render(500_000))

        # ...versus a sudden transient from silence in another.
        transient = LifxAudioAnalyzer(
            CHANNELS, mode="ambient", brightness=0.8, palette=FLAT_PALETTE
        )
        for step in range(9):
            transient.queue_spectrum(step * 50_000, [0.02] * 17)
        transient.queue_spectrum(500_000, [0.9] * 17)
        transient.queue_onset(500_000)
        transient_total = _total(transient.render(500_000))

        # Ambient has no onset boost: the transient render must not
        # exceed the sustained render.
        assert (transient_total) <= (sustained_total)

    def test_energetic_does_respond_to_transients_more_than_ambient(self) -> None:
        """Energetic does respond to transients more than ambient."""

        def run(mode: str) -> float:
            """Render one transient in the given mode and return its total brightness."""
            analyzer = LifxAudioAnalyzer(CHANNELS, mode=mode, brightness=0.8, palette=FLAT_PALETTE)
            for step in range(9):
                analyzer.queue_spectrum(step * 50_000, [0.02] * 17)
            analyzer.queue_spectrum(500_000, [0.95] * 17)
            analyzer.queue_onset(500_000)
            return _total(analyzer.render(500_000))

        assert (run("energetic")) > (run("ambient"))


class TestModeHandling:
    """Tests for Mode Handling."""

    def test_unknown_mode_falls_back_to_default(self) -> None:
        """Unknown mode falls back to default."""
        analyzer = LifxAudioAnalyzer(CHANNELS, mode="bogus-mode")
        assert (analyzer.mode) in (("smooth", "ambient", "flashing", "energetic"))

    def test_set_mode_and_brightness(self) -> None:
        """Set mode and brightness."""
        analyzer = LifxAudioAnalyzer(CHANNELS, mode="smooth", brightness=0.5)
        analyzer.set_mode("flashing")
        assert (analyzer.mode) == ("flashing")
        analyzer.set_mode("bogus-mode")  # ignored
        assert (analyzer.mode) == ("flashing")
        dim = LifxAudioAnalyzer(CHANNELS, brightness=0.2, palette=FLAT_PALETTE)
        bright = LifxAudioAnalyzer(CHANNELS, brightness=1.0, palette=FLAT_PALETTE)
        dim.queue_spectrum(0, [0.5] * 17)
        bright.queue_spectrum(0, [0.5] * 17)
        assert (_total(dim.render(0))) < (_total(bright.render(0)))


class TestSpectrumEncoding:
    """Tests for Spectrum Encoding."""

    def test_uint16_db_scaled_frames_are_not_clamped_flat(self) -> None:
        # Regression: the Sendspin server sends spectrum bins as uint16
        # dB-scaled values in [0, 65535]. Treating them as 0-255 clamped
        # every bin to 1.0 and froze the light output at a static color.
        # Under the transient model a loud frame after silence flashes
        # (fast follower jumps, baseline lags), while a quiet frame —
        # below the noise gate — leaves the channels at their floor, so
        # the two renders must differ clearly.
        """Uint16 db scaled frames are not clamped flat."""
        loud = LifxAudioAnalyzer(CHANNELS, mode="smooth", brightness=0.8)
        loud.queue_spectrum(1_000_000, [45000.0] * 17)
        loud_total = _total(loud.render(1_000_000))
        quiet = LifxAudioAnalyzer(CHANNELS, mode="smooth", brightness=0.8)
        quiet.queue_spectrum(1_000_000, [1000.0] * 17)
        quiet_total = _total(quiet.render(1_000_000))
        assert (loud_total) > (quiet_total), (
            "uint16 spectrum frames must modulate the output brightness"
        )


class TestPlayheadPromotion:
    """Tests for Playhead Promotion."""

    def test_future_frames_do_not_affect_render_until_due(self) -> None:
        # Frames arrive ahead of the playhead (~30 s lookahead from the
        # extraction pipeline). A frame must not change the output until
        # render time reaches its timestamp.
        """Future frames do not affect render until due."""
        analyzer = LifxAudioAnalyzer(CHANNELS, mode="smooth", brightness=0.8)
        analyzer.queue_spectrum(31_000_000, [45000.0] * 17)
        early = analyzer.render(1_000_000)
        reference = LifxAudioAnalyzer(CHANNELS, mode="smooth", brightness=0.8)
        assert ([c.rgb for c in early]) == ([c.rgb for c in reference.render(1_000_000)])
        due = analyzer.render(31_000_000)
        assert ([c.rgb for c in due]) != ([c.rgb for c in early])

    def test_lookahead_burst_survives_until_due(self) -> None:
        # Regression: queue-time pruning/filtering used to destroy
        # lookahead frames before their moment, freezing the output.
        """Lookahead burst survives until due."""
        analyzer = LifxAudioAnalyzer(CHANNELS, mode="smooth", brightness=0.8)
        base = 100_000_000
        for i in range(100):
            analyzer.queue_spectrum(base + 30_000_000 + i * 50_000, [45000.0] * 17)
        before = analyzer.render(base)
        after = analyzer.render(base + 35_000_000)
        assert ([c.rgb for c in after]) != ([c.rgb for c in before])

    def test_beat_schedule_pushed_ahead_is_not_pruned(self) -> None:
        """Beat schedule pushed ahead is not pruned."""
        analyzer = LifxAudioAnalyzer(
            CHANNELS, mode="energetic", brightness=0.8, palette=FLAT_PALETTE
        )
        analyzer.queue_spectrum(1_000_000, [0.5] * 17)
        analyzer.queue_beat(61_000_000, is_downbeat=True)
        analyzer.queue_beat(62_000_000, is_downbeat=True)
        analyzer.queue_beat(63_000_000, is_downbeat=True)
        analyzer.render(1_000_000)
        mid_segment = _total(analyzer.render(61_500_000))
        at_beat = _total(analyzer.render(62_000_000))
        assert (at_beat) > (mid_segment)

    def test_peak_boost_flashes_then_decays(self) -> None:
        """Peak boost flashes then decays."""
        analyzer = LifxAudioAnalyzer(CHANNELS, mode="smooth", brightness=0.8, palette=FLAT_PALETTE)
        analyzer.queue_spectrum(1_000_000, [0.5] * 17)
        base = _total(analyzer.render(1_000_000))
        analyzer.queue_peak(2_000_000, 255)
        flash = _total(analyzer.render(2_000_000))
        assert (flash) > (base)
        decayed = _total(analyzer.render(3_000_000))
        assert (decayed) < (flash)


class TestBassBrightnessPump:
    """
    Energetic routes the bass transient into a global brightness lift.

    Single channel + flat palette: the channel multiplier sees the mean
    of all bins (identical for both frames) and the palette contributes
    no saturation variance, so any total difference is the bass pump.
    """

    ONE = (LightChannel("aaaa00000001", 0.0),)
    BASS_FRAME = [45000.0, 45000.0] + [100.0] * 15
    TREBLE_FRAME = [100.0] * 10 + [45000.0] * 2 + [100.0] * 5

    def _run(self, mode: str, frame: list[float]) -> float:
        """Feed the test frame in the given mode and return the rendered total brightness."""
        analyzer = LifxAudioAnalyzer(self.ONE, mode=mode, brightness=0.8, palette=FLAT_PALETTE)
        for i in range(10):  # quiet lead-in so the baseline settles low
            analyzer.queue_spectrum(1_000_000 + i * 50_000, [1000.0] * 17)
        analyzer.render(1_500_000)
        analyzer.queue_spectrum(2_000_000, frame)
        return _total(analyzer.render(2_000_000))

    def test_energetic_pumps_on_bass_not_treble(self) -> None:
        """Energetic pumps on bass not treble."""
        assert (self._run("energetic", self.BASS_FRAME)) > (
            self._run("energetic", self.TREBLE_FRAME)
        )

    def test_energetic_ignores_high_frequencies(self) -> None:
        # Bins above the low band (voice/instruments) must not move
        # the light at all in the bass-heavy mode.
        """Energetic ignores high frequencies."""
        quiet = LifxAudioAnalyzer(self.ONE, mode="energetic", brightness=0.8, palette=FLAT_PALETTE)
        for i in range(10):
            quiet.queue_spectrum(1_000_000 + i * 50_000, [1000.0] * 17)
        quiet.render(1_500_000)
        quiet_total = _total(quiet.render(2_000_000))
        high_total = self._run("energetic", self.TREBLE_FRAME)
        assert (high_total) == (quiet_total)

    def test_brightness_floor_lowers_rest_not_pop(self) -> None:
        # With brightness 1.0 and floor 0.1, a quiet passage must rest
        # at ~10% output while a bass hit still pops far above it.
        """Brightness floor lowers rest not pop."""
        quiet = LifxAudioAnalyzer(
            self.ONE,
            mode="energetic",
            brightness=1.0,
            brightness_floor=0.1,
            palette=FLAT_PALETTE,
        )
        for i in range(12):
            quiet.queue_spectrum(1_000_000 + i * 50_000, [1000.0] * 17)
        quiet.render(1_600_000)
        rest = _total(quiet.render(2_000_000))
        assert (rest) < (3 * 255 * 0.2)  # well under the old 30% rest
        loud = LifxAudioAnalyzer(
            self.ONE,
            mode="energetic",
            brightness=1.0,
            brightness_floor=0.1,
            palette=FLAT_PALETTE,
        )
        for i in range(12):
            loud.queue_spectrum(1_000_000 + i * 50_000, self.BASS_FRAME)
        loud.render(1_600_000)
        pop = _total(loud.render(2_000_000))
        assert (pop) > (rest * 3)

    def test_smooth_still_reacts_to_high_frequencies(self) -> None:
        # The band restriction is Energetic-only: Smooth keeps the
        # full-range response, so a loud high frame changes its output.
        """Smooth still reacts to high frequencies."""
        quiet = LifxAudioAnalyzer(self.ONE, mode="smooth", brightness=0.8, palette=FLAT_PALETTE)
        for i in range(10):
            quiet.queue_spectrum(1_000_000 + i * 50_000, [1000.0] * 17)
        quiet.render(1_500_000)
        quiet_total = _total(quiet.render(2_000_000))
        high_total = self._run("smooth", self.TREBLE_FRAME)
        assert (high_total) != (quiet_total)


class TestBassMode:
    """
    Dedicated Bass mode: bins 0-1 only, shared envelope, nothing else.

    Regression target: Energetic still reacted to content up to
    887 Hz (bins 2-3) and to every scheduled beat. Bass mode must be
    deaf to all of that — only low-end transients move the lights.
    """

    QUIET_FRAME = [1000.0] * 17
    BASS_FRAME = [45000.0, 45000.0] + [100.0] * 15
    BIN2_FRAME = [100.0, 100.0, 45000.0] + [100.0] * 14  # 369-602 Hz
    TREBLE_FRAME = [100.0] * 10 + [45000.0] * 2 + [100.0] * 5

    def _analyzer(self) -> LifxAudioAnalyzer:
        """Build a Bass-mode analyzer over the shared channels."""
        return LifxAudioAnalyzer(CHANNELS, mode="bass", brightness=0.8, palette=FLAT_PALETTE)

    def _settled(self, frame: list[float]) -> LifxAudioAnalyzer:
        """Analyzer fed 10 quiet frames, settled, then one test frame."""
        analyzer = self._analyzer()
        for i in range(10):
            analyzer.queue_spectrum(1_000_000 + i * 50_000, self.QUIET_FRAME)
        analyzer.render(1_500_000)
        analyzer.queue_spectrum(2_000_000, frame)
        return analyzer

    def test_bass_hit_pops_all_channels_together(self) -> None:
        """Bass hit pops all channels together."""
        commands = self._settled(self.BASS_FRAME).render(2_000_000)
        quiet = self._settled(self.QUIET_FRAME).render(2_000_000)
        assert (_total(commands)) > (_total(quiet) * 2)
        # Shared envelope + flat palette: every channel identical.
        assert (len({c.rgb for c in commands})) == (1)

    def test_voice_fundamental_in_bin1_alone_is_ignored(self) -> None:
        # A sung note at ~250 Hz lives in bin 1 (177-369 Hz) with bin 0
        # quiet. LedFx's Beat/Bass bulbs never hear it (their bands end
        # at 100/250 Hz); with bin 1 gated by bin 0, neither do ours.
        """Voice fundamental in bin1 alone is ignored."""
        voice_frame = [100.0, 45000.0] + [100.0] * 15
        quiet = [c.rgb for c in self._settled(self.QUIET_FRAME).render(2_000_000)]
        commands = [c.rgb for c in self._settled(voice_frame).render(2_000_000)]
        assert (commands) == (quiet)

    def test_bin1_contributes_when_bin0_is_active(self) -> None:
        # A real bass event excites both bins: bin 0 moderately, bin 1
        # strongly. The gate opens and the combined energy pops.
        """Bin1 contributes when bin0 is active."""
        both_frame = [20000.0, 45000.0] + [100.0] * 15
        quiet_total = _total(self._settled(self.QUIET_FRAME).render(2_000_000))
        both_total = _total(self._settled(both_frame).render(2_000_000))
        assert (both_total) > (quiet_total * 2)

    def test_bin2_and_treble_do_not_move_the_lights(self) -> None:
        """Bin2 and treble do not move the lights."""
        quiet = [c.rgb for c in self._settled(self.QUIET_FRAME).render(2_000_000)]
        for frame in (self.BIN2_FRAME, self.TREBLE_FRAME):
            commands = [c.rgb for c in self._settled(frame).render(2_000_000)]
            assert (commands) == (quiet)

    def test_beat_schedule_has_no_effect(self) -> None:
        """Beat schedule has no effect."""
        plain = self._settled(self.BASS_FRAME)
        beaten = self._settled(self.BASS_FRAME)
        for ts in (1_000_000, 2_000_000, 3_000_000):
            beaten.queue_beat(ts, is_downbeat=True)
        assert ([c.rgb for c in beaten.render(2_000_000)]) == (
            [c.rgb for c in plain.render(2_000_000)]
        )

    def test_onset_peak_has_no_effect(self) -> None:
        """Onset peak has no effect."""
        plain = self._settled(self.BASS_FRAME)
        peaked = self._settled(self.BASS_FRAME)
        peaked.queue_peak(2_000_000, 255)
        assert ([c.rgb for c in peaked.render(2_000_000)]) == (
            [c.rgb for c in plain.render(2_000_000)]
        )

    def test_sustained_bass_stays_lit_then_releases_on_quiet(self) -> None:
        # Level following (LedFx behavior): a sustained bass passage
        # keeps the lights up — it must NOT suppress itself the way a
        # transient-vs-baseline design does — and when the bass stops,
        # the level decays back to the floor within a few frames.
        """Sustained bass stays lit then releases on quiet."""
        analyzer = self._analyzer()
        for i in range(10):
            analyzer.queue_spectrum(1_000_000 + i * 50_000, self.QUIET_FRAME)
        analyzer.render(1_500_000)
        first = None
        last = None
        for i in range(60):
            ts = 2_000_000 + i * 50_000
            analyzer.queue_spectrum(ts, self.BASS_FRAME)
            total = _total(analyzer.render(ts))
            if first is None:
                first = total
            last = total
        assert first is not None
        assert last is not None
        assert (last) >= (first * 0.8)
        quiet_total = None
        for i in range(30):
            ts = 6_000_000 + i * 50_000
            analyzer.queue_spectrum(ts, self.QUIET_FRAME)
            quiet_total = _total(analyzer.render(ts))
        assert quiet_total is not None
        assert (quiet_total) < (last * 0.4)
