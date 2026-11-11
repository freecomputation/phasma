#!/usr/bin/env python3
"""Phasma - a paranormal investigation station built on an RTL-SDR receiver.

Phasma measures the radio spectrum directly and records what it measures.
It has three instruments, all writing into one timestamped session log:

  field   an RF field meter (rtl_power): real power per frequency bin,
          a per-room baseline, peak hold, excursions logged as they happen
  spirit  a spirit box (rtl_fm): an SB7-style sweep of the FM broadcast
          band with genuine demodulated audio - real static and real
          station fragments - played live and recorded to a WAV file
          in the session folder; an airband mode sweeps 118-137 MHz AM
          in rtl_fm scan mode, lingering where a carrier holds it
  doctor  report whether the receiver tools are present and whether
          the tuner is already in use by another program

The word cues in the spirit box and the sweep displays are theater fed
by real measurements: a word is offered when the measured audio energy
at a step crosses well above the sweep's own median, and the word
itself is chosen at random. The program says so wherever it happens.
Every number Phasma prints comes from the receiver.

Phasma is offline-first and stdlib-only. It never opens the tuner when
another program (Passband, readsb, another rtl_* tool) is using it:
it reports the conflict and stops. It never kills another process.

Sessions live under ~/.local/share/phasma/sessions/ unless PHASMA_HOME
or --home says otherwise. Each session is a folder named by start time
and command, holding session.log, the raw field-meter CSV, and the
spirit-box WAV.

Exit codes: 0 ok, 2 a required tool is missing, 3 the tuner is busy.
"""

import argparse
import array
import math
import os
import random
import shutil
import statistics
import subprocess
import sys
import time
import wave
from datetime import datetime

VERSION = "0.1.0"

FM_LOW, FM_HIGH, FM_STEP = 88_000_000, 108_000_000, 200_000
AIR_LOW, AIR_HIGH, AIR_STEP = 118_000_000, 137_000_000, 25_000
FIELD_BIN = 125_000
FM_RATE = 32_000          # rtl_fm -M wbfm output rate, int16 mono
AIR_RATE = 12_000         # rtl_fm -M am resampled output rate
TUNER_HOLDERS = ("rtl_power", "rtl_fm", "rtl_sdr", "rtl_tcp",
                 "rtl_airband", "readsb", "passband")

WORDS = (
    "yes", "no", "hello", "goodbye", "here", "there", "listen", "look",
    "wait", "help", "cold", "dark", "light", "door", "stairs", "room",
    "water", "fire", "name", "time", "before", "after", "below", "above",
    "behind", "near", "far", "stay", "leave", "come", "go", "find",
    "lost", "found", "mother", "father", "child", "man", "woman", "friend",
    "seven", "three", "one", "nine", "home", "road", "field", "stone",
    "voice", "noise", "signal", "static", "watch", "sleep", "wake", "run",
    "hide", "ask", "answer", "true", "false", "first", "last", "again",
    "never", "always", "soon", "now", "stop", "speak", "quiet", "loud",
)

BLOCKS = " .:-=+*#%@"


def now_iso():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def stamp():
    return datetime.now().strftime("%Y%m%d-%H%M%S")


# ---------------------------------------------------------------- sessions

class Session:
    """One investigation session: a folder, a log, and its recordings."""

    def __init__(self, home, command):
        root = os.path.join(home, "sessions")
        os.makedirs(root, exist_ok=True)
        base = os.path.join(root, "%s-%s" % (stamp(), command))
        self.dir = base
        suffix = 2
        while os.path.exists(self.dir):
            self.dir = "%s-%d" % (base, suffix)
            suffix += 1
        os.makedirs(self.dir)
        self.log_path = os.path.join(self.dir, "session.log")
        self.log("SESSION", "phasma %s %s started in %s"
                 % (VERSION, command, self.dir))

    def log(self, tag, message):
        line = "[%s] %s %s" % (now_iso(), tag, message)
        with open(self.log_path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        print(line, flush=True)

    def path(self, name):
        return os.path.join(self.dir, name)


def default_home():
    return os.environ.get(
        "PHASMA_HOME",
        os.path.join(os.path.expanduser("~"), ".local", "share", "phasma"))


# ------------------------------------------------------------ tool finding

def find_tool(name):
    """Locate an rtl-sdr tool on PATH or in ~/.local/bin."""
    found = shutil.which(name)
    if found:
        return found
    candidate = os.path.join(os.path.expanduser("~"), ".local", "bin", name)
    if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
        return candidate
    return None


def read_cmdlines(proc_root="/proc"):
    """Yield (pid, cmdline) for every process visible under /proc."""
    try:
        entries = os.listdir(proc_root)
    except OSError:
        return
    for entry in entries:
        if not entry.isdigit():
            continue
        try:
            with open(os.path.join(proc_root, entry, "cmdline"), "rb") as fh:
                raw = fh.read()
        except OSError:
            continue
        yield int(entry), raw.replace(b"\0", b" ").decode("utf-8", "replace")


def tuner_holders(proc_root="/proc", own_pid=None):
    """Names of other programs that appear to be holding the tuner.

    This reads /proc command lines only. It never opens the receiver
    and never signals another process.
    """
    own = own_pid if own_pid is not None else os.getpid()
    holders = []
    for pid, cmdline in read_cmdlines(proc_root):
        if pid == own:
            continue
        for name in TUNER_HOLDERS:
            if name in cmdline:
                holders.append("%s (pid %d)" % (name, pid))
                break
    return holders


def require_tools(session, names):
    tools = {}
    for name in names:
        tools[name] = find_tool(name)
        if tools[name] is None:
            session.log("ERROR", "required tool not found: %s" % name)
    return tools


def refuse_if_busy(session):
    holders = tuner_holders()
    if holders:
        session.log("TUNER", "receiver is in use by %s; phasma will not "
                    "preempt it. Stop that program or wait for it to "
                    "finish, then run phasma again."
                    % ", ".join(sorted(set(holders))))
        return True
    return False


# ------------------------------------------------------------------- audio

def rms_dbfs(samples):
    """RMS level of int16 samples in dBFS. Silence reads as -inf."""
    if not samples:
        return float("-inf")
    total = 0
    for value in samples:
        total += value * value
    rms = math.sqrt(total / len(samples))
    if rms <= 0:
        return float("-inf")
    return 20.0 * math.log10(rms / 32768.0)


def read_wav(path):
    with wave.open(path, "rb") as wav:
        rate = wav.getframerate()
        frames = wav.readframes(wav.getnframes())
    samples = array.array("h")
    samples.frombytes(frames)
    if sys.byteorder == "big":
        samples.byteswap()
    return rate, samples


def write_wav(path, rate, samples):
    frames = array.array("h", samples)
    if sys.byteorder == "big":
        frames.byteswap()
    with wave.open(path, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(frames.tobytes())


def bar(value, low, high, width=1):
    """One display character scaled between low and high."""
    if high <= low:
        return " "
    frac = (value - low) / (high - low)
    frac = max(0.0, min(1.0, frac))
    return BLOCKS[int(frac * (len(BLOCKS) - 1))]


# -------------------------------------------------------------- field meter

def parse_rtl_power_line(line):
    """Parse one rtl_power CSV line.

    Columns: date, time, Hz low, Hz high, Hz step, samples, dB, dB, ...
    Returns (low_hz, step_hz, [db, ...]) or None for a non-data line.
    """
    parts = [part.strip() for part in line.split(",")]
    if len(parts) < 7:
        return None
    try:
        low = float(parts[2])
        step = float(parts[4])
        powers = [float(part) for part in parts[6:] if part != ""]
    except ValueError:
        return None
    if not powers:
        return None
    return low, step, powers


def read_rtl_power_csv(path):
    passes = []
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            parsed = parse_rtl_power_line(line)
            if parsed is not None:
                passes.append(parsed)
    return passes


class FieldMeter:
    """Baseline, peak hold and excursion detection over rtl_power passes.

    The first pass is the room baseline. Every later bin reading above
    baseline + threshold is an excursion and is logged as it is seen.
    """

    def __init__(self, session, threshold_db=6.0):
        self.session = session
        self.threshold = threshold_db
        self.baseline = None
        self.peak = None
        self.peak_freq = None
        self.excursions = 0

    def feed(self, low, step, powers, pass_number):
        if self.baseline is None:
            self.baseline = list(powers)
            self.peak = list(powers)
            self.session.log(
                "FIELD", "baseline taken: %d bins from %.3f MHz, "
                "median %.1f dB, strongest %.1f dB"
                % (len(powers), low / 1e6,
                   statistics.median(powers), max(powers)))
            return
        if len(powers) != len(self.baseline):
            self.session.log("FIELD", "pass %d has %d bins, baseline has "
                             "%d; pass skipped" % (pass_number, len(powers),
                                                   len(self.baseline)))
            return
        strongest = None
        for idx, power in enumerate(powers):
            if power > self.peak[idx]:
                self.peak[idx] = power
            delta = power - self.baseline[idx]
            if delta >= self.threshold:
                freq = low + idx * step
                self.excursions += 1
                self.session.log(
                    "EXCURSION", "%.4f MHz at %.1f dB, %+.1f dB over "
                    "baseline (threshold %.1f dB)"
                    % (freq / 1e6, power, delta, self.threshold))
                if strongest is None or delta > strongest[0]:
                    strongest = (delta, freq, power)
        if strongest is not None:
            self.session.log(
                "FIELD", "pass %d strongest excursion %+.1f dB at "
                "%.4f MHz (%.1f dB)"
                % (pass_number, strongest[0], strongest[1] / 1e6,
                   strongest[2]))

    def display(self, low, step, powers):
        """A one-line sweep display of measured power (the radar layer)."""
        lo = min(powers)
        hi = max(powers)
        text = "".join(bar(power, lo, hi) for power in powers)
        self.session.log("SWEEP", "band activity, measured %.1f to %.1f dB "
                         "across %.1f-%.1f MHz: %s"
                         % (lo, hi, low / 1e6,
                            (low + step * len(powers)) / 1e6, text))

    def summary(self):
        if self.baseline is None:
            self.session.log("FIELD", "no sweep data was collected")
            return
        idx = max(range(len(self.peak)), key=lambda i: self.peak[i])
        self.session.log(
            "FIELD", "peak hold %.1f dB; %d excursions over threshold "
            "logged" % (self.peak[idx], self.excursions))


def cmd_field(args):
    session = Session(args.home, "field")
    meter = FieldMeter(session, threshold_db=args.threshold)
    if args.simulate:
        session.log("FIELD", "simulate mode: reading rtl_power CSV from %s "
                    "(receiver not opened)" % args.simulate)
        passes = read_rtl_power_csv(args.simulate)
        if not passes:
            session.log("ERROR", "no rtl_power data lines in %s"
                        % args.simulate)
            return 2
        shutil.copyfile(args.simulate, session.path("field.csv"))
    else:
        tools = require_tools(session, ["rtl_power"])
        if tools["rtl_power"] is None:
            return 2
        if refuse_if_busy(session):
            return 3
        csv_path = session.path("field.csv")
        command = [tools["rtl_power"],
                   "-f", "%d:%d:%d" % (args.low, args.high, args.bin),
                   "-i", str(args.interval), "-e", str(args.seconds),
                   "-g", str(args.gain), csv_path]
        session.log("FIELD", "running %s" % " ".join(command))
        subprocess.run(command, check=False)
        passes = read_rtl_power_csv(csv_path) \
            if os.path.exists(csv_path) else []
        if not passes:
            session.log("ERROR", "rtl_power produced no data")
            return 2
    for number, (low, step, powers) in enumerate(passes, start=1):
        meter.feed(low, step, powers, number)
        meter.display(low, step, powers)
    meter.summary()
    session.log("SESSION", "field meter finished; files in %s" % session.dir)
    return 0


# -------------------------------------------------------------- spirit box

class WordLayer:
    """Theater fed by real measurements.

    A word cue fires when one window's measured audio energy stands
    well above the median of its own sweep pass. The triggering level
    and frequency are logged with the word. The word itself is chosen
    at random; a word cue is not a measurement and is never logged as
    one.
    """

    def __init__(self, session, rng):
        self.session = session
        self.rng = rng
        self.cooldown = 0

    def consider_pass(self, levels, freq_of):
        """levels: [(freq_hz, dbfs)] for one pass. Fires on outliers."""
        values = [db for _, db in levels if db != float("-inf")]
        if not values:
            return
        median = statistics.median(values)
        for freq, db in levels:
            if self.cooldown > 0:
                self.cooldown -= 1
                continue
            if db != float("-inf") and db >= median + 9.0 and db > -45.0:
                word = self.rng.choice(WORDS)
                self.session.log(
                    "WORD", "theater: word cue \"%s\" at %s - measured "
                    "audio %.1f dBFS against a sweep median of %.1f dBFS. "
                    "The level is measured; the word is chosen at random "
                    "and is not a measurement."
                    % (word, freq_of(freq), db, median))
                self.cooldown = 3


def _read_exactly(stream, count):
    data = b""
    while len(data) < count:
        chunk = stream.read(count - len(data))
        if not chunk:
            break
        data += chunk
    return data


def spirit_fm_live(session, args, words):
    """Sweep FM in per-step rtl_fm hops, one process per 200 kHz step.

    rtl_fm has no timed retune, so the sweep is built from short hops,
    the way an SB7's muting gaps separate its steps. The audio is the
    genuine demodulated output of the receiver at each step.
    """
    tool = find_tool("rtl_fm")
    window_bytes = int(FM_RATE * 2 * args.dwell / 1000)
    player = None
    if args.play:
        aplay = shutil.which("aplay")
        if aplay:
            player = subprocess.Popen(
                [aplay, "-r", str(FM_RATE), "-f", "S16_LE", "-t", "raw",
                 "-c", "1"], stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            session.log("SPIRIT", "aplay not found; recording without "
                        "live playback")
    all_samples = array.array("h")
    deadline = time.time() + args.seconds
    pass_number = 0
    try:
        while time.time() < deadline:
            pass_number += 1
            levels = []
            freq = FM_LOW
            while freq <= FM_HIGH and time.time() < deadline:
                proc = subprocess.Popen(
                    [tool, "-f", str(freq), "-M", "wbfm",
                     "-g", str(args.gain), "-"],
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
                data = _read_exactly(proc.stdout, window_bytes)
                proc.terminate()
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    proc.kill()
                samples = array.array("h")
                samples.frombytes(data[: len(data) - len(data) % 2])
                if sys.byteorder == "big":
                    samples.byteswap()
                all_samples.extend(samples)
                if player is not None:
                    try:
                        player.stdin.write(data)
                    except (BrokenPipeError, OSError):
                        player = None
                levels.append((freq, rms_dbfs(samples)))
                freq += FM_STEP
            session.log("SPIRIT", "FM sweep pass %d complete: %d steps, "
                        "200 kHz apart, %d ms dwell"
                        % (pass_number, len(levels), args.dwell))
            sweep_display(session, levels)
            words.consider_pass(levels, lambda f: "%.1f MHz" % (f / 1e6))
    finally:
        if player is not None:
            try:
                player.stdin.close()
            except OSError:
                pass
    write_wav(session.path("spirit.wav"), FM_RATE, all_samples)
    return len(all_samples) / FM_RATE


def spirit_airband_live(session, args, words):
    """Sweep airband in one rtl_fm scan-mode process.

    rtl_fm scan mode (several frequencies plus a squelch level) hops
    on its own and lingers where a carrier holds it, the way a scanner
    does; dwell on airband is signal-driven, not timed. The recorded
    stream is measured in dwell-sized windows for the word layer.
    """
    tool = find_tool("rtl_fm")
    window_bytes = int(AIR_RATE * 2 * args.dwell / 1000)
    command = [tool, "-f", "%d:%d:%d" % (AIR_LOW, AIR_HIGH, AIR_STEP),
               "-M", "am", "-s", "250k", "-r", str(AIR_RATE), "-A", "fast",
               "-l", str(args.squelch), "-g", str(args.gain), "-"]
    session.log("SPIRIT", "airband scan: %s (dwell is signal-driven; "
                "squelch is in rtl_fm units)" % " ".join(command))
    proc = subprocess.Popen(command, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL)
    player = None
    if args.play:
        aplay = shutil.which("aplay")
        if aplay:
            player = subprocess.Popen(
                [aplay, "-r", str(AIR_RATE), "-f", "S16_LE", "-t", "raw",
                 "-c", "1"], stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    all_samples = array.array("h")
    deadline = time.time() + args.seconds
    window_number = 0
    try:
        while time.time() < deadline:
            data = _read_exactly(proc.stdout, window_bytes)
            if not data:
                break
            samples = array.array("h")
            samples.frombytes(data[: len(data) - len(data) % 2])
            if sys.byteorder == "big":
                samples.byteswap()
            all_samples.extend(samples)
            if player is not None:
                try:
                    player.stdin.write(data)
                except (BrokenPipeError, OSError):
                    player = None
            window_number += 1
            level = rms_dbfs(samples)
            session.log("SPIRIT", "airband window %d: %.1f dBFS measured"
                        % (window_number, level))
            words.consider_pass([(0, level)], lambda f: "airband scan")
    finally:
        proc.terminate()
        if player is not None:
            try:
                player.stdin.close()
            except OSError:
                pass
    write_wav(session.path("spirit.wav"), AIR_RATE, all_samples)
    return len(all_samples) / AIR_RATE


def sweep_display(session, levels):
    values = [db for _, db in levels if db != float("-inf")]
    if not values:
        return
    lo, hi = min(values), max(values)
    text = "".join(bar(db, lo, hi) for _, db in levels)
    session.log("SWEEP", "spirit sweep display (theater view of measured "
                "audio energy, %.1f to %.1f dBFS): %s" % (lo, hi, text))


def spirit_simulate(session, args, words):
    """Run the spirit pipeline over a recorded WAV instead of the tuner.

    The file is cut into dwell-sized windows exactly as a live sweep
    would produce them; every window is measured, word cues fire on
    the same rule, and the audio is filed into the session as the
    sweep recording.
    """
    rate, samples = read_wav(args.simulate)
    per_window = max(1, int(rate * args.dwell / 1000))
    windows = [samples[i:i + per_window]
               for i in range(0, len(samples), per_window)]
    session.log("SPIRIT", "simulate mode: %s gives %d windows of %d ms "
                "at %d Hz (receiver not opened)"
                % (args.simulate, len(windows), args.dwell, rate))
    levels = []
    for idx, window in enumerate(windows):
        freq = idx  # position in the pass stands in for the step
        levels.append((freq, rms_dbfs(window)))
    sweep_display(session, [(f, db) for f, db in levels])
    words.consider_pass(levels, lambda f: "window %d" % f)
    write_wav(session.path("spirit.wav"), rate, samples)
    return len(samples) / rate


def cmd_spirit(args):
    session = Session(args.home, "spirit")
    words = WordLayer(session, random.Random())
    if args.simulate:
        seconds = spirit_simulate(session, args, words)
    else:
        tools = require_tools(session, ["rtl_fm"])
        if tools["rtl_fm"] is None:
            return 2
        if refuse_if_busy(session):
            return 3
        if args.band == "fm":
            session.log("SPIRIT", "FM spirit box: 88-108 MHz in 200 kHz "
                        "steps, %d ms dwell, real demodulated audio"
                        % args.dwell)
            seconds = spirit_fm_live(session, args, words)
        else:
            session.log("SPIRIT", "airband spirit box: 118-137 MHz AM "
                        "scan, squelch %d" % args.squelch)
            seconds = spirit_airband_live(session, args, words)
    wav_path = session.path("spirit.wav")
    session.log("SPIRIT", "sweep recording filed: %s (%.1f seconds, "
                "%d bytes)" % (wav_path, seconds,
                               os.path.getsize(wav_path)))
    session.log("SESSION", "spirit box finished; files in %s" % session.dir)
    return 0


# ------------------------------------------------------------------ doctor

def cmd_doctor(args):
    session = Session(args.home, "doctor")
    ok = True
    for name in ("rtl_power", "rtl_fm", "rtl_sdr"):
        found = find_tool(name)
        session.log("DOCTOR", "%s: %s" % (name, found or "NOT FOUND"))
        if found is None:
            ok = False
    holders = tuner_holders()
    if holders:
        session.log("DOCTOR", "tuner state: in use by %s"
                    % ", ".join(sorted(set(holders))))
    else:
        session.log("DOCTOR", "tuner state: no holder process found")
    player = shutil.which("aplay")
    session.log("DOCTOR", "aplay (live playback): %s" % (player or "none"))
    session.log("DOCTOR", "sessions root: %s" % args.home)
    return 0 if ok else 2


# --------------------------------------------------------------------- CLI

def build_parser():
    parser = argparse.ArgumentParser(
        prog="phasma",
        description="Paranormal investigation station on an RTL-SDR "
                    "receiver. Measurements are real; the word layer is "
                    "theater fed by measurements and says so.")
    parser.add_argument("--home", default=default_home(),
                        help="data root for sessions "
                             "(default: PHASMA_HOME or ~/.local/share/phasma)")
    parser.add_argument("--version", action="version",
                        version="phasma " + VERSION)
    sub = parser.add_subparsers(dest="command", required=True)

    doctor = sub.add_parser("doctor", help="check tools and tuner state")
    doctor.set_defaults(func=cmd_doctor)

    field = sub.add_parser("field", help="RF field meter (rtl_power)")
    field.add_argument("--low", type=int, default=FM_LOW)
    field.add_argument("--high", type=int, default=FM_HIGH)
    field.add_argument("--bin", type=int, default=FIELD_BIN,
                       help="bin width in Hz")
    field.add_argument("--interval", type=int, default=2,
                       help="seconds between rtl_power sweeps")
    field.add_argument("--seconds", type=int, default=20,
                       help="total run time for a live sweep")
    field.add_argument("--gain", type=int, default=35)
    field.add_argument("--threshold", type=float, default=6.0,
                       help="excursion threshold in dB over baseline")
    field.add_argument("--simulate", metavar="CSV",
                       help="read an rtl_power CSV file instead of "
                            "opening the receiver")
    field.set_defaults(func=cmd_field)

    spirit = sub.add_parser("spirit", help="spirit box (rtl_fm)")
    spirit.add_argument("--band", choices=("fm", "airband"), default="fm")
    spirit.add_argument("--dwell", type=int, default=250,
                        help="dwell per step or window, ms (100-500 "
                             "is the SB7 class)")
    spirit.add_argument("--seconds", type=int, default=60,
                        help="total run time for a live sweep")
    spirit.add_argument("--gain", type=int, default=35)
    spirit.add_argument("--squelch", type=int, default=20,
                        help="airband scan squelch, in rtl_fm units")
    spirit.add_argument("--play", action="store_true", default=True,
                        help="play audio live through aplay (default)")
    spirit.add_argument("--no-play", dest="play", action="store_false",
                        help="record without live playback")
    spirit.add_argument("--simulate", metavar="WAV",
                        help="run the sweep pipeline over a WAV file "
                             "instead of opening the receiver")
    spirit.set_defaults(func=cmd_spirit)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
