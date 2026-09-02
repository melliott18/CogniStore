"""Same-origin routes for the dependency-free content-search interface."""

from __future__ import annotations

from importlib.resources import files

from fastapi import FastAPI
from fastapi.responses import RedirectResponse, Response

_SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; base-uri 'none'; connect-src 'self'; "
        "font-src 'self'; form-action 'self'; frame-ancestors 'none'; "
        "img-src 'self' data:; object-src 'none'; script-src 'self'; "
        "style-src 'self'"
    ),
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
}


def _asset(name: str, media_type: str, *, cache_control: str) -> Response:
    payload = files("cognistore.ui").joinpath("static").joinpath(name).read_bytes()
    return Response(
        content=payload,
        media_type=media_type,
        headers={**_SECURITY_HEADERS, "Cache-Control": cache_control},
    )


def register_content_search_ui(app: FastAPI) -> None:
    """Register UI routes without adding implementation assets to OpenAPI v1."""

    @app.get("/", include_in_schema=False)
    def content_search_root() -> RedirectResponse:
        return RedirectResponse(
            "/ui/",
            status_code=307,
            headers={**_SECURITY_HEADERS, "Cache-Control": "no-store"},
        )

    @app.get("/ui", include_in_schema=False)
    def content_search_canonical_redirect() -> RedirectResponse:
        return RedirectResponse(
            "/ui/",
            status_code=307,
            headers={**_SECURITY_HEADERS, "Cache-Control": "no-store"},
        )

    @app.get("/ui/", include_in_schema=False)
    def content_search_index() -> Response:
        return _asset("index.html", "text/html", cache_control="no-cache")

    @app.get("/ui/app.js", include_in_schema=False)
    def content_search_javascript() -> Response:
        return _asset(
            "app.js",
            "text/javascript",
            cache_control="no-cache",
        )

    @app.get("/ui/styles.css", include_in_schema=False)
    def content_search_styles() -> Response:
        return _asset("styles.css", "text/css", cache_control="no-cache")
