from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import secrets
import unicodedata
import warnings
import zipfile
from io import BytesIO
from pathlib import Path
from urllib.parse import quote, urlsplit

from aiohttp import WSMsgType, web
from aiohttp.web_request import FileField
from PIL import Image, ImageOps

from store import Store

ROOT_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("AJOECHAT_DATA", ROOT_DIR / "data")).resolve()
HOST = os.environ.get("AJOECHAT_HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", os.environ.get("AJOECHAT_PORT", "6772")))
COOKIE_SECURE = os.environ.get("AJOECHAT_COOKIE_SECURE", "0").casefold() in {
    "1",
    "true",
    "yes",
}

MAX_TEXT_LENGTH = 3000
MAX_UPLOAD_BYTES = 5_000_000
MAX_IMAGE_PIXELS = 40_000_000
MAX_REQUEST_BYTES = 5_500_000
COOKIE_NAME = "ajoechat_session"
HISTORY_LIMIT = 100

logging.basicConfig(
    level=os.environ.get("AJOECHAT_LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger("ajoechat")


def token_digest(token: str | None) -> str | None:
    if not token:
        return None
    return hashlib.sha256(token.encode("utf8")).hexdigest()


def username_parts(value: object) -> tuple[str, str] | None:
    if not isinstance(value, str):
        return None
    username = unicodedata.normalize("NFKC", value).strip()
    if not 1 <= len(username) <= 24:
        return None
    for index, character in enumerate(username):
        category = unicodedata.category(character)[0]
        allowed = category in {"L", "N"} or character == "_"
        if category == "M" and index > 0:
            allowed = True
        if not allowed:
            return None
    return username, username.casefold()


def valid_text(value: object) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    if len(value) > MAX_TEXT_LENGTH:
        return False
    return not any(0xD800 <= ord(character) <= 0xDFFF for character in value)


def valid_event_id(value: object) -> str | None:
    if not isinstance(value, str) or len(value) != 24:
        return None
    if any(character not in "0123456789abcdef" for character in value.casefold()):
        return None
    return value


def valid_media_id(value: object) -> str | None:
    if not isinstance(value, str) or len(value) != 32:
        return None
    if any(character not in "0123456789abcdef" for character in value.casefold()):
        return None
    return value


def message_id_for(token_hash: str, event_type: str, client_id: str) -> str:
    value = f"{token_hash}:{event_type}:{client_id}"
    return hashlib.sha256(value.encode("utf8")).hexdigest()[:32]


def origin_matches(request: web.Request) -> bool:
    origin = request.headers.get("Origin")
    if not origin:
        return False
    parsed = urlsplit(origin)
    return (
        parsed.scheme in {"http", "https"}
        and bool(parsed.netloc)
        and parsed.netloc.casefold() == request.host.casefold()
    )


def json_error(message: str, status: int) -> web.Response:
    return web.json_response({"error": message}, status=status)


def write_private_file(path: Path, content: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as image_file:
        image_file.write(content)


def sanitize_image(data: bytes) -> tuple[bytes, str, str]:
    allowed_formats = {
        "JPEG": ("image/jpeg", ".jpg"),
        "PNG": ("image/png", ".png"),
        "WEBP": ("image/webp", ".webp"),
    }
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(data)) as source:
                image_format = (source.format or "").upper()
                if image_format not in allowed_formats:
                    raise ValueError("Choose a JPEG, PNG, or WebP image.")
                if source.width * source.height > MAX_IMAGE_PIXELS:
                    raise ValueError("This image has too many pixels.")
                source.seek(0)
                image = ImageOps.exif_transpose(source).copy()

        output = BytesIO()
        if image_format == "JPEG":
            if image.mode not in {"RGB", "L"}:
                image = image.convert("RGB")
            image.save(output, format="JPEG", quality=90, optimize=True)
        elif image_format == "PNG":
            image.save(output, format="PNG", optimize=True)
        else:
            image.save(output, format="WEBP", quality=88, method=4)
        return (
            output.getvalue(),
            allowed_formats[image_format][0],
            allowed_formats[image_format][1],
        )
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError(
            "This image could not be opened. Choose another image."
        ) from exc


def safe_upload_name(value: str | None) -> str:
    name = (value or "").replace("\\", "/").rsplit("/", 1)[-1].strip()
    name = "".join(
        character
        for character in name
        if unicodedata.category(character) not in {"Cc", "Cs"}
    )
    if len(name) > 180:
        suffix = Path(name).suffix
        if len(suffix) > 16:
            suffix = ""
        stem = name[: -len(suffix)] if suffix else name
        name = stem[: max(1, 180 - len(suffix))] + suffix
    return name if name.strip(" .") else "Shared file"


def is_mp3(data: bytes) -> bool:
    offset = 0
    if data.startswith(b"ID3"):
        if len(data) < 10 or data[3] not in {2, 3, 4} or data[4] == 0xFF:
            return False
        size_bytes = data[6:10]
        if any(value & 0x80 for value in size_bytes):
            return False
        tag_size = sum(
            value << shift for value, shift in zip(size_bytes, (21, 14, 7, 0))
        )
        footer_size = 10 if data[3] == 4 and data[5] & 0x10 else 0
        offset = 10 + tag_size + footer_size
    if offset + 4 > len(data):
        return False
    header = int.from_bytes(data[offset : offset + 4], "big")
    sync = header >> 21 == 0x7FF
    version = (header >> 19) & 0b11
    layer = (header >> 17) & 0b11
    bitrate = (header >> 12) & 0b1111
    sample_rate = (header >> 10) & 0b11
    if (
        not sync
        or version == 0b01
        or layer != 0b01
        or not 1 <= bitrate <= 14
        or sample_rate == 0b11
    ):
        return False

    mpeg1_bitrates = (0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320)
    mpeg2_bitrates = (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160)
    base_sample_rates = (44100, 48000, 32000)
    bitrate_kbps = (mpeg1_bitrates if version == 0b11 else mpeg2_bitrates)[bitrate]
    sample_rate_hz = base_sample_rates[sample_rate]
    if version == 0b10:
        sample_rate_hz //= 2
    elif version == 0b00:
        sample_rate_hz //= 4
    frame_length = (
        (144 if version == 0b11 else 72) * bitrate_kbps * 1000 // sample_rate_hz
    )
    frame_length += (header >> 9) & 1
    return offset + frame_length <= len(data)


def is_mp4(data: bytes) -> bool:
    offset = 0
    has_file_type = False
    has_movie = False
    has_media_data = False
    while offset < len(data):
        if len(data) - offset < 8:
            return False
        box_size = int.from_bytes(data[offset : offset + 4], "big")
        box_type = data[offset + 4 : offset + 8]
        header_size = 8
        if box_size == 1:
            if len(data) - offset < 16:
                return False
            box_size = int.from_bytes(data[offset + 8 : offset + 16], "big")
            header_size = 16
        elif box_size == 0:
            box_size = len(data) - offset
        if box_size < header_size or offset + box_size > len(data):
            return False

        payload_start = offset + header_size
        payload_size = box_size - header_size
        if box_type == b"ftyp":
            if payload_size < 8 or (payload_size - 8) % 4:
                return False
            major_brand = data[payload_start : payload_start + 4]
            if not all(0x20 <= character <= 0x7E for character in major_brand):
                return False
            has_file_type = True
        elif box_type in {b"moov", b"moof"} and payload_size:
            has_movie = True
        elif box_type == b"mdat" and payload_size:
            has_media_data = True
        offset += box_size

    return has_file_type and has_movie and has_media_data


def is_pdf(data: bytes) -> bool:
    header = re.search(rb"%PDF-(?:1\.[0-7]|2\.0)", data[:1024])
    eof_offset = data.rfind(b"%%EOF")
    if not header or eof_offset < 0 or data[eof_offset + 5 :].strip():
        return False
    startxref_matches = list(re.finditer(rb"startxref\s+(\d+)\s*$", data[:eof_offset]))
    if not startxref_matches:
        return False
    startxref = startxref_matches[-1]
    xref_offset = int(startxref.group(1))
    if xref_offset >= startxref.start() or xref_offset >= len(data):
        return False
    if data[xref_offset : xref_offset + 4] == b"xref":
        return True
    object_header = re.match(rb"\d+\s+\d+\s+obj\b", data[xref_offset:])
    if not object_header:
        return False
    return bool(re.search(rb"/Type\s*/XRef\b", data[xref_offset:eof_offset]))


def is_docx(data: bytes) -> bool:
    try:
        with zipfile.ZipFile(BytesIO(data)) as archive:
            entries = {item.filename: item for item in archive.infolist()}
            content_types = entries.get("[Content_Types].xml")
            document = entries.get("word/document.xml")
            if not content_types or not document or content_types.file_size > 1_000_000:
                return False
            return b"wordprocessingml.document.main+xml" in archive.read(content_types)
    except (OSError, zipfile.BadZipFile, RuntimeError, ValueError, EOFError):
        return False


def sanitize_upload(data: bytes, filename: str) -> tuple[bytes, str, str, str]:
    suffix = Path(filename).suffix.casefold()
    if suffix in {".jpg", ".jpeg", ".png", ".webp"}:
        content, media_type, extension = sanitize_image(data)
        return content, media_type, extension, "image"

    file_types = {
        ".mp4": ("video/mp4", "video", is_mp4),
        ".mp3": ("audio/mpeg", "audio", is_mp3),
        ".txt": ("text/plain; charset=utf-8", "text", None),
        ".pdf": ("application/pdf", "pdf", None),
        ".docx": (
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "document",
            is_docx,
        ),
    }
    file_type = file_types.get(suffix)
    if not file_type:
        raise ValueError(
            "Choose an image, MP4 video, MP3 audio, TXT file, PDF, or DOCX document."
        )
    media_type, category, validator = file_type
    if validator and not validator(data):
        raise ValueError("This file does not match its file type.")
    if suffix == ".pdf" and not is_pdf(data):
        raise ValueError("This file does not match its file type.")
    if suffix == ".txt":
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise ValueError("TXT files must use UTF 8 text.") from exc
        if "\x00" in text:
            raise ValueError("This file does not match its file type.")
    return data, media_type, suffix, category


class Hub:
    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.members: dict[web.WebSocketResponse, dict] = {}

    async def add(
        self,
        socket: web.WebSocketResponse,
        session: dict,
        store: Store,
    ) -> None:
        async with self.lock:
            history = await store.history(HISTORY_LIMIT)
            self.members[socket] = session
            await socket.send_json({"type": "history", "messages": history})
            await self._broadcast_presence_locked()

    async def remove(self, socket: web.WebSocketResponse) -> None:
        async with self.lock:
            if socket in self.members:
                self.members.pop(socket, None)
                await self._broadcast_presence_locked()

    async def broadcast(self, event: dict) -> None:
        async with self.lock:
            failed = await self._send_locked(event)
            if failed and event.get("type") != "presence.snapshot":
                await self._broadcast_presence_locked()

    async def has_session(self, token_hash: str) -> bool:
        async with self.lock:
            return any(
                session["token_hash"] == token_hash for session in self.members.values()
            )

    async def direct_message(
        self,
        sender_hash: str,
        recipient_hash: str,
        message: dict,
        client_id: str,
    ) -> None:
        async with self.lock:
            targets = [
                (socket, session["token_hash"] == sender_hash)
                for socket, session in self.members.items()
                if session["token_hash"] in {sender_hash, recipient_hash}
            ]
            results = await asyncio.gather(
                *(
                    socket.send_json(
                        {
                            "type": "message.created",
                            "message": message,
                            **({"clientId": client_id} if is_sender else {}),
                        }
                    )
                    for socket, is_sender in targets
                ),
                return_exceptions=True,
            )
            failed = False
            for (socket, _), result in zip(targets, results):
                if isinstance(result, BaseException):
                    self.members.pop(socket, None)
                    failed = True
            if failed:
                await self._broadcast_presence_locked()

    async def _send_locked(self, event: dict) -> bool:
        sockets = list(self.members)
        if not sockets:
            return False
        results = await asyncio.gather(
            *(socket.send_json(event) for socket in sockets),
            return_exceptions=True,
        )
        failed = False
        for socket, result in zip(sockets, results):
            if isinstance(result, BaseException):
                self.members.pop(socket, None)
                failed = True
        return failed

    async def _broadcast_presence_locked(self) -> None:
        unique: dict[str, dict] = {}
        for session in self.members.values():
            unique[session["token_hash"]] = session
        ordered = sorted(unique.values(), key=lambda item: item["joined_at"])
        event = {
            "type": "presence.snapshot",
            "members": [
                {"name": item["username"], "joinedAt": item["joined_at"]}
                for item in ordered
            ],
        }
        await self._send_locked(event)

    async def close_session(self, token_hash: str) -> None:
        async with self.lock:
            sockets = [
                socket
                for socket, session in self.members.items()
                if session["token_hash"] == token_hash
            ]
            for socket in sockets:
                try:
                    await socket.send_json(
                        {
                            "type": "session.ended",
                            "message": "Your session ended. Join again.",
                        }
                    )
                    await socket.close(code=4001, message=b"Session ended")
                except Exception:
                    pass
                self.members.pop(socket, None)
            if sockets:
                await self._broadcast_presence_locked()


STORE_KEY = web.AppKey("store", Store)
HUB_KEY = web.AppKey("hub", Hub)
COOKIE_SECURE_KEY = web.AppKey("cookie_secure", bool)
CLEANUP_TASK_KEY = web.AppKey("cleanup_task", asyncio.Task)


def set_security_headers(response: web.StreamResponse) -> None:
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault(
        "Permissions-Policy", "camera=(), microphone=(), geolocation=()"
    )
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; img-src 'self' blob:; style-src 'self'; "
        "script-src 'self'; connect-src 'self' ws: wss:; base-uri 'self'; "
        "form-action 'self'; frame-ancestors 'none'; object-src 'none'",
    )


@web.middleware
async def security_headers(request: web.Request, handler):
    response = await handler(request)
    if not response.prepared:
        set_security_headers(response)
    return response


async def current_session(request: web.Request) -> dict | None:
    digest = token_digest(request.cookies.get(COOKIE_NAME))
    return await request.app[STORE_KEY].session(digest)


async def index(request: web.Request) -> web.Response:
    return web.FileResponse(ROOT_DIR / "static" / "index.html")


async def style_file(request: web.Request) -> web.Response:
    return web.FileResponse(ROOT_DIR / "static" / "styles.css")


async def script_file(request: web.Request) -> web.Response:
    return web.FileResponse(ROOT_DIR / "static" / "app.js")


async def api_me(request: web.Request) -> web.Response:
    session = await current_session(request)
    if not session:
        return json_error("Your session ended. Join again.", 401)
    if not await request.app[STORE_KEY].refresh_session(session["token_hash"]):
        return json_error("Your session ended. Join again.", 401)
    return web.json_response({"name": session["username"]})


async def api_history(request: web.Request) -> web.Response:
    session = await current_session(request)
    if not session:
        return json_error("Your session ended. Join again.", 401)
    if not await request.app[STORE_KEY].refresh_session(session["token_hash"]):
        return json_error("Your session ended. Join again.", 401)
    before_value = request.query.get("before")
    before_id = valid_media_id(before_value) if before_value else None
    if before_value and not before_id:
        return json_error("Choose a message to continue from.", 400)
    peer_value = request.query.get("with")
    if peer_value:
        peer = username_parts(peer_value)
        if not peer or peer[1] == session["username_key"]:
            return json_error("Choose someone else to chat privately.", 400)
        if before_id:
            page = await request.app[STORE_KEY].private_history_before(
                session["token_hash"], peer[1], before_id
            )
        else:
            page = await request.app[STORE_KEY].private_history_page(
                session["token_hash"], peer[1]
            )
        return web.json_response(page)
    if before_id:
        page = await request.app[STORE_KEY].history_before(before_id)
    else:
        page = await request.app[STORE_KEY].public_history_page()
    return web.json_response(page)


async def health(request: web.Request) -> web.Response:
    return web.json_response({"status": "ok"})


async def api_join(request: web.Request) -> web.Response:
    if not origin_matches(request):
        return json_error("Please join from the AJoeChat page.", 403)
    if request.content_length and request.content_length > 2048:
        return json_error("That request is too large.", 413)
    try:
        body = await request.json()
    except (json.JSONDecodeError, web.HTTPException):
        return json_error("Enter a username to join.", 400)
    parts = username_parts(body.get("username") if isinstance(body, dict) else None)
    if not parts:
        return json_error(
            "Use 1 to 24 letters, numbers, or underscore for your username.", 400
        )
    existing = await current_session(request)
    if existing:
        return json_error("Leave your current chat before joining again.", 409)
    username, username_key = parts
    token = secrets.token_hex(32)
    digest = token_digest(token)
    if not await request.app[STORE_KEY].join(digest, username, username_key):
        return json_error("That username is already in use.", 409)
    response = web.json_response({"name": username})
    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=24 * 60 * 60,
        httponly=True,
        secure=request.app[COOKIE_SECURE_KEY],
        samesite="Strict",
        path="/",
    )
    return response


async def api_leave(request: web.Request) -> web.Response:
    if not origin_matches(request):
        return json_error("Please leave from the AJoeChat page.", 403)
    session = await current_session(request)
    response = web.json_response({"ok": True})
    response.del_cookie(COOKIE_NAME, path="/")
    if session:
        await request.app[STORE_KEY].leave(session["token_hash"])
        await request.app[HUB_KEY].close_session(session["token_hash"])
    return response


async def api_upload(request: web.Request) -> web.Response:
    if not origin_matches(request):
        return json_error("Please upload from the AJoeChat page.", 403)
    session = await current_session(request)
    if not session:
        return json_error("Your session ended. Join again.", 401)
    try:
        form = await request.post()
    except web.HTTPRequestEntityTooLarge:
        return json_error("Files must be 5 MB or smaller.", 413)
    uploaded = form.get("file")
    if not isinstance(uploaded, FileField):
        return json_error("Choose a file to send.", 400)
    raw = await asyncio.to_thread(uploaded.file.read, MAX_UPLOAD_BYTES + 1)
    if len(raw) > MAX_UPLOAD_BYTES:
        return json_error("Files must be 5 MB or smaller.", 413)
    original_name = safe_upload_name(uploaded.filename)
    try:
        content, media_type, extension, category = await asyncio.to_thread(
            sanitize_upload, raw, original_name
        )
    except ValueError as exc:
        return json_error(str(exc), 400)

    media_id = secrets.token_hex(16)
    if len(content) > MAX_UPLOAD_BYTES:
        return json_error("Files must be 5 MB or smaller.", 413)
    file_path = request.app[STORE_KEY].attachment_dir / f"{media_id}{extension}"
    try:
        await asyncio.to_thread(write_private_file, file_path, content)
        await request.app[STORE_KEY].add_media(
            media_id,
            session["token_hash"],
            file_path,
            media_type,
            len(content),
            original_name,
            category,
        )
    except Exception:
        try:
            await asyncio.to_thread(file_path.unlink, missing_ok=True)
        except OSError:
            pass
        logger.exception("File storage failed")
        return json_error("The file could not be saved. Try again.", 500)
    return web.json_response(
        {
            "mediaId": media_id,
            "mediaUrl": f"/media/{media_id}",
            "fileName": original_name,
            "fileCategory": category,
            "fileSize": len(content),
        }
    )


async def media_file(request: web.Request) -> web.Response:
    session = await current_session(request)
    if not session:
        return json_error("Join AJoeChat to view this file.", 401)
    media_id = valid_media_id(request.match_info.get("media_id"))
    if not media_id:
        return json_error("This file is no longer available.", 404)
    media = await request.app[STORE_KEY].media(media_id, session["token_hash"])
    if not media:
        return json_error("This file is no longer available.", 404)
    path = Path(media["file_path"])
    if not path.is_file():
        return json_error("This file is no longer available.", 404)
    headers = {
        "Content-Type": media["media_type"],
        "Cache-Control": "private, max-age=3600",
        "X-Content-Type-Options": "nosniff",
    }
    if media["file_category"] != "image":
        headers["Content-Disposition"] = (
            f"attachment; filename*=UTF-8''{quote(media['original_name'], safe='')}"
        )
    return web.FileResponse(
        path,
        headers=headers,
    )


async def websocket_chat(request: web.Request) -> web.StreamResponse:
    if not origin_matches(request):
        return json_error("Open AJoeChat to connect.", 403)
    session = await current_session(request)
    if not session:
        return json_error("Your session ended. Join again.", 401)
    store: Store = request.app[STORE_KEY]
    if not await store.refresh_session(session["token_hash"]):
        return json_error("Your session ended. Join again.", 401)

    socket = web.WebSocketResponse(heartbeat=30, max_msg_size=64 * 1024)
    set_security_headers(socket)
    await socket.prepare(request)
    hub: Hub = request.app[HUB_KEY]
    try:
        await hub.add(socket, session, store)
        async for message in socket:
            if message.type == WSMsgType.TEXT:
                try:
                    event = json.loads(message.data)
                except json.JSONDecodeError:
                    await socket.send_json(
                        {"type": "error", "message": "That message could not be read."}
                    )
                    continue
                if not isinstance(event, dict):
                    await socket.send_json(
                        {"type": "error", "message": "That message could not be read."}
                    )
                    continue
                event_type = event.get("type")
                if event_type == "presence.heartbeat":
                    if not await store.refresh_session(session["token_hash"]):
                        await socket.send_json(
                            {
                                "type": "session.ended",
                                "message": "Your session ended. Join again.",
                            }
                        )
                        await socket.close(code=4001, message=b"Session ended")
                        break
                    continue

                client_id = valid_event_id(event.get("clientId"))
                if not client_id:
                    await socket.send_json(
                        {
                            "type": "error",
                            "message": "Refresh AJoeChat and try again.",
                        }
                    )
                    continue
                recipient = None
                if event_type in {"chat.sendText", "chat.sendAttachment"}:
                    recipient_value = event.get("recipient")
                    if recipient_value is not None:
                        peer = username_parts(recipient_value)
                        if not peer or peer[1] == session["username_key"]:
                            await socket.send_json(
                                {
                                    "type": "error",
                                    "message": "Choose someone else to chat privately.",
                                    "clientId": client_id,
                                }
                            )
                            continue
                        recipient = await store.active_session_for_name(peer[1])
                        if not recipient or not await hub.has_session(
                            recipient["token_hash"]
                        ):
                            await socket.send_json(
                                {
                                    "type": "error",
                                    "message": "That person is no longer here.",
                                    "clientId": client_id,
                                }
                            )
                            continue
                if event_type == "chat.sendText":
                    content = event.get("text")
                    if not valid_text(content):
                        error = "Write a message with 1 to 3000 characters."
                        if isinstance(content, str) and len(content) > MAX_TEXT_LENGTH:
                            error = "Messages can contain up to 3000 characters."
                        await socket.send_json(
                            {"type": "error", "message": error, "clientId": client_id}
                        )
                        continue
                    message_id = message_id_for(
                        session["token_hash"], event_type, client_id
                    )
                    saved = await store.add_text_message(
                        session["token_hash"], content, message_id, recipient
                    )
                    if not saved:
                        if await store.refresh_session(session["token_hash"]):
                            await socket.send_json(
                                {
                                    "type": "error",
                                    "message": "That message could not be sent. Try again.",
                                    "clientId": client_id,
                                }
                            )
                            continue
                        await socket.send_json(
                            {
                                "type": "session.ended",
                                "message": "Your session ended. Join again.",
                            }
                        )
                        await socket.close(code=4001, message=b"Session ended")
                        break
                    if recipient:
                        await hub.direct_message(
                            session["token_hash"],
                            recipient["token_hash"],
                            saved,
                            client_id,
                        )
                    else:
                        await hub.broadcast(
                            {
                                "type": "message.created",
                                "message": saved,
                                "clientId": client_id,
                            }
                        )
                    continue

                if event_type == "chat.sendAttachment":
                    media_id = valid_media_id(event.get("mediaId"))
                    if not media_id:
                        await socket.send_json(
                            {
                                "type": "error",
                                "message": "Choose a file to send.",
                                "clientId": client_id,
                            }
                        )
                        continue
                    message_id = message_id_for(
                        session["token_hash"], event_type, client_id
                    )
                    saved = await store.add_attachment_message(
                        session["token_hash"], media_id, message_id, recipient
                    )
                    if not saved:
                        await socket.send_json(
                            {
                                "type": "error",
                                "message": "That file is no longer ready. Choose it again.",
                                "clientId": client_id,
                            }
                        )
                        continue
                    if recipient:
                        await hub.direct_message(
                            session["token_hash"],
                            recipient["token_hash"],
                            saved,
                            client_id,
                        )
                    else:
                        await hub.broadcast(
                            {
                                "type": "message.created",
                                "message": saved,
                                "clientId": client_id,
                            }
                        )
                    continue

                await socket.send_json(
                    {"type": "error", "message": "That action is not available."}
                )
            elif message.type == WSMsgType.ERROR:
                logger.info("A chat connection ended with a transport error")
                break
    finally:
        await hub.remove(socket)
    return socket


async def clean_once(app: web.Application) -> None:
    store: Store = app[STORE_KEY]
    hub: Hub = app[HUB_KEY]
    expired_tokens = await store.expired_sessions()
    for token_hash in expired_tokens:
        await hub.close_session(token_hash)
    for media in await store.expired_media():
        path = Path(media["file_path"])
        try:
            await asyncio.to_thread(path.unlink, missing_ok=True)
        except OSError:
            logger.exception("An expired attachment could not be removed")
            continue
        await store.forget_media(media["media_id"])


async def cleanup_loop(app: web.Application) -> None:
    while True:
        await asyncio.sleep(30)
        try:
            await clean_once(app)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Temporary data cleanup failed")


async def on_startup(app: web.Application) -> None:
    await app[STORE_KEY].initialize()
    await clean_once(app)
    app[CLEANUP_TASK_KEY] = asyncio.create_task(cleanup_loop(app))


async def on_cleanup(app: web.Application) -> None:
    task = app.get(CLEANUP_TASK_KEY)
    if task:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def create_app() -> web.Application:
    app = web.Application(
        client_max_size=MAX_REQUEST_BYTES,
        middlewares=[security_headers],
    )
    app[STORE_KEY] = Store(DATA_DIR)
    app[HUB_KEY] = Hub()
    app[COOKIE_SECURE_KEY] = COOKIE_SECURE
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    app.router.add_get("/", index)
    app.router.add_get("/assets/styles.css", style_file)
    app.router.add_get("/assets/app.js", script_file)
    app.router.add_get("/api/me", api_me)
    app.router.add_get("/api/history", api_history)
    app.router.add_get("/health", health)
    app.router.add_post("/api/join", api_join)
    app.router.add_post("/api/leave", api_leave)
    app.router.add_post("/api/upload", api_upload)
    app.router.add_get("/media/{media_id}", media_file)
    app.router.add_get("/ws", websocket_chat)
    return app


if __name__ == "__main__":
    logger.info("AJoeChat is ready at http://%s:%s", HOST, PORT)
    web.run_app(create_app(), host=HOST, port=PORT, access_log=None)
