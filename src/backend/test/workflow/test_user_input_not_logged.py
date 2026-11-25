"""AC-14: what the user typed into a waiting workflow must not be written to the logs."""

from unittest.mock import AsyncMock

from loguru import logger

from bisheng.common.chat.clients.workflow_client import WorkflowClient

MARKER = "SECRET-MARKER-9f3a"


async def test_handle_user_input_logs_the_shape_not_the_content():
    client = WorkflowClient.__new__(WorkflowClient)
    client.client_id = "c1"
    client.workflow = None
    client.ensure_workflow_available = AsyncMock(return_value=True)
    client.send_response = AsyncMock()

    lines = []
    sink = logger.add(lambda message: lines.append(str(message)), level="DEBUG")
    try:
        await client.handle_user_input({"action": "input", "data": {"input_1": {"user_input": MARKER}}})
    finally:
        logger.remove(sink)

    assert any("get user input" in line for line in lines)
    assert not any(MARKER in line for line in lines)
