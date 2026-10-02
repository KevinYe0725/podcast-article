from __future__ import annotations

import io
import struct
import zlib
from pathlib import Path

from PIL import Image
from PIL import ImageDraw
from podcast_article.workspace import data_root, workspace_for


MAX_AVATAR_BYTES = 5 * 1024 * 1024
MULTIPART_MARGIN_BYTES = 64 * 1024


def _image_bytes(*, fmt: str = "JPEG", size: tuple[int, int] = (640, 320), color=(220, 80, 60)) -> bytes:
    image = Image.new("RGB", size, color=color)
    output = io.BytesIO()
    image.save(output, format=fmt)
    return output.getvalue()


def _striped_image(size: tuple[int, int]) -> Image.Image:
    image = Image.new("RGB", size, color="red")
    ImageDraw.Draw(image).rectangle((size[0] // 2, 0, size[0] - 1, size[1] - 1), fill="blue")
    return image


def _assert_red(pixel: tuple[int, ...]) -> None:
    assert pixel[0] > 170 and pixel[1] < 110 and pixel[2] < 110


def _assert_blue(pixel: tuple[int, ...]) -> None:
    assert pixel[2] > 150 and pixel[0] < 110 and pixel[1] < 130


def _oversized_dimension_png(width: int = 100_000, height: int = 100_000) -> bytes:
    """A tiny PNG header with hostile dimensions, without allocating the pixels."""
    signature = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    def chunk(kind: bytes, payload: bytes) -> bytes:
        return (struct.pack(">I", len(payload)) + kind + payload +
                struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF))

    # The IDAT only needs to make Pillow recognize this as a PNG.  The
    # application rejects the dimensions before attempting to decode pixels.
    return signature + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(b"\x00\x00\x00\x00")) + chunk(b"IEND", b"")


def _upload(client, payload: bytes, filename: str = "avatar.jpg"):
    return client.post(
        "/api/auth/avatar",
        data={"avatar": (io.BytesIO(payload), filename)},
        content_type="multipart/form-data",
    )


def test_avatar_upload_is_normalized_to_private_png_and_profile_persists(client, auth_system):
    _, _, admin, _ = auth_system
    source = _striped_image((640, 320))
    payload = io.BytesIO()
    source.save(payload, format="PNG")
    response = _upload(client, payload.getvalue(), "avatar.png")

    assert response.status_code == 200
    body = response.get_json()
    assert body["avatar_url"].startswith("/api/auth/avatar?v=")

    avatar_path = workspace_for(admin.id, data_root()).root / "avatar.png"
    assert avatar_path.exists()
    assert avatar_path.stat().st_mode & 0o777 == 0o600

    image = Image.open(avatar_path)
    assert image.format == "PNG"
    assert image.size == (512, 512)
    assert image.info == {}
    _assert_red(image.getpixel((32, 256)))
    _assert_blue(image.getpixel((480, 256)))

    fetched = client.get("/api/auth/avatar")
    assert fetched.get_data() == avatar_path.read_bytes()
    assert fetched.headers["Cache-Control"] == "no-store"
    assert fetched.headers["X-Content-Type-Options"] == "nosniff"

    profile = client.get("/api/auth/me").get_json()
    assert profile["avatar_url"] == body["avatar_url"]


def test_avatar_upload_applies_exif_orientation_and_reset_removes_profile(client, auth_system):
    image = _striped_image((400, 200))
    exif = image.getexif()
    exif[274] = 6
    payload = io.BytesIO()
    image.save(payload, format="JPEG", exif=exif.tobytes())

    response = _upload(client, payload.getvalue(), "rotated.jpeg")
    assert response.status_code == 200
    rendered = Image.open(workspace_for(auth_system[2].id, data_root()).root / "avatar.png")
    assert rendered.size == (512, 512)
    assert rendered.getexif() == {}
    _assert_red(rendered.getpixel((256, 64)))
    _assert_blue(rendered.getpixel((256, 448)))

    deleted = client.delete("/api/auth/avatar")
    assert deleted.status_code == 200
    assert deleted.get_json() == {"avatar_url": None}
    assert client.get("/api/auth/avatar").status_code == 404
    assert client.get("/api/auth/me").get_json()["avatar_url"] is None


def test_invalid_and_oversized_avatar_preserve_existing_avatar(client):
    created = _upload(client, _image_bytes(fmt="PNG"), "avatar.png")
    assert created.status_code == 200
    original = client.get("/api/auth/avatar").get_data()

    invalid = _upload(client, b"this is not an image", "avatar.webp")
    assert invalid.status_code == 400
    assert invalid.get_json()["error"] == "invalid_avatar"
    assert client.get("/api/auth/avatar").get_data() == original

    oversized = _upload(client, b"x" * (MAX_AVATAR_BYTES + 1), "avatar.jpg")
    assert oversized.status_code == 413
    assert oversized.get_json()["error"] == "avatar_too_large"
    assert client.get("/api/auth/avatar").get_data() == original

    parser_oversized = _upload(
        client,
        b"x" * (MAX_AVATAR_BYTES + MULTIPART_MARGIN_BYTES + 1),
        "avatar.jpg",
    )
    assert parser_oversized.status_code == 413
    assert parser_oversized.get_json() == {
        "error": "avatar_too_large",
        "message": "头像图片不能超过 5 MB",
    }
    assert client.get("/api/auth/avatar").get_data() == original


def test_avatar_rejects_pixel_bomb_without_decoding_huge_image(client):
    response = _upload(client, _oversized_dimension_png())

    assert response.status_code == 413
    assert response.get_json()["error"] == "avatar_too_large"


def test_avatar_storage_is_isolated_per_account(two_user_clients):
    alice = two_user_clients["alice"]
    bob = two_user_clients["bob"]

    assert alice["client"].get("/api/auth/avatar").status_code == 404
    assert bob["client"].get("/api/auth/avatar").status_code == 404

    assert _upload(alice["client"], _image_bytes(color=(255, 0, 0))).status_code == 200
    assert _upload(bob["client"], _image_bytes(color=(0, 0, 255))).status_code == 200
    assert alice["client"].get("/api/auth/avatar").get_data() != bob["client"].get("/api/auth/avatar").get_data()
    assert alice["client"].get("/api/auth/me").get_json()["avatar_url"]
    assert bob["client"].get("/api/auth/me").get_json()["avatar_url"]


def test_avatar_write_requires_csrf_and_read_requires_auth(client):
    client.auto_csrf = False
    response = client.post(
        "/api/auth/avatar",
        data={"avatar": (io.BytesIO(_image_bytes()), "avatar.jpg")},
        content_type="multipart/form-data",
    )
    assert response.status_code == 403
    assert response.get_json()["error"] == "csrf_failed"

    client.auto_csrf = True
    assert client.post("/api/auth/logout").status_code == 200
    assert client.get("/api/auth/avatar").status_code == 401
