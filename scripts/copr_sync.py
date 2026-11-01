#!/usr/bin/env python3
"""Prepare offline source RPMs and submit them to the Fedora COPR build service."""

from __future__ import annotations

import argparse
import configparser
import datetime as dt
import gzip
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import time
from pathlib import Path, PurePosixPath

if __package__:
    from . import sync as repo
else:
    import sync as repo

ROOT = Path(__file__).resolve().parents[1]
COPR_URL = "https://copr.fedorainfracloud.org"
ACTIVE = {"pending", "starting", "running", "waiting", "importing", "forked"}
FAILED = {"failed", "canceled", "skipped"}
LOG = logging.getLogger("copr")


def settings(package="mkbrr"):
    config = json.loads((ROOT / "config/copr.json").read_text())
    repo.require(package in config["packages"], f"No reviewed source recipe for {package}")
    recipe = config["packages"][package]
    repo.require(re.fullmatch(r"[a-z0-9][a-z0-9-]*", package), "Invalid package name")
    repo.require(
        re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", recipe["upstream"]),
        "Invalid upstream repository",
    )
    repo.require(
        type(recipe["release"]) is int and recipe["release"] >= 2,
        "Packaging release must be >=2 to supersede upstream RPM release 1",
    )
    repo.require(
        config["chroots"] and len(config["chroots"]) == len(set(config["chroots"])),
        "Missing or duplicate COPR chroots",
    )
    repo.require(
        all(re.fullmatch(r"fedora-\d+-(x86_64|aarch64)", c) for c in config["chroots"]),
        "Unreviewed COPR build target",
    )
    toolchain = config["toolchain"]
    repo.require(
        all(re.fullmatch(r"\d+\.\d+\.\d+", toolchain[k]) for k in ("version", "bootstrap_version"))
        and re.fullmatch(r"[a-f0-9]{64}", toolchain["sha256"]),
        "Invalid reviewed Go source toolchain",
    )
    spec = (ROOT / recipe["spec"]).resolve()
    repo.require(spec.is_relative_to(ROOT.resolve()) and spec.is_file(), "Invalid spec template")
    return config, recipe


def target(value):
    repo.require(
        re.fullmatch(r"[a-zA-Z0-9_][a-zA-Z0-9_.-]*/[a-zA-Z0-9_][a-zA-Z0-9_.-]*", value or ""),
        "Set COPR_PROJECT to your Fedora account/project, not your GitHub username",
    )
    return value.split("/")


def copr_client(config_path=None):
    try:
        from copr.v3 import Client
    except ImportError:
        raise repo.InvalidRepository("Install copr-cli or run with `uv run --with copr`.") from None
    if config_path is None:
        return Client({"copr_url": COPR_URL})
    config = configparser.ConfigParser(interpolation=None)
    repo.require(config.read(config_path), "COPR config file not found")
    repo.require(config.has_section("copr-cli"), "Expected [copr-cli] in COPR config")
    section = config["copr-cli"]
    repo.require(
        section.get("copr_url", "").rstrip("/") == COPR_URL,
        "Only the Fedora COPR HTTPS endpoint is allowed for credentials",
    )
    repo.require(section.get("login") and section.get("token"), "COPR credentials missing")
    return Client.create_from_config_file(str(config_path))


def watch_build(client, build_id, timeout=5400, interval=20):
    deadline = time.monotonic() + timeout
    previous = None
    while True:
        build = client.build_proxy.get(build_id)
        state = build["state"]
        if state != previous:
            LOG.info("COPR build %s: %s", build_id, state)
            previous = state
        if state == "succeeded":
            return build
        repo.require(
            state in ACTIVE,
            f"COPR build {build_id} ended {state}; see {COPR_URL}/coprs/build/{build_id}/",
        )
        repo.require(
            time.monotonic() < deadline, "COPR build still active; retry later, do not duplicate it"
        )
        time.sleep(interval)


def current_build(client, owner, project, package, version, release, chroots):
    """Use COPR's actual state, not a premature local success/cache marker."""
    expected = f"{version}-{release}"
    for page in range(20):
        builds = client.build_proxy.get_list(
            owner, project, packagename=package, pagination={"limit": 100, "offset": page * 100}
        )
        for build in builds:
            source = build.get("source_package") or {}
            if source.get("name") != package or source.get("version") != expected:
                continue
            if not set(chroots) <= set(build["chroots"]):
                continue
            state = build["state"]
            if state in ACTIVE:
                watch_build(client, build["id"])
                return build["id"]
            if state == "succeeded":
                return build["id"]
            repo.require(state in FAILED, f"Unknown COPR build state: {state}")
            # Retry a failed latest attempt rather than hiding it behind an old success.
            return None
        if len(builds) < 100:
            return None
    raise repo.InvalidRepository("COPR build pagination limit reached")


def check_project(client, owner, project, chroots):
    remote = client.project_proxy.get(owner, project)
    repo.require(
        set(chroots) <= set(remote["chroot_repos"]),
        "COPR project lacks configured chroots; update its settings first",
    )
    repo.require(not remote["enable_net"], "Disable network access in the COPR build project")
    return remote


def upstream_source(network, package, recipe, directory):
    api = f"https://api.github.com/repos/{recipe['upstream']}"
    releases = []
    for page in range(1, 21):
        batch = network.json(f"{api}/releases?per_page=100&page={page}")
        repo.require(isinstance(batch, list), "Invalid release response")
        releases.extend(batch)
        if len(batch) < 100:
            break
    else:
        raise repo.InvalidRepository("Upstream release pagination limit reached")
    release = repo.select_releases(releases, 1)[0]
    tag = release["tag_name"]
    commit = network.json(f"{api}/commits/{tag}")
    sha = commit["sha"]
    repo.require(re.fullmatch(r"[a-f0-9]{40}", sha), "Invalid source commit")
    build_time = commit["commit"]["committer"]["date"]
    parsed_time = dt.datetime.fromisoformat(build_time.replace("Z", "+00:00"))
    repo.require(parsed_time.tzinfo is not None, "Source timestamp requires a timezone")
    directory.mkdir(parents=True)
    archive = directory / f"{package}-{sha}.tar.gz"
    url = f"https://codeload.github.com/{recipe['upstream']}/tar.gz/{sha}"
    network.download(url, archive, limit=64 * repo.MIB)
    with tarfile.open(archive) as source:
        members = source.getmembers()
        repo.require(
            members and sum(m.size for m in members) <= 200 * repo.MIB,
            "Source archive exceeds limit",
        )
        roots = {PurePosixPath(m.name).parts[0] for m in members if m.name}
        repo.require(len(roots) == 1, "Invalid source archive root")
        root = roots.pop()
        repo.require(root == f"{package}-{sha}", "Unexpected source root")
        for member in members:
            path = PurePosixPath(member.name)
            repo.require(
                not path.is_absolute() and ".." not in path.parts, "Source path traversal rejected"
            )
            repo.require(
                member.isfile() or member.isdir() or member.issym() or member.islnk(),
                "Source device/special file rejected",
            )
        # Explicit data filter rejects links escaping the extraction directory.
        source.extractall(directory / "unpacked", filter="data")
    source_dir = directory / "unpacked" / root
    module = (source_dir / "go.mod").read_text()
    repo.require(
        re.search(rf"^module {re.escape(recipe['module'])}\s*$", module, re.M),
        "Unexpected upstream Go module",
    )
    match = re.search(r"^go (\d+\.\d+\.\d+)\s*$", module, re.M)
    repo.require(match, "Unexpected Go version directive")
    repo.require(not re.search(r"^\s*replace\b", module, re.M), "Review module replacements first")
    # This pilot has a reviewed GPL-2.0-or-later grant in the upstream README.
    readme = (source_dir / "README.md").read_text()
    repo.require(
        "either version 2" in readme and "any later version" in readme,
        "Review changed upstream licensing before building",
    )
    plan = {
        "schema": 1,
        "package": package,
        "upstream": recipe["upstream"],
        "tag": tag,
        "version": tag.removeprefix("v"),
        "release": recipe["release"],
        "commit": sha,
        "go_version": match[1],
        "build_time": build_time,
        "source_date_epoch": int(parsed_time.timestamp()),
        "changelog_date": parsed_time.strftime("%a %b %d %Y"),
        "source_url": url,
        "source_sha256": repo.sha256(archive),
        "license": recipe["license"],
    }
    return plan, archive, source_dir


def deterministic_tar(path, roots):
    with path.open("wb") as raw, gzip.GzipFile(fileobj=raw, mode="wb", mtime=0, filename="") as gz:
        with tarfile.open(fileobj=gz, mode="w") as archive:
            for root in roots:
                for file in [root, *sorted(root.rglob("*"))]:
                    repo.require(not file.is_symlink(), "Unexpected vendored symlink")
                    info = archive.gettarinfo(file, str(file.relative_to(root.parent)))
                    info.uid = info.gid = info.mtime = 0
                    info.uname = info.gname = ""
                    info.mode = 0o755 if info.isdir() or info.mode & 0o111 else 0o644
                    if info.isfile():
                        with file.open("rb") as content:
                            archive.addfile(info, content)
                    else:
                        archive.addfile(info)


def vendor_sources(source_dir, output):
    before = {name: repo.sha256(source_dir / name) for name in ("go.mod", "go.sum")}
    env = {
        **os.environ,
        "GOTOOLCHAIN": "auto",
        "GOPROXY": "https://proxy.golang.org",
        "GOSUMDB": "sum.golang.org",
        "GOPRIVATE": "",
        "GONOSUMDB": "",
        "GONOPROXY": "",
        "GOINSECURE": "",
        "GOENV": "off",
        "GOFLAGS": "",
        "GOWORK": "off",
    }
    result = subprocess.run(
        ["go", "mod", "vendor"],
        cwd=source_dir,
        env=env,
        text=True,
        capture_output=True,
        timeout=1200,
        check=False,
    )
    repo.require(result.returncode == 0, f"go mod vendor failed:\n{result.stderr}")
    repo.require(
        all(repo.sha256(source_dir / name) == digest for name, digest in before.items()),
        "Vendoring modified upstream go.mod/go.sum; review dependency metadata",
    )
    vendor = source_dir / "vendor"
    repo.require((vendor / "modules.txt").is_file(), "Missing vendored module manifest")
    licenses = source_dir / "vendor-licenses"
    licenses.mkdir()
    for file in sorted(vendor.rglob("*")):
        if file.is_file() and re.match(r"^(LICENSE|COPYING|NOTICE)(\..*)?$", file.name, re.I):
            if file.suffix == ".go":
                continue
            destination = licenses / file.relative_to(vendor)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(file, destination)
    repo.require(any(licenses.rglob("LICENSE*")), "Missing dependency license notices")
    deterministic_tar(output, [vendor, licenses])


def render_spec(recipe, plan):
    values = {k.upper(): str(v) for k, v in plan.items() if isinstance(v, (str, int))}
    text = (ROOT / recipe["spec"]).read_text()
    for key, value in values.items():
        text = text.replace(f"@{key}@", value)
    repo.require(not re.search(r"@[A-Z_]+@", text), "Unresolved spec template placeholder")
    return text


def toolchain_source(network, toolchain, plan, sources):
    version = toolchain["version"]
    repo.require(
        tuple(map(int, version.split("."))) >= tuple(map(int, plan["go_version"].split("."))),
        "Review the pinned Go source toolchain: upstream requires a newer compiler",
    )
    name = f"go{version}.src.tar.gz"
    url = f"https://go.dev/dl/{name}"
    archive = sources / name
    network.download(url, archive, limit=64 * repo.MIB, hosts={"go.dev", "dl.google.com"})
    repo.verify_checksum(archive, toolchain["sha256"])
    plan.update(
        {
            "toolchain_version": version,
            "bootstrap_version": toolchain["bootstrap_version"],
            "toolchain_source_url": url,
            "toolchain_file": name,
            "toolchain_sha256": toolchain["sha256"],
        }
    )


def prepare(package, output, check_copr=None, force=False, network=None):
    config, recipe = settings(package)
    network = network or repo.Network()
    repo.require(
        not output.exists() or (output / ".copr-generated").is_file(),
        "Refusing to replace an unrelated directory",
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="copr-source-", dir=output.parent) as temporary:
        stage = Path(temporary) / "package"
        sources = stage / "SOURCES"
        plan, archive, source_dir = upstream_source(network, package, recipe, sources)
        plan["chroots"] = config["chroots"]
        plan["packaging_sha256"] = hashlib.sha256(
            (
                repo.canonical(recipe)
                + repo.canonical(config["toolchain"])
                + (ROOT / recipe["spec"]).read_text()
            ).encode()
        ).hexdigest()
        if check_copr:
            owner, project = target(check_copr)
            client = copr_client()
            check_project(client, owner, project, config["chroots"])
            build = current_build(
                client, owner, project, package, plan["version"], plan["release"], plan["chroots"]
            )
            if build and not force:
                LOG.info("Already built successfully: %s/coprs/build/%s/", COPR_URL, build)
                return None
        LOG.info("Vendoring source dependencies for %s %s", package, plan["tag"])
        vendor = sources / f"{package}-{plan['version']}-vendor.tar.gz"
        vendor_sources(source_dir, vendor)
        plan["vendor_sha256"] = repo.sha256(vendor)
        plan["source_file"] = archive.name
        plan["vendor_file"] = vendor.name
        toolchain_source(network, config["toolchain"], plan, sources)
        # No generated binaries or dependency caches enter the source package.
        shutil.rmtree(sources / "unpacked")
        provenance = sources / f"{package}-source-provenance.json"
        provenance.write_text(repo.canonical(plan))
        specs = stage / "SPECS"
        specs.mkdir()
        spec = specs / f"{package}.spec"
        spec.write_text(render_spec(recipe, plan))
        for name in ("BUILD", "BUILDROOT", "RPMS", "SRPMS"):
            (stage / name).mkdir()
        repo.run(
            [
                "rpmbuild",
                "--undefine",
                "dist",
                "--define",
                f"_topdir {stage.resolve()}",
                "-bs",
                spec.resolve(),
            ]
        )
        srpms = list((stage / "SRPMS").glob("*.src.rpm"))
        repo.require(len(srpms) == 1, "Expected exactly one source RPM")
        plan["srpm"] = f"SRPMS/{srpms[0].name}"
        plan["srpm_sha256"] = repo.sha256(srpms[0])
        (stage / "plan.json").write_text(repo.canonical(plan))
        (stage / ".copr-generated").touch()
        backup = Path(temporary) / "previous"
        if output.exists():
            output.rename(backup)
        try:
            stage.rename(output)
        except BaseException:
            if backup.exists():
                backup.rename(output)
            raise
    LOG.info("Prepared %s", output / plan["srpm"])
    return plan


def build_local(output):
    plan = json.loads((output / "plan.json").read_text())
    srpm = output / plan["srpm"]
    repo.verify_checksum(srpm, plan["srpm_sha256"])
    repo.run(["rpmbuild", "--define", f"_topdir {output.resolve()}", "--rebuild", srpm.resolve()])
    packages = list((output / "RPMS").rglob("*.rpm"))
    repo.require(len(packages) == 1, "Expected one native binary RPM")
    metadata = repo.run(["rpm", "-qp", "--qf", "%{NAME} %{VERSION} %{ARCH}\n", packages[0]])
    repo.require(
        metadata.startswith(f"{plan['package']} {plan['version']} "), "Built RPM identity mismatch"
    )
    repo.run(["rpmkeys", "--checksig", "--nosignature", packages[0]])
    LOG.info("Built from source: %s", packages[0])
    return packages[0]


def test_install(output):
    repo.require(
        os.geteuid() == 0, "Run install tests as container root; the host RPM DB is never used"
    )
    plan = json.loads((output / "plan.json").read_text())
    packages = list((output / "RPMS").rglob("*.rpm"))
    repo.require(len(packages) == 1, "Build the source RPM before testing installation")
    with tempfile.TemporaryDirectory(prefix="copr-rpm-install-") as temporary:
        work = Path(temporary)
        root = work / "root"
        root.mkdir()
        home = work / "gnupg"
        home.mkdir(mode=0o700)
        env = {
            k: v
            for k, v in os.environ.items()
            if k not in ("COPR_CONFIG", "RPM_SIGNING_KEY", "RPM_SIGNING_PASSPHRASE")
        }
        env.update({"GNUPGHOME": str(home), "LC_ALL": "C"})
        try:
            repo.run(
                [
                    "gpg",
                    "--batch",
                    "--pinentry-mode",
                    "loopback",
                    "--passphrase",
                    "",
                    "--quick-generate-key",
                    "Disposable source RPM test <test@example.invalid>",
                    "rsa3072",
                    "sign",
                    "1d",
                ],
                env=env,
            )
            listing = repo.run(["gpg", "--batch", "--with-colons", "--list-secret-keys"], env=env)
            fingerprint = next(
                line.split(":")[9] for line in listing.splitlines() if line.startswith("fpr:")
            )
            key = work / "public.asc"
            key.write_text(
                repo.run(["gpg", "--batch", "--armor", "--export", fingerprint], env=env)
            )
            signed = work / packages[0].name
            shutil.copy2(packages[0], signed)
            # RPM 6 requires a signature even for a local install. Sign a disposable
            # copy instead of weakening its verification policy or using a real key.
            repo.run(
                [
                    "rpmsign",
                    "--define",
                    "_openpgp_sign gpg",
                    "--key-id",
                    fingerprint,
                    "--digest-algo",
                    "sha256",
                    "--resign",
                    signed,
                ],
                env=env,
            )
            repo.run(["rpm", "--root", root, "--initdb"])
            repo.run(["rpmkeys", "--root", root, "--import", key])
            verification = repo.run(["rpmkeys", "--root", root, "--checksig", "--verbose", signed])
            repo.require(
                fingerprint[-8:].lower() in verification.lower()
                and re.search(r"signature.*: OK", verification, re.I),
                "Test signature invalid",
            )
            repo.run(["rpm", "--root", root, "--install", signed])
            version = repo.run([root / "usr/bin" / plan["package"], "version"], env=env)
            repo.require(
                f"mkbrr version: {plan['version']}" in version, "Installed binary version mismatch"
            )
            license_dir = root / "usr/share/licenses" / plan["package"]
            repo.require(
                all((license_dir / name).is_file() for name in ("LICENSE", "GOLANG-LICENSE")),
                "Installed license notices missing",
            )
            repo.require((license_dir / "vendor-licenses").is_dir(), "Dependency notices missing")
            LOG.info("Signed test installation passed: %s", version.strip().splitlines()[0])
        finally:
            subprocess.run(
                ["gpgconf", "--homedir", str(home), "--kill", "all"],
                capture_output=True,
                check=False,
                timeout=30,
            )


def submit(output, destination, config_path, force=False, client=None):
    plan = json.loads((output / "plan.json").read_text())
    owner, project = target(destination)
    config, recipe = settings(plan["package"])
    repo.require(
        plan["release"] == recipe["release"] and plan["chroots"] == config["chroots"],
        "Prepared source package does not match current configuration",
    )
    srpm = output / plan["srpm"]
    repo.require(srpm.resolve().is_relative_to(output.resolve()), "Unsafe source RPM path")
    repo.verify_checksum(srpm, plan["srpm_sha256"])
    client = client or copr_client(config_path)
    client.base_proxy.auth_check()
    check_project(client, owner, project, plan["chroots"])
    existing = current_build(
        client, owner, project, plan["package"], plan["version"], plan["release"], plan["chroots"]
    )
    if existing and not force:
        LOG.info("Already current: %s/coprs/build/%s/", COPR_URL, existing)
        return existing
    build = client.build_proxy.create_from_file(
        owner, project, str(srpm), buildopts={"chroots": plan["chroots"], "background": True}
    )
    LOG.info("Submitted %s/coprs/build/%s/", COPR_URL, build["id"])
    watch_build(client, build["id"])
    return build["id"]


def initialize(config_path, github_repository=None):
    config, _ = settings()
    client = copr_client(config_path)
    client.base_proxy.auth_check()
    owner = client.base_proxy.auth_username()
    project = config["project"]
    target(f"{owner}/{project}")
    available = client.mock_chroot_proxy.get_list()
    repo.require(set(config["chroots"]) <= set(available), "Configured COPR chroot unavailable")
    # exist_ok never overwrites an existing project's configuration.
    client.project_proxy.add(
        owner,
        project,
        config["chroots"],
        enable_net=False,
        exist_ok=True,
        description="Unofficial source-built autobrr RPMs. Not affiliated with autobrr.",
        instructions=f"sudo dnf copr enable {owner}/{project}\nsudo dnf install mkbrr",
    )
    check_project(client, owner, project, config["chroots"])
    if github_repository:
        repo.base_url(github_repository)  # Validate OWNER/REPOSITORY before calling gh.
        repo.run(
            ["gh", "secret", "set", "COPR_CONFIG", "--repo", github_repository, "--env", "copr"],
            input=Path(config_path).read_text(),
        )
        repo.run(
            [
                "gh",
                "variable",
                "set",
                "COPR_PROJECT",
                "--repo",
                github_repository,
                "--body",
                f"{owner}/{project}",
            ]
        )
    LOG.info("Configured %s/coprs/%s/%s/", COPR_URL, owner, project)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prep = commands.add_parser(
        "prepare", help="Download verified dependency sources and build an SRPM"
    )
    prep.add_argument("--package", default="mkbrr")
    prep.add_argument("--output", type=Path, default=ROOT / ".work/copr")
    prep.add_argument(
        "--check-copr", help="Skip a successful existing Fedora-account/project build"
    )
    prep.add_argument("--force", action="store_true")
    build = commands.add_parser("build", help="Compile and test a prepared SRPM locally")
    build.add_argument("--output", type=Path, default=ROOT / ".work/copr")
    install = commands.add_parser(
        "test-install", help="Install the unsigned local test RPM in a disposable root"
    )
    install.add_argument("--output", type=Path, default=ROOT / ".work/copr")
    upload = commands.add_parser("submit", help="Upload the SRPM and wait for all COPR builds")
    upload.add_argument("--output", type=Path, default=ROOT / ".work/copr")
    upload.add_argument("--target", required=True)
    upload.add_argument("--config", type=Path, default=Path.home() / ".config/copr")
    upload.add_argument("--force", action="store_true")
    init = commands.add_parser("init", help="Create the COPR project with your Fedora credentials")
    init.add_argument("--config", type=Path, default=Path.home() / ".config/copr")
    init.add_argument("--configure-github", metavar="OWNER/REPOSITORY")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    try:
        if args.command == "prepare":
            plan = prepare(args.package, args.output.resolve(), args.check_copr, args.force)
            if output := os.environ.get("GITHUB_OUTPUT"):
                with Path(output).open("a") as stream:
                    stream.write(f"changed={str(plan is not None).lower()}\n")
        elif args.command == "build":
            build_local(args.output.resolve())
        elif args.command == "test-install":
            test_install(args.output.resolve())
        elif args.command == "init":
            initialize(args.config, args.configure_github)
        else:
            secret = os.environ.get("COPR_CONFIG")
            if secret:
                with tempfile.TemporaryDirectory(prefix="copr-credentials-") as temporary:
                    config = Path(temporary) / "copr"
                    config.write_text(secret)
                    config.chmod(0o600)
                    submit(args.output.resolve(), args.target, config, args.force)
            else:
                submit(args.output.resolve(), args.target, args.config, args.force)
    except (repo.InvalidRepository, OSError, ValueError, KeyError, tarfile.TarError) as exc:
        LOG.error("No COPR publication: %s", exc)
        raise SystemExit(1) from None
    except Exception as exc:
        if not type(exc).__module__.startswith(("copr.", "requests.")):
            raise
        # Do not dump SDK/config objects containing the API token into CI logs.
        LOG.error(
            "COPR request failed (%s); check credentials, permissions and service status",
            type(exc).__name__,
        )
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
