import inspect
import unittest
import warnings
from importlib.metadata import version

from google import genai
from google.auth.credentials import AnonymousCredentials
from google.genai import files, models, operations


class GoogleGenAiSdkContractTests(unittest.TestCase):
    def setUp(self):
        self.client = genai.Client(
            vertexai=True,
            project="offline-contract",
            location="global",
            credentials=AnonymousCredentials(),
        )

    def tearDown(self):
        self.client.close()

    def _interactions_service(self):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            return self.client.interactions

    def test_smallest_approved_sdk_release_is_installed(self):
        self.assertEqual(version("google-genai"), "1.55.0")

    def test_client_exposes_required_public_services(self):
        for service_name in ("interactions", "models", "operations", "files"):
            with self.subTest(service_name=service_name):
                self.assertIsInstance(
                    inspect.getattr_static(genai.Client, service_name), property
                )

    def test_required_generation_and_download_methods_are_public(self):
        required_methods = (
            (self._interactions_service(), "create"),
            (models.Models, "generate_videos"),
            (operations.Operations, "get"),
            (files.Files, "download"),
        )
        for owner, method_name in required_methods:
            owner_name = getattr(owner, "__name__", type(owner).__name__)
            with self.subTest(owner=owner_name, method_name=method_name):
                self.assertTrue(callable(inspect.getattr_static(owner, method_name)))

    def test_required_method_signatures_match_adapter_calls(self):
        interaction_parameters = inspect.signature(
            self._interactions_service().create
        ).parameters
        video_parameters = inspect.signature(models.Models.generate_videos).parameters
        operation_parameters = inspect.signature(operations.Operations.get).parameters
        download_parameters = inspect.signature(files.Files.download).parameters

        self.assertTrue({"model", "input"}.issubset(interaction_parameters))
        self.assertTrue(
            {"model", "prompt", "image", "config"}.issubset(video_parameters)
        )
        self.assertIn("operation", operation_parameters)
        self.assertIn("file", download_parameters)


if __name__ == "__main__":
    unittest.main()
