"""The standard-library ffprobe probe and free-space read of local.py, on a fake runner: no ffmpeg in the suite."""
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from pirillo_studio.preflight import probe
from pirillo_studio.preflight.local import _rate, ffprobe_media, volume

INFO = {"format": {"format_name": "mov,mp4", "duration": "10.0", "start_time": "0.0"}, "streams": [
    {"index": 0, "codec_type": "video", "r_frame_rate": "30/1", "width": 3840, "height": 2160, "color_space": "bt709"},
    {"index": 1, "codec_type": "audio", "sample_rate": "48000"}, {"index": 2, "codec_type": "data"}]}   # timecode
AAC = 1024 / 48000
PACKETS = "\n".join([f"0,{i / 30.0686:.6f},N/A,{'_K'[i % 30 == 0]}_" for i in range(301)]   # a keyframe a second
                    + [f"1,{i * AAC:.6f},{AAC:.6f}" for i in range(469)])


def runner(windows=None, packets=PACKETS, info=INFO):
    """Replies in ffprobe's call order: streams, packets, then one decode window each."""
    calls = []
    s = float(info["format"]["start_time"])
    last = s + float(info["format"]["duration"])

    def run(argv):
        calls.append(argv)
        if "-show_streams" in argv:
            return 0, json.dumps(info), ""
        if "-read_intervals" in argv:   # frames across the asked window, inside the file's pts, unless the case says
            w = argv[argv.index("-read_intervals") + 1]
            t, e = (s, s + float(w[2:])) if w.startswith("%+") else (float(x) for x in w.split("%"))
            xs = [x for x in (t + i / 4 for i in range(int((min(e, last) - t) * 4) + 1)) if x >= s]
            return 0, (windows or {}).get(f"{e:g}", "".join(f"{i},{x:g}\n" for x in xs for i in (0, 1))), ""
        return 0, packets, ""
    return run, calls


class FfprobeMedia(unittest.TestCase):
    """docs/preflight.md section 1 media detail, measured, with each unreadable case raised."""

    def test_detail_measured_from_packets_and_decodes(self):
        run, calls = runner()
        d = ffprobe_media("/x/ffprobe", "s.mp4", run=run)
        self.assertIsNone(probe.media_problem(d, 0.1))
        self.assertEqual((d["fps_measured"], d["fps_tagged"], d["streams"]), (30.0686, "30/1", {"video": 1, "audio": 1, "data": 1}))
        self.assertEqual(d["decoded"], ["0-2s", "4-6s", "8-10s"])
        self.assertTrue(all(c[0] == "/x/ffprobe" for c in calls))
        self.assertRaisesRegex(probe.BrokenMedia, "no video keyframe", ffprobe_media, "ffprobe", "s.mp4",
                               run=runner(packets=PACKETS.replace("K_", "__"))[0])

    def test_audio_gaps_are_measured(self):
        """guard: a source with a hole in its audio, or a lone packet for its length, never passes the media probe."""
        holed = "\n".join(x for x in PACKETS.split("\n") if not x.startswith(("1,3.0", "1,3.1")))
        for packets, gap in ((holed, 0.11), ("\n".join(PACKETS.split("\n")[:302]), 9.97)):
            d = ffprobe_media("ffprobe", "s.mp4", run=runner(packets=packets)[0])
            self.assertGreater(d["audio_gap_s"], gap)
            self.assertIn("audio gap", probe.media_problem(d, 0.1))
        for bad in ("1,9.99,N/A", "1,9.99,inf", "1,nan,0.02", "0,inf", "0,9.99,inf"):   # guard: no non-finite packet passes
            self.assertRaises(probe.BrokenMedia, ffprobe_media, "ffprobe", "s.mp4", run=runner(packets=PACKETS + "\n" + bad)[0])
        self.assertEqual([_rate(r) for r in ("inf/1", "nan/1", "1/0", "30/1")], [None, None, None, "30/1"])

    def test_failures_are_tool_or_media(self):
        def gone(argv):
            raise FileNotFoundError(argv[0])
        self.assertRaises(probe.ToolFailed, ffprobe_media, "ffprobe", "s.mp4", run=gone)
        self.assertRaises(probe.ToolFailed, ffprobe_media, "ffprobe", "s.mp4", run=lambda argv: (-9, "", ""))  # guard: killed
        self.assertRaises(probe.BrokenMedia, ffprobe_media, "ffprobe", "s.mp4",
                          run=lambda argv: (1, "", "s.mp4: moov atom not found\n"))
        pictures = "".join(f"0,{4 + i / 30:.4f}\n" for i in range(61))   # the video whole across the 4-6 s window
        for frames in ("", "1,1\n1,3\n", "1,4\n1,5\n", "1,5.6\n1,6\n"):
            frames = pictures + frames
            # none, all before the window, short of its end, none from its start
            with self.subTest(frames), self.assertRaisesRegex(probe.BrokenMedia, "stream 1 at 4-6s"):
                ffprobe_media("ffprobe", "s.mp4", run=runner({"6": frames})[0])
        self.assertRaises(probe.BrokenMedia, ffprobe_media, "ffprobe", "s.mp4", run=runner({"6": "0,4\n1,N/A\n"})[0])

    def test_a_held_frame_covers_its_window(self):
        """guard: a variable-frame-rate screen recording holds a still screen as one long frame, valid footage."""
        held = "0,0,0.033\n1,0,1\n1,1,1\n"   # the frame at 0, the next at 3 s; its duration is not its hold
        still = "\n".join(x for x in PACKETS.split("\n") if not x.startswith(("0,0.", "0,1.", "0,2.")) or x.startswith("0,0.000"))
        self.assertEqual(ffprobe_media("ffprobe", "s.mp4", run=runner({"2": held}, packets=still)[0])["decoded"][0], "0-2s")
        self.assertRaisesRegex(probe.BrokenMedia, "stream 0 at 0-2s", ffprobe_media, "ffprobe", "s.mp4",
                               run=runner({"2": held})[0])   # frames every 1/30 s: the rest of the window was lost
        self.assertRaises(probe.BrokenMedia, ffprobe_media, "ffprobe", "s.mp4", run=runner({"2": held.replace(",1\n", ",inf\n", 1)}, packets=still)[0])
        self.assertRaisesRegex(probe.BrokenMedia, "stream 0 at 4-6s", ffprobe_media, "ffprobe", "s.mp4",   # around it
                               run=runner({"6": "0,3,0.033\n0,7,0.033\n1,4,1\n1,5,1\n"})[0])
        self.assertRaisesRegex(probe.BrokenMedia, "stream 0 at 4-6s", ffprobe_media, "ffprobe", "s.mp4",   # a hole inside
                               run=runner({"6": "0,4\n0,5.98\n1,4,1\n1,5,1\n"})[0])
        lines = PACKETS.split("\n")   # a B-frame beside the still: decode order runs 3.5 s ahead of the frames to show
        run, calls = runner(packets="\n".join(lines[:1] + ["0,3.5,N/A,__"] + lines[1:]))
        ffprobe_media("ffprobe", "s.mp4", run=run)
        reads = [c[c.index("-read_intervals") + 1] for c in calls if "-read_intervals" in c]
        self.assertEqual(reads[0], "%+5.46674")   # ffprobe ends a read at the first packet past its end

    def test_each_video_stream_is_held_on_its_own_timeline(self):
        """guard: a second video stream recorded VFR (frames at 0, 3, 7, 9 s) is valid, not lost between its frames."""
        info = {**INFO, "streams": INFO["streams"] + [{"index": 3, "codec_type": "video", "r_frame_rate": "30/1"}]}
        second = "".join(f"3,{x},0.033\n" for x in (0, 3, 7, 9))
        windows = {f"{e:g}": "".join(f"0,{w + i / 4}\n1,{w + i / 4}\n" for i in range(9)) + second
                   for w, e in ((0, 2), (4, 6), (8, 10))}
        d = ffprobe_media("ffprobe", "s.mp4", run=runner(windows, packets=PACKETS + "\n" + second.replace(",0.033", ",N/A,K_"), info=info)[0])
        self.assertEqual(d["decoded"], ["0-2s", "4-6s", "8-10s"])

    def test_a_seek_starts_at_the_windows_own_keyframe(self):
        """guard: one long keyframe interval early on never makes every later window decode it (preflight is cheap)."""
        sparse = "\n".join(x.replace("K_", "__") if x.startswith("0,") and float(x.split(",")[1]) < 4.9 and float(
            x.split(",")[1]) > 0 else x for x in PACKETS.split("\n"))
        run, calls = runner(packets=sparse)
        ffprobe_media("ffprobe", "s.mp4", run=run)
        reads = [float(w.split("%")[0]) for c in calls if "-read_intervals" in c for w in [c[c.index("-read_intervals") + 1]] if w != "%+2"]
        self.assertEqual([r > t - 2 for r, t in zip(reads, (4, 8))], [False, True])

    def test_cover_art_is_not_a_video_stream(self):
        """guard: an attached picture (yt-dlp --embed-thumbnail) has no frames to decode and is not the footage."""
        art = dict(INFO, streams=INFO["streams"] + [{"index": 3, "codec_type": "video", "disposition": {"attached_pic": 1}}])
        run = runner(info=art)[0]
        d = ffprobe_media("ffprobe", "s.mp4", run=lambda a: (lambda r: (r[0], r[1] + "2,N/A\n" * ("-read_intervals" in a), r[2]))(run(a)))
        self.assertEqual((probe.media_problem(d, 0.1), d["streams"]), (None, {"video": 1, "audio": 1, "data": 1}))

    def test_windows_start_at_the_first_timestamp(self):
        """guard: a source whose timestamps begin after 0 (a camcorder .MTS at 1.4 s) is decoded where its frames are."""
        info = dict(INFO, format=dict(INFO["format"], start_time="1.4"))
        d = ffprobe_media("ffprobe", "s.mts", run=runner(info=info)[0])
        self.assertEqual(d["decoded"], ["1.4-3.4s", "5.4-7.4s", "9.4-11.4s"])

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "no ffmpeg on PATH")
    def test_a_real_camcorder_stream_is_not_broken(self):
        """guard: an MPEG-TS (no seek index, pts from 1.4 s, short GOP) decodes at every window on the real ffprobe."""
        with tempfile.TemporaryDirectory() as d:
            for gop in ("15", "60"):
                p = os.path.join(d, f"c{gop}.mts")
                subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=30000/1001",
                                "-f", "lavfi", "-i", "sine=sample_rate=48000", "-t", "10", "-c:v", "libx264", "-g", gop,
                                "-c:a", "aac", "-output_ts_offset", "1.4", "-f", "mpegts", p], check=True)
                m = ffprobe_media(shutil.which("ffprobe"), p)
                self.assertIsNone(probe.media_problem(m, 0.1), (gop, m))

    def test_volume_of_a_folder_not_made_yet(self):
        with tempfile.TemporaryDirectory() as d:
            mount, free = volume(os.path.join(d, "Draft", "draft.mp4"))
            self.assertTrue(os.path.ismount(mount))
            self.assertGreaterEqual(free, 0)
            real = os.path.realpath(os.path.join(d, "disk"))
            os.mkdir(real)
            os.symlink(real, os.path.join(d, "out"))
            with mock.patch("os.path.ismount", lambda p: p in (real, "/")):   # guard: a linked folder is its target's volume
                self.assertEqual(volume(os.path.join(d, "out", "Draft", "draft.mp4"))[0], real)
