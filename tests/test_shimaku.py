import contextlib
import hashlib
import io
from pathlib import Path
import random
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import shimaku as s


class SubtitleReviewTests(unittest.TestCase):
    def setUp(self):
        self.workspace = tempfile.TemporaryDirectory()
        self.root = Path(self.workspace.name)
        self.input = self.root / "字幕 & sample.srt"
        self.sample = "1\n00:00:00,000 --> 00:00:03,000\n字幕を確認します。\n"
        self.input.write_text(self.sample, encoding="utf-8")

    def tearDown(self):
        self.workspace.cleanup()

    def run_cli(self, *extra):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return s.main([str(self.input), *map(str, extra)])

    def test_real_cli_preserves_original_and_creates_reports(self):
        before = hashlib.sha256(self.input.read_bytes()).digest()
        out = self.root / "review"
        self.assertEqual(self.run_cli("--out", out, "--normalize"), 0)
        self.assertEqual(hashlib.sha256(self.input.read_bytes()).digest(), before)
        self.assertEqual(sorted(p.name for p in out.iterdir()),
                         ["changes.diff", "format_candidate.srt", "report.html", "report.json"])
        self.assertEqual(s.parse_srt((out / "format_candidate.srt").read_text()), s.parse_srt(self.sample))

    def test_default_does_not_emit_modified_subtitles(self):
        self.assertEqual(self.run_cli(), 0)
        self.assertFalse((self.root / "字幕 & sample_review" / "format_candidate.srt").exists())

    def test_existing_destination_is_never_overwritten(self):
        out = self.root / "review"
        out.mkdir()
        (out / "report.html").write_text("existing result")
        self.assertEqual(self.run_cli("--out", out), 2)
        self.assertEqual((out / "report.html").read_text(), "existing result")

    def test_input_itself_cannot_be_output(self):
        self.assertEqual(self.run_cli("--out", self.input), 2)
        self.assertEqual(self.input.read_text(), self.sample)

    def test_symlink_destination_is_rejected(self):
        target = self.root / "target"
        target.mkdir()
        out = self.root / "linked"
        out.symlink_to(target, target_is_directory=True)
        self.assertEqual(self.run_cli("--out", out), 2)
        self.assertEqual(list(target.iterdir()), [])

    def test_bom_crlf_and_nonconsecutive_numbers(self):
        cues = s.parse_srt("\ufeff7\r\n00:00:00,000 --> 00:00:02,000\r\nテスト\r\n")
        self.assertEqual(cues[0].number, 7)
        self.assertIn("INDEX", {x['code'] for x in s.analyze(cues, s.Settings())})

    def test_bad_timestamp_no_partial_output(self):
        self.input.write_text(self.sample.replace("00:00:00", "00:70:00"))
        out = self.root / "review"
        self.assertEqual(self.run_cli("--out", out), 2)
        self.assertFalse(out.exists())

    def test_empty_and_missing_text_rejected(self):
        for text in ["", "1\n00:00:00,000 --> 00:00:01,000", "1\n00:00:00,000 --> 00:00:01,000\n   "]:
            with self.subTest(text=text), self.assertRaises(ValueError):
                s.parse_srt(text)

    def test_zero_negative_duration_flagged_without_division_error(self):
        cues = [s.Cue(1, 1000, 1000, "あ"), s.Cue(2, 2000, 1000, "い")]
        found = s.analyze(cues, s.Settings())
        self.assertEqual([i["code"] for i in found], ["TIME_ORDER", "TIME_ORDER"])

    def test_nested_overlaps_and_exact_boundary(self):
        cues = [s.Cue(1, 0, 10000, "あ"), s.Cue(2, 1000, 2000, "い"),
                s.Cue(3, 3000, 4000, "う"), s.Cue(4, 10000, 12000, "え")]
        self.assertEqual({x['position'] for x in s.analyze(cues, s.Settings()) if x['code'] == 'OVERLAP'}, {2, 3})

    def test_overlap_detector_matches_independent_pairwise_oracle(self):
        rng = random.Random(10000)
        for _ in range(60):
            cues = [s.Cue(i+1, rng.randrange(20000), 0, "テスト") for i in range(35)]
            cues = [s.Cue(c.number, c.start, c.start+rng.randrange(1, 5000), c.text) for c in cues]
            ordered = sorted(enumerate(cues, 1), key=lambda x: (x[1].start, x[0]))
            expected = {pos for j, (pos, cue) in enumerate(ordered)
                        if any(prior.end > cue.start for _, prior in ordered[:j])}
            actual = {x['position'] for x in s.analyze(cues, s.Settings()) if x['code'] == 'OVERLAP'}
            self.assertEqual(actual, expected)

    def test_normalization_preserves_words_order_and_timings(self):
        cues = [s.Cue(99, 9000, 12000, "  原文を残す。  \n二行目"), s.Cue(7, 1000, 2000, "次の字幕")]
        result = s.parse_srt(s.normalize(cues))
        self.assertEqual([(c.start, c.end) for c in result], [(c.start, c.end) for c in cues])
        self.assertEqual([c.text for c in result], ["原文を残す。\n二行目", "次の字幕"])
        self.assertEqual([c.number for c in result], [1, 2])

    def test_japanese_display_width_and_speed(self):
        self.assertEqual(s.display_width("あいうABC"), 9)
        self.assertEqual(s.display_width("e\u0301"), 1)
        cues = [s.Cue(1, 0, 200, "あ"*24)]
        self.assertTrue({'FAST', 'WIDE', 'SHORT'} <= {x['code'] for x in s.analyze(cues, s.Settings())})

    def test_subtitle_html_is_escaped_and_no_network_assets(self):
        cues = [s.Cue(1, 0, 2000, '<script>alert(1)</script><img src="https://evil.invalid/a">')]
        report = s.make_html(cues, s.analyze(cues, s.Settings()),
                             {'input_name':'<evil>', 'sha256':'abc', 'encoding':'utf-8', 'settings':s.asdict(s.Settings())})
        self.assertNotIn('<script>', report)
        self.assertNotIn('<img ', report)
        self.assertIn('&lt;script&gt;', report)
        self.assertIn("default-src 'none'", report)

    def test_explicit_legacy_encodings(self):
        for encoding in ['cp932', 'utf-16']:
            with self.subTest(encoding=encoding):
                self.input.write_bytes(self.sample.encode(encoding))
                self.assertEqual(self.run_cli('--encoding', encoding, '--out', self.root / encoding), 0)

    def test_wrong_encoding_fails_without_replacement_characters(self):
        self.input.write_bytes(self.sample.encode('cp932'))
        self.assertEqual(self.run_cli('--out', self.root / 'out'), 2)
        self.assertFalse((self.root / 'out').exists())

    def test_large_file_rejected(self):
        self.input.write_bytes(b'x' * (s.MAX_BYTES+1))
        self.assertEqual(self.run_cli('--out', self.root / 'out'), 2)
        self.assertFalse((self.root / 'out').exists())

    def test_invalid_thresholds_rejected(self):
        for flags in [('--cps', 'nan'), ('--width', '0'), ('--min-seconds', '8', '--max-seconds', '7')]:
            with self.subTest(flags=flags), self.assertRaises(SystemExit):
                self.run_cli(*flags)


if __name__ == '__main__':
    unittest.main()
