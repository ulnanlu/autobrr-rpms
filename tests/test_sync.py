from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import shutil
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import Mock, patch

from scripts import sync


def asset(name, identifier=1):
    return {
        "name": name,
        "id": identifier,
        "state": "uploaded",
        "size": 42,
        "updated_at": "2026-01-01T00:00:00Z",
        "digest": None,
        "browser_download_url": f"https://github.com/autobrr/mkbrr/releases/download/v1.2.3/{name}",
    }


def release(tag="v1.2.3", **kwargs):
    version = tag.removeprefix("v")
    return {
        "tag_name": tag,
        "id": 1,
        "draft": False,
        "prerelease": False,
        "assets": [
            asset(f"mkbrr_{version}_linux_amd64.rpm"),
            asset(f"mkbrr_{version}_linux_arm64.rpm", 2),
            asset(f"mkbrr_{version}_linux_armv6.rpm", 3),
            asset(f"mkbrr_{version}_checksums.txt", 4),
        ],
        **kwargs,
    }


class SelectionTests(unittest.TestCase):
    def test_latest_stable_semantic_order(self):
        data = [
            release("v1.9.0"),
            release("v1.10.0"),
            release("v9.0.0", draft=True),
            release("v8.0.0", prerelease=True),
            release("v1.8.0"),
            release("v1.7.0"),
        ]
        self.assertEqual(
            [r["tag_name"] for r in sync.select_releases(data)], ["v1.10.0", "v1.9.0", "v1.8.0"]
        )

    def test_no_stable_and_unexpected_tag_fail(self):
        for data in ([], [release(prerelease=True)], [release("stable")]):
            with self.assertRaises(sync.InvalidRepository):
                sync.select_releases(data)

    def test_rpm_architecture_discovery(self):
        rpms, manifest = sync.discover(release())
        self.assertEqual({a["rpm_arch"] for a in rpms}, {"x86_64", "aarch64", "armv6hl"})
        self.assertEqual(manifest["name"], "mkbrr_1.2.3_checksums.txt")

    def test_missing_rpm_manifest_unknown_arch_and_unsafe_filename(self):
        for assets in (
            [asset("mkbrr_1.2.3_checksums.txt")],
            release()["assets"][:-1],
            [*release()["assets"], asset("../attack.rpm")],
            [*release()["assets"], asset("mkbrr_1.2.3_linux_mystery.rpm")],
            [*release()["assets"], release()["assets"][0]],
        ):
            with self.assertRaises(sync.InvalidRepository):
                sync.discover(release(assets=assets))

    def test_incomplete_asset_rejected(self):
        data = release()
        data["assets"][0]["state"] = "new"
        with self.assertRaises(sync.InvalidRepository):
            sync.discover(data)

    def test_architecture_drop_rejected(self):
        class FakeNetwork:
            def releases(self):
                newest = release("v1.3.0")
                del newest["assets"][2]
                return [newest, release()]

            def json(self, url):
                return {"sha": "a" * 40}

        with self.assertRaisesRegex(sync.InvalidRepository, "dropped an architecture"):
            sync.prepare_plan(
                FakeNetwork(), "owner/repo", "https://owner.github.io/repo", 3, "a" * 40
            )


class VerificationTests(unittest.TestCase):
    def test_metadata_xml_rejects_entities(self):
        for xml in (b'<!DOCTYPE a [<!ENTITY b "expanded">]><a>&b;</a>', b'<!ENTITY a "bad"><a/>'):
            with self.assertRaises(sync.InvalidRepository):
                sync.parse_xml(xml)

    def test_checksums_and_invalid_checksum(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "mkbrr.rpm"
            path.write_bytes(b"verified data")
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            parsed = sync.parse_checksums(f"{digest}  mkbrr.rpm\n")
            sync.verify_checksum(path, parsed[path.name])
            self.assertEqual(sync.parse_checksums(f"{digest} *mkbrr.rpm")[path.name], digest)
            for invalid in ("0" * 64, None):
                with self.assertRaises(sync.InvalidRepository):
                    sync.verify_checksum(path, invalid)

    def test_bad_manifest_formats(self):
        digest = "a" * 64
        for text in ("", "nope", f"{digest}  ../escape", f"{digest}  a\n{digest}  a"):
            with self.assertRaises(sync.InvalidRepository):
                sync.parse_checksums(text)

    def test_https_and_token_redirect_boundary(self):
        for url in (
            "http://github.com/file",
            "https://evil.test/file",
            "https://name@github.com/file",
            "https://github.com:99/file",
        ):
            with self.assertRaises(sync.InvalidRepository):
                sync.validate_url(url, sync.HOSTS)
        request = sync.urllib.request.Request(
            "https://api.github.com/a", headers={"Authorization": "secret"}
        )
        redirected = sync.SafeRedirect(sync.HOSTS).redirect_request(
            request, None, 302, "Found", {}, "https://github.com/b"
        )
        self.assertFalse(redirected.has_header("Authorization"))

    def test_rpm_identity_validation(self):
        responses = [
            "Payload SHA256 digest: OK",
            "mkbrr\t1.2.3\t1\tx86_64\t0\n",
            "/usr/bin/mkbrr\n",
            "",
            "(none)\n(none)\n",
        ]
        with patch.object(sync, "run", side_effect=responses):
            self.assertEqual(sync.rpm_metadata(Path("a.rpm"), "1.2.3", "x86_64")["arch"], "x86_64")
        for invalid in (
            "other\t1.2.3\t1\tx86_64\t0",
            "mkbrr\t1.2.4\t1\tx86_64\t0",
            "mkbrr\t1.2.3\t1\taarch64\t0",
            "mkbrr\t1.2.3\t1\tx86_64\t1",
        ):
            with patch.object(sync, "run", side_effect=[responses[0], invalid]):
                with self.assertRaises(sync.InvalidRepository):
                    sync.rpm_metadata(Path("a.rpm"), "1.2.3", "x86_64")

    def test_rpm_integrity_and_scriptlets(self):
        with patch.object(sync, "run", return_value="Payload SHA256 digest: BAD"):
            with self.assertRaises(sync.InvalidRepository):
                sync.rpm_metadata(Path("a.rpm"), "1.2.3", "x86_64")
        with patch.object(
            sync,
            "run",
            side_effect=[
                "Payload SHA256 digest: OK",
                "mkbrr\t1.2.3\t1\tx86_64\t0",
                "/usr/bin/mkbrr",
                "postinstall script",
            ],
        ):
            with self.assertRaises(sync.InvalidRepository):
                sync.rpm_metadata(Path("a.rpm"), "1.2.3", "x86_64")

    def test_go_module_hash_and_escaping(self):
        content = b"module example.org/test\n"
        entry = hashlib.sha256(content).hexdigest() + "  go.mod\n"
        expected = "h1:" + sync.base64.b64encode(hashlib.sha256(entry.encode()).digest()).decode()
        self.assertEqual(sync.go_h1([("go.mod", content)]), expected)
        self.assertEqual(
            sync.escape_module("github.com/Masterminds/semver"), "github.com/!masterminds/semver"
        )
        with self.assertRaises(sync.InvalidRepository):
            sync.escape_module("../../escape")


class OutputTests(unittest.TestCase):
    def test_incomplete_manifest_and_checksum_failure_precede_signing(self):
        for manifest in (
            "a" * 64 + "  missing.rpm\n",
            "a" * 64 + "  mkbrr_1.2.3_linux_amd64.rpm\n",
        ):
            data = release(assets=[release()["assets"][0], release()["assets"][-1]])

            class FakeNetwork:
                def __init__(self, checksum_text):
                    self.manifest = checksum_text

                def download(self, url, target, **kwargs):
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(self.manifest.encode() if "checksums" in url else b"bad rpm")

            with (
                tempfile.TemporaryDirectory() as directory,
                patch.object(sync, "mirror_sources", return_value={}),
                patch.object(sync, "rpm_metadata") as metadata,
            ):
                signer = Mock(public="public", fingerprint="a" * 40)
                with self.assertRaises(sync.InvalidRepository):
                    sync.build_site(
                        FakeNetwork(manifest),
                        Path(directory) / "site",
                        {"releases": [{"commit": "a" * 40}]},
                        [data],
                        signer,
                        True,
                    )
                signer.sign_rpm.assert_not_called()
                metadata.assert_not_called()

    def test_project_url_and_repo_configuration(self):
        url = sync.base_url("SomeOwner/some-repository")
        self.assertEqual(url, "https://someowner.github.io/some-repository")
        config = sync.render_repo(url)
        for setting in (
            "[mkbrr-community]",
            "gpgcheck=1",
            "repo_gpgcheck=1",
            "enabled=1",
            f"baseurl={url}/rpm/stable/",
            f"gpgkey={url}/RPM-GPG-KEY-mkbrr-community",
        ):
            self.assertIn(setting, config)
        self.assertEqual(
            sync.base_url("owner/repo", "https://rpm.example.org/repo/"),
            "https://rpm.example.org/repo",
        )
        with self.assertRaises(sync.InvalidRepository):
            sync.base_url("bad owner/repo")

    def test_remote_state_missing_bootstraps_but_network_errors_fail(self):
        with tempfile.TemporaryDirectory() as directory:
            for status in (404, 403, 503):
                network = Mock()
                network.download.side_effect = sync.urllib.error.HTTPError(
                    "https://owner.github.io/repo/state.json", status, "failure", {}, None
                )
                if status == 404:
                    self.assertFalse(
                        sync.previous_matches(
                            network,
                            "https://owner.github.io/repo",
                            {},
                            Path("unused-key"),
                            Path(directory),
                        )
                    )
                else:
                    with self.assertRaises(sync.urllib.error.HTTPError):
                        sync.previous_matches(
                            network,
                            "https://owner.github.io/repo",
                            {},
                            Path("unused-key"),
                            Path(directory),
                        )

    def test_secrets_required_no_unsigned_fallback(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(sync.InvalidRepository, "Missing signing configuration"):
                sync.Signer(Path(directory))

    def test_preserve_unrelated_output(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            args = argparse.Namespace(repository="owner/repo", base_url=None, output=path)
            (path / "keep.txt").write_text("user work")
            with self.assertRaises(sync.InvalidRepository):
                sync.synchronize(args)
            self.assertEqual((path / "keep.txt").read_text(), "user work")

    def test_public_key_renewal_changes_plan_implementation_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "config").mkdir()
            public = root / "config/signing-key.asc"
            with patch.object(sync, "ROOT", root):
                before = sync.implementation_hash()
                public.write_text("renewed public key fixture")
                self.assertNotEqual(before, sync.implementation_hash())

    def test_failure_preserves_previous_site(self):
        class FakeSigner:
            fingerprint = "a" * 40

            def __init__(self, *args, **kwargs):
                pass

            def close(self):
                pass

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "site"
            path.mkdir()
            (path / ".mkbrr-generated").touch()
            (path / "keep").write_text("previous valid deployment")
            args = argparse.Namespace(
                repository="owner/repo",
                base_url=None,
                output=path,
                dry_run=True,
                retain=3,
                force=True,
                check_published=False,
            )
            with (
                patch.object(sync, "Signer", FakeSigner),
                patch.object(sync, "prepare_plan", return_value=({}, [])),
                patch.object(sync, "build_site", side_effect=sync.InvalidRepository("incomplete")),
            ):
                with self.assertRaises(sync.InvalidRepository):
                    sync.synchronize(args)
            self.assertEqual((path / "keep").read_text(), "previous valid deployment")

    def test_local_idempotency_validates_before_skipping(self):
        class FakeSigner:
            fingerprint = "a" * 40

            def __init__(self, *args, **kwargs):
                pass

            def close(self):
                pass

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "site"
            path.mkdir()
            (path / ".mkbrr-generated").touch()
            (path / "state.json").write_text(json.dumps({"plan": {"stable": True}}))
            args = argparse.Namespace(
                repository="owner/repo",
                base_url=None,
                output=path,
                dry_run=False,
                retain=3,
                force=False,
                check_published=False,
            )
            with (
                patch.object(sync, "Signer", FakeSigner),
                patch.object(sync, "prepare_plan", return_value=({"stable": True}, [])),
                patch.object(sync, "validate_site") as validate,
                patch.object(sync, "build_site") as build,
            ):
                self.assertFalse(sync.synchronize(args))
                validate.assert_called_once_with(path)
                build.assert_not_called()


@unittest.skipUnless(
    all(shutil.which(t) for t in ("rpmbuild", "rpmkeys", "rpmsign", "gpg", "createrepo_c")),
    "Packaging tools unavailable (required and installed in CI)",
)
class SigningIntegrationTests(unittest.TestCase):
    def test_imported_private_key_and_fixed_key_site_idempotency(self):
        with tempfile.TemporaryDirectory(prefix="mkbrr-import-test-") as directory:
            root = Path(directory)
            private = root / "private"
            private.mkdir()
            signer = sync.Signer(private, dry_run=True)
            config = root / "config"
            config.mkdir()
            for template in (sync.ROOT / "config").glob("*.in"):
                shutil.copy2(template, config / template.name)
            (config / "signing-key.asc").write_text(signer.public)
            secret = signer.gpg(
                "--pinentry-mode",
                "loopback",
                "--passphrase-file",
                signer.passfile,
                "--armor",
                "--export-secret-keys",
                signer.fingerprint,
            )
            test_env = {
                "RPM_SIGNING_KEY": secret,
                "RPM_SIGNING_FINGERPRINT": signer.fingerprint,
                "RPM_SIGNING_PASSPHRASE": "disposable-test-passphrase",
            }
            try:
                with patch.dict(os.environ, test_env), patch.object(sync, "ROOT", root):
                    imported_home = root / "imported"
                    imported_home.mkdir()
                    imported = sync.Signer(imported_home)
                    try:
                        message = root / "message.txt"
                        message.write_text("headless import/sign test")
                        imported.sign_file(message)
                        imported.verify_file(message)
                        self.assertNotIn("RPM_SIGNING_KEY", imported.env)
                        self.assertNotIn("RPM_SIGNING_PASSPHRASE", imported.env)
                    finally:
                        imported.close()
                    # A valid generated fixture site exercises real file signatures,
                    # templates and state checks without requiring network access.
                    args = argparse.Namespace(
                        repository="owner/repo",
                        base_url=None,
                        output=root / "site",
                        dry_run=False,
                        retain=3,
                        force=False,
                        check_published=False,
                    )

                    def fixture_site(network, site, plan, releases, active_signer, dry_run):
                        site.mkdir()
                        (site / ".mkbrr-generated").touch()
                        (site / "state.json").write_text(sync.canonical({"plan": plan}))
                        active_signer.sign_file(site / "state.json")
                        (site / "RPM-GPG-KEY-mkbrr-community").write_text(active_signer.public)

                    def validate_fixture(site):
                        sync.verify_detached(
                            site / "RPM-GPG-KEY-mkbrr-community", site / "state.json"
                        )

                    with (
                        patch.object(sync, "prepare_plan", return_value=({"fixed": True}, [])),
                        patch.object(sync, "build_site", side_effect=fixture_site) as build,
                        patch.object(sync, "validate_site", side_effect=validate_fixture),
                    ):
                        self.assertTrue(sync.synchronize(args))
                        before = sync.sha256(args.output / "state.json.asc")
                        self.assertFalse(sync.synchronize(args))
                        self.assertEqual(before, sync.sha256(args.output / "state.json.asc"))
                        self.assertEqual(build.call_count, 1)
            finally:
                signer.close()

    def test_disposable_signing_metadata_and_tampering(self):
        # Build only a tiny synthetic fixture, never rebuild mkbrr. Live integration
        # in validate.yml verifies the untouched official upstream RPMs as well.
        with tempfile.TemporaryDirectory(prefix="mkbrr-fixture-") as directory:
            root = Path(directory)
            spec = root / "fixture.spec"
            spec.write_text("""Name: mkbrr
Version: 1.2.3
Release: 1
Summary: Synthetic repository test fixture, not the application
License: MIT
BuildArch: x86_64
%description
Synthetic fixture only.
%install
mkdir -p %{buildroot}/usr/bin
echo fixture > %{buildroot}/usr/bin/mkbrr
%files
/usr/bin/mkbrr
""")
            sync.run(["rpmbuild", "--define", f"%_topdir {root}", "-bb", spec])
            rpm = next(root.rglob("*.rpm"))
            original = sync.sha256(rpm)
            private = root / "private"
            private.mkdir()
            signer = sync.Signer(private, dry_run=True)
            try:
                sync.rpm_metadata(rpm, "1.2.3", "x86_64")
                with self.assertRaises(sync.InvalidRepository):
                    signer.verify_rpm(rpm)
                signer.sign_rpm(rpm)
                self.assertNotEqual(original, sync.sha256(rpm))
                signer.verify_rpm(rpm)
                repo = root / "repo"
                repo.mkdir()
                shutil.copy2(rpm, repo / rpm.name)
                sync.run(
                    ["createrepo_c", "--checksum", "sha256", "--general-compress-type", "gz", repo]
                )
                self.assertEqual(sync.validate_metadata(repo), 1)
                metadata = repo / "repodata/repomd.xml"
                signer.sign_file(metadata)
                metadata.write_bytes(metadata.read_bytes() + b"tampered")
                with self.assertRaises(sync.InvalidRepository):
                    signer.verify_file(metadata)
                with rpm.open("r+b") as stream:
                    stream.seek(-1, 2)
                    stream.write(b"X")
                with self.assertRaises(sync.InvalidRepository):
                    signer.verify_rpm(rpm)
            finally:
                signer.close()


class SourceTests(unittest.TestCase):
    def test_source_mirroring_and_dependency_checksum_rejection(self):
        module = "example.org/module"
        version = "v1.0.0"
        mod = b"module example.org/module\n"
        zip_content = io.BytesIO()
        with zipfile.ZipFile(zip_content, "w") as archive:
            archive.writestr(f"{module}@{version}/go.mod", mod)
            archive.writestr(f"{module}@{version}/LICENSE", "Fixture source license")
        entries = [
            (f"{module}@{version}/go.mod", mod),
            (f"{module}@{version}/LICENSE", b"Fixture source license"),
        ]
        sums = (
            f"{module} {version} {sync.go_h1(entries)}\n"
            f"{module} {version}/go.mod {sync.go_h1([('go.mod', mod)])}\n"
        )
        source = io.BytesIO()
        with tarfile.open(fileobj=source, mode="w:gz") as archive:
            files = {
                "LICENSE": "GNU GENERAL PUBLIC LICENSE\nVersion 2, June 1991",
                "go.sum": sums,
                "go.mod": f"module example.org/app\nrequire {module} {version}\n",
                ".goreleaser.yml": "fixture",
                ".github/workflows/release.yml": "fixture",
            }
            for name, content in files.items():
                info = tarfile.TarInfo(f"fixture/{name}")
                info.size = len(content.encode())
                archive.addfile(info, io.BytesIO(content.encode()))

        class FakeNetwork:
            corrupt = False

            def download(self, url, target, **kwargs):
                target.parent.mkdir(parents=True, exist_ok=True)
                content = (
                    source.getvalue()
                    if "codeload" in url
                    else mod
                    if url.endswith(".mod")
                    else zip_content.getvalue()
                )
                target.write_bytes(
                    b"corruption" if self.corrupt and url.endswith(".mod") else content
                )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = sync.mirror_sources(FakeNetwork(), root / "site", release(), "a" * 40)
            self.assertEqual(len(result["dependencies"]), 2)
            sync.verify_checksum(root / "site" / result["path"], result["sha256"])
            network = FakeNetwork()
            network.corrupt = True
            with self.assertRaisesRegex(sync.InvalidRepository, "Go source checksum mismatch"):
                sync.mirror_sources(network, root / "bad-site", release(), "a" * 40)


if __name__ == "__main__":
    unittest.main()
