import math
import shutil
import struct
import sys
import tempfile
import time
import unittest
import wave
from pathlib import Path

from gazevoice.components.microphone import wav_level_dbfs
from gazevoice.voice.audio import (
    SILENCE_HELP,
    MicError,
    MicStream,
    RingBuffer,
    is_digital_silence,
    mic_argv,
    write_wav,
)

RATE = 16000


def tone(seconds, amplitude=0.5, freq=440.0):
    n = int(seconds * RATE)
    return b"".join(struct.pack("<h", int(amplitude * 32767 * math.sin(2 * math.pi * freq * i / RATE))) for i in range(n))


class RingBufferTest(unittest.TestCase):
    def test_capture_slices_exact_samples(self):
        ring = RingBuffer(rate=RATE, seconds=60)
        for i in range(3):  # three 0.1 s chunks ending at t=10.1, 10.2, 10.3
            ring.append(bytes([i + 1]) * 3200, end_time=10.1 + i * 0.1)
        pcm = ring.capture(10.05, 10.25)  # 0.2 s spanning all three chunks
        self.assertEqual(len(pcm), int(0.2 * RATE) * 2)
        self.assertEqual(pcm[:2], b"\x01\x01")
        self.assertEqual(pcm[-2:], b"\x03\x03")
        self.assertAlmostEqual(ring.latest_time, 10.3)

    def test_capture_before_any_audio_is_empty(self):
        self.assertEqual(RingBuffer().capture(0, 5), b"")

    def test_old_audio_is_trimmed(self):
        ring = RingBuffer(rate=RATE, seconds=1)
        for i in range(30):
            ring.append(b"\x00" * 3200, end_time=100 + i * 0.1)
        self.assertEqual(ring.capture(100, 101.5), b"")  # older than one second ago
        self.assertGreater(len(ring.capture(102, 103)), 0)

    def test_odd_byte_counts_keep_sample_alignment(self):
        ring = RingBuffer(rate=RATE, seconds=60)
        ring.append(b"\x01", end_time=1.0)
        ring.append(b"\x02\x03\x04", end_time=1.1)
        self.assertEqual(len(ring.capture(0, 2)) % 2, 0)


class WavTest(unittest.TestCase):
    def test_write_wav_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "x.wav"
            write_wav(path, tone(0.5))
            with wave.open(str(path)) as w:
                self.assertEqual((w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()), (1, 2, RATE, 8000))


class MicStreamTest(unittest.TestCase):
    def test_mic_argv(self):
        self.assertEqual(
            mic_argv("2"),
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "avfoundation", "-i", ":2",
             "-ac", "1", "-ar", "16000", "-f", "s16le", "-"],
        )

    @unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg not installed")
    def test_real_ffmpeg_stream_fills_the_buffer(self):
        argv = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-re", "-f", "lavfi",
                "-i", "sine=frequency=440:sample_rate=16000", "-ac", "1", "-ar", "16000", "-f", "s16le", "-"]
        stream = MicStream(argv)
        stream.start(wait_seconds=5)
        try:
            time.sleep(1.2)
            now = time.time()
            pcm = stream.buffer.capture(now - 0.6, now - 0.1)
        finally:
            stream.stop()
        self.assertGreater(len(pcm), int(0.4 * RATE) * 2)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "tone.wav"
            write_wav(path, pcm)
            self.assertGreater(wav_level_dbfs(path), -40)
        self.assertFalse(stream.alive)

    @unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg not installed")
    def test_a_stream_of_exact_zeros_is_noticed(self):
        # macOS hands an app without microphone permission a stream of exact zeros.
        def lavfi(source):
            return ["ffmpeg", "-hide_banner", "-loglevel", "error", "-re", "-f", "lavfi", "-i", source,
                    "-ac", "1", "-ar", "16000", "-f", "s16le", "-"]

        silent = MicStream(lavfi("anullsrc=channel_layout=mono:sample_rate=16000"))
        tone = MicStream(lavfi("sine=frequency=440:sample_rate=16000"))
        silent.start(wait_seconds=5)
        tone.start(wait_seconds=5)
        try:
            self.assertFalse(silent.heard_only_silence(min_seconds=1.0))  # too early to tell
            time.sleep(1.3)
            self.assertTrue(silent.heard_only_silence(min_seconds=1.0))
            self.assertFalse(tone.heard_only_silence(min_seconds=1.0))
        finally:
            silent.stop()
            tone.stop()
        self.assertIn("Privacy & Security", SILENCE_HELP)

    def test_silence_check(self):
        self.assertTrue(is_digital_silence(b"\x00" * 3200))
        self.assertFalse(is_digital_silence(b"\x00" * 3198 + b"\x01\x00"))
        self.assertFalse(is_digital_silence(b""))

    def test_failing_capture_explains_permission(self):
        argv = [sys.executable, "-c", "import sys; sys.stderr.write('Error opening input: Operation not permitted\\n'); sys.exit(1)"]
        with self.assertRaises(MicError) as caught:
            MicStream(argv).start(wait_seconds=3)
        self.assertIn("Privacy & Security", str(caught.exception))
        self.assertIn("Operation not permitted", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
