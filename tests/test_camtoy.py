"""Tests for camtoy's maths and state machines — no camera required.

The parts worth pinning down are the ones where a regression is invisible:
an image operator that silently stops operating, a ring buffer that stops
shearing across time, an ANSI writer that emits a well-formed frame of the
wrong colour. Capture itself is exercised by running `camtoy` on the board.

Run with:  python3 -m unittest discover -s tests   (or pytest, if installed)
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import numpy as np

from camtoy import imageops as io
from camtoy.capture import CameraMode, _FOURCC_TO_FFMPEG, pick_mode, probe_modes
from camtoy.modes.lightpaint import LightPaintMode
from camtoy.modes.live import LiveMode
from camtoy.modes.slitscan import SlitScanMode
from camtoy.modes.theremin import ThereminMode
from camtoy.render.terminal import COLOR256, TRUECOLOR, _xterm256
from camtoy.synth import SCALES, scale_freqs


def frame(h=12, w=16, value=128):
    return np.full((h, w, 3), value, np.uint8)


class ImageOpsTest(unittest.TestCase):
    def test_luma_weights_sum_to_one(self):
        white = np.full((2, 2, 3), 255, np.uint8)
        self.assertAlmostEqual(float(io.luma(white).max()), 255.0, places=3)

    def test_autolevel_stretches_a_flat_bright_frame(self):
        # The case this exists for: an auto-exposed sensor pointed at a wall.
        img = np.zeros((8, 8, 3), np.uint8)
        img[:, :4] = 200
        img[:, 4:] = 240
        out = io.autolevel(img)
        self.assertGreater(int(out.max()) - int(out.min()), int(img.max()) - int(img.min()))

    def test_autolevel_leaves_a_constant_frame_alone(self):
        # No dynamic range to recover, and dividing by zero would be worse.
        img = np.full((4, 4, 3), 200, np.uint8)
        np.testing.assert_array_equal(io.autolevel(img), img)

    def test_sobel_finds_an_edge_and_ignores_a_flat_field(self):
        step = np.zeros((5, 6), np.float32)
        step[:, 3:] = 255
        self.assertEqual(int(io.sobel(step).shape[0]), 5)
        self.assertEqual(int(np.argmax(io.sobel(step)[2])), 2)
        self.assertEqual(float(io.sobel(np.full((5, 5), 50.0)).max()), 0.0)

    def test_box_blur_preserves_a_constant_and_conserves_mass(self):
        flat = np.full((9, 11), 100.0, np.float32)
        np.testing.assert_allclose(io.box_blur(flat, 3), flat)
        spike = np.zeros((7, 7), np.float32)
        spike[3, 3] = 9.0
        self.assertAlmostEqual(float(io.box_blur(spike, 1)[3, 3]), 1.0, places=5)

    def test_ordered_dither_is_positional_not_sequential(self):
        # The whole reason it is used for video: identical input regions must
        # dither identically, so still areas do not boil between frames.
        mid = np.full((32, 32), 127.5, np.float32)
        out = io.ordered_dither(mid, 2)
        self.assertEqual(sorted(np.unique(out).tolist()), [0, 255])
        self.assertAlmostEqual(float((out > 127).mean()), 0.5, places=2)
        np.testing.assert_array_equal(out[:8, :8], out[8:16, 8:16])

    def test_floyd_steinberg_hits_the_same_average(self):
        out = io.floyd_steinberg(np.full((16, 16), 127.5, np.float32), 2)
        self.assertEqual(sorted(np.unique(out).tolist()), [0, 255])
        self.assertAlmostEqual(float((out > 127).mean()), 0.5, places=1)

    def test_every_palette_is_a_full_lut(self):
        for name, lut in io.PALETTES.items():
            with self.subTest(palette=name):
                self.assertEqual(lut.shape, (256, 3))
                self.assertEqual(lut.dtype, np.uint8)
                # Monotonic brightness keeps the mapping readable as an image.
                self.assertLess(int(lut[0].sum()), int(lut[255].sum()))

    def test_resize_reaches_the_requested_shape_both_ways(self):
        img = frame(20, 30)
        self.assertEqual(io.resize(img, 7, 5).shape, (5, 7, 3))
        self.assertEqual(io.resize(img, 60, 40).shape, (40, 60, 3))

    def test_normalize_handles_a_degenerate_plane(self):
        np.testing.assert_array_equal(io.normalize(np.full((3, 3), 7.0)), np.zeros((3, 3)))


class CaptureTest(unittest.TestCase):
    def test_fourcc_maps_to_names_ffmpeg_accepts(self):
        # A raw v4l2 FOURCC in -input_format is rejected outright; this table
        # is the difference between a working camera and "No such input format".
        self.assertEqual(_FOURCC_TO_FFMPEG["YUYV"], "yuyv422")
        self.assertEqual(_FOURCC_TO_FFMPEG["MJPG"], "mjpeg")

    def test_pick_mode_prefers_frame_rate_then_size(self):
        modes = [
            CameraMode(160, 120, 30), CameraMode(640, 480, 30), CameraMode(640, 480, 15),
        ]
        self.assertEqual(pick_mode(modes), CameraMode(640, 480, 30))

    def test_pick_mode_ignores_uselessly_small_modes(self):
        modes = [CameraMode(64, 48, 60), CameraMode(320, 240, 30)]
        self.assertEqual(pick_mode(modes).width, 320)

    def test_pick_mode_falls_back_when_nothing_is_known(self):
        self.assertEqual(pick_mode([]).width, 160)

    def test_probe_modes_never_raises(self):
        self.assertIsInstance(probe_modes("/dev/does-not-exist"), list)


class TerminalTest(unittest.TestCase):
    def test_cell_templates_are_fixed_width(self):
        # Vectorised rendering depends on every cell being the same length.
        self.assertEqual(TRUECOLOR.width, len(TRUECOLOR.template))
        for offset in TRUECOLOR.fg_at + TRUECOLOR.bg_at:
            self.assertEqual(bytes(TRUECOLOR.template[offset:offset + 3]), b"000")
        for offset in COLOR256.fg_at + COLOR256.bg_at:
            self.assertEqual(bytes(COLOR256.template[offset:offset + 3]), b"000")

    def test_xterm256_matches_the_standard_ramp(self):
        probe = np.array([[[0, 0, 0], [255, 255, 255], [255, 0, 0], [0, 255, 0]]], np.uint8)
        self.assertEqual(_xterm256(probe)[0].tolist(), [232, 255, 196, 46])

    def test_xterm256_never_lands_in_the_system_colours(self):
        noise = np.random.randint(0, 256, (64, 64, 3), dtype=np.uint8)
        self.assertGreaterEqual(int(_xterm256(noise).min()), 16)


class SlitScanTest(unittest.TestCase):
    def test_rows_walk_backwards_through_time(self):
        mode = SlitScanMode(depth=16)
        for value in range(40):
            out = mode.render(np.full((8, 4, 3), value, np.uint8))
        column = out[:, 0, 0].tolist()
        self.assertEqual(column[0], 39)                  # top row is now
        self.assertTrue(all(a >= b for a, b in zip(column, column[1:])))
        self.assertGreater(column[0], column[-1])        # bottom row is the past

    def test_column_axis_shears_across_x(self):
        mode = SlitScanMode(depth=16, axis="col")
        for value in range(40):
            out = mode.render(np.full((8, 6, 3), value, np.uint8))
        self.assertGreater(len(set(out[0, :, 0].tolist())), 1)

    def test_freeze_stops_taking_new_frames(self):
        mode = SlitScanMode(depth=8)
        for value in range(20):
            mode.render(np.full((4, 4, 3), value, np.uint8))
        mode.on_key("space")
        before = mode.render(np.full((4, 4, 3), 200, np.uint8))
        after = mode.render(np.full((4, 4, 3), 250, np.uint8))
        np.testing.assert_array_equal(before, after)

    def test_ring_is_capped_by_the_memory_budget(self):
        # A 120-deep buffer of full HD would be gigabytes; depth must yield.
        mode = SlitScanMode(depth=100_000)
        mode.render(frame(64, 64))
        self.assertLess(mode.depth, 100_000)

    def test_geometry_change_rebuilds_the_ring(self):
        mode = SlitScanMode(depth=8)
        mode.render(frame(8, 8))
        self.assertEqual(mode.render(frame(6, 10)).shape, (6, 10, 3))


class LightPaintTest(unittest.TestCase):
    def _mode(self, **kw):
        mode = LightPaintMode(full_size=(20, 10), autosave=False, **kw)
        mode.preview = False
        return mode

    def test_lighten_keeps_the_brightest_pixel_ever_seen(self):
        mode = self._mode()
        dark = np.zeros((10, 20, 3), np.uint8)
        spot = dark.copy()
        spot[5, 5] = 255
        mode.render(dark)
        mode.render(spot)
        self.assertEqual(int(mode.render(dark)[5, 5, 0]), 255)

    def test_clear_empties_the_canvas(self):
        mode = self._mode()
        spot = np.zeros((10, 20, 3), np.uint8)
        spot[1, 1] = 255
        mode.render(spot)
        mode.on_key("c")
        self.assertEqual(int(mode.render(np.zeros((10, 20, 3), np.uint8)).max()), 0)

    def test_decay_blend_fades_an_old_streak(self):
        mode = self._mode(blend="decay")
        spot = np.zeros((10, 20, 3), np.uint8)
        spot[5, 5] = 255
        mode.render(spot)
        dark = np.zeros((10, 20, 3), np.uint8)
        for _ in range(30):
            faded = mode.render(dark)
        self.assertLess(int(faded[5, 5, 0]), 255)

    def test_it_paints_at_sensor_resolution_not_display_resolution(self):
        mode = self._mode()
        self.assertEqual(mode.work_size((40, 30)), (20, 10))

    def test_background_reference_subtracts_the_ambient_room(self):
        mode = self._mode()
        room = np.full((10, 20, 3), 120, np.uint8)
        mode.render(room)
        mode.on_key("k")
        self.assertEqual(int(mode.render(room).max()), 0)


class LiveTest(unittest.TestCase):
    def test_every_key_keeps_the_output_a_valid_frame(self):
        # 's' saves a PNG, so give it somewhere disposable to write — a test
        # run should not leave files in the working tree.
        with tempfile.TemporaryDirectory() as tmp:
            mode = LiveMode(outdir=Path(tmp))
            img = (np.random.rand(20, 24, 3) * 255).astype(np.uint8)
            for key in "lepdtis[]" + "pppp" + "dd" + "tt":
                with self.subTest(key=key):
                    mode.on_key(key)
                    out = mode.render(img)
                    self.assertEqual(out.shape, (20, 24, 3))
                    self.assertEqual(out.dtype, np.uint8)
            self.assertEqual(len(list(Path(tmp).glob("live-*.png"))), 1)

    def test_trails_reset_when_the_geometry_changes(self):
        mode = LiveMode()
        mode.on_key("t")
        mode.render(frame(10, 10))
        self.assertEqual(mode.render(frame(14, 12)).shape, (14, 12, 3))

    def test_unknown_keys_are_ignored(self):
        self.assertIsNone(LiveMode().on_key("z"))


class ThereminTest(unittest.TestCase):
    def test_a_still_scene_is_silent(self):
        mode = ThereminMode(voices=4)
        mode.synth.stop()
        still = np.full((20, 32, 3), 100, np.uint8)
        mode.render(still)
        mode.render(still)
        self.assertEqual(float(mode._energy.max()), 0.0)

    def test_motion_drives_the_band_it_happened_in(self):
        mode = ThereminMode(voices=4)
        mode.synth.stop()
        still = np.full((20, 32, 3), 100, np.uint8)
        mode.render(still)
        mode.render(still)
        moved = still.copy()
        moved[:, 24:] = 255                      # rightmost quarter
        mode.render(moved)
        self.assertGreater(float(mode._energy[3]), float(mode._energy[0]))

    def test_it_runs_without_an_audio_device(self):
        mode = ThereminMode(voices=4)
        mode.synth.stop()
        mode.audio_ok = False
        out = mode.render(frame(20, 32))
        self.assertEqual(out.shape, (20, 32, 3))
        mode.close()


class SynthTest(unittest.TestCase):
    def test_scales_ascend_and_stay_audible(self):
        for name in SCALES:
            with self.subTest(scale=name):
                freqs = scale_freqs(8, name)
                self.assertEqual(len(freqs), 8)
                self.assertTrue(all(a < b for a, b in zip(freqs, freqs[1:])))
                self.assertGreater(freqs[0], 20.0)
                self.assertLess(freqs[-1], 20000.0)

    def test_the_root_note_is_the_note_it_claims(self):
        # MIDI 45 is A2 = 110Hz. If this drifts, every scale is transposed.
        self.assertAlmostEqual(float(scale_freqs(1, "chromatic", 45)[0]), 110.0, places=6)

    def test_a_block_is_finite_and_within_range(self):
        from camtoy.synth import Synth
        synth = Synth(voices=4, blocksize=64)
        synth.targets = np.full(4, 0.9)
        out = np.zeros((64, 1), np.float32)
        synth._callback(out, 64, None, None)
        self.assertTrue(np.isfinite(out).all())
        self.assertLessEqual(float(np.abs(out).max()), 1.0)

    def test_phase_is_continuous_across_blocks(self):
        # A reset phase each block is an audible click 86 times a second.
        from camtoy.synth import Synth
        synth = Synth(voices=1, blocksize=64)
        synth.targets = np.full(1, 1.0)
        first = np.zeros((64, 1), np.float32)
        second = np.zeros((64, 1), np.float32)
        synth._callback(first, 64, None, None)
        synth._callback(second, 64, None, None)
        self.assertGreater(abs(float(synth._phase[0])), 0.0)
        self.assertNotAlmostEqual(float(first[0, 0]), float(second[0, 0]), places=6)


# --------------------------------------------------------------------------
# neural network support code
#
# Only the parts that are pure numpy are covered here: the maths around the
# models, not the models. Inference needs onnxruntime and 850MB of weights,
# and it is checked by running `camtoy detect` and friends on the board.
# --------------------------------------------------------------------------

from camtoy.nn import preprocess as pre                                  # noqa: E402
from camtoy.nn.bodies import PALM_LAYOUT, PERSON_LAYOUT, anchors        # noqa: E402
from camtoy.nn.faces import ARCFACE_TEMPLATE, similarity_transform       # noqa: E402
from camtoy.nn.labels import COCO80, CRNN_CHARSET, palette               # noqa: E402
from camtoy.nn.reading import connected_boxes                            # noqa: E402
from camtoy.nn.registry import INTERACTIVE_MS, MODELS, by_task, total_bytes  # noqa: E402


class PreprocessTest(unittest.TestCase):
    def test_letterbox_preserves_aspect_and_fills_exactly(self):
        img = frame(60, 100)
        out, box = pre.letterbox(img, 64, 64)
        self.assertEqual(out.shape, (64, 64, 3))
        # A 100x60 frame fits by width, so the padding is vertical only.
        self.assertEqual(box.pad_x, 0)
        self.assertGreater(box.pad_y, 0)

    def test_letterbox_round_trips_a_coordinate(self):
        img = frame(60, 100)
        _out, box = pre.letterbox(img, 64, 64)
        point = np.array([[37.0, 21.0]])
        forward = point * box.scale + np.array([[box.pad_x, box.pad_y]])
        np.testing.assert_allclose(box.to_source(forward), point, atol=1e-4)

    def test_to_tensor_layouts_and_channel_order(self):
        img = np.zeros((4, 5, 3), np.uint8)
        img[..., 0] = 10                       # red only
        nchw = pre.to_tensor(img)
        self.assertEqual(nchw.shape, (1, 3, 4, 5))
        self.assertEqual(float(nchw[0, 0].max()), 10.0)
        nhwc = pre.to_tensor(img, nhwc=True)
        self.assertEqual(nhwc.shape, (1, 4, 5, 3))
        swapped = pre.to_tensor(img, bgr=True)
        self.assertEqual(float(swapped[0, 2].max()), 10.0)   # red moved to slot 2

    def test_to_tensor_applies_scale_then_mean(self):
        img = np.full((2, 2, 3), 255, np.uint8)
        out = pre.to_tensor(img, scale=1 / 127.5, mean=1.0)
        self.assertAlmostEqual(float(out.max()), 1.0, places=5)
        out = pre.to_tensor(np.zeros((2, 2, 3), np.uint8), scale=1 / 127.5, mean=1.0)
        self.assertAlmostEqual(float(out.min()), -1.0, places=5)

    def test_sigmoid_is_stable_at_both_extremes(self):
        x = np.array([-800.0, -1.0, 0.0, 1.0, 800.0], np.float32)
        out = pre.sigmoid(x)
        self.assertTrue(np.isfinite(out).all())
        self.assertAlmostEqual(float(out[2]), 0.5, places=6)
        self.assertGreaterEqual(float(out[0]), 0.0)
        self.assertLessEqual(float(out[4]), 1.0)

    def test_softmax_sums_to_one_on_the_chosen_axis(self):
        x = np.random.rand(3, 7).astype(np.float32) * 20
        np.testing.assert_allclose(pre.softmax(x, axis=1).sum(axis=1), 1.0, rtol=1e-6)

    def test_nms_drops_overlaps_and_keeps_the_best(self):
        boxes = np.array([[0, 0, 10, 10], [1, 1, 11, 11], [50, 50, 60, 60]], np.float32)
        scores = np.array([0.9, 0.8, 0.7], np.float32)
        keep = pre.nms(boxes, scores, 0.45)
        self.assertEqual(sorted(keep.tolist()), [0, 2])

    def test_nms_on_nothing(self):
        self.assertEqual(len(pre.nms(np.empty((0, 4)), np.empty(0))), 0)


class LabelTest(unittest.TestCase):
    def test_coco_has_eighty_classes_in_the_trained_order(self):
        self.assertEqual(len(COCO80), 80)
        self.assertEqual(COCO80[0], "person")
        self.assertEqual(COCO80[79], "toothbrush")

    def test_crnn_charset_matches_the_head_width(self):
        # The model emits 37 logits: one CTC blank plus these characters.
        self.assertEqual(len(CRNN_CHARSET) + 1, 37)

    def test_palette_colours_are_distinct(self):
        colours = palette(80)
        self.assertEqual(colours.shape, (80, 3))
        self.assertEqual(len({tuple(c) for c in colours}), 80)

    def test_adjacent_classes_get_unlike_colours(self):
        colours = palette(80).astype(int)
        gaps = np.abs(np.diff(colours, axis=0)).sum(axis=1)
        self.assertGreater(int(gaps.min()), 60)


class AnchorTest(unittest.TestCase):
    def test_layouts_match_the_models_output_counts(self):
        # If these drift, every box and landmark silently lands elsewhere.
        self.assertEqual(len(anchors(PALM_LAYOUT, 192)), 2016)
        self.assertEqual(len(anchors(PERSON_LAYOUT, 224)), 2254)

    def test_anchors_are_normalised_cell_centres(self):
        grid = anchors(((8, 1),), 32)           # 4x4 cells
        self.assertEqual(len(grid), 16)
        self.assertAlmostEqual(float(grid[0, 0]), 0.125, places=6)
        self.assertTrue((grid >= 0).all() and (grid <= 1).all())


class AlignmentTest(unittest.TestCase):
    def test_transform_recovers_a_known_rotation_and_scale(self):
        angle = np.radians(30.0)
        rotation = np.array([[np.cos(angle), -np.sin(angle)],
                             [np.sin(angle), np.cos(angle)]], np.float32)
        source = ARCFACE_TEMPLATE @ rotation.T * 1.7 + np.array([12.0, -5.0], np.float32)
        matrix = similarity_transform(source, ARCFACE_TEMPLATE)
        mapped = source @ matrix[:2, :2].T + matrix[:2, 2]
        np.testing.assert_allclose(mapped, ARCFACE_TEMPLATE, atol=1e-3)

    def test_transform_stays_rigid_rather_than_shearing(self):
        # The 2x2 block must be a scaled rotation: its columns stay orthogonal
        # and equal length, or face geometry gets "corrected" out of existence.
        source = ARCFACE_TEMPLATE * 0.8 + 3.0
        block = similarity_transform(source, ARCFACE_TEMPLATE)[:2, :2]
        self.assertAlmostEqual(float(np.dot(block[:, 0], block[:, 1])), 0.0, places=4)
        self.assertAlmostEqual(float(np.linalg.norm(block[:, 0])),
                               float(np.linalg.norm(block[:, 1])), places=4)


class ConnectedComponentTest(unittest.TestCase):
    def test_finds_separate_blobs(self):
        mask = np.zeros((10, 10), bool)
        mask[1:4, 1:4] = True
        mask[6:9, 6:9] = True
        self.assertEqual(sorted(connected_boxes(mask, min_area=4)),
                         [(1, 1, 3, 3), (6, 6, 8, 8)])

    def test_joins_a_diagonal_free_l_shape(self):
        mask = np.zeros((6, 6), bool)
        mask[1, 1:5] = True
        mask[1:5, 4] = True
        self.assertEqual(connected_boxes(mask, min_area=4), [(1, 1, 4, 4)])

    def test_small_specks_are_dropped(self):
        mask = np.zeros((8, 8), bool)
        mask[0, 0] = True
        self.assertEqual(connected_boxes(mask, min_area=4), [])

    def test_empty_mask(self):
        self.assertEqual(connected_boxes(np.zeros((5, 5), bool)), [])


class RegistryTest(unittest.TestCase):
    def test_keys_paths_and_tasks_are_consistent(self):
        for key, spec in MODELS.items():
            with self.subTest(model=key):
                self.assertEqual(spec.key, key)
                self.assertTrue(spec.url.startswith("https://"))
                self.assertGreater(spec.size, 1000)
                self.assertTrue(str(spec.path).endswith(f"{key}.onnx"))

    def test_realtime_follows_the_measured_time(self):
        for spec in MODELS.values():
            with self.subTest(model=spec.key):
                if spec.broken:
                    self.assertFalse(spec.realtime)
                else:
                    self.assertEqual(spec.realtime, 0 < spec.ms <= INTERACTIVE_MS)

    def test_every_task_has_at_least_one_model(self):
        from camtoy.nn.registry import TASKS
        for task in TASKS:
            with self.subTest(task=task):
                self.assertTrue(by_task(task))

    def test_broken_models_say_why(self):
        for spec in MODELS.values():
            if spec.broken:
                self.assertGreater(len(spec.broken), 40)   # a reason, not a flag

    def test_total_is_the_sum_of_the_parts(self):
        self.assertEqual(total_bytes(), sum(m.size for m in MODELS.values()))


if __name__ == "__main__":
    unittest.main()
