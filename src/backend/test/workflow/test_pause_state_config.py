"""F002: error codes and retention setting for persisted workflow pause state."""

import pytest

from bisheng.common.errcode.flow import (
    WorkFlowPauseResumeConflictError,
    WorkFlowPauseStateExpiredError,
    WorkFlowPauseStateInvalidError,
)
from bisheng.core.config.settings import WorkflowConf

SEVEN_DAYS = 7 * 24 * 3600


def test_the_new_error_codes_are_distinct_and_in_the_workflow_range():
    codes = {
        WorkFlowPauseStateExpiredError.Code,
        WorkFlowPauseStateInvalidError.Code,
        WorkFlowPauseResumeConflictError.Code,
    }
    assert codes == {10544, 10545, 10546}


def test_retention_defaults_to_seven_days():
    assert WorkflowConf().pause_retention_seconds == SEVEN_DAYS


def test_retention_can_be_configured():
    assert WorkflowConf(pause_retention_seconds=3600).pause_retention_seconds == 3600


@pytest.mark.parametrize("value", [0, -1, "abc", None, 1.5, True, [], {}])
def test_invalid_retention_falls_back_to_the_default(value):
    assert WorkflowConf(pause_retention_seconds=value).pause_retention_seconds == SEVEN_DAYS
