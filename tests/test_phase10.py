"""Phase 10 tests: The Split Screen.

The failure modes the spec names:

- a dashboard of metric tiles, which shows that a system exists rather than
  showing it working
- reasoning shown as a collapsed log requiring clicks to expand
- beautiful but static, with no live data behind it
"""

import re

from blackbox.ui import SPLIT_SCREEN_HTML as PAGE


def test_the_page_is_self_contained():
    """No CDN, no build step, so it works on a locked-down network."""
    assert PAGE.lstrip().startswith("<!doctype html>")
    assert "</html>" in PAGE
    for remote in ("https://cdn", "http://cdn", "unpkg.com", "jsdelivr", "googleapis.com/css"):
        assert remote not in PAGE, f"the page loads something from {remote}"


def test_reasoning_streams_rather_than_collapsing():
    """A collapsed log you have to click into hides the thing worth seeing."""
    assert "EventSource" in PAGE
    assert "/stream/reasoning" in PAGE
    # No disclosure widgets around the reasoning.
    assert "<details" not in PAGE


def test_every_panel_reads_a_real_endpoint():
    """Beautiful but static, with no live data behind it, is a named failure."""
    for endpoint in (
        "/overview",
        "/stream/reasoning",
        "/suspensions",
        "/replay",
        "/taint/",
        "/retractions",
        "/as-of/",
        "/redteam/metrics",
        "/cases/",
    ):
        assert endpoint in PAGE, f"nothing on the page reads {endpoint}"


def test_all_six_views_are_present():
    """Each of the things the spec asks the Split Screen to show."""
    for view in ("v-live", "v-split", "v-ink", "v-eraser", "v-time", "v-immune"):
        assert f'id="{view}"' in PAGE, f"{view} is missing"


def test_the_divergence_view_leads_with_the_rule_change():
    """A rule reaching a different verdict is the interesting difference.

    Index alignment alone would report the replay not re-emitting a tool call as
    the first difference, which is true and dull.
    """
    assert "rule_changes" in PAGE
    assert "A rule reached a different verdict" in PAGE


def test_the_page_says_when_there_is_nothing_to_show():
    """Rather than rendering plausible-looking placeholder data."""
    assert "No case is open yet" in PAGE
    assert "No campaign has been run on this instance yet" in PAGE
    assert "No retraction on this instance yet" in PAGE


def test_the_numbers_are_context_not_the_point():
    """Tiles show a system exists. The page has one row of them, above the fold."""
    stat_blocks = len(re.findall(r"class=.stat.", PAGE))
    assert stat_blocks <= 3, "the page is turning into a dashboard of tiles"


def test_the_taint_view_shows_the_source_text():
    """The trail has to reach the customer's own sentence to mean anything."""
    assert "source_text" in PAGE
    assert "newly_restricted_by" in PAGE
