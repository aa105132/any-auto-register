from __future__ import annotations

from domain.actions import ActionExecutionCommand
from application.tasks import create_batch_action_task, create_platform_action_task
from services.task_runtime import task_runtime
from infrastructure.platform_runtime import PlatformRuntime


class ActionsService:
    def __init__(self, runtime: PlatformRuntime | None = None):
        self.runtime = runtime or PlatformRuntime()

    def list_actions(self, platform: str) -> dict:
        actions = self.runtime.list_actions(platform)
        return {
            "actions": [
                {
                    "id": action.id,
                    "label": action.label,
                    "params": [
                        {
                            "key": param.key,
                            "label": param.label,
                            "type": param.type,
                            "options": param.options,
                        }
                        for param in action.params
                    ],
                }
                for action in actions
            ]
        }

    def execute_action(self, command: ActionExecutionCommand) -> dict:
        task = create_platform_action_task(
            {
                "platform": command.platform,
                "account_id": command.account_id,
                "action_id": command.action_id,
                "params": command.params,
            }
        )
        task_runtime.wake_up()
        return task

    def execute_batch_action(
        self,
        platform: str,
        action_id: str,
        account_ids: list[int],
        params: dict | None = None,
    ) -> dict:
        task = create_batch_action_task(
            {
                "platform": platform,
                "action_id": action_id,
                "account_ids": account_ids,
                "params": params or {},
            }
        )
        task_runtime.wake_up()
        return task
