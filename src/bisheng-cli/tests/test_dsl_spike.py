"""T001 spike (temporary): the CLI test run must see the LIVE neutral package source."""

import bisheng_flow_dsl


def test_neutral_package_is_the_live_source():
    assert bisheng_flow_dsl.PACKAGE == "bisheng_flow_dsl"
    assert "backend" in bisheng_flow_dsl.__file__.replace("\\", "/")
