"""Tests for lifx_lights_sync.analyzer (pure stdlib module)."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lifx_lights_sync.analyzer import (  # noqa: E402
    LightChannel,
    LifxAudioAnalyzer,
)

CHANNELS = [
    LightChannel("aaaa00000001", 0.0),
    LightChannel("aaaa00000002", 0.33),
    LightChannel("aaaa00000003", 0.66),
    LightChannel("aaaa00000004", 1.0),
]
FLAT_PALETTE = [(200, 200, 200)]


def _total(commands):
    return sum(sum(command.rgb) for command in commands)


class TestRenderBasics(unittest.TestCase):
    def test_render_returns_one_command_per_channel(self):
        analyzer = LifxAudioAnalyzer(CHANNELS, mode="smooth", brightness=0.8)
        analyzer.queue_spectrum(1_000_000, [0.4] * 17)
        commands = analyzer.render(1_000_000)
        self.assertEqual(len(commands), len(CHANNELS))
        self.assertEqual(
            [command.channel_id for command in commands],
            [channel.channel_id for channel in CHANNELS],
        )
        for command in commands:
            self.assertTrue(all(0 <= value <= 255 for value in command.rgb))

    def test_render_without_inputs_still_renders(self):
        analyzer = LifxAudioAnalyzer(CHANNELS)
        commands = analyzer.render(500_000)
        self.assertEqual(len(commands), len(CHANNELS))

    def test_matrix_zone_channels_render(self):
        channels = [
            LightChannel("bbbb00000001:zone:0", 0.0, is_matrix_zone=True),
            LightChannel("bbbb00000001:zone:1", 1.0, is_matrix_zone=True),
        ]
        analyzer = LifxAudioAnalyzer(channels, mode="energetic")
        analyzer.queue_spectrum(0, [0.9] * 17)
        commands = analyzer.render(0)
        self.assertEqual(len(commands), 2)
        self.assertTrue(commands[0].channel_id.endswith(":zone:0"))

    def test_peak_values_in_255_range_are_accepted(self):
        analyzer = LifxAudioAnalyzer(CHANNELS, mode="energetic", palette=FLAT_PALETTE)
        analyzer.queue_spectrum(0, [200] * 17)  # 0-255 style bins
        commands = analyzer.render(0)
        self.assertGreater(_total(commands), 0)


class TestBeatPulse(unittest.TestCase):
    def test_beat_pulse_raises_brightness_near_a_beat(self):
        analyzer = LifxAudioAnalyzer(
            CHANNELS, mode="energetic", brightness=0.8, palette=FLAT_PALETTE
        )
        analyzer.queue_spectrum(1_000_000, [0.5] * 17)
        before = _total(analyzer.render(1_000_000))
        # A second analyzer at the same musical moment, but with a beat:
        # identical spectrum, plus pulse -> strictly brighter.
        other = LifxAudioAnalyzer(
            CHANNELS, mode="energetic", brightness=0.8, palette=FLAT_PALETTE
        )
        other.queue_spectrum(1_000_000, [0.5] * 17)
        other.render(1_000_000)  # settle smoothing identically
        other.queue_beat(2_000_000, is_downbeat=True)
        at_beat = _total(other.render(2_000_000))
        self.assertGreater(at_beat, before)

    def test_pulse_decays_after_the_beat(self):
        analyzer = LifxAudioAnalyzer(
            CHANNELS, mode="energetic", brightness=0.8, palette=FLAT_PALETTE
        )
        analyzer.queue_spectrum(0, [0.5] * 17)
        analyzer.queue_beat(1_000_000, is_downbeat=False)
        at_beat = _total(analyzer.render(1_000_000))
        analyzer.queue_spectrum(3_000_000, [0.5] * 17)
        much_later = _total(analyzer.render(3_000_000))
        self.assertGreater(at_beat, much_later)


class TestAmbientIgnoresOnset(unittest.TestCase):
    def test_ambient_does_not_spike_on_a_transient(self):
        # Sustained loud spectrum in one analyzer...
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
        self.assertLessEqual(transient_total, sustained_total)

    def test_energetic_does_respond_to_transients_more_than_ambient(self):
        def run(mode):
            analyzer = LifxAudioAnalyzer(
                CHANNELS, mode=mode, brightness=0.8, palette=FLAT_PALETTE
            )
            for step in range(9):
                analyzer.queue_spectrum(step * 50_000, [0.02] * 17)
            analyzer.queue_spectrum(500_000, [0.95] * 17)
            analyzer.queue_onset(500_000)
            return _total(analyzer.render(500_000))

        self.assertGreater(run("energetic"), run("ambient"))


class TestModeHandling(unittest.TestCase):
    def test_unknown_mode_falls_back_to_default(self):
        analyzer = LifxAudioAnalyzer(CHANNELS, mode="bogus-mode")
        self.assertIn(analyzer.mode, ("smooth", "ambient", "flashing", "energetic"))

    def test_set_mode_and_brightness(self):
        analyzer = LifxAudioAnalyzer(CHANNELS, mode="smooth", brightness=0.5)
        analyzer.set_mode("flashing")
        self.assertEqual(analyzer.mode, "flashing")
        analyzer.set_mode("bogus-mode")  # ignored
        self.assertEqual(analyzer.mode, "flashing")
        dim = LifxAudioAnalyzer(CHANNELS, brightness=0.2, palette=FLAT_PALETTE)
        bright = LifxAudioAnalyzer(CHANNELS, brightness=1.0, palette=FLAT_PALETTE)
        dim.queue_spectrum(0, [0.5] * 17)
        bright.queue_spectrum(0, [0.5] * 17)
        self.assertLess(_total(dim.render(0)), _total(bright.render(0)))


if __name__ == "__main__":
    unittest.main()
