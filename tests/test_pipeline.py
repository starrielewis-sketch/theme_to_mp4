import unittest

from pipeline import _merge_segments, _parse_timestamp, parse_model_segments, parse_video_id, parse_url_list, Segment


class PipelineUtilityTests(unittest.TestCase):
    def test_parse_video_id_watch_url(self):
        self.assertEqual(parse_video_id("https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=5"), "dQw4w9WgXcQ")

    def test_parse_video_id_short_url(self):
        self.assertEqual(parse_video_id("https://youtu.be/dQw4w9WgXcQ?si=abc"), "dQw4w9WgXcQ")

    def test_parse_video_id_shorts_url(self):
        self.assertEqual(parse_video_id("https://youtube.com/shorts/dQw4w9WgXcQ"), "dQw4w9WgXcQ")

    def test_reject_bad_url(self):
        with self.assertRaises(ValueError):
            parse_video_id("https://example.com/not-a-video")

    def test_parse_url_list_deduplicates(self):
        urls = parse_url_list("https://youtu.be/dQw4w9WgXcQ\nhttps://youtu.be/dQw4w9WgXcQ")
        self.assertEqual(urls, ["https://youtu.be/dQw4w9WgXcQ"])

    def test_parse_timestamp(self):
        self.assertAlmostEqual(_parse_timestamp("01:02.5"), 62.5)
        self.assertAlmostEqual(_parse_timestamp("00:01:02.5"), 62.5)
        self.assertAlmostEqual(_parse_timestamp(12.25), 12.25)

    def test_parse_model_segments_bounds_and_merge(self):
        raw = '[{"start": 10, "end": 15}, {"start": 14.8, "end": 20}, {"start": 999, "end": 1000}, {"start": -1, "end": 2}]'
        segments = parse_model_segments(raw, video_duration=120)
        self.assertEqual(segments, [Segment(10.0, 20.0)])

    def test_segments_object_wrapper(self):
        segments = parse_model_segments('{"segments": [{"start": 1, "end": 2}]}', video_duration=30)
        self.assertEqual(segments, [Segment(1.0, 2.0)])

    def test_merge_gap_tolerance(self):
        result = _merge_segments([Segment(2, 3), Segment(1, 2), Segment(3.1, 4)])
        self.assertEqual(result, [Segment(1, 4)])


if __name__ == "__main__":
    unittest.main()
