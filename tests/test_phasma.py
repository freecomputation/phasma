#!/usr/bin/env python3
"""Tests for phasma. Stdlib unittest only; all fixtures are generated.

Run from the repository root:  python3 -m unittest discover -s tests
"""

import array
import math
import os
import sys
import tempfile
import unittest
import wave

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import phasma


def make_session(tmp):
    return phasma.Session(tmp, "test")


class FakeArgs:
    pass


class RtlPowerParseTest(unittest.TestCase):
    def test_data_line(self):
        line = ("2026-11-10, 23:00:00, 88000000, 108000000, 125000, "
                "12, -45.0, -44.0, -20.5")
        low, step, powers = phasma.parse_rtl_power_line(line)
        self.assertEqual(low, 88_000_000)
        self.assertEqual(step, 125_000)
        self.assertEqual(powers, [-45.0, -44.0, -20.5])

    def test_garbage_lines(self):
        self.assertIsNone(phasma.parse_rtl_power_line(""))
        self.assertIsNone(phasma.parse_rtl_power_line("date, time, hz"))
        self.assertIsNone(phasma.parse_rtl_power_line("a,b,c,d,e,f"))


class FieldMeterTest(unittest.TestCase):
    def test_baseline_then_excursion(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = make_session(tmp)
            meter = phasma.FieldMeter(session, threshold_db=6.0)
            meter.feed(88e6, 125e3, [-45.0, -46.0, -44.0], 1)
            self.assertEqual(meter.excursions, 0)
            meter.feed(88e6, 125e3, [-45.0, -30.0, -44.0], 2)
            self.assertEqual(meter.excursions, 1)
            self.assertEqual(meter.peak[1], -30.0)
            with open(session.log_path) as fh:
                log = fh.read()
            self.assertIn("EXCURSION", log)
            self.assertIn("88.1250 MHz", log)  # bin 1 of the pass

    def test_quiet_room_no_excursions(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = make_session(tmp)
            meter = phasma.FieldMeter(session, threshold_db=6.0)
            meter.feed(88e6, 125e3, [-45.0, -45.5], 1)
            meter.feed(88e6, 125e3, [-44.8, -45.2], 2)
            self.assertEqual(meter.excursions, 0)


class AudioTest(unittest.TestCase):
    def test_rms_dbfs(self):
        self.assertEqual(phasma.rms_dbfs(array.array("h", [0, 0, 0])),
                         float("-inf"))
        loud = array.array("h", [16384] * 100)
        self.assertAlmostEqual(phasma.rms_dbfs(loud), -6.02, places=1)

    def test_wav_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "round.wav")
            samples = array.array("h", [100, -200, 300] * 50)
            phasma.write_wav(path, 32000, samples)
            rate, back = phasma.read_wav(path)
            self.assertEqual(rate, 32000)
            self.assertEqual(list(back), list(samples))


class WordLayerTest(unittest.TestCase):
    def test_word_fires_on_outlier_and_is_labeled(self):
        import random
        with tempfile.TemporaryDirectory() as tmp:
            session = make_session(tmp)
            layer = phasma.WordLayer(session, random.Random(7))
            levels = [(88_000_000 + i * 200_000, -30.0) for i in range(20)]
            levels[10] = (90_000_000, -12.0)
            layer.consider_pass(levels, lambda f: "%.1f MHz" % (f / 1e6))
            with open(session.log_path) as fh:
                log = fh.read()
            self.assertIn("WORD", log)
            self.assertIn("not a measurement", log)
            self.assertIn("90.0 MHz", log)

    def test_no_word_in_flat_noise(self):
        import random
        with tempfile.TemporaryDirectory() as tmp:
            session = make_session(tmp)
            layer = phasma.WordLayer(session, random.Random(7))
            levels = [(i, -30.0) for i in range(20)]
            layer.consider_pass(levels, lambda f: str(f))
            with open(session.log_path) as fh:
                self.assertNotIn("WORD", fh.read())


class TunerHoldersTest(unittest.TestCase):
    def test_holder_detection_from_proc(self):
        with tempfile.TemporaryDirectory() as proc:
            for pid, cmd in ((101, "python3 -m passband run\0"),
                             (102, "/bin/true\0"),
                             (103, "rtl_fm -f 100M\0")):
                folder = os.path.join(proc, str(pid))
                os.makedirs(folder)
                with open(os.path.join(folder, "cmdline"), "wb") as fh:
                    fh.write(cmd.encode())
            holders = phasma.tuner_holders(proc_root=proc, own_pid=999)
            text = " ".join(holders)
            self.assertIn("passband", text)
            self.assertIn("rtl_fm", text)
            self.assertEqual(len(holders), 2)


class SessionTest(unittest.TestCase):
    def test_two_sessions_in_one_second_get_distinct_folders(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = make_session(tmp)
            second = make_session(tmp)
            self.assertNotEqual(first.dir, second.dir)
            self.assertTrue(os.path.isdir(first.dir))
            self.assertTrue(os.path.isdir(second.dir))


class SimulatePipelinesTest(unittest.TestCase):
    """The tuner-free paths: field over a CSV, spirit over a WAV."""

    def _field_csv(self, path):
        lines = []
        for second_pass in (False, True):
            powers = [-45.0] * 8
            if second_pass:
                powers[3] = -19.0   # a "station" appears in bin 3
            row = ["2026-11-10", "23:00:00", "88000000", "108000000",
                   "2500000", "12"] + ["%.1f" % p for p in powers]
            lines.append(", ".join(row))
        with open(path, "w") as fh:
            fh.write("\n".join(lines) + "\n")

    def test_field_simulate_end_to_end(self):
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = os.path.join(tmp, "sweep.csv")
            self._field_csv(csv_path)
            args = FakeArgs()
            args.home = tmp
            args.simulate = csv_path
            args.threshold = 6.0
            self.assertEqual(phasma.cmd_field(args), 0)
            session_dirs = os.listdir(os.path.join(tmp, "sessions"))
            self.assertEqual(len(session_dirs), 1)
            sdir = os.path.join(tmp, "sessions", session_dirs[0])
            with open(os.path.join(sdir, "session.log")) as fh:
                log = fh.read()
            self.assertIn("EXCURSION", log)
            self.assertIn("95.5000 MHz", log)   # 88 + 3 * 2.5 MHz
            self.assertTrue(os.path.exists(os.path.join(sdir, "field.csv")))

    def test_spirit_simulate_end_to_end(self):
        with tempfile.TemporaryDirectory() as tmp:
            rate = 32000
            samples = array.array("h")
            # 10 quiet windows then one loud "station" window
            for idx in range(11):
                amp = 12000 if idx == 5 else 300
                for n in range(rate // 4):       # 250 ms per window
                    samples.append(int(amp * math.sin(
                        2 * math.pi * 440 * n / rate)))
            wav_path = os.path.join(tmp, "fixture.wav")
            with wave.open(wav_path, "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(rate)
                wav.writeframes(samples.tobytes())
            args = FakeArgs()
            args.home = tmp
            args.simulate = wav_path
            args.dwell = 250
            self.assertEqual(phasma.cmd_spirit(args), 0)
            session_dirs = os.listdir(os.path.join(tmp, "sessions"))
            sdir = os.path.join(tmp, "sessions", session_dirs[0])
            out_wav = os.path.join(sdir, "spirit.wav")
            self.assertTrue(os.path.exists(out_wav))
            self.assertGreater(os.path.getsize(out_wav), 40_000)
            with open(os.path.join(sdir, "session.log")) as fh:
                log = fh.read()
            self.assertIn("simulate mode", log)
            self.assertIn("WORD", log)          # the loud window fired
            self.assertIn("not a measurement", log)


if __name__ == "__main__":
    unittest.main()
