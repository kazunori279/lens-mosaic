from __future__ import annotations

import argparse
import asyncio
import os
import subprocess
import sys
import tempfile
import time
import wave
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv
from google import genai
from google.genai import types


APP_DIR = Path(__file__).resolve().parent / "app"
ENV_PATH = APP_DIR / ".env"
DEFAULT_VERTEX_MODEL = "gemini-live-2.5-flash-native-audio"
DEFAULT_GEMINI_MODEL = "gemini-2.5-flash-native-audio-preview-12-2025"
PCM_RATE = 16_000
PCM_CHUNK_MS = 100
PCM_BYTES_PER_MS = (PCM_RATE * 2) / 1000
PCM_CHUNK_BYTES = int(PCM_CHUNK_MS * PCM_BYTES_PER_MS)


@dataclass
class ProbeResult:
    name: str
    started_at: float
    input_first_at: float | None = None
    input_final_at: float | None = None
    output_first_at: float | None = None
    output_final_at: float | None = None
    upload_finished_at: float | None = None
    input_text: str = ""
    output_text: str = ""
    audio_duration_s: float | None = None


@dataclass
class ProviderConfig:
    name: str
    model: str
    client: genai.Client


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Direct Gemini Live latency probe for text and audio turns."
    )
    parser.add_argument(
        "--text",
        default="hello",
        help="Prompt to send for both the text and audio probes. Default: hello",
    )
    parser.add_argument(
        "--voice",
        default="Samantha",
        help="macOS `say` voice for the audio probe. Default: Samantha",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="Override both provider models with the same value.",
    )
    parser.add_argument(
        "--vertex-model",
        default=None,
        help=(
            "Vertex AI model override. "
            f"Default: AGENT_MODEL or {DEFAULT_VERTEX_MODEL}"
        ),
    )
    parser.add_argument(
        "--gemini-model",
        default=None,
        help=f"Gemini API model override. Default: {DEFAULT_GEMINI_MODEL}",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print Live API event progress while running.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=240.0,
        help="Seconds to wait for each probe to finish. Default: 240",
    )
    return parser


def require_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def synthesize_pcm(text: str, voice: str) -> bytes:
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        aiff_path = tmp / "input.aiff"
        wav_path = tmp / "input.wav"

        subprocess.run(["say", "-v", voice, "-o", str(aiff_path), text], check=True)
        subprocess.run(
            [
                "afconvert",
                "-f",
                "WAVE",
                "-d",
                f"LEI16@{PCM_RATE}",
                "-c",
                "1",
                str(aiff_path),
                str(wav_path),
            ],
            check=True,
        )

        with wave.open(str(wav_path), "rb") as wav_file:
            sample_rate = wav_file.getframerate()
            channels = wav_file.getnchannels()
            sample_width = wav_file.getsampwidth()
            frames = wav_file.readframes(wav_file.getnframes())

        if sample_rate != PCM_RATE or channels != 1 or sample_width != 2:
            raise RuntimeError(
                "Unexpected WAV format after conversion: "
                f"rate={sample_rate}, channels={channels}, sample_width={sample_width}"
            )
        return frames


def merge_transcript(current: str, chunk: str) -> str:
    chunk = chunk or ""
    if not chunk:
        return current
    if not current:
        return chunk
    if chunk.startswith(current):
        return chunk
    return current + chunk


def latency_str(started_at: float, timestamp: float | None) -> str:
    if timestamp is None:
        return "n/a"
    return f"{timestamp - started_at:.3f}s"


def post_upload_latency_str(upload_finished_at: float | None, timestamp: float | None) -> str:
    if upload_finished_at is None or timestamp is None:
        return "n/a"
    return f"{timestamp - upload_finished_at:.3f}s"


async def collect_probe(
    session,
    result: ProbeResult,
    *,
    verbose: bool,
    timeout: float,
    track_input_audio: bool,
) -> None:
    done = asyncio.Event()
    receiver_error: Exception | None = None

    async def receiver() -> None:
        nonlocal receiver_error
        try:
            async for message in session.receive():
                if verbose:
                    parts = [result.name]
                    if message.setup_complete:
                        parts.append("setup_complete")
                    if message.server_content:
                        sc = message.server_content
                        if sc.input_transcription and sc.input_transcription.text:
                            parts.append(
                                f"input={sc.input_transcription.text!r}"
                                f"/finished={sc.input_transcription.finished}"
                            )
                        if sc.output_transcription and sc.output_transcription.text:
                            parts.append(
                                f"output={sc.output_transcription.text!r}"
                                f"/finished={sc.output_transcription.finished}"
                            )
                        if sc.generation_complete:
                            parts.append("generation_complete")
                        if sc.turn_complete:
                            parts.append("turn_complete")
                        if sc.waiting_for_input:
                            parts.append("waiting_for_input")
                    if message.go_away:
                        parts.append("go_away")
                    if message.usage_metadata:
                        parts.append("usage_metadata")
                    print("event:", ", ".join(parts), flush=True)

                server_content = message.server_content
                if not server_content:
                    continue

                if (
                    track_input_audio
                    and server_content.input_transcription
                    and server_content.input_transcription.text
                ):
                    if result.input_first_at is None:
                        result.input_first_at = time.perf_counter()
                    result.input_text = merge_transcript(
                        result.input_text, server_content.input_transcription.text
                    )
                    if server_content.input_transcription.finished:
                        result.input_final_at = time.perf_counter()

                if server_content.output_transcription and server_content.output_transcription.text:
                    if result.output_first_at is None:
                        result.output_first_at = time.perf_counter()
                    result.output_text = merge_transcript(
                        result.output_text, server_content.output_transcription.text
                    )
                    if server_content.output_transcription.finished:
                        result.output_final_at = time.perf_counter()

                if (
                    server_content.turn_complete
                    or server_content.generation_complete
                    or server_content.waiting_for_input
                ):
                    if result.output_text and result.output_final_at is None:
                        result.output_final_at = time.perf_counter()
                    done.set()
                    break
        except Exception as exc:
            receiver_error = exc
            done.set()

    receiver_task = asyncio.create_task(receiver())
    await asyncio.wait_for(done.wait(), timeout=timeout)
    await receiver_task

    if receiver_error is not None:
        raise receiver_error


async def run_text_probe(
    client: genai.Client,
    text: str,
    model: str,
    *,
    verbose: bool,
    timeout: float,
) -> ProbeResult:
    result = ProbeResult(name="text", started_at=time.perf_counter())
    config = types.LiveConnectConfig(
        response_modalities=["AUDIO"],
        output_audio_transcription=types.AudioTranscriptionConfig(),
    )

    async with client.aio.live.connect(model=model, config=config) as session:
        result.started_at = time.perf_counter()
        await session.send_client_content(
            turns=types.Content(role="user", parts=[types.Part(text=text)]),
            turn_complete=True,
        )
        await collect_probe(
            session,
            result,
            verbose=verbose,
            timeout=timeout,
            track_input_audio=False,
        )

    return result


async def run_audio_probe(
    client: genai.Client,
    text: str,
    voice: str,
    model: str,
    *,
    verbose: bool,
    timeout: float,
) -> ProbeResult:
    audio_bytes = synthesize_pcm(text, voice)
    result = ProbeResult(
        name="audio",
        started_at=time.perf_counter(),
        audio_duration_s=len(audio_bytes) / (PCM_RATE * 2),
    )
    config = types.LiveConnectConfig(
        response_modalities=["AUDIO"],
        input_audio_transcription=types.AudioTranscriptionConfig(),
        output_audio_transcription=types.AudioTranscriptionConfig(),
    )

    async with client.aio.live.connect(model=model, config=config) as session:
        result.started_at = time.perf_counter()
        receiver_task = asyncio.create_task(
            collect_probe(
                session,
                result,
                verbose=verbose,
                timeout=timeout,
                track_input_audio=True,
            )
        )
        for offset in range(0, len(audio_bytes), PCM_CHUNK_BYTES):
            await session.send_realtime_input(
                audio=types.Blob(
                    mime_type=f"audio/pcm;rate={PCM_RATE}",
                    data=audio_bytes[offset : offset + PCM_CHUNK_BYTES],
                )
            )
            await asyncio.sleep(PCM_CHUNK_MS / 1000)

        result.upload_finished_at = time.perf_counter()
        await session.send_realtime_input(audio_stream_end=True)
        await receiver_task

    return result


def print_text_result(result: ProbeResult) -> None:
    print("[text]")
    print(f"output_first_latency: {latency_str(result.started_at, result.output_first_at)}")
    print(f"output_final_latency: {latency_str(result.started_at, result.output_final_at)}")
    print(f"output_transcript: {result.output_text.strip() or '<empty>'}")


def print_audio_result(result: ProbeResult) -> None:
    print("[audio]")
    if result.audio_duration_s is not None:
        print(f"audio_duration: {result.audio_duration_s:.3f}s")
    print(
        f"upload_finished_latency: {latency_str(result.started_at, result.upload_finished_at)}"
    )
    print(f"input_first_latency: {latency_str(result.started_at, result.input_first_at)}")
    print(f"input_final_latency: {latency_str(result.started_at, result.input_final_at)}")
    print(f"output_first_latency: {latency_str(result.started_at, result.output_first_at)}")
    print(f"output_final_latency: {latency_str(result.started_at, result.output_final_at)}")
    print(
        "input_first_after_upload: "
        f"{post_upload_latency_str(result.upload_finished_at, result.input_first_at)}"
    )
    print(
        "input_final_after_upload: "
        f"{post_upload_latency_str(result.upload_finished_at, result.input_final_at)}"
    )
    print(
        "output_first_after_upload: "
        f"{post_upload_latency_str(result.upload_finished_at, result.output_first_at)}"
    )
    print(
        "output_final_after_upload: "
        f"{post_upload_latency_str(result.upload_finished_at, result.output_final_at)}"
    )
    print(f"input_transcript: {result.input_text.strip() or '<empty>'}")
    print(f"output_transcript: {result.output_text.strip() or '<empty>'}")


def build_provider_configs(
    vertex_model: str,
    gemini_model: str,
) -> tuple[list[ProviderConfig], list[str]]:
    providers: list[ProviderConfig] = []
    skipped: list[str] = []

    try:
        providers.append(
            ProviderConfig(
                name="vertexai",
                model=vertex_model,
                client=genai.Client(
                    vertexai=True,
                    project=require_env("GOOGLE_CLOUD_PROJECT"),
                    location=os.getenv("GOOGLE_CLOUD_LOCATION", "us-central1"),
                ),
            )
        )
    except Exception as exc:
        skipped.append(f"vertexai skipped: {exc}")

    gemini_api_key = os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY")
    if gemini_api_key:
        providers.append(
            ProviderConfig(
                name="gemini-api",
                model=gemini_model,
                client=genai.Client(vertexai=False, api_key=gemini_api_key),
            )
        )
    else:
        skipped.append("gemini-api skipped: GOOGLE_API_KEY is missing")

    return providers, skipped


async def run_all(
    text: str,
    voice: str,
    vertex_model: str,
    gemini_model: str,
    *,
    verbose: bool,
    timeout: float,
) -> int:
    load_dotenv(ENV_PATH, override=True)
    providers, skipped = build_provider_configs(vertex_model, gemini_model)
    failures = 0
    print(f"text: {text}")
    print(f"voice: {voice}")
    print(f"timeout_per_probe: {timeout:.1f}s")
    for note in skipped:
        print(note)

    if not providers:
        print("failed: no provider credentials/configuration available")
        return 1

    for provider in providers:
        print(f"[{provider.name}]")
        print(f"model: {provider.model}")

        try:
            print_text_result(
                await run_text_probe(
                    provider.client,
                    text,
                    provider.model,
                    verbose=verbose,
                    timeout=timeout,
                )
            )
        except asyncio.TimeoutError:
            failures += 1
            print("[text]")
            print(f"failed: timed out after {timeout:.1f}s")
        except Exception as exc:
            failures += 1
            print("[text]")
            print(f"failed: {exc}")

        try:
            print_audio_result(
                await run_audio_probe(
                    provider.client,
                    text,
                    voice,
                    provider.model,
                    verbose=verbose,
                    timeout=timeout,
                )
            )
        except asyncio.TimeoutError:
            failures += 1
            print("[audio]")
            print(f"failed: timed out after {timeout:.1f}s")
        except Exception as exc:
            failures += 1
            print("[audio]")
            print(f"failed: {exc}")

    return 0 if failures == 0 else 1


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    load_dotenv(ENV_PATH, override=True)
    vertex_model = args.vertex_model or args.model or os.getenv(
        "AGENT_MODEL", DEFAULT_VERTEX_MODEL
    )
    gemini_model = args.gemini_model or args.model or DEFAULT_GEMINI_MODEL

    try:
        return asyncio.run(
            run_all(
                args.text,
                args.voice,
                vertex_model,
                gemini_model,
                verbose=args.verbose,
                timeout=args.timeout,
            )
        )
    except Exception as exc:
        print(f"model_test failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
