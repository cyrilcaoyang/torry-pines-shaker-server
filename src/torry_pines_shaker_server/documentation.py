"""Agent documentation served from resources shipped inside the package.

Same shape as the other lab device services (sense-every-zone,
mt-xpr-balance-server): a Markdown agent guide, a Markdown API reference,
and a plain-text ``/llms.txt`` index, so an agent — or the dashboard's API
reference page — can discover how to drive this device without reading the
repo.
"""

from __future__ import annotations

from importlib.resources import files

from fastapi import APIRouter
from fastapi.responses import PlainTextResponse

router = APIRouter(tags=["documentation"])


class MarkdownResponse(PlainTextResponse):
    media_type = "text/markdown"


def _document(name: str) -> str:
    return files("torry_pines_shaker_server").joinpath("docs", name).read_text(encoding="utf-8")


@router.get("/agent-docs", response_class=MarkdownResponse, summary="Agent guide (Markdown)")
async def agent_docs() -> str:
    return _document("AGENT_GUIDE.md")


@router.get(
    "/agent-docs/api-reference",
    response_class=MarkdownResponse,
    summary="API reference (Markdown)",
)
async def api_reference() -> str:
    return _document("API_REFERENCE.md")


@router.get("/llms.txt", response_class=PlainTextResponse, summary="Discovery index for agents")
async def llms_txt() -> str:
    return (
        "# Torrey Pines SC25XR shaker (STATUS_SPEC v1.2 device service)\n\n"
        "## Documentation\n\n"
        "- [Agent guide](agent-docs): health vs activity, claims, startup/shutdown semantics, preconditions.\n"
        "- [API reference](agent-docs/api-reference): every route with bodies and refusal codes.\n"
        "- [OpenAPI](openapi.json): request/response schemas.\n\n"
        "## Live status\n\n"
        "Read the service's `GET /status` through the lab-skills SDK or dashboard; "
        "live status is not a documentation-proxy resource. Read allowed_actions before acting.\n"
    )
