"""Chat providers for game_chat mode (T5.7): interface + ghost placeholder.

The providers mechanism is the v1.1 extension point (design doc §8):
config/providers.yaml declares one provider per slot; v1 ships only the
ghost kind. M6 fills in the real cloud stack (DSv4 + CPU ASR + CosyVoice
cloud API) behind the same ChatProvider interface.
"""

from __future__ import annotations

from typing import Any, Protocol


class ChatProvider(Protocol):
    """One chat backend usable in game_chat mode."""

    name: str

    def available(self) -> bool: ...

    async def chat(self, prompt: str) -> str: ...


class GhostChatProvider:
    """Placeholder provider: present in config, refuses to run (v1.1 lands in M6)."""

    name = "ghost"

    def available(self) -> bool:
        return False

    async def chat(self, prompt: str) -> str:
        raise NotImplementedError(
            "game_chat provider arrives in v1.1 (M6): cloud DSv4 + CPU ASR + CosyVoice cloud API"
        )


def load_providers(config: dict[str, Any]) -> dict[str, ChatProvider]:
    """Build the provider registry from a parsed providers.yaml.

    Format:
        providers:
          game_chat:
            kind: ghost
    """
    registry: dict[str, ChatProvider] = {}
    for slot, spec in (config.get("providers") or {}).items():
        if not isinstance(spec, dict):
            raise ValueError(f"provider slot '{slot}' must be a mapping")
        kind = str(spec.get("kind", "ghost"))
        if kind == "ghost":
            registry[str(slot)] = GhostChatProvider()
        else:
            raise ValueError(f"unknown provider kind '{kind}' for slot '{slot}' (v1: ghost only)")
    return registry
