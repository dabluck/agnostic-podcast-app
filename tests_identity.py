#!/usr/bin/env python3
"""Tests for identity — the shared identity/normalization rules.

Run: python3 tests_identity.py
Cases are drawn from real data seen in this project (see DEDUP_RULES.md),
so a failure means a rule regression, not a hypothetical.
"""

import unittest

from identity import (PRIVATE_URL, core_url, dur_to_sec, iso_any,
                         iso_from_epoch, norm_title, strip_suffix)


class TestCoreUrl(unittest.TestCase):
    def test_strips_query(self):
        self.assertEqual(core_url("https://traffic.megaphone.fm/CAD123.mp3?updated=1780934549"),
                         "traffic.megaphone.fm/cad123.mp3")

    def test_strips_tracking_prefix_chain(self):
        # podtrac -> swap.fm -> omny chain seen in The Daily's feed
        u = ("https://podtrac.com/pts/redirect.mp3/tracking.swap.fm/track/abc/"
             "traffic.omny.fm/d/clips/x/y/z/audio.mp3?utm_source=Podcast")
        self.assertEqual(core_url(u), "traffic.omny.fm/d/clips/x/y/z/audio.mp3")

    def test_pscrb_prefix(self):
        u = "https://pscrb.fm/rss/p/media.transistor.fm/04eefe71/39c635fd.mp3"
        self.assertEqual(core_url(u), "media.transistor.fm/04eefe71/39c635fd.mp3")

    def test_non_media_url_passthrough_lowercased(self):
        self.assertEqual(core_url("https://Example.com/Page"), "https://example.com/page")

    def test_none_and_empty(self):
        self.assertEqual(core_url(None), "")
        self.assertEqual(core_url(""), "")


class TestTitles(unittest.TestCase):
    def test_norm_strips_private_feed_parenthetical(self):
        self.assertEqual(norm_title("Politix (private feed for you@example.com)"), "politix")
        self.assertEqual(norm_title("Politix"), "politix")

    def test_norm_strips_adfree_and_binge(self):
        self.assertEqual(norm_title("Cut, Color, Kill (Ad-Free)"), "cutcolorkill")
        self.assertEqual(norm_title("Chameleon: Dr. Miracle (Ad-Free, THE BINGE)"),
                         "chameleondrmiracle")

    def test_norm_keeps_plus_insensitive(self):
        # "Bulwark+ Takes" must equal public "Bulwark Takes"
        self.assertEqual(norm_title("Bulwark+ Takes (private feed for x)"),
                         norm_title("Bulwark Takes"))

    def test_suffix_fold_club_and_patrons(self):
        self.assertEqual(strip_suffix("The Rest Is History Club"), "therestishistory")
        self.assertEqual(strip_suffix("Blocked and Reported Patrons-Only Episodes Feed"),
                         "blockedandreported")
        self.assertEqual(strip_suffix("Crime Writers On...Bonus Content!"),
                         "crimewriterson")

    def test_suffix_none_when_no_marker(self):
        # "The Founder" must NOT reduce toward "Founders"
        self.assertIsNone(strip_suffix("The Founder"))
        self.assertIsNone(strip_suffix("Hysteria"))

    def test_suffix_all_markers(self):
        for variant, base in [("X Members", "x"), ("X Members Only", "x"),
                              ("X Archives", "x"), ("X Ad-Free", "x"),
                              ("X Premium", "x"), ("X Plus", "x"),
                              ("X: Club", "x"), ("X Bonus Episodes", "x")]:
            self.assertEqual(strip_suffix(variant), base, variant)

    def test_suffix_strips_only_trailing_marker(self):
        # a marker word mid-title is part of the name, not a variant suffix;
        # the linker's family-match requirement is the real over-fold guard,
        # but the function itself must not touch non-trailing occurrences
        self.assertIsNone(strip_suffix("Plus One with Sam"))
        self.assertEqual(strip_suffix("Stratechery Plus"), "stratechery")


class TestPrivateUrl(unittest.TestCase):
    # placeholder URLs — one per PRIVATE_URL branch (no real tokens/ids)
    PRIVATE = [
        "https://api.substack.com/feed/podcast/000000/private/PLACEHOLDER.rss",
        "https://www.patreon.com/rss/000000?auth=PLACEHOLDER",
        "https://rss.example.com/members/000000/feed?access_token=PLACEHOLDER",
        "https://binge.supportingcast.fm/content/PLACEHOLDER.rss",
        "https://example.passport.online/feed/podcast/PLACEHOLDER",
    ]
    PUBLIC = [
        "https://feeds.megaphone.fm/vergecast",
        "https://audioboom.com/channels/5149464.rss",
        "https://www.thisamericanlife.org/podcast/rss.xml",
        "http://feeds.feedburner.com/dancarlin/commonsense?format=xml",
    ]

    def test_private_urls_flagged(self):
        for u in self.PRIVATE:
            self.assertTrue(PRIVATE_URL.search(u), u)

    def test_public_urls_not_flagged(self):
        for u in self.PUBLIC:
            self.assertFalse(PRIVATE_URL.search(u), u)


class TestDates(unittest.TestCase):
    def test_epoch_seconds(self):
        # Aurelian pubDate (seconds) — the unit bug that made 5,651 episodes 1970
        self.assertEqual(iso_from_epoch(1667538041), "2022-11-04T05:00:41Z")

    def test_epoch_milliseconds(self):
        self.assertEqual(iso_from_epoch(1667538041000), "2022-11-04T05:00:41Z")

    def test_epoch_falsy(self):
        self.assertIsNone(iso_from_epoch(0))
        self.assertIsNone(iso_from_epoch(None))

    def test_epoch_threshold_boundary(self):
        # just under 1e11 is seconds (year 5138... of course absurd, but the
        # rule is the rule); just over is milliseconds (year 1973)
        self.assertEqual(iso_from_epoch(99_999_999_999)[:4], "5138")
        self.assertEqual(iso_from_epoch(100_000_000_001)[:4], "1973")

    def test_epoch_apple_reference_limitation(self):
        # DOCUMENTED LIMITATION (rebuild.py guards it with the plays/sessions
        # < 2015 invariant): an Apple-reference date misfiled as Unix seconds
        # cannot be detected by magnitude — it lands in ~1994, not 1970.
        self.assertEqual(iso_from_epoch(978_307_200 - 200_000_000)[:4], "1994")

    def test_iso_any_iso_input(self):
        self.assertEqual(iso_any("2026-06-22T15:39:49.000Z"), "2026-06-22T15:39:49Z")

    def test_iso_any_rfc2822_input(self):
        self.assertEqual(iso_any("Wed, 04 Nov 2022 05:00:41 +0000"), "2022-11-04T05:00:41Z")

    def test_iso_any_naive_assumed_utc(self):
        self.assertEqual(iso_any("2022-11-04T05:00:41"), "2022-11-04T05:00:41Z")

    def test_iso_any_garbage(self):
        self.assertIsNone(iso_any("not a date"))
        self.assertIsNone(iso_any(None))


class TestDuration(unittest.TestCase):
    def test_forms(self):
        self.assertEqual(dur_to_sec("2912"), 2912.0)
        self.assertEqual(dur_to_sec("48:32"), 2912.0)
        self.assertEqual(dur_to_sec("1:02:03"), 3723.0)
        self.assertIsNone(dur_to_sec(""))
        self.assertIsNone(dur_to_sec("n/a"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
