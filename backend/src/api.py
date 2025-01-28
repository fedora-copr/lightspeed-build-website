import json
import logging
import os
import tempfile
from asyncio import create_task
from base64 import b64decode
from datetime import datetime
from http import HTTPStatus
from pathlib import Path
from typing import Optional

import requests

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    FileResponse,
    RedirectResponse,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from starlette.background import BackgroundTask
from starlette.exceptions import HTTPException

from src.constants import (
    COPR_BUILD_URL,
    KOJI_BUILD_URL,
    FEEDBACK_DIR,
    REVIEWS_DIR,
    SERVER_URL,
    BuildIdTitleEnum,
    ProvidersEnum,
)
from src.fetcher import (
    ContainerProvider,
    CoprProvider,
    KojiProvider,
    PackitProvider,
    URLProvider,
    fetch_debug_logs,
)
from src.schema import (
    ContributeResponseSchema,
    FeedbackInputSchema,
    FeedbackSchema,
    FeedbackLogSchema,
    schema_inp_to_out,
)
from src.spells import make_tar, find_file_by_name
from src.store import Storator3000
from src.exceptions import NoDataFound

logger = logging.getLogger(__name__)

app = FastAPI()

# TODO: use absolute path perhaps?
template_dir = "../../frontend/public"
app.mount("/static", StaticFiles(directory=template_dir), name="static")
# blame scarlette for not being able to mount directories recursively
for root, directories, _ in os.walk(template_dir):
    for directory in directories:
        app.mount(
            f"/{directory}",
            StaticFiles(directory=os.path.join(root, directory)),
            name=directory,
        )

templates = Jinja2Templates(directory=template_dir)
template_response = templates.TemplateResponse


@app.exception_handler(Exception)
@app.exception_handler(HTTPException)
@app.exception_handler(RequestValidationError)
def _custom_http_exception_handler(
    request: Request, exc: HTTPException | RequestValidationError | Exception
) -> JSONResponse:
    if isinstance(exc, HTTPException):
        status_code = exc.status_code
    elif isinstance(exc, RequestValidationError):
        status_code = HTTPStatus.UNPROCESSABLE_ENTITY
    else:
        status_code = HTTPStatus.INTERNAL_SERVER_ERROR

    if isinstance(exc, HTTPException):
        description = exc.detail
    else:
        description = str(exc)

    return JSONResponse(
        status_code=status_code,
        content={
            "error": f"Server error: {status_code}",
            "description": description,
        },
    )


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    return template_response("index.html", {"request": request})


@app.get("/contribute/{args:path}", response_class=HTMLResponse)
def contribute(request: Request, args: str):
    # TODO: once ready for production, drop path and use proper paths
    _ = args
    mapping = {"name": "app-contribute", "request": request}
    return template_response("app.html", mapping)


@app.get("/documentation", response_class=HTMLResponse)
def documentation(request: Request):
    return template_response("documentation.html", {"request": request})


@app.get("/review", response_class=RedirectResponse)
def review_redirect():
    """
    Redirect to a review URL that contains some result ID
    """
    result = Storator3000.get_random()
    return f"/review/{result.stem}"


@app.get("/review/{result_id}", response_class=HTMLResponse)
def review(request: Request):
    mapping = {"name": "app-review", "request": request}
    return template_response("app.html", mapping)


@app.get("/explain", response_class=HTMLResponse)
def explain(request: Request, url: Optional[str] = None):
    # the URL is parsed by frontend, but having it mentioned here
    # means we have it documented
    _ = url
    mapping = {"name": "app-prompt", "request": request}
    return template_response("app.html", mapping)


# Frontend API routes
# These are called from JavaScript to asynchronously fetch or post data


@app.get("/frontend/contribute/copr/{build_id}/{chroot}")
@app.get("/frontend/contribute/koji/{build_id}/{chroot}")
def get_build_logs_with_chroot(
    request: Request, build_id: int, chroot: str
) -> ContributeResponseSchema:
    provider_name = request.url.path.lstrip("/").split("/")[2]
    prov_kls = CoprProvider if provider_name == ProvidersEnum.copr else KojiProvider
    provider = prov_kls(build_id, chroot)
    if provider_name == ProvidersEnum.copr:
        build_title = BuildIdTitleEnum.copr
        build_url = COPR_BUILD_URL.format(build_id)
    else:
        build_title = BuildIdTitleEnum.koji
        build_url = KOJI_BUILD_URL.format(build_id)

    return ContributeResponseSchema(
        build_id=build_id,
        build_id_title=build_title,
        build_url=build_url,
        logs=provider.fetch_logs(),
        spec_file=provider.fetch_spec_file(),
    )


@app.get("/frontend/contribute/packit/{packit_id}")
def get_packit_build_logs(packit_id: int) -> ContributeResponseSchema:
    provider = PackitProvider(packit_id)
    return ContributeResponseSchema(
        build_id=packit_id,
        build_id_title=BuildIdTitleEnum.packit,
        build_url=provider.url,
        logs=provider.fetch_logs(),
        spec_file=provider.fetch_spec_file(),
    )


@app.get("/frontend/contribute/url/{base64}")
def get_build_logs_from_url(base64: str) -> ContributeResponseSchema:
    build_url = b64decode(base64).decode("utf-8")
    provider = URLProvider(build_url)
    return ContributeResponseSchema(
        build_id=None,
        build_id_title=BuildIdTitleEnum.url,
        build_url=build_url,
        logs=provider.fetch_logs(),
        spec_file=provider.fetch_spec_file(),
    )


@app.get("/frontend/contribute/container/{base64}")
def get_logs_from_container(base64: str) -> ContributeResponseSchema:
    build_url = b64decode(base64).decode("utf-8")
    provider = ContainerProvider(build_url)
    return ContributeResponseSchema(
        build_id=None,
        build_id_title=BuildIdTitleEnum.container,
        build_url=build_url,
        logs=provider.fetch_logs(),
    )


# TODO: no response checking here, it will be deleted anyway
@app.get("/frontend/contribute/debug")
def get_debug_build_logs():
    return {
        "build_id": 123456,
        "build_id_title": BuildIdTitleEnum.debug,
        "build_url": "#",
        "logs": fetch_debug_logs(),
        "spec_file": "fake spec file",
    }


# TODO: delete this once in production
@app.post("/frontend/contribute/debug")
def frontend_debug_contribute():
    logger.info("Debug data were fakely stored.")
    return {"status": "ok"}


# TODO: some reasonable ok response would be better
class OkResponse(BaseModel):
    status: str = "ok"


def _store_data_for_providers(
    feedback_input: FeedbackInputSchema, provider: str, id_: int | str, *args
) -> OkResponse:
    storator = Storator3000(ProvidersEnum[provider], id_)

    if provider == ProvidersEnum.container:
        result_to_store = schema_inp_to_out(feedback_input, is_with_spec=False)
    else:
        result_to_store = schema_inp_to_out(feedback_input)

    storator.store(result_to_store)
    if len(args) > 0:
        rest = f"/{args[0]}"
    else:
        rest = ""

    logger.info("Submitted data for {%s}: #{%s}{%s}", provider, id_, rest)
    return OkResponse()


@app.post("/frontend/contribute/copr/{build_id}/{chroot}")
def contribute_review_copr(
    feedback_input: FeedbackInputSchema, build_id: int, chroot: str
) -> OkResponse:
    return _store_data_for_providers(
        feedback_input, ProvidersEnum.copr, build_id, chroot
    )


@app.post("/frontend/contribute/koji/{build_id}/{arch}")
def contribute_review_koji(
    feedback_input: FeedbackInputSchema, build_id: int, arch: str
) -> OkResponse:
    return _store_data_for_providers(feedback_input, ProvidersEnum.koji, build_id, arch)


@app.post("/frontend/contribute/packit/{packit_id}")
def contribute_review_packit(
    feedback_input: FeedbackInputSchema, packit_id: int
) -> OkResponse:
    return _store_data_for_providers(feedback_input, ProvidersEnum.packit, packit_id)


@app.post("/frontend/contribute/upload")
def contribute_upload_file(feedback_input: FeedbackInputSchema) -> OkResponse:
    dirname = int(datetime.now().timestamp())
    return _store_data_for_providers(feedback_input, ProvidersEnum.upload, dirname)


@app.post("/frontend/contribute/url/{url}")
def contribute_review_url(feedback_input: FeedbackInputSchema, url: str) -> OkResponse:
    return _store_data_for_providers(feedback_input, ProvidersEnum.url, url)


@app.post("/frontend/contribute/container/{url}")
def contribute_review_container_logs(
    feedback_input: FeedbackInputSchema, url: str
) -> OkResponse:
    return _store_data_for_providers(feedback_input, ProvidersEnum.container, url)


@app.get("/frontend/review/{result_id}")
def frontend_review_random(result_id):
    if result_id == "random":
        feedback_file = Storator3000.get_random()
    else:
        feedback_file = Storator3000.get_by_id(result_id)

    if not feedback_file:
        raise NoDataFound(f"No result with ID {result_id}")

    with open(feedback_file) as fp:
        content = json.loads(fp.read())
        return FeedbackSchema(**content).dict() \
            | {"id": feedback_file.name.rstrip(".json")}


@app.post("/frontend/explain/")
async def frontend_explain_post(request: Request):
    """Communicate with the logdetective server and process data.

    :returns: {
        "explanation": str,
        "reasoning": {"snippet": str, "comment": str},
        "certainty": int,
        "log": {"name": str, "content": str}
    }
    """
    data = await request.json()
    log_url = data['prompt']

    logger.info("Asking server to analyze log '%s'", log_url)
    data = {"url": log_url}
    headers = {"Content-Type": "application/json"}
    server_url = f"{SERVER_URL}/analyze/staged"

    # download log_file when waiting for logdetective server processing
    download_log_task = create_task(_download_log_content(log_url))

    try:
        response = requests.post(server_url, headers=headers, data=json.dumps(data), timeout=1800)
    except (requests.ConnectionError, requests.Timeout) as ex:
        raise HTTPException(
            status_code=408, detail=str(ex)
        ) from ex

    try:
        logger.debug("headers: %s data: %s", response.request.headers, response.request.body)
        response.raise_for_status()
    except requests.HTTPError as ex:
        detail = (
            f"{ex.response.status_code} {ex.response.reason}\n{ex.response.url}"
        )
        raise HTTPException(
            status_code=ex.response.status_code, detail=detail
        ) from ex

    result = _process_server_data(response.content)

    log = {
        "name": f"{log_url}",
        "content": await download_log_task
    }

    # add missing data from the original log
    result["log"] = log

    print(result)
    return result


def _process_server_data(data):
    """Process data received from logdetective server.

    Return them in format:
    {
        "explanation": str,
        "reasoning": {"snippet": str, "comment": str},
        "certainty": int,
    }
    """
    try:
        r_data = json.loads(data)
    except json.JSONDecodeError as ex:
        raise HTTPException(
            status_code=408, detail="Received invalid data from server"
        ) from ex

    explanation = r_data["explanation"]["choices"][0]["text"]
    reasoning = []

    for snippet in r_data["snippets"]:
        reasoning.append({
            "snippet": snippet["snippet"],
            "comment": snippet["comment"]["choices"][0]["text"]
        })

    try:
        certainty = int(r_data["response_certainty"])
    except ValueError as ex:
        raise HTTPException(
            status_code=408, detail="Wrong certainty data received from the server"
        ) from ex

    return {
        "explanation": explanation,
        "reasoning": reasoning,
        "certainty": certainty,
    }


async def _download_log_content(url):
    """Download content of the log file and returns it."""

    try:
        response = requests.get(url, timeout=600)
    except (requests.ConnectionError, requests.Timeout, requests.RequestException) as ex:
        raise HTTPException(
            status_code=408, detail=str(ex)
        ) from ex
    try:
        response.raise_for_status()
    except requests.HTTPError as ex:
        detail = (
            f"{ex.response.status_code} {ex.response.reason}\n{ex.response.url}"
        )
        raise HTTPException(
            status_code=ex.response.status_code, detail=detail
        ) from ex

    return response.text


def _get_text_from_feedback(item: dict) -> str:
    if item["vote"] != 1:
        return ""

    return item["text"]


def _parse_logs(logs_orig: dict[str, FeedbackLogSchema], review_snippets: list[dict]) -> None:
    for name, item in logs_orig.items():
        item.snippets = []
        for snippet in review_snippets:
            if snippet["file"] == name and snippet["vote"] == 1:
                # mypy can't see this far and hinting to it is messy
                item.snippets.append(snippet)  # type: ignore[arg-type]


def _parse_feedback(review_d: dict, origin_id: int) -> FeedbackSchema:
    original_file_path = find_file_by_name(f"{origin_id}.json", Path(FEEDBACK_DIR))
    if original_file_path is None:
        raise HTTPException(
            status_code=HTTPStatus.NOT_FOUND,
            detail=f"Original feedback file for ID {origin_id} not found",
        )

    with open(original_file_path, encoding="utf-8") as fp:
        original_content = json.load(fp)
        schema = FeedbackSchema(**original_content)
        # no reason to store username
        schema.username = None
        schema.fail_reason = _get_text_from_feedback(review_d["fail_reason"])
        schema.how_to_fix = _get_text_from_feedback(review_d["how_to_fix"])
        _parse_logs(schema.logs, review_d["snippets"])
        return schema.dict(exclude_unset=True)


@app.post("/frontend/review")
async def store_random_review(feedback_input: Request) -> OkResponse:
    """
    Store review from frontend.
    """
    # TODO: temporary silly solution until database is created
    #  (missing provider but we can dig it from original feedback file)
    reviews_dir = Path(REVIEWS_DIR)
    parsed_reviews_dir = reviews_dir / "parsed"
    parsed_reviews_dir.mkdir(parents=True, exist_ok=True)
    content = await feedback_input.json()
    original_file_id = content.pop("id")
    # avoid duplicates - same ID can be reviewed multiple times
    file_name = f"{original_file_id}-{int(datetime.now().timestamp())}"
    with open(reviews_dir / f"{file_name}.json", "w", encoding="utf-8") as fp:
        json.dump(content | {"id": original_file_id}, fp, indent=4)

    with open(parsed_reviews_dir / f"{file_name}.json", "w", encoding="utf-8") as fp:
        json.dump(
            _parse_feedback(content, original_file_id) | {"id": original_file_id},
            fp,
            indent=4
        )

    return OkResponse()


@app.get("/download")
def download_results():
    """
    Download all results we have as a tar.gz archive.

    We create a temporary directory and store the archive in there.
    After the client browser gets the whole file, we delete our temp
    file and directory using the cleanup() method as a background task.

    This function was rewritten from an async implementation that stopped working
    (probably after an update to fastapi and starlette).
    https://github.com/fedora-copr/log-detective-website/issues/157
    """
    if not FEEDBACK_DIR:
        raise HTTPException(status_code=HTTPStatus.NOT_FOUND, detail="No data found")

    tmp_dir = Path(tempfile.mkdtemp())
    tar_name = f"results-{int(datetime.now().timestamp())}.tar.gz"
    tar_path = make_tar(tar_name, [Path(FEEDBACK_DIR), Path(REVIEWS_DIR)], tmp_dir)

    def cleanup():
        os.unlink(tar_path)
        os.rmdir(tmp_dir)
    # https://fastapi.tiangolo.com/advanced/custom-response/?h=fileresponse#fileresponse
    # https://fastapi.tiangolo.com/reference/background/?h=background
    return FileResponse(
        tar_path,
        filename=tar_name,
        media_type="application/x-tar",
        background=BackgroundTask(cleanup))


@app.get("/stats")
def get_report_stats() -> dict:
    return Storator3000.get_stats()
