#!/usr/bin/env python3
"""Consume signed metadata using real DNF; optionally install in a disposable root.

Never writes to the host RPM database or installs on the host. file:// is used
only in this test; the distributed .repo always uses HTTPS.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import tempfile
from pathlib import Path


def exercise(site, install=False):
    if install and os.geteuid() != 0:
        raise RuntimeError(
            "DNF requires root for installation, even into a disposable root. "
            "Run this test with sudo (or inside a disposable rootful container), "
            "or omit --install for unprivileged metadata validation."
        )
    executable = shutil.which("dnf")
    if not executable:
        raise RuntimeError("DNF is required for consumer integration validation")
    version = subprocess.run(
        [executable, "--version"], capture_output=True, text=True, check=True
    ).stdout
    dnf5 = "dnf5" in version or version.startswith("5.")
    with tempfile.TemporaryDirectory(prefix="mkbrr-dnf-") as directory:
        work = Path(directory)
        root = work / "root"
        root.mkdir()
        repos = work / "repos"
        repos.mkdir()
        (repos / "community.repo").write_text(
            "[mkbrr-community]\nname=Disposable consumer test\nenabled=1\nskip_if_unavailable=0\n"
            f"baseurl={site.as_uri()}/rpm/stable/\ngpgcheck=1\nrepo_gpgcheck=1\n"
            f"gpgkey={site.as_uri()}/RPM-GPG-KEY-mkbrr-community\n"
        )
        args = [
            executable,
            "--assumeyes",
            "--installroot",
            str(root),
            "--releasever=44",
            f"--setopt=reposdir={repos}",
            f"--setopt=cachedir={work / 'cache'}",
            f"--setopt=persistdir={work / 'persist'}",
            "--setopt=install_weak_deps=False",
        ]
        if dnf5:
            args += ["--no-plugins"]
        else:
            args += ["--noplugins"]

        def command(*extra):
            result = subprocess.run(
                args + list(extra),
                capture_output=True,
                text=True,
                timeout=300,
                env={**os.environ, "LC_ALL": "C"},
                check=False,
            )
            print(result.stdout)
            if result.returncode:
                raise RuntimeError(result.stderr)
            return result.stdout

        command("makecache")
        result = command("repoquery", "--available", "mkbrr")
        if "mkbrr" not in result:
            raise RuntimeError("DNF did not find mkbrr")
        if install:
            command("install", "mkbrr")
            query = subprocess.run(
                ["rpm", "--root", str(root), "-q", "mkbrr"],
                capture_output=True,
                text=True,
                check=True,
            )
            print("Installed in disposable root:", query.stdout.strip())
            binary = root / "usr/bin/mkbrr"
            if not binary.is_file():
                raise RuntimeError("Installed binary missing")
            output = subprocess.run(
                [str(binary), "version"], capture_output=True, text=True, timeout=30, check=True
            )
            print(output.stdout)
            command("upgrade", "mkbrr")
        # Prove repo_gpgcheck is effective: corrupt the metadata signature, clear
        # consumer caches, and demand rejection, never silently accepting it.
        bad = work / "bad-repository"
        shutil.copytree(site / "rpm/stable", bad)
        (bad / "repodata/repomd.xml.asc").write_text("invalid signature\n")
        repo_file = repos / "community.repo"
        repo_file.write_text(
            repo_file.read_text().replace(f"{site.as_uri()}/rpm/stable/", bad.as_uri())
        )
        command("clean", "all")
        rejected = subprocess.run(
            args + ["makecache"], capture_output=True, text=True, timeout=120, check=False
        )
        if rejected.returncode == 0:
            raise RuntimeError(
                "DNF did not fail closed on bad metadata signature:\n"
                + rejected.stdout
                + rejected.stderr
            )
        if not any(
            word in (rejected.stdout + rejected.stderr).lower()
            for word in ("signature", "gpg", "repomd.xml.asc")
        ):
            raise RuntimeError("DNF failed for an unrelated reason: " + rejected.stderr)
        print("DNF correctly rejected tampered metadata signature")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--site", type=Path, required=True)
    parser.add_argument("--install", action="store_true")
    options = parser.parse_args()
    exercise(options.site.resolve(), options.install)
