import hashlib
import io
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Union

import requests
from PIL import Image
from pydantic import ValidationError

from product import ProductImage
from product_image_downloads import (
    MAX_IMAGE_BYTES,
    CorruptImageError,
    DownloadedProductImage,
    ImageContentTypeError,
    ImageFilesystemError,
    ImageHttpError,
    ImageIntegrityError,
    ImageResponseTooLargeError,
    ImageTimeoutError,
    InvalidImageUrlError,
    UnsupportedImageFormatError,
    UnsupportedProductImageSourceError,
    download_product_image,
    verify_downloaded_product_image,
)


def encoded_image(
    image_format: str,
    size: tuple = (7, 5),
    color: tuple = (20, 100, 180),
) -> bytes:
    output = io.BytesIO()
    mode = "RGBA" if image_format == "PNG" else "RGB"
    fill = (*color, 128) if mode == "RGBA" else color
    Image.new(mode, size, fill).save(output, format=image_format)
    return output.getvalue()


class FakeResponse:
    def __init__(
        self,
        data: bytes = b"",
        *,
        content_type: str = "image/jpeg",
        status_code: int = 200,
        url: str = "https://cdn.example/final.jpg",
        chunks: Optional[List[bytes]] = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> None:
        self.data = data
        self.status_code = status_code
        self.url = url
        self.headers = {"Content-Type": content_type, **(headers or {})}
        self.chunks = chunks
        self.chunks_yielded = 0
        self.closed = False

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(response=self)

    def iter_content(self, chunk_size: int = 65536) -> Iterable[bytes]:
        chunks = self.chunks
        if chunks is None:
            chunks = [
                self.data[index : index + chunk_size]
                for index in range(0, len(self.data), chunk_size)
            ]
        for chunk in chunks:
            self.chunks_yielded += 1
            yield chunk

    def close(self) -> None:
        self.closed = True


class FakeSession:
    def __init__(
        self,
        result: Union[FakeResponse, Exception],
    ) -> None:
        self.result = result
        self.calls = []

    def get(self, url: str, **kwargs: object) -> FakeResponse:
        self.calls.append((url, kwargs))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class StreamingTimeoutResponse(FakeResponse):
    def iter_content(self, chunk_size: int = 65536) -> Iterable[bytes]:
        yield b"partial"
        raise requests.Timeout("stream stalled")


def discovered_image(
    *,
    image_id: str = "image-source",
    url: str = "https://cdn.example/original.jpg",
    source_type: str = "open_graph_product_image",
    width: Optional[int] = 800,
    height: Optional[int] = 600,
) -> ProductImage:
    return ProductImage(
        id=image_id,
        url=url,
        source_url="https://shop.example/products/speaker",
        source_type=source_type,
        width=width,
        height=height,
        confidence=0.80,
    )


def downloaded_asset(
    path: Path,
    data: bytes,
    *,
    width: int = 7,
    height: int = 5,
    image_format: str = "PNG",
    mime_type: str = "image/png",
) -> DownloadedProductImage:
    return DownloadedProductImage(
        product_id="example-product",
        source_image_id="image-source",
        source_url="https://cdn.example/source.png",
        final_url="https://cdn.example/final.png",
        local_path=str(path),
        sha256=hashlib.sha256(data).hexdigest(),
        byte_size=len(data),
        width=width,
        height=height,
        format=image_format,
        mime_type=mime_type,
    )


class DownloadedProductImageModelTests(unittest.TestCase):
    def test_model_round_trips_as_strict_json_with_utc_timestamp(self) -> None:
        asset = DownloadedProductImage(
            product_id="shop-example-product-products-speaker",
            source_image_id="image-source",
            source_url="https://cdn.example/original.jpg",
            final_url="https://cdn.example/final.jpg",
            local_path="products/speaker/assets/originals/hash.jpg",
            sha256="a" * 64,
            byte_size=123,
            width=7,
            height=5,
            format="JPEG",
            mime_type="image/jpeg",
        )

        restored = DownloadedProductImage.model_validate_json(
            asset.model_dump_json()
        )

        self.assertEqual(restored, asset)
        self.assertEqual(restored.downloaded_at.tzinfo, timezone.utc)
        invalid = json.loads(asset.model_dump_json())
        invalid["licensed"] = True
        with self.assertRaises(ValidationError):
            DownloadedProductImage.model_validate(invalid)

    def test_model_rejects_invalid_hash_and_naive_timestamp(self) -> None:
        data = {
            "product_id": "speaker",
            "source_image_id": "image-source",
            "source_url": "https://cdn.example/original.jpg",
            "final_url": "https://cdn.example/final.jpg",
            "local_path": "products/speaker/image.jpg",
            "sha256": "not-a-hash",
            "byte_size": 1,
            "width": 1,
            "height": 1,
            "format": "JPEG",
            "mime_type": "image/jpeg",
            "downloaded_at": datetime(2026, 8, 10, 12, 0),
        }

        with self.assertRaises(ValidationError):
            DownloadedProductImage.model_validate(data)


class ProductImageDownloadTests(unittest.TestCase):
    def download(
        self,
        image_format: str,
        content_type: str,
        *,
        discovered: Optional[ProductImage] = None,
        response_url: Optional[str] = None,
    ) -> DownloadedProductImage:
        data = encoded_image(image_format)
        response = FakeResponse(
            data,
            content_type=content_type,
            url=response_url or "https://cdn.example/final-image",
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            asset = download_product_image(
                "shop-example-product-products-speaker",
                discovered or discovered_image(),
                storage_root=temporary_directory,
                session=FakeSession(response),
            )
            self.assertTrue(Path(asset.local_path).is_file())
            self.assertEqual(Path(asset.local_path).read_bytes(), data)
            return asset

    def test_downloads_and_verifies_jpeg_original(self) -> None:
        data = encoded_image("JPEG", size=(11, 9))
        response = FakeResponse(data, content_type="image/jpeg")

        with tempfile.TemporaryDirectory() as temporary_directory:
            asset = download_product_image(
                "shop-example-product-products-speaker",
                discovered_image(width=800, height=600),
                storage_root=temporary_directory,
                session=FakeSession(response),
            )

            self.assertEqual(asset.format, "JPEG")
            self.assertEqual(asset.mime_type, "image/jpeg")
            self.assertEqual((asset.width, asset.height), (11, 9))
            self.assertEqual(asset.byte_size, len(data))
            self.assertEqual(asset.sha256, hashlib.sha256(data).hexdigest())
            self.assertEqual(Path(asset.local_path).read_bytes(), data)
            self.assertTrue(asset.local_path.endswith(f"{asset.sha256}.jpg"))
            self.assertTrue(response.closed)

    def test_downloads_and_verifies_png_original(self) -> None:
        asset = self.download("PNG", "image/png")

        self.assertEqual(asset.format, "PNG")
        self.assertEqual(asset.mime_type, "image/png")
        self.assertTrue(asset.local_path.endswith(".png"))

    def test_downloads_and_verifies_webp_original(self) -> None:
        asset = self.download("WEBP", "image/webp")

        self.assertEqual(asset.format, "WEBP")
        self.assertEqual(asset.mime_type, "image/webp")
        self.assertTrue(asset.local_path.endswith(".webp"))

    def test_preserves_source_and_final_redirect_provenance(self) -> None:
        source = discovered_image(
            image_id="image-original-reference",
            url="https://cdn.example/redirecting-image",
        )
        final_url = "https://media.example/final.png?version=2"

        asset = self.download(
            "PNG",
            "image/png",
            discovered=source,
            response_url=final_url,
        )

        self.assertEqual(asset.source_image_id, source.id)
        self.assertEqual(asset.source_url, source.url)
        self.assertEqual(asset.final_url, final_url)

    def test_actual_dimensions_override_discovered_metadata(self) -> None:
        asset = self.download(
            "PNG",
            "image/png",
            discovered=discovered_image(width=800, height=600),
        )

        self.assertEqual((asset.width, asset.height), (7, 5))

    def test_request_uses_streaming_timeout_redirects_and_headers(self) -> None:
        response = FakeResponse(encoded_image("PNG"), content_type="image/png")
        session = FakeSession(response)

        with tempfile.TemporaryDirectory() as temporary_directory:
            download_product_image(
                "speaker",
                discovered_image(),
                storage_root=temporary_directory,
                timeout=9,
                session=session,
            )

        _, kwargs = session.calls[0]
        self.assertEqual(kwargs["timeout"], 9)
        self.assertTrue(kwargs["allow_redirects"])
        self.assertTrue(kwargs["stream"])
        self.assertIn("User-Agent", kwargs["headers"])

    def test_non_success_response_fails_clearly(self) -> None:
        response = FakeResponse(status_code=404)

        with tempfile.TemporaryDirectory() as temporary_directory:
            with self.assertRaisesRegex(ImageHttpError, "404"):
                download_product_image(
                    "speaker",
                    discovered_image(),
                    storage_root=temporary_directory,
                    session=FakeSession(response),
                )
        self.assertTrue(response.closed)

    def test_timeout_fails_clearly(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            with self.assertRaises(ImageTimeoutError):
                download_product_image(
                    "speaker",
                    discovered_image(),
                    storage_root=temporary_directory,
                    session=FakeSession(requests.Timeout()),
                )

    def test_timeout_during_streaming_is_wrapped_and_response_closes(self) -> None:
        response = StreamingTimeoutResponse(content_type="image/png")

        with tempfile.TemporaryDirectory() as temporary_directory:
            with self.assertRaises(ImageTimeoutError):
                download_product_image(
                    "speaker",
                    discovered_image(),
                    storage_root=temporary_directory,
                    session=FakeSession(response),
                )
        self.assertTrue(response.closed)

    def test_non_image_content_type_is_rejected_before_streaming(self) -> None:
        response = FakeResponse(
            encoded_image("PNG"),
            content_type="text/html; charset=utf-8",
        )

        with tempfile.TemporaryDirectory() as temporary_directory:
            with self.assertRaises(ImageContentTypeError):
                download_product_image(
                    "speaker",
                    discovered_image(),
                    storage_root=temporary_directory,
                    session=FakeSession(response),
                )
        self.assertEqual(response.chunks_yielded, 0)

    def test_corrupt_image_bytes_are_rejected(self) -> None:
        response = FakeResponse(b"not an image", content_type="image/png")

        with tempfile.TemporaryDirectory() as temporary_directory:
            with self.assertRaises(CorruptImageError):
                download_product_image(
                    "speaker",
                    discovered_image(),
                    storage_root=temporary_directory,
                    session=FakeSession(response),
                )
            self.assertEqual(list(Path(temporary_directory).rglob("*")), [])

    def test_decodable_unsupported_format_is_rejected(self) -> None:
        response = FakeResponse(
            encoded_image("GIF"),
            content_type="image/gif",
        )

        with tempfile.TemporaryDirectory() as temporary_directory:
            with self.assertRaisesRegex(UnsupportedImageFormatError, "GIF"):
                download_product_image(
                    "speaker",
                    discovered_image(),
                    storage_root=temporary_directory,
                    session=FakeSession(response),
                )

    def test_content_length_over_20_mb_is_rejected_before_streaming(self) -> None:
        response = FakeResponse(
            headers={"Content-Length": str(MAX_IMAGE_BYTES + 1)},
        )

        with tempfile.TemporaryDirectory() as temporary_directory:
            with self.assertRaises(ImageResponseTooLargeError):
                download_product_image(
                    "speaker",
                    discovered_image(),
                    storage_root=temporary_directory,
                    session=FakeSession(response),
                )
        self.assertEqual(MAX_IMAGE_BYTES, 20 * 1024 * 1024)
        self.assertEqual(response.chunks_yielded, 0)

    def test_stream_stops_immediately_when_size_limit_is_crossed(self) -> None:
        response = FakeResponse(
            content_type="image/png",
            chunks=[b"12345678", b"abcdefgh", b"must-not-be-read"],
        )

        with tempfile.TemporaryDirectory() as temporary_directory:
            with self.assertRaises(ImageResponseTooLargeError):
                download_product_image(
                    "speaker",
                    discovered_image(),
                    storage_root=temporary_directory,
                    session=FakeSession(response),
                    max_bytes=10,
                )
        self.assertEqual(response.chunks_yielded, 2)

    def test_caller_cannot_raise_the_absolute_20_mb_limit(self) -> None:
        session = FakeSession(FakeResponse())

        with tempfile.TemporaryDirectory() as temporary_directory:
            with self.assertRaisesRegex(ValueError, "20 MB"):
                download_product_image(
                    "speaker",
                    discovered_image(),
                    storage_root=temporary_directory,
                    session=session,
                    max_bytes=MAX_IMAGE_BYTES + 1,
                )
        self.assertEqual(session.calls, [])

    def test_invalid_or_credentialed_urls_are_rejected_before_request(self) -> None:
        for url in (
            "file:///tmp/image.png",
            "https://user:password@cdn.example/image.png",
            "not-a-url",
        ):
            with self.subTest(url=url):
                session = FakeSession(FakeResponse())
                with tempfile.TemporaryDirectory() as temporary_directory:
                    with self.assertRaises(InvalidImageUrlError):
                        download_product_image(
                            "speaker",
                            discovered_image(url=url),
                            storage_root=temporary_directory,
                            session=session,
                        )
                self.assertEqual(session.calls, [])

    def test_only_currently_accepted_discovery_sources_can_download(self) -> None:
        session = FakeSession(FakeResponse())

        with tempfile.TemporaryDirectory() as temporary_directory:
            with self.assertRaises(UnsupportedProductImageSourceError):
                download_product_image(
                    "speaker",
                    discovered_image(source_type="arbitrary_html_image"),
                    storage_root=temporary_directory,
                    session=session,
                )
        self.assertEqual(session.calls, [])

    def test_missing_source_url_is_rejected(self) -> None:
        source = ProductImage(
            id="image-without-url",
            source_type="json_ld_product_image",
        )

        with tempfile.TemporaryDirectory() as temporary_directory:
            with self.assertRaises(InvalidImageUrlError):
                download_product_image(
                    "speaker",
                    source,
                    storage_root=temporary_directory,
                    session=FakeSession(FakeResponse()),
                )

    def test_exact_duplicates_share_canonical_binary_but_keep_source_links(self) -> None:
        data = encoded_image("PNG")
        first_source = discovered_image(
            image_id="first-source",
            url="https://cdn.example/first.png",
        )
        second_source = discovered_image(
            image_id="second-source",
            url="https://media.example/second.png",
        )

        with tempfile.TemporaryDirectory() as temporary_directory:
            first = download_product_image(
                "speaker",
                first_source,
                storage_root=temporary_directory,
                session=FakeSession(FakeResponse(data, content_type="image/png")),
            )
            second = download_product_image(
                "speaker",
                second_source,
                storage_root=temporary_directory,
                session=FakeSession(FakeResponse(data, content_type="image/png")),
            )

            self.assertEqual(first.sha256, second.sha256)
            self.assertEqual(first.local_path, second.local_path)
            self.assertNotEqual(first.source_image_id, second.source_image_id)
            self.assertEqual(
                len(list(Path(temporary_directory).rglob("*.png"))),
                1,
            )

    def test_existing_canonical_file_must_still_match_its_hash(self) -> None:
        data = encoded_image("PNG")

        with tempfile.TemporaryDirectory() as temporary_directory:
            first = download_product_image(
                "speaker",
                discovered_image(),
                storage_root=temporary_directory,
                session=FakeSession(FakeResponse(data, content_type="image/png")),
            )
            Path(first.local_path).write_bytes(b"locally corrupted")

            with self.assertRaisesRegex(ImageFilesystemError, "SHA-256"):
                download_product_image(
                    "speaker",
                    discovered_image(),
                    storage_root=temporary_directory,
                    session=FakeSession(
                        FakeResponse(data, content_type="image/png")
                    ),
                )

    def test_canonical_storage_path_cannot_reuse_a_symbolic_link(self) -> None:
        data = encoded_image("PNG")
        sha256 = hashlib.sha256(data).hexdigest()

        with tempfile.TemporaryDirectory() as temporary_directory:
            storage_root = Path(temporary_directory) / "products"
            target_directory = (
                storage_root / "speaker" / "assets" / "originals"
            )
            target_directory.mkdir(parents=True)
            external_file = Path(temporary_directory) / "external.png"
            external_file.write_bytes(data)
            target = target_directory / f"{sha256}.png"
            target.symlink_to(external_file)

            with self.assertRaisesRegex(ImageFilesystemError, "symbolic link"):
                download_product_image(
                    "speaker",
                    discovered_image(),
                    storage_root=storage_root,
                    session=FakeSession(
                        FakeResponse(data, content_type="image/png")
                    ),
                )

    def test_different_bytes_with_same_url_basename_do_not_collide(self) -> None:
        first_data = encoded_image("PNG", color=(1, 2, 3))
        second_data = encoded_image("PNG", color=(4, 5, 6))

        with tempfile.TemporaryDirectory() as temporary_directory:
            first = download_product_image(
                "speaker",
                discovered_image(url="https://one.example/image.png"),
                storage_root=temporary_directory,
                session=FakeSession(
                    FakeResponse(first_data, content_type="image/png")
                ),
            )
            second = download_product_image(
                "speaker",
                discovered_image(url="https://two.example/image.png"),
                storage_root=temporary_directory,
                session=FakeSession(
                    FakeResponse(second_data, content_type="image/png")
                ),
            )

        self.assertNotEqual(first.sha256, second.sha256)
        self.assertNotEqual(first.local_path, second.local_path)

    def test_input_product_image_is_not_mutated(self) -> None:
        source = discovered_image()
        before = source.model_dump(mode="json")

        self.download("PNG", "image/png", discovered=source)

        self.assertEqual(source.model_dump(mode="json"), before)
        self.assertIsNone(source.local_path)

    def test_filesystem_failure_is_wrapped_without_mutating_source(self) -> None:
        source = discovered_image()
        data = encoded_image("PNG")

        with tempfile.TemporaryDirectory() as temporary_directory:
            storage_file = Path(temporary_directory) / "not-a-directory"
            storage_file.write_text("occupied", encoding="utf-8")
            with self.assertRaises(ImageFilesystemError):
                download_product_image(
                    "speaker",
                    source,
                    storage_root=storage_file,
                    session=FakeSession(
                        FakeResponse(data, content_type="image/png")
                    ),
                )
        self.assertIsNone(source.local_path)


class DownloadedProductImageIntegrityTests(unittest.TestCase):
    def test_valid_original_reverifies_and_returns_an_isolated_copy(self) -> None:
        data = encoded_image("PNG")
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "original.png"
            path.write_bytes(data)
            asset = downloaded_asset(path, data)

            verified = verify_downloaded_product_image(asset)

        self.assertEqual(verified, asset)
        self.assertIsNot(verified, asset)

    def test_missing_or_non_file_path_fails_clearly(self) -> None:
        data = encoded_image("PNG")
        with tempfile.TemporaryDirectory() as temporary_directory:
            missing = downloaded_asset(
                Path(temporary_directory) / "missing.png",
                data,
            )
            directory = Path(temporary_directory) / "directory.png"
            directory.mkdir()
            non_file = downloaded_asset(directory, data)

            for asset in (missing, non_file):
                with self.subTest(path=asset.local_path):
                    with self.assertRaisesRegex(
                        ImageIntegrityError,
                        "regular file",
                    ):
                        verify_downloaded_product_image(asset)

    def test_symbolic_link_is_rejected(self) -> None:
        data = encoded_image("PNG")
        with tempfile.TemporaryDirectory() as temporary_directory:
            original = Path(temporary_directory) / "original.png"
            original.write_bytes(data)
            link = Path(temporary_directory) / "link.png"
            link.symlink_to(original)
            asset = downloaded_asset(link, data)

            with self.assertRaisesRegex(ImageIntegrityError, "symbolic link"):
                verify_downloaded_product_image(asset)

    def test_byte_size_mismatch_is_detected_before_decoding(self) -> None:
        data = encoded_image("PNG")
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "original.png"
            path.write_bytes(data + b"changed")
            asset = downloaded_asset(path, data)

            with self.assertRaisesRegex(ImageIntegrityError, "byte size"):
                verify_downloaded_product_image(asset)

    def test_sha256_mismatch_is_detected(self) -> None:
        data = encoded_image("PNG")
        changed = bytes([data[0] ^ 1]) + data[1:]
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "original.png"
            path.write_bytes(changed)
            asset = downloaded_asset(path, data)

            with self.assertRaisesRegex(ImageIntegrityError, "SHA-256"):
                verify_downloaded_product_image(asset)

    def test_decoded_metadata_mismatch_is_detected(self) -> None:
        data = encoded_image("PNG")
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "original.png"
            path.write_bytes(data)
            asset = downloaded_asset(path, data, width=99)

            with self.assertRaisesRegex(ImageIntegrityError, "dimensions"):
                verify_downloaded_product_image(asset)

            for field_name, asset in (
                (
                    "format",
                    downloaded_asset(path, data, image_format="JPEG"),
                ),
                (
                    "MIME type",
                    downloaded_asset(path, data, mime_type="image/jpeg"),
                ),
            ):
                with self.subTest(field_name=field_name):
                    with self.assertRaisesRegex(
                        ImageIntegrityError,
                        field_name,
                    ):
                        verify_downloaded_product_image(asset)

    def test_corrupt_bytes_are_rejected_even_when_hash_and_size_match(self) -> None:
        data = b"not actually an image"
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "original.png"
            path.write_bytes(data)
            asset = downloaded_asset(path, data)

            with self.assertRaisesRegex(ImageIntegrityError, "decode"):
                verify_downloaded_product_image(asset)

    def test_local_file_cannot_exceed_absolute_image_limit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "oversized.png"
            with path.open("wb") as oversized:
                oversized.seek(MAX_IMAGE_BYTES)
                oversized.write(b"x")
            data = b"placeholder"
            asset = downloaded_asset(path, data)

            with self.assertRaisesRegex(ImageIntegrityError, "20 MB"):
                verify_downloaded_product_image(asset)

    def test_reverification_does_not_mutate_manifest_metadata(self) -> None:
        data = encoded_image("PNG")
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "original.png"
            path.write_bytes(data)
            asset = downloaded_asset(path, data)
            before = asset.model_dump(mode="json")

            verify_downloaded_product_image(asset)

        self.assertEqual(asset.model_dump(mode="json"), before)

if __name__ == "__main__":
    unittest.main()
