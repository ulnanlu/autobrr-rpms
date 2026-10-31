#!/usr/bin/env python3
"""Verify official upstream RPMs, independently sign, and stage an atomic Pages site.

No network response is executed and no application is rebuilt. Publishing is a
separate GitHub Pages deployment job, only reached after all validation succeeds.
"""

from __future__ import annotations

import argparse
import base64
import gzip
import hashlib
import html
import json
import logging
import os
import re
import subprocess
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
UPSTREAM = "autobrr/mkbrr"
API = f"https://api.github.com/repos/{UPSTREAM}"
MIB = 1024 * 1024
MAX_SITE = 800 * MIB
VERSION = re.compile(r"v?(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)\Z")
SAFE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.+-]{0,200}\Z")
ARCHES = {
    "amd64": "x86_64",
    "x86_64": "x86_64",
    "arm64": "aarch64",
    "aarch64": "aarch64",
    "armv6": "armv6hl",
    "armv7": "armv7hl",
    "386": "i386",
    "ppc64le": "ppc64le",
    "s390x": "s390x",
    "riscv64": "riscv64",
}
HOSTS = {
    "api.github.com",
    "github.com",
    "codeload.github.com",
    "release-assets.githubusercontent.com",
    "objects.githubusercontent.com",
    "proxy.golang.org",
    "storage.googleapis.com",
}
LOG = logging.getLogger("mkbrr-repository")


class InvalidRepository(RuntimeError):
    """Fail closed: nothing from this run may be deployed."""


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


def canonical(value):
    return json.dumps(value, sort_keys=True, indent=2) + "\n"


def safe_name(name):
    require(isinstance(name, str) and SAFE_NAME.fullmatch(name), f"Unsafe filename: {name!r}")
    return name


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
        headers = {"User-Agent": "mkbrr-community-repository", "Accept": "application/octet-stream"}
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
        with tempfile.TemporaryDirectory(prefix="mkbrr-api-") as directory:
            target = Path(directory) / "response.json"
            self.download(url, target, limit=20 * MIB, api=True)
            return json.loads(target.read_text())

    def releases(self):
        releases = []
        for page in range(1, 21):
            batch = self.json(f"{API}/releases?per_page=100&page={page}")
            require(isinstance(batch, list), "Invalid Releases API response")
            releases.extend(batch)
            if len(batch) < 100:
                return releases
        raise InvalidRepository("Release pagination limit reached; review before publishing")


def select_releases(releases, retain=3):
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


def discover(release):
    version = release["tag_name"].removeprefix("v")
    assets = release["assets"]
    names = [safe_name(a["name"]) for a in assets]
    require(len(names) == len(set(names)), "Duplicate release asset filename")
    rpms = []
    for asset in assets:
        name = asset["name"]
        if not name.lower().endswith(".rpm"):
            continue
        match = re.fullmatch(rf"mkbrr_{re.escape(version)}_linux_([a-zA-Z0-9_]+)\.rpm", name)
        require(match and match[1] in ARCHES, f"Unexpected RPM asset: {name}")
        require(
            asset.get("state") == "uploaded" and 0 < asset["size"] <= 100 * MIB,
            f"Incomplete RPM asset: {name}",
        )
        rpms.append({**asset, "rpm_arch": ARCHES[match[1]]})
    require(rpms, f"No RPM assets for {release['tag_name']}; refusing an older-release fallback")
    require(len({a["rpm_arch"] for a in rpms}) == len(rpms), "Duplicate RPM architecture")
    manifests = [a for a in assets if "checksum" in a["name"].lower()]
    expected = f"mkbrr_{version}_checksums.txt"
    require(
        len(manifests) == 1
        and manifests[0]["name"] == expected
        and manifests[0].get("state") == "uploaded",
        "Missing or ambiguous checksum manifest",
    )
    return sorted(rpms, key=lambda a: a["name"]), manifests[0]


def parse_checksums(text):
    checksums = {}
    for line in text.splitlines():
        if not line.strip():
            continue
        match = re.fullmatch(r"([a-fA-F0-9]{64}) [ *](.+)", line)
        require(match, "Malformed SHA-256 checksum manifest")
        name = safe_name(match[2])
        require(name not in checksums, "Duplicate checksum manifest entry")
        checksums[name] = match[1].lower()
    require(checksums, "Empty checksum manifest")
    return checksums


def verify_checksum(path, expected):
    require(
        re.fullmatch(r"[0-9a-f]{64}", expected or "") and sha256(path) == expected,
        f"SHA-256 mismatch: {path.name}",
    )


def rpm_metadata(path, version, architecture):
    integrity = run(["rpmkeys", "--checksig", "--verbose", "--nosignature", path])
    require(
        "digest: OK" in integrity and not re.search(r"NOT OK|BAD|NOKEY", integrity),
        "RPM integrity check failed",
    )
    query = "%{NAME}\t%{VERSION}\t%{RELEASE}\t%{ARCH}\t%{EPOCHNUM}\n"
    fields = run(["rpm", "-qp", "--qf", query, path]).strip().split("\t")
    require(len(fields) == 5, "Invalid RPM header")
    name, actual_version, release, arch, epoch = fields
    require(
        name == "mkbrr"
        and actual_version == version
        and arch == architecture
        and epoch == "0"
        and re.fullmatch(r"[1-9][0-9]*", release),
        f"Unexpected RPM metadata: {fields}",
    )
    files = run(["rpm", "-qpl", path]).splitlines()
    require(
        "/usr/bin/mkbrr" in files
        and all(
            (
                f == "/usr/bin/mkbrr"
                or f.startswith(("/usr/share/doc/mkbrr/", "/usr/share/licenses/mkbrr/"))
            )
            and ".." not in PurePosixPath(f).parts
            for f in files
        ),
        "Unexpected RPM file layout",
    )
    require(not run(["rpm", "-qp", "--scripts", path]).strip(), "Unexpected RPM scriptlets")
    signature = run(["rpm", "-qp", "--qf", "%{RSAHEADER:pgpsig}\n%{DSAHEADER:pgpsig}\n", path])
    return {
        "name": name,
        "version": actual_version,
        "release": release,
        "arch": arch,
        "epoch": epoch,
        "original_signature": signature.strip(),
    }


class Signer:
    """Isolated GnuPG and RPM trust databases; no host keyring is used."""

    def __init__(self, directory, *, dry_run=False):
        self.home = directory / "gnupg"
        self.home.mkdir(mode=0o700)
        self.db = directory / "rpmdb"
        self.db.mkdir()
        self.passfile = directory / "passphrase"
        self.env = {
            k: v
            for k, v in os.environ.items()
            if k not in ("RPM_SIGNING_KEY", "RPM_SIGNING_PASSPHRASE")
        }
        self.env.update({"GNUPGHOME": str(self.home), "LC_ALL": "C"})
        if dry_run:
            self.passfile.write_text("disposable-test-passphrase")
            self.passfile.chmod(0o600)
            self.gpg(
                "--pinentry-mode",
                "loopback",
                "--passphrase-file",
                self.passfile,
                "--quick-generate-key",
                "DISPOSABLE TEST ONLY <test@example.invalid>",
                "rsa3072",
                "sign",
                "1d",
            )
            expected = None
        else:
            private = os.environ.get("RPM_SIGNING_KEY", "")
            expected = os.environ.get("RPM_SIGNING_FINGERPRINT", "").upper()
            require(
                private and re.fullmatch(r"[A-F0-9]{40}", expected or ""),
                "Missing signing configuration. Set RPM_SIGNING_KEY secret, "
                "RPM_SIGNING_PASSPHRASE secret and RPM_SIGNING_FINGERPRINT variable; "
                "commit config/signing-key.asc. See README.md#signing-key-setup",
            )
            self.passfile.write_text(os.environ.get("RPM_SIGNING_PASSPHRASE", ""))
            self.passfile.chmod(0o600)
            self.gpg("--import", input=private)
        listing = self.gpg("--with-colons", "--list-secret-keys")
        primaries = [line for line in listing.splitlines() if line.startswith("sec:")]
        require(len(primaries) == 1, "Exactly one dedicated primary signing key required")
        primary = primaries[0].split(":")
        require(
            primary[1] not in ("r", "e", "d")
            and primary[3] == "1"
            and int(primary[2]) >= 3072
            and "s" in primary[11],
            "Require a usable RSA >=3072 signing key",
        )
        self.fingerprint = next(
            line.split(":")[9] for line in listing.splitlines() if line.startswith("fpr:")
        )
        require(not expected or self.fingerprint == expected, "Signing key fingerprint mismatch")
        # Pin the exact primary key, rather than letting GPG choose a subkey.
        self.key = self.fingerprint + "!"
        self.public = self.gpg("--armor", "--export", self.fingerprint)
        self.public_file = directory / "public.asc"
        self.public_file.write_text(self.public)
        run(["rpmkeys", "--dbpath", self.db, "--import", self.public_file], env=self.env)
        if not dry_run:
            committed = ROOT / "config/signing-key.asc"
            require(committed.is_file(), "Commit config/signing-key.asc before publishing")
            require(key_fingerprint(committed) == self.fingerprint, "Committed public key mismatch")
        wrapper = directory / "gpg-sign"
        # rpmsign's macro language cannot safely represent arbitrary user paths.
        require(re.fullmatch(r"[A-Za-z0-9_./-]+", str(directory)), "Unsafe signing workspace path")
        wrapper.write_text(
            "#!/bin/sh\nset -eu\nexec /usr/bin/gpg --batch --no-tty --pinentry-mode loopback "
            f'--passphrase-file "{self.passfile}" "$@"\n'
        )
        wrapper.chmod(0o700)
        self.wrapper = wrapper

    def gpg(self, *args, input=None):
        return run(["gpg", "--batch", "--no-tty", *args], env=self.env, input=input)

    def sign_rpm(self, path):
        run(
            [
                "rpmsign",
                "--define",
                "%_signature gpg",
                "--define",
                f"%_gpg_name {self.key}",
                "--define",
                f"%__gpg {self.wrapper}",
                "--define",
                "%_gpg_digest_algo sha256",
                "--resign",
                path,
            ],
            env=self.env,
        )
        self.verify_rpm(path)

    def verify_rpm(self, path):
        output = run(
            ["rpmkeys", "--dbpath", self.db, "--checksig", "--verbose", path], env=self.env
        )
        require(
            re.search(r"Signature.*: OK", output, re.I)
            and self.fingerprint[-8:].lower() in output.lower()
            and not re.search(r"NOT OK|NOKEY|BAD|UNTRUSTED", output),
            "RPM signature is not valid",
        )

    def sign_file(self, path):
        self.gpg(
            "--pinentry-mode",
            "loopback",
            "--passphrase-file",
            self.passfile,
            "--local-user",
            self.key,
            "--digest-algo",
            "SHA256",
            "--armor",
            "--detach-sign",
            path,
        )
        self.verify_file(path)

    def verify_file(self, path):
        verify_detached(self.public_file, path)

    def close(self):
        subprocess.run(
            ["gpgconf", "--homedir", str(self.home), "--kill", "all"],
            capture_output=True,
            check=False,
            timeout=30,
        )


def key_fingerprint(key):
    with tempfile.TemporaryDirectory(prefix="mkbrr-public-") as directory:
        output = run(
            ["gpg", "--homedir", directory, "--batch", "--with-colons", "--show-keys", key]
        )
        require(
            sum(line.startswith("pub:") for line in output.splitlines()) == 1,
            "Expected exactly one public key",
        )
        return next(line.split(":")[9] for line in output.splitlines() if line.startswith("fpr:"))


def verify_detached(public, path):
    with tempfile.TemporaryDirectory(prefix="mkbrr-verify-") as directory:
        run(["gpg", "--homedir", directory, "--batch", "--import", public])
        output = run(
            [
                "gpg",
                "--homedir",
                directory,
                "--batch",
                "--status-fd",
                "1",
                "--verify",
                str(path) + ".asc",
                path,
            ]
        )
        require(
            f"[GNUPG:] VALIDSIG {key_fingerprint(public)} " in output, "Detached signer mismatch"
        )


def go_h1(entries):
    digest = hashlib.sha256()
    for name, content in sorted(entries):
        require("\n" not in name, "Invalid Go module path")
        digest.update(f"{hashlib.sha256(content).hexdigest()}  {name}\n".encode())
    return "h1:" + base64.b64encode(digest.digest()).decode()


def go_zip_h1(path, module, version):
    with zipfile.ZipFile(path) as archive:
        files = [f for f in archive.infolist() if not f.is_dir()]
        require(
            len({f.filename for f in files}) == len(files)
            and sum(f.file_size for f in files) < 400 * MIB,
            "Invalid module archive",
        )
        require(
            all(
                f.filename.startswith(f"{module}@{version}/")
                and ".." not in PurePosixPath(f.filename).parts
                for f in files
            ),
            "Unsafe module archive",
        )
        return go_h1((f.filename, archive.read(f)) for f in files)


def escape_module(value):
    require(
        re.fullmatch(r"[A-Za-z0-9_./+~-]+", value)
        and ".." not in value
        and not value.startswith("/"),
        "Unsafe Go module name/version",
    )
    return "".join("!" + c.lower() if c.isupper() else c for c in value)


def mirror_sources(network, site, release, commit):
    tag = release["tag_name"]
    directory = site / "sources" / tag
    directory.mkdir(parents=True)
    archive = directory / f"mkbrr-{tag}.tar.gz"
    url = f"https://codeload.github.com/{UPSTREAM}/tar.gz/{commit}"
    network.download(url, archive, limit=64 * MIB)
    with tarfile.open(archive, "r:gz") as tar:
        members = tar.getmembers()
        require(sum(m.size for m in members) < 200 * MIB, "Source archive too large")
        roots = {PurePosixPath(m.name).parts[0] for m in members}
        require(len(roots) == 1, "Invalid source archive root")
        root = roots.pop()
        contents = {}
        for name in (
            "LICENSE",
            "go.mod",
            "go.sum",
            ".goreleaser.yml",
            ".github/workflows/release.yml",
        ):
            member = tar.getmember(f"{root}/{name}")
            require(member.isfile() and member.size <= 2 * MIB, "Invalid source control file")
            contents[name] = tar.extractfile(member).read().decode()
        require(
            re.search(r"GNU GENERAL PUBLIC LICENSE\s+Version 2, June 1991", contents["LICENSE"]),
            "Review changed upstream licensing",
        )
        # Never extract the untrusted archive. Only read the exact control files above.
        (directory / "LICENSE").write_text(contents["LICENSE"])
    dependencies = []
    for line in contents["go.sum"].splitlines():
        fields = line.split()
        require(
            len(fields) == 3 and re.fullmatch(r"h1:[A-Za-z0-9+/]{43}=", fields[2]), "Invalid go.sum"
        )
        module, version, expected = fields
        is_mod = version.endswith("/go.mod")
        version = version.removesuffix("/go.mod")
        escaped = f"{escape_module(module)}/@v/{escape_module(version)}"
        suffix = ".mod" if is_mod else ".zip"
        relative = f"sources/modules/{escaped}{suffix}"
        target = site / relative
        if not target.exists():
            network.download(f"https://proxy.golang.org/{escaped}{suffix}", target, limit=128 * MIB)
        actual = (
            go_h1([("go.mod", target.read_bytes())])
            if is_mod
            else go_zip_h1(target, module, version)
        )
        require(actual == expected, f"Go source checksum mismatch: {module} {version}{suffix}")
        dependencies.append(
            {
                "module": module,
                "version": version,
                "path": relative,
                "go_sum": expected,
                "sha256": sha256(target),
            }
        )
    # All selected module versions must have source zip entries, not just go.mod hashes.
    require(
        not re.search(r"^\s*replace\b", contents["go.mod"], re.M),
        "Review new Go module replacement directives before redistributing source",
    )
    required = re.findall(
        r"^\s*(?:require\s+)?([\w.+/-]+)\s+(v[\w.+-]+)(?:\s|$)", contents["go.mod"], re.M
    )
    full_sources = {(d["module"], d["version"]) for d in dependencies if d["path"].endswith(".zip")}
    require(
        required and set(required) <= full_sources, "Missing required dependency sources in go.sum"
    )
    return {
        "commit": commit,
        "upstream_url": url,
        "path": str(archive.relative_to(site)),
        "sha256": sha256(archive),
        "dependencies": dependencies,
        "upstream_tag_url": f"https://github.com/{UPSTREAM}/tree/{tag}",
    }


def parse_xml(data):
    require(
        len(data) <= 20 * MIB
        and b"<!DOCTYPE" not in data.upper()
        and b"<!ENTITY" not in data.upper(),
        "Unsafe or oversized metadata XML",
    )
    # ElementTree does not fetch external resources; forbid DTDs/entities too.
    return ET.fromstring(data)  # noqa: S314 -- bounded input with DTDs/entities rejected above


def validate_metadata(repo):
    ns = {"r": "http://linux.duke.edu/metadata/repo", "m": "http://linux.duke.edu/metadata/common"}
    root = parse_xml((repo / "repodata/repomd.xml").read_bytes())
    seen = set()
    primary = None
    for data in root.findall("r:data", ns):
        kind = data.attrib["type"]
        seen.add(kind)
        href = data.find("r:location", ns).attrib["href"]
        require(
            not PurePosixPath(href).is_absolute() and ".." not in PurePosixPath(href).parts,
            "Unsafe metadata location",
        )
        path = repo / href
        checksum = data.find("r:checksum", ns)
        require(checksum.attrib["type"] == "sha256", "Unexpected metadata checksum algorithm")
        verify_checksum(path, checksum.text)
        require(path.stat().st_size == int(data.find("r:size", ns).text), "Metadata size mismatch")
        if kind == "primary":
            with gzip.open(path, "rb") as stream:
                primary = parse_xml(stream.read(20 * MIB + 1))
    require(
        {"primary", "filelists", "other"} <= seen and primary is not None, "Missing DNF metadata"
    )
    packages = primary.findall("m:package", ns)
    require(
        packages and len(packages) == int(primary.attrib["packages"]), "Incomplete primary metadata"
    )
    locations = set()
    for package in packages:
        require(package.find("m:name", ns).text == "mkbrr", "Unexpected metadata package")
        href = package.find("m:location", ns).attrib["href"]
        require(
            not PurePosixPath(href).is_absolute() and ".." not in PurePosixPath(href).parts,
            "Unsafe package location",
        )
        require(href not in locations, "Duplicate metadata package")
        locations.add(href)
        checksum = package.find("m:checksum", ns)
        require(checksum.attrib["type"] == "sha256", "Unexpected package checksum algorithm")
        verify_checksum(repo / href, checksum.text)
    require(
        locations == {str(p.relative_to(repo)) for p in repo.rglob("*.rpm")},
        "RPM metadata set mismatch",
    )
    return len(packages)


def validate_site(site):
    key = site / "RPM-GPG-KEY-mkbrr-community"
    fingerprint = key_fingerprint(key)
    verify_detached(key, site / "state.json")
    verify_detached(key, site / "provenance.json")
    verify_detached(key, site / "rpm/stable/repodata/repomd.xml")
    state = json.loads((site / "state.json").read_text())
    require(state["plan"]["fingerprint"] == fingerprint, "Site fingerprint mismatch")
    require(
        (site / "mkbrr-community.repo").read_text() == render_repo(state["plan"]["base_url"]),
        "Repository configuration mismatch",
    )
    count = validate_metadata(site / "rpm/stable")
    provenance = json.loads((site / "provenance.json").read_text())
    require(
        count == sum(len(r["packages"]) for r in provenance["releases"]),
        "Provenance count mismatch",
    )
    with tempfile.TemporaryDirectory(prefix="mkbrr-rpm-verify-") as directory:
        db = Path(directory) / "rpmdb"
        db.mkdir()
        run(["rpmkeys", "--dbpath", db, "--import", key])
        for path in site.rglob("*.rpm"):
            output = run(["rpmkeys", "--dbpath", db, "--checksig", "--verbose", path])
            require(
                re.search(r"Signature.*: OK", output, re.I)
                and fingerprint[-8:].lower() in output.lower()
                and not re.search(r"NOT OK|NOKEY|BAD", output),
                "Invalid site RPM signature",
            )
    for release in provenance["releases"]:
        verify_checksum(site / release["checksum_manifest"], release["checksum_manifest_sha256"])
        for package in release["packages"]:
            verify_checksum(site / package["path"], package["signed_sha256"])
        source = release["source"]
        verify_checksum(site / source["path"], source["sha256"])
        for dependency in source["dependencies"]:
            verify_checksum(site / dependency["path"], dependency["sha256"])
    require(
        sum(p.stat().st_size for p in site.rglob("*") if p.is_file()) <= MAX_SITE,
        "Site exceeds 800 MiB budget; no release was silently dropped",
    )
    require(not any(p.is_symlink() for p in site.rglob("*")), "Site must not contain symlinks")
    return count


def base_url(repository, override=None):
    require(
        re.fullmatch(r"[A-Za-z0-9-]+/[A-Za-z0-9_.-]+", repository),
        "Use --repository OWNER/REPOSITORY",
    )
    owner, name = repository.split("/")
    url = override or f"https://{owner.lower()}.github.io/{name}"
    parsed = urllib.parse.urlsplit(url)
    require(
        parsed.scheme == "https"
        and parsed.hostname
        and not parsed.username
        and not parsed.password
        and not parsed.query
        and not parsed.fragment
        and not re.search(r"[\s{}]", url),
        "Invalid Pages URL",
    )
    return url.rstrip("/")


def render_repo(url):
    return (ROOT / "config/mkbrr-community.repo.in").read_text().format(base_url=url)


def implementation_hash():
    files = sorted((ROOT / "scripts").glob("*.py")) + sorted((ROOT / "config").glob("*.in"))
    public = ROOT / "config/signing-key.asc"
    if public.is_file():
        files.append(public)
    return hashlib.sha256("".join(p.name + sha256(p) for p in files).encode()).hexdigest()


def prepare_plan(network, repository, url, retain, fingerprint):
    releases = select_releases(network.releases(), retain)
    plan_releases = []
    previous_arches = None
    for release in releases:
        rpms, manifest = discover(release)
        arches = {a["rpm_arch"] for a in rpms}
        if previous_arches is not None:
            require(
                previous_arches >= arches,
                "New release dropped an architecture; review upstream completeness",
            )
        previous_arches = arches
        commit = network.json(f"{API}/commits/{urllib.parse.quote(release['tag_name'], safe='')}")[
            "sha"
        ]
        require(re.fullmatch(r"[a-f0-9]{40}", commit), "Invalid source commit")
        plan_releases.append(
            {
                "tag": release["tag_name"],
                "release_id": release["id"],
                "commit": commit,
                "assets": [
                    {k: a.get(k) for k in ("id", "name", "size", "updated_at", "digest")}
                    for a in [*rpms, manifest]
                ],
            }
        )
    plan = {
        "schema": 1,
        "repository": repository,
        "base_url": url,
        "retain": retain,
        "fingerprint": fingerprint,
        "implementation": implementation_hash(),
        "releases": plan_releases,
    }
    return plan, releases


def previous_matches(network, url, plan, public, scratch):
    path = scratch / "state.json"
    hosts = {urllib.parse.urlsplit(url).hostname}
    try:
        network.download(f"{url}/state.json", path, limit=2 * MIB, hosts=hosts)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return False
        raise
    network.download(f"{url}/state.json.asc", Path(str(path) + ".asc"), limit=MIB, hosts=hosts)
    verify_detached(public, path)
    return json.loads(path.read_text())["plan"] == plan


def build_site(network, site, plan, releases, signer, dry_run):
    site.mkdir()
    (site / ".mkbrr-generated").touch()
    (site / ".nojekyll").touch()
    (site / "RPM-GPG-KEY-mkbrr-community").write_text(signer.public)
    repo = site / "rpm/stable"
    repo.mkdir(parents=True)
    provenance = {
        "unofficial": True,
        "upstream": f"https://github.com/{UPSTREAM}",
        "signing_fingerprint": signer.fingerprint,
        "releases": [],
    }
    for release, release_plan in zip(releases, plan["releases"], strict=True):
        tag = release["tag_name"]
        LOG.info("Verifying and signing %s", tag)
        source = mirror_sources(network, site, release, release_plan["commit"])
        directory = site / "provenance" / tag
        directory.mkdir(parents=True)
        rpms, manifest = discover(release)
        checksum_path = directory / manifest["name"]
        network.download(
            manifest["browser_download_url"], checksum_path, limit=2 * MIB, size=manifest["size"]
        )
        if manifest.get("digest"):
            require(
                manifest["digest"] == f"sha256:{sha256(checksum_path)}",
                "API checksum-manifest digest disagreement",
            )
        checksums = parse_checksums(checksum_path.read_text())
        require(
            {n for n in checksums if n.lower().endswith(".rpm")} == {a["name"] for a in rpms},
            "Release/checksum RPM set mismatch (possibly incomplete upstream release)",
        )
        packages = []
        for asset in rpms:
            path = repo / "Packages" / tag / asset["name"]
            network.download(asset["browser_download_url"], path, size=asset["size"])
            original = checksums.get(asset["name"])
            verify_checksum(path, original)
            if asset.get("digest"):
                require(asset["digest"] == f"sha256:{original}", "API digest/checksum disagreement")
            metadata = rpm_metadata(path, tag.removeprefix("v"), asset["rpm_arch"])
            signer.sign_rpm(path)
            # Recheck digests/identity after signing as well.
            rpm_metadata(path, tag.removeprefix("v"), asset["rpm_arch"])
            packages.append(
                {
                    "asset_id": asset["id"],
                    "asset_name": asset["name"],
                    "upstream_url": asset["browser_download_url"],
                    "original_sha256": original,
                    "signed_sha256": sha256(path),
                    "metadata": metadata,
                    "path": str(path.relative_to(site)),
                }
            )
        provenance["releases"].append(
            {
                "tag": tag,
                "release_id": release["id"],
                "release_url": f"https://github.com/{UPSTREAM}/releases/tag/{tag}",
                "checksum_manifest": str(checksum_path.relative_to(site)),
                "checksum_manifest_sha256": sha256(checksum_path),
                "source": source,
                "packages": packages,
            }
        )
    run(
        [
            "createrepo_c",
            "--checksum",
            "sha256",
            "--general-compress-type",
            "gz",
            "--database",
            "--unique-md-filenames",
            repo,
        ]
    )
    signer.sign_file(repo / "repodata/repomd.xml")
    (site / "mkbrr-community.repo").write_text(render_repo(plan["base_url"]))
    for name, value in (("provenance.json", provenance), ("state.json", {"plan": plan})):
        path = site / name
        path.write_text(canonical(value))
        signer.sign_file(path)
    links = []
    for release in provenance["releases"]:
        tag = html.escape(release["tag"])
        arches = ", ".join(p["metadata"]["arch"] for p in release["packages"])
        links.append(
            f'<li><a href="{release["release_url"]}">{tag}</a> ({arches}): '
            f'<a href="{release["source"]["path"]}">matching source</a>, '
            f'<a href="sources/{tag}/LICENSE">GPL license</a></li>'
        )
    (site / "index.html").write_text(
        (ROOT / "config/index.html.in")
        .read_text()
        .format(
            mode="DISPOSABLE TEST KEY — DO NOT USE IN PRODUCTION. " if dry_run else "",
            base_url=html.escape(plan["base_url"]),
            repository=html.escape(plan["repository"]),
            fingerprint=signer.fingerprint,
            releases="<ul>" + "".join(links) + "</ul>",
        )
    )
    (site / "sources/README.txt").write_text(
        "Corresponding source for the unmodified official mkbrr CLI RPMs\n\n"
        "Each version has an upstream commit-pinned tar.gz, its LICENSE and build scripts.\n"
        "provenance.json records commits, original RPM checksums and source SHA-256 hashes.\n"
        "modules/ contains dependency .zip and .mod source files, verified using the\n"
        "matching source go.sum h1 directory hashes. Dependencies retain their licenses.\n"
        "To inspect/build independently, unpack the source, use its Go version, and\n"
        "follow its Makefile/.goreleaser.yml/.github/workflows/release.yml. Dependency\n"
        "zip archives can be unpacked locally or placed in Go's module download cache.\n"
        "The upstream workflow documents regeneration of its optional PGO profile.\n"
        "This repository does not rebuild or modify the application. Upstream: GPL-2.0.\n"
    )
    count = validate_site(site)
    LOG.info(
        "Validated %d signed RPMs; site size %.1f MiB",
        count,
        sum(p.stat().st_size for p in site.rglob("*") if p.is_file()) / MIB,
    )


def synchronize(args, network=None):
    network = network or Network()
    output = args.output.resolve()
    require(
        not output.exists() or (output / ".mkbrr-generated").is_file(),
        "Refusing to replace a directory not generated by this tool",
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    url = base_url(args.repository, args.base_url)
    # Private material and passphrase are removed automatically, even on failure.
    with tempfile.TemporaryDirectory(prefix="mkbrr-sign-") as private_dir:
        signer = None
        try:
            signer = Signer(Path(private_dir), dry_run=args.dry_run)
            plan, releases = prepare_plan(
                network, args.repository, url, args.retain, signer.fingerprint
            )
            if not args.force:
                if (
                    output.exists()
                    and json.loads((output / "state.json").read_text())["plan"] == plan
                ):
                    validate_site(output)
                    LOG.info("Unchanged verified local repository; nothing to publish")
                    return False
                if args.check_published and previous_matches(
                    network, url, plan, signer.public_file, Path(private_dir)
                ):
                    LOG.info("Published signed state is current; nothing to publish")
                    return False
            with tempfile.TemporaryDirectory(prefix=".mkbrr-stage-", dir=output.parent) as work:
                stage = Path(work) / "site"
                build_site(network, stage, plan, releases, signer, args.dry_run)
                backup = Path(work) / "previous"
                if output.exists():
                    output.rename(backup)
                try:
                    stage.rename(output)
                except BaseException:
                    if backup.exists():
                        backup.rename(output)
                    raise
            return True
        finally:
            if signer:
                signer.close()
            else:
                # __init__ can fail after importing a private key/starting an agent.
                subprocess.run(
                    ["gpgconf", "--homedir", str(Path(private_dir) / "gnupg"), "--kill", "all"],
                    capture_output=True,
                    check=False,
                    timeout=30,
                )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", default=os.environ.get("GITHUB_REPOSITORY", ""))
    parser.add_argument(
        "--base-url", help="HTTPS Pages URL, including project path (or custom domain)"
    )
    parser.add_argument("--output", type=Path, default=ROOT / "site")
    parser.add_argument("--retain", type=int, choices=range(1, 4), default=3)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Real verification with a disposable signing key; never deploy",
    )
    parser.add_argument("--force", action="store_true")
    parser.add_argument(
        "--check-published", action="store_true", help="Skip unchanged signed published state"
    )
    parser.add_argument(
        "--validate-site", type=Path, help="Validate an already generated site without private keys"
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    try:
        if args.validate_site:
            LOG.info("Validated %d RPMs", validate_site(args.validate_site.resolve()))
            return
        changed = synchronize(args)
        if output := os.environ.get("GITHUB_OUTPUT"):
            with Path(output).open("a") as stream:
                stream.write(f"changed={str(changed).lower()}\n")
    except (
        InvalidRepository,
        OSError,
        ValueError,
        KeyError,
        tarfile.TarError,
        zipfile.BadZipFile,
    ) as exc:
        LOG.error("No deployment: %s", exc)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
