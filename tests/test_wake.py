"""'Hey Jarvis' for the browser app."""

import json

from jarvis.wakeweb import WakeListener


def test_off_by_default_and_remembered(tmp_path):
    w = WakeListener(tmp_path / "wake.json", on_wake=lambda: None)
    assert w.enabled is False and w.status()["running"] is False
    w.start()  # does nothing while switched off
    assert w.status()["running"] is False
    w.enabled = True
    w.stop()
    (tmp_path / "wake.json").write_text(json.dumps({"enabled": True}))
    assert WakeListener(tmp_path / "wake.json", on_wake=lambda: None).enabled is True


def test_scores_above_the_bar_wake_once_per_cooldown(tmp_path):
    w = WakeListener(tmp_path / "wake.json", on_wake=lambda: None, threshold=0.5, cooldown=2.0)
    assert not w.heard(0.3, now=10.0)
    assert w.heard(0.9, now=10.0)
    assert not w.heard(0.95, now=11.0)  # still the same "Hey Jarvis"
    assert w.heard(0.8, now=12.5)
