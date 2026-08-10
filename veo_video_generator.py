"""Google Gemini API Veo 3.1 Fast adapter for one approved video shot."""

import html
import json
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional, Union

from google.genai import types
from pydantic import ValidationError

from video_shot_artifact import (
    VideoGenerationFailure,
    VideoGenerationReceipt,
    VideoGenerationResult,
    VideoShotArtifactError,
    store_validated_video_shot_artifact,
)
from video_shot_generation import (
    VideoGenerationOptions,
    VideoProviderCapabilities,
    VideoProviderCompatibilityError,
    VideoShotGenerationInstruction,
    compile_video_shot_instruction,
    preflight_video_generation,
)
from video_shot_request import VideoShotRequest


VEO_3_1_FAST_MODEL = "veo-3.1-fast-generate-preview"
VEO_PROVIDER = "google-gemini-api"
VEO_3_1_FAST_CAPABILITIES = VideoProviderCapabilities(
    provider=VEO_PROVIDER,
    model=VEO_3_1_FAST_MODEL,
    supports_start_image=True,
    supported_aspect_ratios=("9:16",),
    supported_resolutions=("720p",),
    supported_durations_seconds=(4, 6, 8),
    videos_per_request=1,
    audio_generation="always_on",
)


class VeoProviderError(RuntimeError):
    """Raised for adapter misuse; provider attempts return terminal results."""


class VeoProviderConfigurationError(VeoProviderError):
    """Raised when an explicit live attempt lacks a compatible SDK client."""


class VeoVideoGenerator:
    """Injectable exactly-one-attempt adapter with no retry or fallback."""

    def __init__(
        self,
        *,
        client=None,
        storage_root: Union[str, Path] = "products",
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        sleep: Callable[[float], None] = time.sleep,
        max_polls: int = 180,
        attempt_id_factory: Callable[[], str] = lambda: str(uuid.uuid4()),
    ) -> None:
        self._client = client
        self._storage_root = Path(storage_root)
        self._clock = clock
        self._sleep = sleep
        self._max_polls = max_polls
        self._attempt_id_factory = attempt_id_factory

    def capabilities(self) -> VideoProviderCapabilities:
        return VEO_3_1_FAST_CAPABILITIES.model_copy(deep=True)

    def _client_for_attempt(self):
        if self._client is not None:
            client = self._client
        else:
            api_key = os.environ.get("GEMINI_API_KEY")
            if not api_key:
                raise VeoProviderConfigurationError(
                    "GEMINI_API_KEY is required for an explicit live generation"
                )
            try:
                from google import genai
            except ImportError as error:
                raise VeoProviderConfigurationError(
                    "google-genai is not installed in the current environment"
                ) from error
            client = genai.Client(api_key=api_key)
        required = (
            (getattr(client, "models", None), "generate_videos"),
            (getattr(client, "operations", None), "get"),
            (getattr(client, "files", None), "download"),
        )
        if any(owner is None or not callable(getattr(owner, name, None)) for owner, name in required):
            raise VeoProviderConfigurationError(
                "the google-genai client does not expose the required Veo API"
            )
        return client

    def generate(self, request: VideoShotRequest) -> VideoGenerationResult:
        # Validate domain intent before resolving live credentials; the lower
        # boundary repeats integrity verification immediately before upload.
        preflight_video_generation(
            request,
            VideoGenerationOptions(),
            self.capabilities(),
        )
        return generate_veo_video_shot(
            request=request,
            client=self._client_for_attempt(),
            storage_root=self._storage_root,
            now=self._clock,
            sleep=self._sleep,
            max_polls=self._max_polls,
            attempt_id_factory=self._attempt_id_factory,
        )


def _untrusted_block(name: str, value: Optional[str]) -> str:
    rendered = html.escape(value if value is not None else "(none)")
    return f"<UNTRUSTED_{name}>\n{rendered}\n</UNTRUSTED_{name}>"


def render_veo_video_prompt(
    instruction: VideoShotGenerationInstruction,
) -> str:
    """Render deterministic visual instructions with escaped untrusted data."""

    try:
        validated = VideoShotGenerationInstruction.model_validate(
            instruction.model_dump()
        )
    except ValidationError as error:
        raise VeoProviderError(
            f"video instruction must be structurally valid: {error}"
        ) from error
    facts = (
        "\n".join(
            _untrusted_block(
                "MOTION_RELEVANT_FACT",
                json.dumps(
                    fact.model_dump(mode="json", exclude_none=True),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            )
            for fact in validated.motion_relevant_facts
        )
        or "(none)"
    )
    return "\n\n".join(
        (
            "AUTHORITATIVE START IMAGE POLICY\n"
            "Animate the supplied approved start image naturally. It is the sole "
            "authoritative visual start state. Preserve the Product appearance "
            "already established in that frame; do not recreate it from text.",
            "OPTIONAL MOTION-RELEVANT FACTS\n" + facts,
            "SUBJECT MOTION\n"
            + _untrusted_block("SUBJECT_MOTION", validated.subject_motion),
            "ENVIRONMENT MOTION\n"
            + _untrusted_block(
                "ENVIRONMENT_MOTION", validated.environment_motion
            ),
            "CAMERA MOTION\n"
            + _untrusted_block("CAMERA_MOTION", validated.camera_motion),
            "MOTION CONSTRAINTS\n"
            + _untrusted_block(
                "MOTION_CONSTRAINTS", validated.motion_constraints
            ),
            "PRODUCT ANIMATION FIDELITY\n"
            "Animate the scene already established in the approved frame. Keep "
            "Product motion small and natural. Preserve visible geometry, "
            "proportions, controls, buttons, panels, seams, logos, markings, "
            "color, materials, and construction. Do not rotate, morph, redesign, "
            "or reconstruct the Product. Do not reveal substantially new Product "
            "surfaces.",
            "NO-ADDED-TEXT POLICY\n"
            "Do not add advertising text, captions, CTA copy, claim copy, or "
            "Product-name cards. Preserve physical text, logos, and markings "
            "already visible in the start image.",
            "AUDIO-DISPOSITION NOTE\n"
            "This request is primarily visual. Native provider audio may be "
            "generated and will be discarded before editing. Do not create "
            "factual speech, voice-over, music, lyrics, or dialogue.",
        )
    )


def _receipt(
    *,
    attempt_id: str,
    outcome: str,
    started_at: datetime,
    completed_at: datetime,
    artifact_id: Optional[str],
    operation_id: Optional[str],
) -> VideoGenerationReceipt:
    return VideoGenerationReceipt(
        attempt_id=attempt_id,
        outcome=outcome,
        artifact_id=artifact_id,
        provider=VEO_PROVIDER,
        model=VEO_3_1_FAST_MODEL,
        started_at=started_at,
        completed_at=completed_at,
        provider_operation_id=operation_id,
    )


def _failure_result(
    *,
    attempt_id: str,
    code: str,
    message: str,
    started_at: datetime,
    completed_at: datetime,
    operation_id: Optional[str],
) -> VideoGenerationResult:
    return VideoGenerationResult(
        status="failed",
        receipt=_receipt(
            attempt_id=attempt_id,
            outcome="failed",
            started_at=started_at,
            completed_at=completed_at,
            artifact_id=None,
            operation_id=operation_id,
        ),
        failure=VideoGenerationFailure(code=code, message=message),
    )


def _operation_id(operation) -> Optional[str]:
    value = getattr(operation, "name", None)
    return value if isinstance(value, str) and value.strip() else None


def _operation_error_code(error: object) -> str:
    rendered = str(error).lower()
    if "audio" in rendered and any(
        token in rendered for token in ("filter", "processing", "safety")
    ):
        return "provider_audio_filter_error"
    return "provider_rejected"


def generate_veo_video_shot(
    *,
    request: VideoShotRequest,
    client,
    storage_root: Union[str, Path] = "products",
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    sleep: Callable[[float], None] = time.sleep,
    max_polls: int = 180,
    attempt_id_factory: Callable[[], str] = lambda: str(uuid.uuid4()),
) -> VideoGenerationResult:
    """Execute exactly one Veo generation attempt; never retry or fall back."""

    if type(max_polls) is not int or max_polls < 0:
        raise VeoProviderError("max_polls must be a non-negative integer")
    try:
        validated_request = VideoShotRequest.model_validate(request.model_dump())
    except ValidationError as error:
        raise VideoProviderCompatibilityError(
            f"Video Shot request must be structurally valid: {error}"
        ) from error
    options = VideoGenerationOptions()
    instruction = compile_video_shot_instruction(validated_request)
    prompt = render_veo_video_prompt(instruction)
    start_image_bytes = preflight_video_generation(
        validated_request,
        options,
        VEO_3_1_FAST_CAPABILITIES,
    )

    started_at = now()
    attempt_id = attempt_id_factory()
    operation = None
    try:
        operation = client.models.generate_videos(
            model=VEO_3_1_FAST_MODEL,
            prompt=prompt,
            image=types.Image(
                image_bytes=start_image_bytes,
                mime_type="image/png",
            ),
            config=types.GenerateVideosConfig(
                number_of_videos=1,
                duration_seconds=int(
                    validated_request.requested_generation_duration_seconds
                ),
                aspect_ratio=validated_request.aspect_ratio,
                resolution=options.resolution,
                enhance_prompt=False,
                person_generation="allow_adult",
            ),
        )
    except Exception:
        return _failure_result(
            attempt_id=attempt_id,
            code="provider_error",
            message="Veo provider generation attempt failed",
            started_at=started_at,
            completed_at=now(),
            operation_id=None,
        )

    operation_id = _operation_id(operation)
    polls = 0
    try:
        while not bool(getattr(operation, "done", False)):
            if polls >= max_polls:
                return _failure_result(
                    attempt_id=attempt_id,
                    code="polling_exhausted",
                    message="Veo operation did not complete within the poll limit",
                    started_at=started_at,
                    completed_at=now(),
                    operation_id=operation_id,
                )
            sleep(10)
            operation = client.operations.get(operation)
            operation_id = _operation_id(operation) or operation_id
            polls += 1
    except Exception:
        return _failure_result(
            attempt_id=attempt_id,
            code="provider_error",
            message="Veo operation polling failed",
            started_at=started_at,
            completed_at=now(),
            operation_id=operation_id,
        )

    operation_error = getattr(operation, "error", None)
    if operation_error:
        return _failure_result(
            attempt_id=attempt_id,
            code=_operation_error_code(operation_error),
            message="Veo operation returned a terminal provider rejection",
            started_at=started_at,
            completed_at=now(),
            operation_id=operation_id,
        )
    response = getattr(operation, "response", None)
    filtered_reasons = getattr(response, "rai_media_filtered_reasons", None)
    if isinstance(filtered_reasons, list) and filtered_reasons:
        rendered_reasons = " ".join(str(item) for item in filtered_reasons)
        failure_code = _operation_error_code(rendered_reasons)
        return _failure_result(
            attempt_id=attempt_id,
            code=failure_code,
            message="Veo response reported filtered generated media",
            started_at=started_at,
            completed_at=now(),
            operation_id=operation_id,
        )
    generated_videos = getattr(response, "generated_videos", None)
    if not isinstance(generated_videos, list) or len(generated_videos) != 1:
        return _failure_result(
            attempt_id=attempt_id,
            code="malformed_output",
            message="Veo response must contain exactly one generated video",
            started_at=started_at,
            completed_at=now(),
            operation_id=operation_id,
        )
    video = getattr(generated_videos[0], "video", None)
    if video is None:
        return _failure_result(
            attempt_id=attempt_id,
            code="malformed_output",
            message="Veo response did not contain a video object",
            started_at=started_at,
            completed_at=now(),
            operation_id=operation_id,
        )
    try:
        downloaded = client.files.download(file=video)
        output_bytes = (
            downloaded
            if isinstance(downloaded, bytes)
            else getattr(video, "video_bytes", None)
        )
    except Exception:
        return _failure_result(
            attempt_id=attempt_id,
            code="provider_error",
            message="Veo output download failed",
            started_at=started_at,
            completed_at=now(),
            operation_id=operation_id,
        )
    if not isinstance(output_bytes, bytes):
        return _failure_result(
            attempt_id=attempt_id,
            code="malformed_output",
            message="Veo download did not provide raw video bytes",
            started_at=started_at,
            completed_at=now(),
            operation_id=operation_id,
        )
    completed_at = now()
    try:
        artifact = store_validated_video_shot_artifact(
            output_bytes,
            validated_request,
            storage_root,
            created_at=completed_at,
        )
    except VideoShotArtifactError:
        return _failure_result(
            attempt_id=attempt_id,
            code="invalid_output",
            message="Veo output failed raw-video artifact validation",
            started_at=started_at,
            completed_at=completed_at,
            operation_id=operation_id,
        )
    return VideoGenerationResult(
        status="succeeded",
        artifact=artifact,
        receipt=_receipt(
            attempt_id=attempt_id,
            outcome="succeeded",
            started_at=started_at,
            completed_at=completed_at,
            artifact_id=artifact.id,
            operation_id=operation_id,
        ),
    )
