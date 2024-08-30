from typing import Any, Dict

from discord.gateway import DiscordVoiceWebSocket

from .voice_client import VoiceClient, VoiceConnectionState


__all__ = ("hook",)


async def hook(self: DiscordVoiceWebSocket, msg: Dict[str, Any]):
    # TODO: implement other voice events
    op: int = msg["op"]
    data: Dict[str, Any] = msg.get("d", {})
    vc = self._connection.voice_client

    if not isinstance(vc, VoiceClient):
        raise ValueError("VoiceClient is not the expected type")

    if op == DiscordVoiceWebSocket.SPEAKING:
        vc.update_ssrc(data)
