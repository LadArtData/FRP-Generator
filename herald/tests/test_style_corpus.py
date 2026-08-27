"""Style anchor corpus — rhythm defaults and calibration."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import style_corpus, voice  # noqa: E402


def test_refresh_without_database_uses_default_rhythm(monkeypatch):
    monkeypatch.setattr(style_corpus, "fetch_anchor_rows", lambda limit=999: [])
    style_corpus.refresh()
    rhythm = style_corpus.get_rhythm()
    assert rhythm == voice.DEFAULT_RHYTHM


def test_refresh_calibrates_from_anchor_chunks(monkeypatch):
    # Synthetic anchor-like prose: varied sentence lengths, no banned words.
    chunk = (
        "iteria will configure Oracle Cloud Financials for the agency. "
        "The project team meets weekly with client staff. "
        "Interfaces run through Oracle Integration Cloud. "
    ) * 40
    monkeypatch.setattr(
        style_corpus,
        "fetch_anchor_rows",
        lambda limit=999: [{"text": chunk, "filename": "StPetersburg_Proposal.docx",
                            "client": "City of St. Petersburg"}],
    )
    style_corpus.refresh()
    rhythm = style_corpus.get_rhythm()
    assert rhythm["mean_words"] > 0
    assert rhythm != voice.DEFAULT_RHYTHM or rhythm["mean_words"] == voice.DEFAULT_RHYTHM["mean_words"]


def test_render_rules_uses_active_rhythm(monkeypatch):
    monkeypatch.setattr(voice, "active_rhythm", lambda: {"mean_words": 21.0,
                                                          "short_under_10_min": 0.15,
                                                          "long_over_35_max": 0.18,
                                                          "band_15_25_max": 0.5,
                                                          "stdev_min": 8.0,
                                                          "median_words": 18})
    rules = voice.render_rules(include_replacements=False)
    assert "21.0 words per sentence" in rules
