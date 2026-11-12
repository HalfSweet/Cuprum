"""Running KiCad's long-running API tasks (ERC, DRC) to completion."""

from __future__ import annotations

import time

from kipy.proto.common import tasks_pb2


def run_task(client, start_command, what: str, timeout_s: float):
    """Send the command that starts a task, wait for the task to finish and return its token."""
    token = client.send(start_command, tasks_pb2.StartTaskResponse).token

    deadline = time.monotonic() + timeout_s
    while True:
        query = tasks_pb2.GetTaskStatus()
        query.token.CopyFrom(token)
        status = client.send(query, tasks_pb2.TaskStatusResponse)
        if status.state == tasks_pb2.TS_COMPLETED:
            return token
        if status.state != tasks_pb2.TS_RUNNING:
            state = tasks_pb2.TaskState.Name(status.state)
            raise RuntimeError(f"{what} task did not complete ({state}): {status.error_message}")
        if time.monotonic() >= deadline:
            raise TimeoutError(f"timed out waiting for {what} to finish")
        time.sleep(0.1)
