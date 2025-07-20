"""Ingest endpoint for the API."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from prometheus_client import Counter

from gitingest.config import TMP_BASE_PATH
from gitingest.utils.s3_utils import get_s3_url_for_ingest_id, is_s3_enabled
from server.models import IngestErrorResponse
from server.routers_utils import COMMON_INGEST_RESPONSES, _perform_ingestion
from server.server_config import MAX_DISPLAY_SIZE
from server.server_utils import limiter

ingest_counter = Counter("gitingest_ingest_total", "Number of ingests", ["status", "url"])

router = APIRouter()


@router.post("/api/ingest", responses=COMMON_INGEST_RESPONSES)
@limiter.limit("10/minute")
async def api_ingest(
    request: Request,  # noqa: ARG001 (unused-function-argument) # pylint: disable=unused-argument
    ingest_data: dict[str, Any],
) -> JSONResponse:
    """Ingest a Git repository and return processed content.

    **This endpoint processes a Git repository by cloning it, analyzing its structure,**
    and returning a summary with the repository's content. The response includes
    file tree structure, processed content, and metadata about the ingestion.

    **Parameters**

    - **ingest_request** (`IngestRequest`): Pydantic model containing ingestion parameters

    **Returns**

    - **JSONResponse**: Success response with ingestion results or error response with appropriate HTTP status code

    """

    # Extract and validate data from dictionary
    def _validate_input_text(text: str) -> None:
        if not text:
            msg = "input_text cannot be empty"
            raise ValueError(msg)

    try:
        input_text = ingest_data.get("input_text", "").strip()
        _validate_input_text(input_text)

        max_file_size = ingest_data.get("max_file_size", 243)
        if isinstance(max_file_size, str):
            max_file_size = int(max_file_size)

        pattern_type = ingest_data.get("pattern_type", "exclude")
        pattern = ingest_data.get("pattern", "").strip()
        token = ingest_data.get("token") or None

    except (ValueError, TypeError) as e:
        error_response = IngestErrorResponse(error=f"Invalid request data: {e}")
        return JSONResponse(status_code=status.HTTP_400_BAD_REQUEST, content=error_response.model_dump())

    response = await _perform_ingestion(
        input_text=input_text,
        max_file_size=max_file_size,
        pattern_type=pattern_type,
        pattern=pattern,
        token=token,
    )
    # limit URL to 255 characters
    ingest_counter.labels(status=response.status_code, url=input_text[:255]).inc()
    return response


@router.get("/api/{user}/{repository}", responses=COMMON_INGEST_RESPONSES)
@limiter.limit("10/minute")
async def api_ingest_get(
    request: Request,  # noqa: ARG001 (unused-function-argument) # pylint: disable=unused-argument
    user: str,
    repository: str,
    max_file_size: int = MAX_DISPLAY_SIZE,
    pattern_type: str = "exclude",
    pattern: str = "",
    token: str = "",
) -> JSONResponse:
    """Ingest a GitHub repository via GET and return processed content.

    **This endpoint processes a GitHub repository by analyzing its structure and returning a summary**
    with the repository's content. The response includes file tree structure, processed content, and
    metadata about the ingestion. All ingestion parameters are optional and can be provided as query parameters.

    **Path Parameters**
    - **user** (`str`): GitHub username or organization
    - **repository** (`str`): GitHub repository name

    **Query Parameters**
    - **max_file_size** (`int`, optional): Maximum file size to include in the digest (default: 50 KB)
    - **pattern_type** (`str`, optional): Type of pattern to use ("include" or "exclude", default: "exclude")
    - **pattern** (`str`, optional): Pattern to include or exclude in the query (default: "")
    - **token** (`str`, optional): GitHub personal access token for private repositories (default: "")

    **Returns**
    - **JSONResponse**: Success response with ingestion results or error response with appropriate HTTP status code
    """
    response = await _perform_ingestion(
        input_text=f"{user}/{repository}",
        max_file_size=max_file_size,
        pattern_type=pattern_type,
        pattern=pattern,
        token=token or None,
    )
    # limit URL to 255 characters
    ingest_counter.labels(status=response.status_code, url=f"{user}/{repository}"[:255]).inc()
    return response


@router.get("/api/download/file/{ingest_id}", response_model=None)
async def download_ingest(ingest_id: str) -> RedirectResponse | FileResponse:
    """Download the first text file produced for an ingest ID.

    **This endpoint retrieves the first ``*.txt`` file produced during the ingestion process**
    and returns it as a downloadable file. If S3 is enabled and the file is stored in S3,
    it redirects to the S3 URL. Otherwise, it serves the local file.

    **Parameters**

    - **ingest_id** (`str`): Identifier that the ingest step emitted

    **Returns**

    - **RedirectResponse**: Redirect to S3 URL if S3 is enabled and file exists in S3
    - **FileResponse**: Streamed response with media type ``text/plain`` for local files

    **Raises**

    - **HTTPException**: **404** - digest directory is missing or contains no ``*.txt`` file
    - **HTTPException**: **403** - the process lacks permission to read the directory or file

    """
    # Check if S3 is enabled and file exists in S3
    if is_s3_enabled():
        s3_url = get_s3_url_for_ingest_id(ingest_id)
        if s3_url:
            return RedirectResponse(url=s3_url, status_code=302)

    # Fall back to local file serving
    # Normalize and validate the directory path
    directory = (TMP_BASE_PATH / ingest_id).resolve()
    if not str(directory).startswith(str(TMP_BASE_PATH.resolve())):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=f"Invalid ingest ID: {ingest_id!r}")

    if not directory.is_dir():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Digest {ingest_id!r} not found")

    try:
        first_txt_file = next(directory.glob("*.txt"))
    except StopIteration as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No .txt file found for digest {ingest_id!r}",
        ) from exc

    try:
        return FileResponse(path=first_txt_file, media_type="text/plain", filename=first_txt_file.name)
    except PermissionError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Permission denied for {first_txt_file}",
        ) from exc
