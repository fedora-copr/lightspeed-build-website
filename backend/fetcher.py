import binascii
import os
import re
import subprocess
from abc import ABC, abstractmethod
from functools import cached_property
from http import HTTPStatus
from typing import Optional
from urllib.error import HTTPError

import copr.v3
import koji
import requests
from fastapi import HTTPException

from backend.constants import COPR_RESULT_TEMPLATE
from backend.data import LOG_OUTPUT
from backend.exceptions import FetchError


def handle_errors(func):
    """
    Decorator to catch all client API and network issues and re-raise them as
    HTTPException to API which handles them
    """

    def inner(*args, **kwargs):
        try:
            return func(*args, **kwargs)

        except copr.v3.exceptions.CoprNoResultException or koji.GenericError as ex:
            raise HTTPException(
                status_code=HTTPStatus.BAD_REQUEST, detail=str(ex)
            ) from ex

        except binascii.Error as ex:
            detail = (
                "Unable to decode a log URL from the base64 hash. "
                "How did you get to this page?"
            )
            raise HTTPException(status_code=HTTPStatus.NOT_FOUND, detail=detail) from ex

        except requests.HTTPError as ex:
            detail = (
                f"{ex.response.status_code} {ex.response.reason}\n{ex.response.url}"
            )
            raise HTTPException(
                status_code=ex.response.status_code, detail=detail
            ) from ex

        except subprocess.CalledProcessError as ex:
            # When koji can't find the task.
            if "No such task" in str(ex.stderr):
                raise HTTPException(
                    status_code=HTTPStatus.INTERNAL_SERVER_ERROR, detail=ex.stderr.decode()
                ) from ex

            # Everything else
            if os.environ.get("ENV") != "production":
                raise HTTPException(
                    status_code=HTTPStatus.INTERNAL_SERVER_ERROR,
                    detail=f"stdout: {ex.stdout} stderr: {ex.stderr}"
                ) from ex

            raise HTTPException(
                    status_code=HTTPStatus.INTERNAL_SERVER_ERROR
                ) from ex

    return inner


class Provider(ABC):
    @abstractmethod
    def fetch_logs(self) -> list[dict[str, str]]:
        """
        Fetches logs from a provider with name and content.

        Returns:
            List of dict where each dict contains log name and its content.
        """
        ...


class RPMProvider(Provider):
    """
    Is able to provide spec file on top of the logs.
    """

    @abstractmethod
    def fetch_spec_file(self) -> dict[str, str]:
        """
        Fetches spec file with its content and name.

        Returns:
            Dict which contains spec name and its content.
        """
        ...


class CoprProvider(RPMProvider):
    copr_url = "https://copr.fedorainfracloud.org"

    def __init__(self, build_id: int, chroot: str) -> None:
        self.build_id = build_id
        self.chroot = chroot
        self.client = copr.v3.Client({"copr_url": self.copr_url})

    @handle_errors
    def fetch_logs(self) -> list[dict[str, str]]:
        log_names = ["builder-live.log.gz", "backend.log.gz"]

        if self.chroot == "srpm-builds":
            build = self.client.build_proxy.get(self.build_id)
            baseurl = COPR_RESULT_TEMPLATE.format(build.ownername, build.project_dirname, build.id)
        else:
            build_chroot = self.client.build_chroot_proxy.get(
                self.build_id, self.chroot
            )
            baseurl = build_chroot.result_url
            log_names.append("build.log.gz")

        if not baseurl:
            raise FetchError(
                "There are no results for {}/{}".format(self.build_id, self.chroot)
            )

        logs = []
        for name in log_names:
            url = "{}/{}".format(baseurl, name)
            response = requests.get(url)
            response.raise_for_status()
            logs.append(
                {
                    "name": name.removesuffix(".gz"),
                    "content": response.text,
                }
            )
        return logs

    @handle_errors
    def fetch_spec_file(self) -> Optional[dict[str, str]]:
        build = self.client.build_proxy.get(self.build_id)
        name = build.source_package["name"]
        if self.chroot == "srpm-builds":
            baseurl = COPR_RESULT_TEMPLATE.format(build.ownername, build.project_dirname, build.id)
        else:
            build_chroot = self.client.build_chroot_proxy.get(
                self.build_id, self.chroot
            )
            baseurl = build_chroot.result_url

        spec_name = f"{name}.spec"
        response = requests.get(f"{baseurl}/{spec_name}")
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return {"name": spec_name, "content": response.text}


class KojiProvider(RPMProvider):
    koji_url = "https://koji.fedoraproject.org"
    # checkout.log - for dist-git repo cloning problems
    logs_to_look_for = ["build.log", "root.log", "mock_output.log",
                        "checkout.log"]
    koji_pkgs_url = "https://kojipkgs.fedoraproject.org/work"

    def __init__(self, build_or_task_id: int, arch: str) -> None:
        api_url = "{}/kojihub".format(self.koji_url)
        self.client = koji.ClientSession(api_url)

        self.arch = arch
        self.task_id = None
        self.build_id = None
        # this block detects what we got: is it build or task?
        # failed builds are useless sadly, we will only work with tasks
        try:
            self.build = self.client.getBuild(build_or_task_id)
        except koji.GenericError:
            # great, no need to take care of builds
            self.build = None

        if self.build:
            self.build_id = build_or_task_id
            # it's a build, we need to find the right task now
            root_task_id = self.build['task_id']
            # the response of getTaskDescendents:
            #   {'112162296': [{'arch': 'noarch', 'awaited': False...
            task_descendants = self.client.getTaskDescendents(root_task_id)[str(root_task_id)]
            for task_info in task_descendants:
                if task_info['arch'] == arch \
                        and task_info['method'] in ("buildArch", "buildSRPMFromSCM") \
                        and task_info['state'] == 5:
                    # this is the one and only ring!
                    self.task_id = task_info['id']
                    break
            else:
                raise HTTPException(
                    detail=f"Build {build_or_task_id} doesn't have a failed task for arch {arch}",
                    status_code=HTTPStatus.BAD_REQUEST,
                )
        else:
            self.task_id = build_or_task_id

    @cached_property
    def task_info(self) -> dict:
        task = self.client.getTaskInfo(self.task_id)
        if not task:
            raise HTTPException(
                detail=f"Task {self.task_id} is empty",
                status_code=HTTPStatus.BAD_REQUEST,
            )
        return task

    @cached_property
    def task_request(self) -> list:
        return self.client.getTaskRequest(self.task_id)

    def get_task_request_url(self) -> str:
        """
        We need this:
            'git+https://src.fedoraproject.org/rpms/libphonenumber.git#c88bd3...
        This info is in self.task_request[0]

        But not every task has this though, buildArch
        contains file-based path to SRPM, build and buildFromSCM has it
        """
        task_request_url = self.task_request[0]
        if task_request_url.startswith("git+https"):
            return task_request_url
        parent_task = self.task_info['parent']
        if parent_task:
            task_request_url = self.client.getTaskInfo(parent_task, request=True)["request"][0]
        if task_request_url.startswith("git+https"):
            return task_request_url
        raise HTTPException(
            detail=(
                f"Task {self.task_id}, parent task {parent_task} do not have a link to sources. "
                "We can't locate the specfile."
            ),
            status_code=HTTPStatus.BAD_REQUEST,
        )

    def _fetch_task_logs_from_task_id(self) -> list[dict[str, str]]:
        if self.task_info["arch"] != self.arch:
            raise HTTPException(
                detail=(
                    f"Bad arch of task {self.task_id}: "
                    f'expected: {self.arch} actual: {self.task_info["arch"]}'
                ),
                status_code=HTTPStatus.BAD_REQUEST,
            )

        if self.task_info["method"] not in ("buildArch", "buildSRPMFromSCM"):
            raise HTTPException(
                detail=(
                    f"Task {self.task_id} method is "
                    f"{self.task_info['method']}. "
                    "Please select task with method buildArch."),
                status_code=HTTPStatus.BAD_REQUEST,
            )

        logs = []
        for log_name in self.logs_to_look_for:
            try:
                log_content = self.client.downloadTaskOutput(self.task_id, log_name)
            except koji.GenericError:
                # checkout.log not available for buildArch
                continue
            logs.append(
                {
                    "name": log_name,
                    "content": log_content
                }
            )

        return logs

    @handle_errors
    def fetch_logs(self) -> list[dict[str, str]]:
        logs = self._fetch_task_logs_from_task_id()

        if not logs:
            raise FetchError(
                f"No logs for build {self.build_id} task #{self.task_id} and architecture"
                f" {self.arch}"
            )

        return logs

    @handle_errors
    def fetch_spec_file(self) -> dict[str, str]:
        try:
            request_url = self.get_task_request_url()
            package_name = re.findall(r"/rpms/(.+)\.git", request_url)[0]
            commit_hash = re.findall(r"\.git#(.+)$", request_url)[0]
            spec_url = "https://src.fedoraproject.org/rpms/" \
                f"{package_name}/raw/{commit_hash}/f/{package_name}.spec"
            response = requests.get(spec_url)
            response.raise_for_status()
            spec_dict = {"name": f"{package_name}.spec", "content": response.text}
        except HTTPError as exc:
            raise FetchError(
                "No spec file found in koji for task "
                f"#{self.task_id} and arch {self.arch}."
                f"Reason: {exc}"
            ) from exc
        return spec_dict


class PackitProvider(RPMProvider):
    """
    The `packit_id` is hard to get. Open https://prod.packit.dev/api

    1) Use the `/copr-builds` route. The results contain a dictionary
       named `packit_id_per_chroot`. Use these IDs.

    2) Use the `/koji-builds` route. The results contain `packit_id`. Use these.

    I don't know if it is possible to get the `packit_id` in a WebUI
    """

    packit_api_url = "https://prod.packit.dev/api"

    def __init__(self, packit_id: int) -> None:
        self.packit_id = packit_id
        self.copr_url = f"{self.packit_api_url}/copr-builds/{self.packit_id}"
        self.koji_url = f"{self.packit_api_url}/koji-builds/{self.packit_id}"

    def _get_correct_provider(self) -> CoprProvider | KojiProvider:
        resp = requests.get(self.copr_url)
        if resp.ok:
            build = resp.json()
            return CoprProvider(build["build_id"], build["chroot"])

        resp = requests.get(self.koji_url)
        if not resp.ok:
            raise FetchError(
                f"Couldn't find any build logs for Packit ID #{self.packit_id}."
            )

        build = resp.json()
        task_id = build["task_id"]
        koji_api_url = f"{KojiProvider.koji_url}/kojihub"
        koji_client = koji.ClientSession(koji_api_url)
        arch = koji_client.getTaskInfo(task_id, strict=True).get("arch")
        if arch is None:
            raise FetchError(f"No arch was found for koji task #{task_id}")

        return KojiProvider(task_id, arch)

    @handle_errors
    def fetch_logs(self) -> list[dict[str, str]]:
        return self._get_correct_provider().fetch_logs()

    @handle_errors
    def fetch_spec_file(self) -> dict[str, str]:
        return self._get_correct_provider().fetch_spec_file()


class URLProvider(RPMProvider):
    def __init__(self, url: str) -> None:
        self.url = url

    @handle_errors
    def fetch_logs(self) -> list[dict[str, str]]:
        # TODO Can we recognize a directory listing and show _all_ logs?
        #  also this will allow us to fetch spec files
        response = requests.get(self.url)
        response.raise_for_status()
        if "text/plain" not in response.headers["Content-Type"]:
            raise FetchError(
                "The URL must point to a raw text file. " f"This URL isn't: {self.url}"
            )
        return [
            {
                "name": "Log file",
                "content": response.text,
            }
        ]

    @handle_errors
    def fetch_spec_file(self) -> dict[str, str]:
        # FIXME: Please implement me!
        #  raise NotImplementedError("Please implement me!")
        return None  # type: ignore


class ContainerProvider(Provider):
    """
    Fetching container logs only from URL for now
    """

    def __init__(self, url: str) -> None:
        self.url = url

    @handle_errors
    def fetch_logs(self) -> list[dict[str, str]]:
        # TODO: c&p from url provider for now, integrate with containers better later on
        response = requests.get(self.url)
        response.raise_for_status()
        if "text/plain" not in response.headers["Content-Type"]:
            raise FetchError(
                "The URL must point to a raw text file. " f"This URL isn't: {self.url}"
            )
        return [
            {
                "name": "Container log",
                "content": response.text,
            }
        ]


def fetch_debug_logs():
    return [
        {
            "name": "fake-builder-live.log",
            "content": LOG_OUTPUT,
        }
    ]
