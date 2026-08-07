"""Typed application errors mapped to HTTP responses by the API layer."""
from __future__ import annotations


class HaraldError(Exception):
    """Base error. status is the HTTP status the API should return."""

    status = 500
    code = "internal_error"

    def __init__(self, message: str, detail: dict | None = None):
        super().__init__(message)
        self.message = message
        self.detail = detail or {}

    def to_dict(self) -> dict:
        return {"error": self.code, "message": self.message, "detail": self.detail}


class NotFound(HaraldError):
    status = 404
    code = "not_found"


class ValidationFailed(HaraldError):
    status = 400
    code = "validation_failed"


class Conflict(HaraldError):
    status = 409
    code = "conflict"


class Forbidden(HaraldError):
    status = 403
    code = "forbidden"


class Unauthorized(HaraldError):
    status = 401
    code = "unauthorized"


class UpstreamError(HaraldError):
    """A dependency (OCI Generative AI, LibreOffice) failed after retries."""

    status = 502
    code = "upstream_error"
