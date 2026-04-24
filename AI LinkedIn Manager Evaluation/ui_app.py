import asyncio
import base64
import json
import os
import re
import socket
import time
from contextlib import nullcontext
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import httpx
import streamlit as st
from dotenv import load_dotenv
from langfuse import Langfuse
from langfuse._version import __version__ as _langfuse_sdk_version
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.trace.export import SpanExporter

# ---------------------- ENV ----------------------

ENV_PATH = Path(__file__).resolve().parent / ".env"
load_dotenv(dotenv_path=ENV_PATH, override=True)

# ---------------------- CONFIG ----------------------

STAGE_BASE_URL = "https://stage-linkedin-manager-content-creation-53521016621.us-central1.run.app"
POST_PATHS = ["/create-post", "/default/create_post_create_post_post"]

EXAMPLE_PAYLOAD = """{
  "model_provider": "openai",
  "model_version": "gpt-4o",
  "profile": {
    "name": "Jane Doe",
    "headline": "AI Engineer | Building the future",
    "industry": "Technology"
  }
}"""


def _lan_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.2)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return "127.0.0.1"


# ---------------------- LANGFUSE HELPERS ----------------------


def _langfuse_base_url() -> str:
    """Strip whitespace/trailing slash so SDK and trace URLs stay consistent."""
    return (os.getenv("LANGFUSE_BASE_URL") or "").strip().rstrip("/")


def _streamlit_public_base() -> str:
    """Optional external URL of this app (e.g. http://host:20202) from .env."""
    return (os.getenv("STREAMLIT_PUBLIC_BASE_URL") or "").strip().rstrip("/")


def _langfuse_insecure_skip_verify() -> bool:
    v = (os.getenv("LANGFUSE_INSECURE_SKIP_VERIFY") or "").strip().lower()
    return v in ("1", "true", "yes", "on")


def _langfuse_timeout_seconds() -> int:
    try:
        return int(os.getenv("LANGFUSE_TIMEOUT", "30"))
    except ValueError:
        return 30


def _otlp_span_exporter_insecure(
    base_url: str, public_key: str, secret_key: str, timeout: float
) -> SpanExporter:
    """OTLP to Langfuse with TLS verify off (self-signed). Patches private attr OpenTelemetry disallows in ctor."""
    basic = "Basic " + base64.b64encode(
        f"{public_key}:{secret_key}".encode("utf-8")
    ).decode("ascii")
    headers = {
        "Authorization": basic,
        "x-langfuse-sdk-name": "python",
        "x-langfuse-sdk-version": _langfuse_sdk_version,
        "x-langfuse-public-key": public_key,
    }
    endpoint = f"{base_url.rstrip('/')}/api/public/otel/v1/traces"
    exporter = OTLPSpanExporter(
        endpoint=endpoint,
        headers=headers,
        timeout=timeout,
    )
    exporter._certificate_file = False  # noqa: SLF001
    return exporter


@st.cache_resource
def get_langfuse_client(_base_url: str, _insecure: bool) -> Tuple[Optional[Langfuse], str]:
    """Cache keys: base URL and insecure flag (change .env and restart to refresh)."""
    pk = os.getenv("LANGFUSE_PUBLIC_KEY")
    sk = os.getenv("LANGFUSE_SECRET_KEY")
    host = _base_url or _langfuse_base_url()
    if not pk or not sk or not host:
        return None, "disabled — missing LANGFUSE_PUBLIC_KEY / SECRET_KEY / BASE_URL in .env"
    tsec = _langfuse_timeout_seconds()
    try:
        if _insecure:
            import urllib3

            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
            httpx_c = httpx.Client(verify=False, timeout=float(tsec))
            span_e = _otlp_span_exporter_insecure(host, pk, sk, float(tsec))
            client = Langfuse(
                public_key=pk,
                secret_key=sk,
                host=host,
                timeout=tsec,
                httpx_client=httpx_c,
                span_exporter=span_e,
            )
            return client, f"connected — {host} (TLS verify off for self-signed)"
        return Langfuse(public_key=pk, secret_key=sk, host=host, timeout=tsec), f"connected ({host})"
    except Exception as exc:
        return None, f"disabled (init error: {exc})"


# ---------------------- CORE REQUEST ----------------------


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
                resp = await client.post(
                    url, json=profile_input, params={"test_type": "interactive-test"}
                )
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
            except Exception as exc:
                result["error"] = f"{type(exc).__name__} on {path}: {exc}"
    return result


def create_trace_and_call(payload: dict) -> Dict[str, Any]:
    lf, _ = get_langfuse_client(
        _langfuse_base_url(), _langfuse_insecure_skip_verify()
    )
    trace_id: Optional[str] = None
    trace_url: Optional[str] = None

    obs_ctx = (
        lf.start_as_current_observation(
            name="create-post-stage",
            input=payload,
            metadata={
                "source": "ui",
                "environment": "stage",
                "model_provider": payload.get("model_provider"),
                "model_version": payload.get("model_version"),
            },
        )
        if lf
        else nullcontext()
    )

    with obs_ctx as obs:
        result = asyncio.run(call_create_post(STAGE_BASE_URL, payload))
        if obs and lf:
            obs.update(
                output=result.get("response"),
                metadata={
                    "stage_ok": result["ok"],
                    "stage_latency_ms": result["latency_ms"],
                    "http_status": result["status_code"],
                    "used_path": result["used_path"],
                },
            )
            try:
                lf.flush()
                trace_id = lf.get_current_trace_id()
                trace_url = lf.get_trace_url()
            except Exception as exc:
                result["langfuse_error"] = str(exc)

    result["trace_id"] = trace_id
    result["trace_url"] = trace_url
    return result


# ---------------------- UI ----------------------

st.set_page_config(
    page_title="LinkedIn Post Creator – Stage",
    page_icon="💼",
    layout="wide",
)

# --- Sidebar ---
with st.sidebar:
    st.header("Langfuse")
    lf_client, lf_status = get_langfuse_client(
        _langfuse_base_url(), _langfuse_insecure_skip_verify()
    )
    if lf_client:
        st.success(lf_status)
    else:
        st.warning(lf_status)
        st.caption("Set LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY, LANGFUSE_BASE_URL in .env")

    st.divider()
    st.header("Langfuse project")
    st.caption("Traces and LLM-as-a-judge (scores) in this project:")
    st.code(_langfuse_base_url() or "(set LANGFUSE_BASE_URL)", language=None)
    st.divider()
    st.header("Stage API")
    st.code(STAGE_BASE_URL, language=None)

# --- Header ---
st.title("LinkedIn Post Creator")
st.caption("Stage · Langfuse tracing · evaluation via Langfuse (LLM-as-a-judge)")

_uport = os.environ.get("STREAMLIT_SERVER_PORT", "8501")
pub = _streamlit_public_base()
if pub:
    st.caption(
        f"**This UI (external):** {pub}/  ·  **Langfuse:** {_langfuse_base_url() or '—'}"
    )
else:
    st.caption(
        "The app listens on `0.0.0.0`. On another machine, use e.g. "
        f"**http://{_lan_ip()}:{_uport}/** — set `STREAMLIT_PUBLIC_BASE_URL` in `.env` to pin your public URL."
    )

st.divider()

# --- JSON Input ---
st.subheader("Persona JSON Payload")
json_input = st.text_area(
    label="payload",
    value=EXAMPLE_PAYLOAD,
    height=280,
    label_visibility="collapsed",
)

if st.button("Send Request", type="primary", use_container_width=True):
    payload, err = parse_json_robust(json_input)
    if err:
        st.error(f"Invalid JSON: {err}")
    elif not payload:
        st.warning("Payload is empty.")
    else:
        with st.spinner("Calling Stage API…"):
            result = create_trace_and_call(payload)
        st.session_state["stage_result"] = result

st.divider()

# --- Results ---
if "stage_result" in st.session_state:
    result = st.session_state["stage_result"]

    c1, c2, c3 = st.columns(3)
    with c1:
        st.metric("Status", "OK" if result["ok"] else "FAILED")
    with c2:
        st.metric("Latency", f"{result['latency_ms']} ms" if result["latency_ms"] else "—")
    with c3:
        st.metric("HTTP", result["status_code"] or "—")

    if result.get("trace_id"):
        lf_col, _ = st.columns([2, 3])
        with lf_col:
            if result.get("trace_url"):
                st.link_button("View trace in Langfuse", result["trace_url"], use_container_width=True)
            st.caption(f"Trace ID: `{result['trace_id']}`")
    elif not lf_client:
        st.info("Langfuse not configured — trace not recorded.")
    elif result.get("langfuse_error"):
        st.error(f"Langfuse ingest failed: `{result['langfuse_error']}`")
    elif lf_client and result.get("ok"):
        st.warning(
            "No trace id returned — if logs show **401**, `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` "
            "do not match the project on `LANGFUSE_BASE_URL` (copy keys from Langfuse → Project → Settings → API keys)."
        )

    st.divider()

    if result["ok"]:
        posts = (
            result["response"].get("posts", [])
            if isinstance(result["response"], dict)
            else []
        )
        st.subheader(f"Generated Posts · {len(posts)} returned")
        st.json(result["response"])
    else:
        st.error(f"Error: {result['error']}")
