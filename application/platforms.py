from __future__ import annotations

from infrastructure.platform_runtime import PlatformRuntime


class PlatformsService:
    def __init__(self, runtime: PlatformRuntime | None = None):
        self.runtime = runtime or PlatformRuntime()

    def list_platforms(self) -> list[dict]:
        result = []
        for item in self.runtime.list_platforms():
            result.append(
                {
                    "name": item.name,
                    "display_name": item.display_name,
                    "version": item.version,
                    "supported_executors": item.capabilities.supported_executors,
                    "supported_identity_modes": item.capabilities.supported_identity_modes,
                    "supported_oauth_providers": item.capabilities.supported_oauth_providers,
                    "default_mail_provider": item.capabilities.default_mail_provider,
                }
            )
        return result

    def get_desktop_state(self, platform: str) -> dict:
        return self.runtime.get_desktop_state(platform)
