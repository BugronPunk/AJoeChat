from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
import zipfile
from html.parser import HTMLParser
from io import BytesIO
from pathlib import Path

from aiohttp.web_request import FileField
from multidict import CIMultiDict, CIMultiDictProxy
from PIL import Image

import main
from store import Store


def image_bytes(image_format: str) -> bytes:
    output = BytesIO()
    Image.new("RGB", (3, 2), (48, 112, 78)).save(output, format=image_format)
    return output.getvalue()


def mp3_bytes() -> bytes:
    header = b"\xff\xfb\x90\x64"
    frame_length = 144 * 128_000 // 44_100
    return header + bytes(frame_length - len(header))


def mp4_bytes() -> bytes:
    def box(name: bytes, content: bytes) -> bytes:
        return (len(content) + 8).to_bytes(4, "big") + name + content

    file_type = box(b"ftyp", b"isom\x00\x00\x02\x00isom")
    movie = box(b"moov", bytes(8))
    media = box(b"mdat", b"\x00")
    return file_type + movie + media


def pdf_bytes() -> bytes:
    header = b"%PDF-1.7\n"
    catalog = b"1 0 obj\n<< /Type /Catalog >>\nendobj\n"
    object_offset = len(header)
    xref_offset = object_offset + len(catalog)
    xref = (
        b"xref\n0 2\n0000000000 65535 f \n"
        + f"{object_offset:010d} 00000 n \n".encode()
    )
    trailer = (
        b"trailer\n<< /Size 2 /Root 1 0 R >>\nstartxref\n"
        + f"{xref_offset}\n".encode()
        + b"%%EOF\n"
    )
    return header + catalog + xref + trailer


def docx_bytes() -> bytes:
    output = BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr(
            "[Content_Types].xml",
            '<Types><Override ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>',
        )
        archive.writestr("word/document.xml", "<document/>")
    return output.getvalue()


class UploadRequest:
    def __init__(self, store: Store, token: str, filename: str, content: bytes) -> None:
        headers = CIMultiDict({"Origin": "http://chat.example"})
        self.headers = headers
        self.host = "chat.example"
        self.cookies = {main.COOKIE_NAME: token}
        self.app = {main.STORE_KEY: store, main.COOKIE_SECURE_KEY: False}
        file_headers = CIMultiDictProxy(CIMultiDict())
        self.upload = FileField(
            name="file",
            filename=filename,
            file=BytesIO(content),
            content_type="application/octet-stream",
            headers=file_headers,
        )

    async def post(self):
        return {"file": self.upload}


class UploadValidationTests(unittest.TestCase):
    def test_supported_formats_are_checked_and_classified(self) -> None:
        fixtures = [
            ("photo.jpg", image_bytes("JPEG"), "image/jpeg", ".jpg", "image"),
            ("photo.png", image_bytes("PNG"), "image/png", ".png", "image"),
            ("photo.webp", image_bytes("WEBP"), "image/webp", ".webp", "image"),
            (
                "clip.mp4",
                mp4_bytes(),
                "video/mp4",
                ".mp4",
                "video",
            ),
            (
                "audio.mp3",
                mp3_bytes(),
                "audio/mpeg",
                ".mp3",
                "audio",
            ),
            (
                "note.txt",
                "Hello 🌿".encode(),
                "text/plain; charset=utf-8",
                ".txt",
                "text",
            ),
            (
                "report.pdf",
                pdf_bytes(),
                "application/pdf",
                ".pdf",
                "pdf",
            ),
            (
                "report.docx",
                docx_bytes(),
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                ".docx",
                "document",
            ),
        ]
        for filename, content, media_type, extension, category in fixtures:
            with self.subTest(filename=filename):
                result = main.sanitize_upload(content, filename)
                self.assertEqual(result[1:], (media_type, extension, category))
                self.assertTrue(result[0])

    def test_invalid_content_and_unsupported_suffixes_are_rejected(self) -> None:
        invalid_files = [
            (b"not a video", "clip.mp4"),
            (b"not audio", "track.mp3"),
            (b"\xff\xfb\x90\x64" + bytes(24), "short.mp3"),
            (b"\x00\x00\x00\x14ftypisommoovmdat", "broken.mp4"),
            (b"\xff", "note.txt"),
            (b"%PDF-1.7\ncontent\n%%EOF\n", "report.pdf"),
            (b"not a Word document", "report.docx"),
            (b"anything", "script.html"),
        ]
        for content, filename in invalid_files:
            with self.subTest(filename=filename):
                with self.assertRaises(ValueError):
                    main.sanitize_upload(content, filename)

    def test_names_cannot_escape_the_upload_directory(self) -> None:
        self.assertEqual(main.safe_upload_name("../../notes.txt"), "notes.txt")
        self.assertEqual(main.safe_upload_name("folder\\clip.mp4"), "clip.mp4")
        self.assertEqual(main.safe_upload_name("  "), "Shared file")

    def test_message_limit_counts_unicode_code_points(self) -> None:
        self.assertTrue(main.valid_text("🌿" * 3000))
        self.assertFalse(main.valid_text("🌿" * 3001))


class ChatStoreTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.store = Store(self.root)
        await self.store.initialize()
        self.alice_token = "a" * 64
        self.bob_token = "b" * 64
        self.cara_token = "c" * 64
        self.assertTrue(await self.store.join(self.alice_token, "Alice", "alice"))
        self.assertTrue(await self.store.join(self.bob_token, "Bobby", "bobby"))
        self.assertTrue(await self.store.join(self.cara_token, "Cara", "cara"))

    async def test_usernames_are_reserved_without_regard_to_case(self) -> None:
        self.assertFalse(await self.store.join("d" * 64, "ALICE", "alice"))

    async def test_text_and_attachment_history_obey_room_privacy(self) -> None:
        public = await self.store.add_text_message(
            self.alice_token, "Hello room", "1" * 32
        )
        private = await self.store.add_text_message(
            self.alice_token,
            "For Bobby",
            "2" * 32,
            {"token_hash": self.bob_token, "username_key": "bobby"},
        )
        self.assertEqual(public["kind"], "text")
        self.assertEqual(private["recipientName"], "Bobby")

        upload_path = self.root / "attachments" / "note.txt"
        upload_path.write_text("A shared note", encoding="utf-8")
        await self.store.add_media(
            "1" * 32,
            self.alice_token,
            upload_path,
            "text/plain; charset=utf-8",
            upload_path.stat().st_size,
            "note.txt",
            "text",
        )
        public_file = await self.store.add_attachment_message(
            self.alice_token, "1" * 32, "3" * 32
        )
        self.assertEqual(public_file["fileName"], "note.txt")
        self.assertEqual(public_file["fileCategory"], "text")

        upload_path2 = self.root / "attachments" / "private.pdf"
        upload_path2.write_bytes(b"%PDF-1.7\ncontent\n%%EOF\n")
        await self.store.add_media(
            "2" * 32,
            self.alice_token,
            upload_path2,
            "application/pdf",
            upload_path2.stat().st_size,
            "private.pdf",
            "pdf",
        )
        private_file = await self.store.add_attachment_message(
            self.alice_token,
            "2" * 32,
            "4" * 32,
            {"token_hash": self.bob_token, "username_key": "bobby"},
        )
        self.assertEqual(private_file["kind"], "file")
        self.assertEqual(private_file["recipientName"], "Bobby")

        public_history = await self.store.public_history_page()
        self.assertEqual(
            [message["id"] for message in public_history["messages"]],
            ["1" * 32, "3" * 32],
        )
        alice_history = await self.store.private_history_page(self.alice_token, "bobby")
        bob_history = await self.store.private_history_page(self.bob_token, "alice")
        cara_history = await self.store.private_history_page(self.cara_token, "alice")
        self.assertEqual(
            [item["id"] for item in alice_history["messages"]], ["2" * 32, "4" * 32]
        )
        self.assertEqual(
            [item["id"] for item in bob_history["messages"]], ["2" * 32, "4" * 32]
        )
        self.assertEqual(cara_history["messages"], [])
        self.assertIsNotNone(await self.store.media("2" * 32, self.alice_token))
        self.assertIsNotNone(await self.store.media("2" * 32, self.bob_token))
        self.assertIsNone(await self.store.media("2" * 32, self.cara_token))


class UploadApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_uploaded_files_are_served_with_a_download_name(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory))
            await store.initialize()
            token = "f" * 64
            await store.join(main.token_digest(token), "Frank", "frank")
            request = UploadRequest(store, token, "quarterly report.pdf", pdf_bytes())
            upload = await main.api_upload(request)
            self.assertEqual(upload.status, 200)
            media_id = json.loads(upload.body)["mediaId"]

            request.match_info = {"media_id": media_id}
            download = await main.media_file(request)

            self.assertEqual(download.status, 200)
            self.assertEqual(download.headers["Content-Type"], "application/pdf")
            self.assertIn(
                "quarterly%20report.pdf", download.headers["Content-Disposition"]
            )

    async def test_five_megabyte_limit_is_enforced_at_the_endpoint(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory))
            await store.initialize()
            token = "e" * 64
            token_hash = main.token_digest(token)
            await store.join(token_hash, "Elena", "elena")

            accepted = await main.api_upload(
                UploadRequest(store, token, "exact.txt", b"a" * main.MAX_UPLOAD_BYTES)
            )
            self.assertEqual(accepted.status, 200)
            saved = json.loads(accepted.body)
            self.assertEqual(saved["fileSize"], main.MAX_UPLOAD_BYTES)

            rejected = await main.api_upload(
                UploadRequest(
                    store, token, "large.txt", b"a" * (main.MAX_UPLOAD_BYTES + 1)
                )
            )
            self.assertEqual(rejected.status, 413)


class FakeSocket:
    def __init__(self) -> None:
        self.events: list[dict] = []

    async def send_json(self, event: dict) -> None:
        self.events.append(event)


class DirectDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_private_message_reaches_only_the_two_people(self) -> None:
        hub = main.Hub()
        alice = FakeSocket()
        bobby = FakeSocket()
        cara = FakeSocket()
        hub.members = {
            alice: {"token_hash": "alice", "username": "Alice", "joined_at": 1},
            bobby: {"token_hash": "bobby", "username": "Bobby", "joined_at": 2},
            cara: {"token_hash": "cara", "username": "Cara", "joined_at": 3},
        }

        await hub.direct_message("alice", "bobby", {"id": "message"}, "event")

        self.assertEqual([item["type"] for item in alice.events], ["message.created"])
        self.assertEqual([item["type"] for item in bobby.events], ["message.created"])
        self.assertEqual(cara.events, [])
        self.assertEqual(alice.events[0]["clientId"], "event")
        self.assertNotIn("clientId", bobby.events[0])


class FrontendWiringTests(unittest.TestCase):
    def test_browser_selectors_exist_and_shared_room_has_no_chevron(self) -> None:
        class IdReader(HTMLParser):
            def __init__(self) -> None:
                super().__init__()
                self.ids: list[str] = []

            def handle_starttag(self, tag, attrs):
                self.ids.extend(value for name, value in attrs if name == "id")

        root = Path(__file__).resolve().parents[1]
        markup = (root / "static" / "index.html").read_text(encoding="utf-8")
        script = (root / "static" / "app.js").read_text(encoding="utf-8")
        styles = (root / "static" / "styles.css").read_text(encoding="utf-8")
        reader = IdReader()
        reader.feed(markup)
        self.assertEqual(len(reader.ids), len(set(reader.ids)))
        references = set(re.findall(r'byId\("([^\"]+)"\)', script))
        self.assertFalse(references - set(reader.ids))
        self.assertIn("fileInput", reader.ids)
        self.assertIn("grid-template-columns: 34px minmax(0, 1fr)", styles)
        self.assertIn("grid-template-columns: 29px minmax(0, 1fr) 12px", styles)
        self.assertNotIn("topRoomDot", markup + styles)
        self.assertNotIn("roomChevron", markup + styles)
        self.assertIn(".introEyebrow > span:first-child", styles)
        self.assertIn(".introEyebrow > span:last-child", styles)
        self.assertNotIn(".introEyebrow span {", styles)
        self.assertIn('d="M7 4.5h10a3 3 0 0 1 3 3v5', markup)
        self.assertIn("chat.sendAttachment", script)
        self.assertIn("toastRegion.replaceChildren()", script)
        self.assertIn("Close notification", script)
        self.assertIn("context.textContent = message.recipientName", script)
        self.assertIn('"PRIVATE NOTE"', script)
        self.assertIn('"SHARED ROOM"', script)
        self.assertIn('aria-controls="sidePanel"', markup)
        self.assertIn('id="panelClose"', markup)
        self.assertIn("syncPanelAccessibility", script)
        self.assertIn("Please wait before switching chats", script)
        self.assertIn("if (!state.isSending) toastRegion.replaceChildren()", script)
        self.assertIn("font-size: 14px", styles)
        self.assertIn(
            "PORT` to `6772`", (root / "README.md").read_text(encoding="utf-8")
        )
        self.assertIn("fresh state", (root / "README.md").read_text(encoding="utf-8"))
        punctuation = markup + script + styles
        self.assertFalse(
            any(symbol in punctuation for symbol in (chr(0x2014), chr(0x2013)))
        )


class DeploymentConfigurationTests(unittest.TestCase):
    def test_local_default_and_railway_startup_use_one_service(self) -> None:
        root = Path(__file__).resolve().parents[1]
        environment = dict(os.environ)
        environment.pop("PORT", None)
        environment.pop("AJOECHAT_PORT", None)
        result = subprocess.run(
            [sys.executable, "-c", "import main; print(main.PORT)"],
            cwd=root,
            env=environment,
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertEqual(result.stdout.strip(), "6772")
        configuration = json.loads((root / "railway.json").read_text(encoding="utf-8"))
        self.assertEqual(configuration["deploy"]["startCommand"], "python3 main.py")
        self.assertEqual(configuration["deploy"]["healthcheckPath"], "/health")
        app = main.create_app()
        paths = {route.resource.canonical for route in app.router.routes()}
        required_paths = {
            "/",
            "/assets/styles.css",
            "/api/upload",
            "/media/{media_id}",
            "/ws",
        }
        self.assertTrue(required_paths.issubset(paths))


if __name__ == "__main__":
    unittest.main()
