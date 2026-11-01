from __future__ import annotations

import io
import json
import os
import tarfile
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from scripts import copr_sync as copr
from scripts import sync as repo

CHROOTS = ["fedora-45-x86_64", "fedora-45-aarch64"]
COMMIT = "a" * 40


def build(state="succeeded", version="1.2.3-2", chroots=None):
    return {
        "id": 42,
        "state": state,
        "chroots": chroots or CHROOTS,
        "source_package": {"name": "mkbrr", "version": version},
    }


def client_with(builds):
    client = Mock()
    client.build_proxy.get_list.return_value = builds
    client.project_proxy.get.return_value = {
        "enable_net": False,
        "chroot_repos": dict.fromkeys(CHROOTS, "https://example.invalid/"),
    }
    return client


class FakeSource:
    def __init__(self, extra=None, module="github.com/autobrr/mkbrr", go="1.27.0"):
        archive = io.BytesIO()
        with tarfile.open(fileobj=archive, mode="w:gz") as tar:
            files = {
                "go.mod": f"module {module}\n\ngo {go}\n",
                "go.sum": "fixture\n",
                "README.md": "either version 2 of the License, or any later version",
                "LICENSE": "GNU GENERAL PUBLIC LICENSE\nVersion 2, June 1991\n",
            }
            for name, content in files.items():
                member = tarfile.TarInfo(f"mkbrr-{COMMIT}/{name}")
                member.size = len(content.encode())
                tar.addfile(member, io.BytesIO(content.encode()))
            if extra:
                tar.addfile(extra)
        self.archive = archive.getvalue()

    def json(self, url):
        if "/releases?" in url:
            return [{"tag_name": "v1.2.3", "draft": False, "prerelease": False}]
        return {"sha": COMMIT, "commit": {"committer": {"date": "2026-01-01T00:00:00Z"}}}

    def download(self, url, path, **kwargs):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(self.archive)


class SourceTests(unittest.TestCase):
    def test_reviewed_recipe_and_targets(self):
        config, recipe = copr.settings()
        self.assertEqual(config["chroots"], CHROOTS)
        self.assertGreater(recipe["release"], 1)
        self.assertIn("GPL-2.0-or-later", recipe["license"])
        with self.assertRaises(repo.InvalidRepository):
            copr.settings("unreviewed-app")

    def test_source_commit_module_and_go_version(self):
        _, recipe = copr.settings()
        with tempfile.TemporaryDirectory() as directory:
            plan, archive, source = copr.upstream_source(
                FakeSource(), "mkbrr", recipe, Path(directory) / "sources"
            )
            self.assertEqual(plan["commit"], COMMIT)
            self.assertEqual(plan["version"], "1.2.3")
            self.assertEqual(plan["go_version"], "1.27.0")
            repo.verify_checksum(archive, plan["source_sha256"])
            self.assertTrue((source / "LICENSE").is_file())

    def test_changed_module_or_malformed_go_version_rejected(self):
        _, recipe = copr.settings()
        for network in (FakeSource(module="example.org/other"), FakeSource(go="garbage")):
            with tempfile.TemporaryDirectory() as directory:
                with self.assertRaises(repo.InvalidRepository):
                    copr.upstream_source(network, "mkbrr", recipe, Path(directory) / "sources")

    def test_source_path_traversal_rejected(self):
        _, recipe = copr.settings()
        extra = tarfile.TarInfo(f"mkbrr-{COMMIT}/../../escape")
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(repo.InvalidRepository):
                copr.upstream_source(
                    FakeSource(extra), "mkbrr", recipe, Path(directory) / "sources"
                )

    def test_source_link_escape_rejected(self):
        _, recipe = copr.settings()
        extra = tarfile.TarInfo(f"mkbrr-{COMMIT}/escape")
        extra.type = tarfile.SYMTYPE
        extra.linkname = "/etc/passwd"
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(tarfile.TarError):
                copr.upstream_source(
                    FakeSource(extra), "mkbrr", recipe, Path(directory) / "sources"
                )

    def test_vendor_tar_is_deterministic(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            vendor = root / "vendor"
            vendor.mkdir()
            file = vendor / "module.go"
            file.write_text("package fixture\n")
            copr.deterministic_tar(root / "first.tar.gz", [vendor])
            os.utime(file, (1, 1))
            copr.deterministic_tar(root / "second.tar.gz", [vendor])
            self.assertEqual(
                repo.sha256(root / "first.tar.gz"), repo.sha256(root / "second.tar.gz")
            )

    def test_vendor_mutating_upstream_module_metadata_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "go.mod").write_text("module fixture\n")
            (root / "go.sum").write_text("original\n")

            def mutate(*args, **kwargs):
                (root / "go.sum").write_text("changed\n")
                return types.SimpleNamespace(returncode=0, stderr="")

            with patch.object(copr.subprocess, "run", side_effect=mutate):
                with self.assertRaisesRegex(repo.InvalidRepository, "modified upstream"):
                    copr.vendor_sources(root, root / "vendor.tar.gz")

    def test_spec_requires_offline_source_build_and_licenses(self):
        _, recipe = copr.settings()
        plan = {
            "version": "1.2.3",
            "release": 2,
            "commit": COMMIT,
            "go_version": "1.27.0",
            "source_sha256": "b" * 64,
            "vendor_sha256": "c" * 64,
            "source_date_epoch": 1767225600,
            "build_time": "2026-01-01T00:00:00Z",
            "changelog_date": "Thu Jan 01 2026",
            "license": recipe["license"],
        }
        spec = copr.render_spec(recipe, plan)
        for required in (
            "go build -mod=vendor",
            "go test -mod=vendor",
            "GOPROXY=off",
            "GOTOOLCHAIN=local",
            "golang >= 1.27.0",
            "sha256sum --check",
            "%license LICENSE GOLANG-LICENSE vendor-licenses",
        ):
            self.assertIn(required, spec)
        self.assertNotIn("@VERSION@", spec)
        self.assertNotIn("curl ", spec)
        self.assertNotIn("wget ", spec)

    def test_existing_unrelated_output_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "keep").write_text("user work")
            with self.assertRaises(repo.InvalidRepository):
                copr.prepare("mkbrr", root)
            self.assertEqual((root / "keep").read_text(), "user work")

    def test_failed_prepare_preserves_previous_package(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "copr"
            output.mkdir()
            (output / ".copr-generated").touch()
            (output / "keep").write_text("last good package")
            with patch.object(
                copr, "vendor_sources", side_effect=repo.InvalidRepository("bad vendor")
            ):
                with self.assertRaises(repo.InvalidRepository):
                    copr.prepare("mkbrr", output, network=FakeSource())
            self.assertEqual((output / "keep").read_text(), "last good package")


class CoprStateTests(unittest.TestCase):
    def test_copr_destination_validation(self):
        self.assertEqual(
            copr.target("fedora-user/autobrr-community"), ["fedora-user", "autobrr-community"]
        )
        for value in (
            "",
            "https://evil.test/project",
            "user/project/extra",
            "user/project;command",
        ):
            with self.assertRaises(repo.InvalidRepository):
                copr.target(value)

    def test_matching_success_is_idempotent(self):
        client = client_with([build()])
        self.assertEqual(
            copr.current_build(client, "owner", "project", "mkbrr", "1.2.3", 2, CHROOTS), 42
        )
        client.build_proxy.create_from_file.assert_not_called()

    def test_other_versions_or_incomplete_architecture_do_not_skip(self):
        for row in (build(version="1.2.2-2"), build(chroots=[CHROOTS[0]])):
            self.assertIsNone(
                copr.current_build(
                    client_with([row]), "owner", "project", "mkbrr", "1.2.3", 2, CHROOTS
                )
            )

    def test_failed_latest_attempt_is_retried(self):
        client = client_with([build("failed"), build()])
        self.assertIsNone(
            copr.current_build(client, "owner", "project", "mkbrr", "1.2.3", 2, CHROOTS)
        )

    def test_active_build_is_watched_not_duplicated(self):
        client = client_with([build("running")])
        with patch.object(copr, "watch_build") as watch:
            self.assertEqual(
                copr.current_build(client, "owner", "project", "mkbrr", "1.2.3", 2, CHROOTS), 42
            )
        watch.assert_called_once_with(client, 42)
        client.build_proxy.create_from_file.assert_not_called()

    def test_build_monitor_success_failure_and_timeout(self):
        client = Mock()
        client.build_proxy.get.side_effect = [build("pending"), build("running"), build()]
        with patch.object(copr.time, "sleep"):
            self.assertEqual(copr.watch_build(client, 42)["state"], "succeeded")
        for state in ("failed", "unknown"):
            client.build_proxy.get.side_effect = None
            client.build_proxy.get.return_value = build(state)
            with self.assertRaises(repo.InvalidRepository):
                copr.watch_build(client, 42)
        client.build_proxy.get.return_value = build("pending")
        with self.assertRaisesRegex(repo.InvalidRepository, "still active"):
            copr.watch_build(client, 42, timeout=0)

    def test_project_requires_targets_and_network_isolation(self):
        client = client_with([])
        copr.check_project(client, "owner", "project", CHROOTS)
        for remote in (
            {"enable_net": True, "chroot_repos": dict.fromkeys(CHROOTS)},
            {"enable_net": False, "chroot_repos": {CHROOTS[0]: "url"}},
        ):
            client.project_proxy.get.return_value = remote
            with self.assertRaises(repo.InvalidRepository):
                copr.check_project(client, "owner", "project", CHROOTS)

    def test_successful_preparation_skips_vendoring_when_current(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(copr, "copr_client", return_value=client_with([build()])),
            patch.object(copr, "vendor_sources") as vendor,
        ):
            self.assertIsNone(
                copr.prepare(
                    "mkbrr", Path(directory) / "output", "owner/project", network=FakeSource()
                )
            )
            vendor.assert_not_called()

    def test_corrupted_source_rpm_never_uploaded(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            artifact = output / "package.src.rpm"
            artifact.write_bytes(b"corrupted")
            plan = {
                "package": "mkbrr",
                "release": 2,
                "chroots": CHROOTS,
                "srpm": artifact.name,
                "srpm_sha256": "a" * 64,
            }
            (output / "plan.json").write_text(json.dumps(plan))
            client = client_with([])
            with self.assertRaises(repo.InvalidRepository):
                copr.submit(output, "owner/project", None, client=client)
            client.base_proxy.auth_check.assert_not_called()
            client.build_proxy.create_from_file.assert_not_called()

    def test_submission_waits_for_completion_and_reuses_success(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            artifact = output / "package.src.rpm"
            artifact.write_bytes(b"synthetic SRPM fixture")
            plan = {
                "package": "mkbrr",
                "version": "1.2.3",
                "release": 2,
                "chroots": CHROOTS,
                "srpm": artifact.name,
                "srpm_sha256": repo.sha256(artifact),
            }
            (output / "plan.json").write_text(json.dumps(plan))
            client = client_with([])
            client.build_proxy.create_from_file.return_value = {"id": 99}
            with patch.object(copr, "watch_build") as watch:
                self.assertEqual(copr.submit(output, "owner/project", None, client=client), 99)
            watch.assert_called_once_with(client, 99)
            client.build_proxy.get_list.return_value = [build()]
            client.build_proxy.create_from_file.reset_mock()
            self.assertEqual(copr.submit(output, "owner/project", None, client=client), 42)
            client.build_proxy.create_from_file.assert_not_called()

    def test_config_credentials_never_sent_to_another_host(self):
        sdk = types.ModuleType("copr.v3")
        sdk.Client = Mock()
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict("sys.modules", {"copr": types.ModuleType("copr"), "copr.v3": sdk}),
        ):
            config = Path(directory) / "config"
            config.write_text(
                "[copr-cli]\ncopr_url=https://evil.test\nlogin=fixture\ntoken=fixture\n"
            )
            with self.assertRaisesRegex(repo.InvalidRepository, "HTTPS endpoint"):
                copr.copr_client(config)
            sdk.Client.create_from_config_file.assert_not_called()

    def test_initialize_uses_authenticated_fedora_identity(self):
        client = client_with([])
        client.base_proxy.auth_username.return_value = "fedora-user"
        client.mock_chroot_proxy.get_list.return_value = dict.fromkeys(CHROOTS)
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(copr, "copr_client", return_value=client),
            patch.object(repo, "run") as run,
        ):
            config = Path(directory) / "config"
            config.write_text("private config fixture")
            copr.initialize(config, "different-github-owner/repo")
            self.assertEqual(
                client.project_proxy.add.call_args.args[:2], ("fedora-user", "autobrr-community")
            )
            self.assertEqual(run.call_args_list[-1].args[0][-1], "fedora-user/autobrr-community")


if __name__ == "__main__":
    unittest.main()
