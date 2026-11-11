"""Proof that the top-level notifier capture covers subdirectories too."""

import scripts.openclaw_alerts as oa


def test_alert_openclaw_from_unit_dir_is_recorded(sent_alerts):
    assert oa.alert_openclaw("from tests/unit") is True
    assert [e["fn"] for e in sent_alerts] == ["alert_openclaw"]
