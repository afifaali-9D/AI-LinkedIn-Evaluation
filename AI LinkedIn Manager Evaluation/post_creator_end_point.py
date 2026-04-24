


import asyncio
import json
import logging
import os
import re
import sys
import time
from contextlib import nullcontext
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import httpx
from dotenv import load_dotenv
from langfuse import Langfuse

# ---------------------- ENV + LOGGING ----------------------

ENV_PATH = Path(__file__).resolve().parent / ".env"
load_dotenv(dotenv_path=ENV_PATH, override=True)

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger(__name__)

STAGE_BASE_URL = "https://stage-linkedin-manager-content-creation-53521016621.us-central1.run.app"
POST_PATHS = ["/create-post", "/default/create_post_create_post_post"]


def parse_json_robust(text: str) -> Tuple[Optional[dict], Optional[str]]:
    text = re.sub(r"#.*$", "", text, flags=re.MULTILINE)
    text = re.sub(r",\s*([}\]])", r"\1", text)
    text = text.strip()

    match = re.search(r"(\{.*\})", text, re.DOTALL)
    if match:
        text = match.group(1)

    try:
        return json.loads(text), None
    except json.JSONDecodeError as exc:
        return None, str(exc)


def read_request_from_stdin() -> str:
    data = sys.stdin.read()
    if not data:
        return ""
    if "\nDONE\n" in data:
        return data.split("\nDONE\n", 1)[0]
    if data.rstrip().endswith("\nDONE"):
        return data.rsplit("\nDONE", 1)[0]
    return data


def get_langfuse_client() -> Tuple[Optional[Langfuse], str]:
    public_key = os.getenv("LANGFUSE_PUBLIC_KEY")
    secret_key = os.getenv("LANGFUSE_SECRET_KEY")
    base_url = os.getenv("LANGFUSE_BASE_URL")

    if not public_key or not secret_key or not base_url:
        return None, "disabled (missing LANGFUSE_PUBLIC_KEY/SECRET_KEY/BASE_URL)"

    try:
        client = Langfuse(public_key=public_key, secret_key=secret_key, host=base_url)
        return client, f"enabled ({base_url})"
    except Exception as exc:  # defensive: tracing should never block API testing
        logger.warning("Langfuse init failed: %s", exc)
        return None, f"disabled (init failed: {type(exc).__name__})"


async def call_create_post(base_url: str, profile_input: dict) -> Dict[str, Any]:
    result: Dict[str, Any] = {
        "base_url": base_url,
        "used_path": None,
        "ok": False,
        "status_code": None,
        "latency_ms": None,
        "response": None,
        "error": None,
    }

    async with httpx.AsyncClient(timeout=300) as client:
        for path in POST_PATHS:
            url = f"{base_url}{path}"
            start = time.perf_counter()
            try:
                logger.info("POST -> %s", url)
                resp = await client.post(url, json=profile_input, params={"test_type": "interactive-test"})
                latency_ms = round((time.perf_counter() - start) * 1000, 2)
                result["status_code"] = resp.status_code
                result["latency_ms"] = latency_ms

                resp.raise_for_status()
                result["ok"] = True
                result["used_path"] = path
                result["response"] = resp.json()
                return result
            except httpx.HTTPStatusError as exc:
                result["error"] = f"HTTP {exc.response.status_code} on {path}"
                logger.warning("%s | body: %s", result["error"], exc.response.text[:300])
            except Exception as exc:
                result["error"] = f"{type(exc).__name__} on {path}: {exc}"
                logger.warning("%s", result["error"])

    return result


def print_result(label: str, result: Dict[str, Any]) -> None:
    print(f"\n--- {label} ---")
    print(f"Base URL  : {result['base_url']}")
    print(f"Path      : {result['used_path']}")
    print(f"Status    : {'SUCCESS' if result['ok'] else 'FAILED'}")
    print(f"HTTP Code : {result['status_code']}")
    print(f"Latency   : {result['latency_ms']} ms")
    if result["ok"]:
        posts = result["response"].get("posts", []) if isinstance(result["response"], dict) else []
        print(f"Posts     : {len(posts)}")
        print(json.dumps(result["response"], indent=2, ensure_ascii=False))
    else:
        print(f"Error     : {result['error']}")


def print_comparison(stage_result: Dict[str, Any]) -> None:
    print("\n" + "=" * 100)
    print("RESULT (Stage)")
    print("=" * 100)
    print(
        f"STAGE -> ok={stage_result['ok']}, status={stage_result['status_code']}, latency_ms={stage_result['latency_ms']}"
    )
    print("=" * 100 + "\n")
async def run_single_request(request_dict: dict, langfuse_client: Optional[Langfuse]) -> None:
    metadata = {
        "source": "manual-tester",
        "environment": "stage",
        "model_provider": request_dict.get("model_provider"),
        "model_version": request_dict.get("model_version"),
    }

    observation_ctx = (
        langfuse_client.start_as_current_observation(
            name="create-post-stage",
            input=request_dict,
            metadata=metadata,
        )
        if langfuse_client
        else nullcontext()
    )

    with observation_ctx as observation:
        stage_result = await call_create_post(STAGE_BASE_URL, request_dict)

        print_result("STAGE", stage_result)
        print_comparison(stage_result)

        if observation:
            observation.update(
                output={"stage": stage_result},
                metadata={
                    "stage_ok": stage_result["ok"],
                    "stage_latency_ms": stage_result["latency_ms"],
                },
            )
            # In interactive mode, flush after each run so traces show up immediately.
            try:
                langfuse_client.flush()
                obs_id = getattr(observation, "id", None)
                if obs_id:
                    print(f"Langfuse observation id: {obs_id}")
                trace_id = langfuse_client.get_current_trace_id()
                if trace_id:
                    print(f"Langfuse trace id: {trace_id}")
                    try:
                        print(f"Langfuse trace url: {langfuse_client.get_trace_url()}")
                    except Exception:
                        pass
            except Exception as exc:
                logger.warning("Langfuse flush failed: %s", exc)


async def main() -> Optional[Langfuse]:
    langfuse_client, langfuse_status = get_langfuse_client()

    print("=" * 100)
    print("LinkedIn Post Creator (Stage)")
    print(f"Stage URL : {STAGE_BASE_URL}")
    print(f"Langfuse  : {langfuse_status}")
    print("=" * 100 + "\n")

    if not sys.stdin.isatty():
        request_text = read_request_from_stdin()
        request_dict, error = parse_json_robust(request_text)
        if error:
            print(f"JSON Error: {error}")
            return
        if not request_dict:
            print("No JSON received on stdin.")
            return
        await run_single_request(request_dict, langfuse_client)
        return langfuse_client

    print("Paste your JSON payload, then type DONE on a new line.")
    print("Type 'exit' or 'quit' to stop.\n")

    while True:
        print("-> Paste JSON:")
        lines = []
        while True:
            try:
                line = input()
            except EOFError:
                if not lines:
                    return
                break

            stripped = line.strip()
            if stripped.upper() == "DONE":
                break
            if stripped.lower() in ("exit", "quit"):
                print("Goodbye.")
                return langfuse_client
            lines.append(line)

        if not lines:
            continue

        request_text = "\n".join(lines)
        request_dict, error = parse_json_robust(request_text)
        if error:
            print(f"JSON Error: {error}\n")
            continue

        await run_single_request(request_dict, langfuse_client)

    return langfuse_client


if __name__ == "__main__":
    client_for_flush: Optional[Langfuse] = None
    try:
        client_for_flush = asyncio.run(main())
    except KeyboardInterrupt:
        print("\nStopped by user.")
    finally:
        if client_for_flush:
            client_for_flush.flush()
            print("Langfuse traces flushed.")