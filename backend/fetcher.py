import binascii
import os
import subprocess
from abc import ABC, abstractmethod
from functools import cached_property
from http import HTTPStatus
from pathlib import Path
from typing import Optional

import copr.v3
import koji
import requests
from fastapi import HTTPException

from backend.data import LOG_OUTPUT
from backend.exceptions import FetchError
from backend.spells import get_temporary_dir


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
            baseurl = os.path.dirname(build.source_package.get("url", ""))
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
    def fetch_spec_file(self) -> dict[str, str]:
        build = self.client.build_proxy.get(self.build_id)
        name = build.source_package["name"]
        if self.chroot == "srpm-builds":
            baseurl = os.path.dirname(build.source_package["url"])
        else:
            build_chroot = self.client.build_chroot_proxy.get(
                self.build_id, self.chroot
            )
            baseurl = build_chroot.result_url

        spec_name = f"{name}.spec"
        response = requests.get(f"{baseurl}/{spec_name}")
        response.raise_for_status()
        return {"name": spec_name, "content": response.text}


class KojiProvider(RPMProvider):
    koji_url = "https://koji.fedoraproject.org"
    logs_to_look_for = ["build.log", "root.log", "mock_output.log"]

    def __init__(self, build_or_task_id: int, arch: str) -> None:
        self.build_or_task_id = build_or_task_id
        self.arch = arch
        api_url = "{}/kojihub".format(self.koji_url)
        self.client = koji.ClientSession(api_url)

    @cached_property
    def _is_build_id(self) -> bool:
        try:
            return self.client.getBuild(self.build_or_task_id) is not None
        except koji.GenericError:
            return False

    def _fetch_build_logs_from_koji_api(self) -> list[dict[str, str]]:
        koji_logs = self.client.getBuildLogs(self.build_or_task_id)
        logs = []
        for log in koji_logs:
            if log["dir"] != self.arch:
                continue

            if log["name"] not in self.logs_to_look_for:
                continue

            url = "{}/{}".format(self.koji_url, log["path"])
            response = requests.get(url)
            response.raise_for_status()
            logs.append(
                {
                    "name": log["name"],
                    "content": response.text,
                }
            )

        return logs

    def _fetch_task_logs_from_koji_cli(self, temp_dir: Path) -> list[dict[str, str]]:
        cmd = (
            f"koji download-task --arch={self.arch} --skip=.rpm --logs"
            f" {self.build_or_task_id}"
        )
        subprocess.run(cmd, shell=True, check=True, cwd=temp_dir, capture_output=True)
        logs = []
        for file in temp_dir.iterdir():
            file_parts = file.name.split(".")
            if len(file_parts) > 2:
                file_name_without_arch = f"{file_parts[0]}.{file_parts[2]}"
            else:
                file_name_without_arch = file.name

            if (
                file.is_file()
                and self.arch in file.name
                and file_name_without_arch in self.logs_to_look_for
            ):
                with open(file) as f_log:
                    logs.append(
                        {
                            "name": file_name_without_arch,
                            "content": f_log.read(),
                        }
                    )

        return logs

    @handle_errors
    def fetch_logs(self) -> list[dict[str, str]]:
        if not self._is_build_id:
            with get_temporary_dir() as temp_dir:
                logs = self._fetch_task_logs_from_koji_cli(temp_dir)
        else:
            logs = self._fetch_build_logs_from_koji_api()

        if not logs:
            raise FetchError(
                f"No logs for build #{self.build_or_task_id} and architecture"
                f" {self.arch}"
            )

        return logs

    @staticmethod
    def _get_spec_file_content_from_srpm(
        srpm_path: Path, temp_dir: Path
    ) -> Optional[dict[str, str]]:
        # extract spec file from srpm
        cmd = f"rpm2archive -n < {str(srpm_path)} | tar xf - '*.spec'"
        subprocess.run(cmd, shell=True, check=True, cwd=temp_dir, capture_output=True)
        fst_spec_file = next(temp_dir.glob("*.spec"), None)
        if fst_spec_file is None:
            return None

        with open(fst_spec_file) as spec_file:
            return {"name": fst_spec_file.name, "content": spec_file.read()}

    def _fetch_spec_file_from_task_id(self) -> Optional[dict[str, str]]:
        with get_temporary_dir() as temp_dir:
            cmd = f"koji download-task {self.build_or_task_id}"
            subprocess.run(cmd, shell=True, check=True, cwd=temp_dir, capture_output=True)
            srpm = next(temp_dir.glob("*.src.rpm"), None)
            if srpm is None:
                return None

            return self._get_spec_file_content_from_srpm(srpm, temp_dir)

    def _fetch_spec_file_from_build_id(self) -> Optional[dict[str, str]]:
        koji_build = self.client.getBuild(self.build_or_task_id)
        srpm_url = (
            f"{self.koji_url}/packages/{koji_build['package_name']}"
            f"/{koji_build['version']}/{koji_build['release']}/src/{koji_build['nvr']}"
            ".src.rpm"
        )
        response = requests.get(srpm_url)
        with get_temporary_dir() as temp_dir:
            koji_srpm_path = temp_dir / f"koji_{self.build_or_task_id}.src.rpm"
            with open(koji_srpm_path, "wb") as src_rpm:
                src_rpm.write(response.content)
                return self._get_spec_file_content_from_srpm(koji_srpm_path, temp_dir)

    @handle_errors
    def fetch_spec_file(self) -> dict[str, str]:
        if self._is_build_id:
            fetch_spec_fn = self._fetch_spec_file_from_build_id
        else:
            fetch_spec_fn = self._fetch_spec_file_from_task_id

        spec_dict = fetch_spec_fn()
        if spec_dict is None:
            raise FetchError(
                "No spec file found in koji for build/task id "
                f"#{self.build_or_task_id} and arch {self.arch}"
            )

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
        return {"name": "fake_spec_name.spec", "content": "fake spec file"}


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
