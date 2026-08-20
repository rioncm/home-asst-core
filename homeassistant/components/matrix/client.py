"""Client for UI-managed outbound Matrix delivery."""

import asyncio
from collections.abc import Sequence
import mimetypes
import os
from typing import Any

import aiofiles
import aiofiles.os
from aiohttp import ClientError
from nio import AsyncClient, AsyncClientConfig
from nio.responses import (
    ErrorResponse,
    LoginError,
    LoginResponse,
    RoomResolveAliasResponse,
    UploadError,
    UploadResponse,
    WhoamiError,
    WhoamiResponse,
)
from PIL import Image

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError

from .const import ATTR_FORMAT, ATTR_IMAGES, ATTR_THREAD_ID, DOMAIN, FORMAT_HTML

DEFAULT_CONTENT_TYPE = "application/octet-stream"


class MatrixAuthenticationError(HomeAssistantError):
    """Raised when Matrix authentication fails."""


class MatrixConnectionError(HomeAssistantError):
    """Raised when a Matrix homeserver cannot be reached."""


async def async_login_client(
    homeserver: str,
    verify_ssl: bool,
    username: str,
    password: str,
    access_token: str | None = None,
) -> tuple[AsyncClient, str, str]:
    """Connect and authenticate a Matrix client."""
    client = AsyncClient(
        homeserver=homeserver,
        user=username,
        ssl=verify_ssl,
        config=AsyncClientConfig(max_limit_exceeded=0, max_timeouts=0),
    )
    try:
        if access_token is not None:
            client.restore_login(
                user_id=username,
                device_id="",
                access_token=access_token,
            )
            whoami_response = await client.whoami()
            if isinstance(whoami_response, WhoamiResponse):
                return client, whoami_response.user_id, access_token

        login_response = await client.login(password=password)
    except (ClientError, OSError, TimeoutError, ValueError) as err:
        await client.close()
        raise MatrixConnectionError from err

    if isinstance(login_response, LoginResponse):
        return client, login_response.user_id, login_response.access_token

    await client.close()
    if isinstance(login_response, LoginError) or (
        access_token is not None and isinstance(whoami_response, WhoamiError)
    ):
        raise MatrixAuthenticationError
    raise MatrixConnectionError


def _read_image_size(image_path: str) -> tuple[int, int]:
    """Open image to determine image size."""
    with Image.open(image_path) as image:
        return image.size


class MatrixOutboundClient:
    """Manage a Matrix client used only for outbound delivery."""

    def __init__(self, hass: HomeAssistant, client: AsyncClient) -> None:
        """Initialize the outbound client."""
        self._hass = hass
        self._client = client

    async def async_close(self) -> None:
        """Close the Matrix client."""
        await self._client.close()

    async def _async_resolve_room(self, room: str) -> str:
        """Resolve a room alias to a room ID."""
        if room.startswith("!"):
            return room
        try:
            response = await self._client.room_resolve_alias(room)
        except (ClientError, OSError, TimeoutError) as err:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="delivery_failed",
            ) from err
        if isinstance(response, RoomResolveAliasResponse):
            return str(response.room_id)
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="room_not_found",
        )

    async def _async_room_send(
        self, room: str, message_type: str, content: dict[str, Any]
    ) -> None:
        """Send content to one room."""
        room_id = await self._async_resolve_room(room)
        try:
            response = await self._client.room_send(
                room_id=room_id,
                message_type=message_type,
                content=content,
            )
        except (ClientError, OSError, TimeoutError) as err:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="delivery_failed",
            ) from err
        if isinstance(response, ErrorResponse):
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="delivery_failed",
            )

    async def _async_multi_room_send(
        self, rooms: Sequence[str], message_type: str, content: dict[str, Any]
    ) -> None:
        """Send content to multiple rooms."""
        await asyncio.gather(
            *(self._async_room_send(room, message_type, content) for room in rooms)
        )

    async def _async_send_image(
        self, image_path: str, rooms: Sequence[str], thread_id: str | None
    ) -> None:
        """Upload an image and send it to the target rooms."""
        is_allowed_path = await self._hass.async_add_executor_job(
            self._hass.config.is_allowed_path, image_path
        )
        if not is_allowed_path:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="path_not_allowed",
            )

        width, height = await self._hass.async_add_executor_job(
            _read_image_size, image_path
        )
        mime_type = mimetypes.guess_type(image_path)[0] or DEFAULT_CONTENT_TYPE
        file_stat = await aiofiles.os.stat(image_path)

        try:
            async with aiofiles.open(image_path, "rb") as image_file:
                response, _ = await self._client.upload(
                    image_file,
                    content_type=mime_type,
                    filename=os.path.basename(image_path),
                    filesize=file_stat.st_size,
                )
        except (ClientError, OSError, TimeoutError) as err:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="upload_failed",
            ) from err
        if isinstance(response, UploadError):
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="upload_failed",
            )
        if not isinstance(response, UploadResponse):
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="upload_failed",
            )

        content: dict[str, Any] = {
            "body": os.path.basename(image_path),
            "info": {
                "size": file_stat.st_size,
                "mimetype": mime_type,
                "w": width,
                "h": height,
            },
            "msgtype": "m.image",
            "url": response.content_uri,
        }
        if thread_id is not None:
            content["m.relates_to"] = {
                "event_id": thread_id,
                "rel_type": "m.thread",
            }

        await self._async_multi_room_send(rooms, "m.room.message", content)

    async def async_send_message(
        self, message: str, rooms: list[str], data: dict[str, Any] | None
    ) -> None:
        """Send a text or HTML message and optional images."""
        content: dict[str, Any] = {"msgtype": "m.text", "body": message}
        thread_id: str | None = None
        if data is not None:
            thread_id = data.get(ATTR_THREAD_ID)
            if data.get(ATTR_FORMAT) == FORMAT_HTML:
                content |= {
                    "format": "org.matrix.custom.html",
                    "formatted_body": message,
                }
            if thread_id is not None:
                content["m.relates_to"] = {
                    "event_id": thread_id,
                    "rel_type": "m.thread",
                }

        await self._async_multi_room_send(rooms, "m.room.message", content)

        if data is not None:
            for image_path in data.get(ATTR_IMAGES, []):
                await self._async_send_image(image_path, rooms, thread_id)

    async def async_send_reaction(
        self, reaction: str, room: str, message_id: str
    ) -> None:
        """Send a reaction to a Matrix event."""
        content = {
            "m.relates_to": {
                "event_id": message_id,
                "key": reaction,
                "rel_type": "m.annotation",
            }
        }
        await self._async_room_send(room, "m.reaction", content)
