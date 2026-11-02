"""Shared validation and download helpers for source RPM preparation."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

MIB = 1024 * 1024
HOSTS = {"api.github.com", "codeload.github.com"}
VERSION = re.compile(r"v?(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)\Z")
SAFE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.+-]{0,200}\Z")


class InvalidRepository(RuntimeError):
    """Fail closed without publishing invalid inputs."""


def require(condition, message):
    if not condition:
        raise InvalidRepository(message)


def run(args, *, env=None, input=None):
    result = subprocess.run(
        [str(a) for a in args],
        input=input,
        text=True,
        capture_output=True,
        env=env,
        timeout=600,
        check=False,
    )
    require(
        result.returncode == 0,
        f"{args[0]} failed ({result.returncode}): {result.stdout}\n{result.stderr}",
    )
    return result.stdout


def sha256(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def verify_checksum(path, expected):
    require(
        re.fullmatch(r"[0-9a-f]{64}", expected or "") and sha256(path) == expected,
        f"SHA-256 mismatch: {path.name}",
    )


def canonical(value):
    return json.dumps(value, sort_keys=True, indent=2) + "\n"


def safe_name(name):
    require(isinstance(name, str) and SAFE_NAME.fullmatch(name), f"Unsafe filename: {name!r}")
    return name


def validate_github_repository(repository):
    require(
        re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository or ""),
        "Expected a GitHub OWNER/REPOSITORY",
    )


def validate_url(url, hosts):
    parsed = urllib.parse.urlsplit(url)
    require(
        parsed.scheme == "https"
        and parsed.hostname in hosts
        and not parsed.username
        and not parsed.password
        and parsed.port in (None, 443),
        "Untrusted HTTPS URL",
    )
    return url


class SafeRedirect(urllib.request.HTTPRedirectHandler):
    def __init__(self, hosts):
        self.hosts = hosts

    def redirect_request(self, request, fp, code, msg, headers, newurl):
        validate_url(newurl, self.hosts)
        redirect = super().redirect_request(request, fp, code, msg, headers, newurl)
        if redirect:
            redirect.remove_header("Authorization")
        return redirect


class Network:
    def __init__(self):
        self.token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")

    def download(self, url, target, *, limit=100 * MIB, size=None, hosts=None, api=False):
        hosts = hosts or HOSTS
        validate_url(url, hosts)
        headers = {"User-Agent": "autobrr-rpms", "Accept": "application/octet-stream"}
        if api:
            require(urllib.parse.urlsplit(url).hostname == "api.github.com", "API token boundary")
            headers.update(
                {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
            )
            if self.token:
                headers["Authorization"] = f"Bearer {self.token}"
        target.parent.mkdir(parents=True, exist_ok=True)
        for attempt in range(3):
            try:
                opener = urllib.request.build_opener(SafeRedirect(hosts))
                with opener.open(
                    urllib.request.Request(url, headers=headers), timeout=90
                ) as response:
                    require(response.status == 200, "Unexpected download status")
                    total = 0
                    with target.open("wb") as stream:
                        while chunk := response.read(MIB):
                            total += len(chunk)
                            require(total <= limit, "Download exceeds size limit")
                            stream.write(chunk)
                    require(total > 0 and (size is None or total == size), "Incomplete download")
                    return
            except urllib.error.HTTPError as exc:
                if exc.code not in (429, 500, 502, 503, 504) or attempt == 2:
                    raise
            except (urllib.error.URLError, TimeoutError, ConnectionError):
                if attempt == 2:
                    raise
            time.sleep(2 ** (attempt + 1))

    def json(self, url):
        with tempfile.TemporaryDirectory(prefix="autobrr-api-") as directory:
            target = Path(directory) / "response.json"
            self.download(url, target, limit=20 * MIB, api=True)
            return json.loads(target.read_text())


def select_releases(releases, retain=1):
    stable = [r for r in releases if not r.get("draft") and not r.get("prerelease")]
    require(stable, "No stable upstream releases")
    for release in stable:
        require(VERSION.fullmatch(release["tag_name"]), "Unexpected stable version format")
    stable.sort(
        key=lambda r: tuple(map(int, VERSION.fullmatch(r["tag_name"]).groups())), reverse=True
    )
    versions = [r["tag_name"].removeprefix("v") for r in stable]
    require(len(versions) == len(set(versions)), "Duplicate stable version")
    return stable[:retain]
