from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import tarfile
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from scripts import common as repo
from scripts import copr_sync as copr

CHROOTS = ["fedora-44-x86_64", "fedora-44-aarch64", "fedora-45-x86_64", "fedora-45-aarch64"]
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
    def __init__(self, extra=None, module="github.com/autobrr/mkbrr", go="1.27.0", replace=""):
        archive = io.BytesIO()
        with tarfile.open(fileobj=archive, mode="w:gz") as tar:
            files = {
                "go.mod": f"module {module}\n\ngo {go}\n{replace}",
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

    def test_only_exact_reviewed_module_replacements_allowed(self):
        _, recipe = copr.settings("autobrr")
        replacement = recipe["module_replacements"][0]
        fixture_recipe = {
            **recipe,
            "module": "github.com/autobrr/mkbrr",
            "license_markers": ["any later version"],
        }
        for line in (replacement, replacement.replace("530e06346d7d", "aaaaaaaaaaaa"), ""):
            with tempfile.TemporaryDirectory() as directory:
                if line == replacement:
                    copr.upstream_source(
                        FakeSource(replace=line),
                        "mkbrr",
                        fixture_recipe,
                        Path(directory) / "sources",
                    )
                else:
                    with self.assertRaisesRegex(repo.InvalidRepository, "module replacements"):
                        copr.upstream_source(
                            FakeSource(replace=line),
                            "mkbrr",
                            fixture_recipe,
                            Path(directory) / "sources",
                        )

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
            "toolchain_version": "1.27.3",
            "bootstrap_version": "1.26.0",
            "toolchain_sha256": "d" * 64,
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
            "golang >= 1.26.0",
            "https://go.dev/dl/go1.27.3.src.tar.gz",
            "export GOROOT_BOOTSTRAP=",
            "./make.bash",
            "sha256sum --check",
            "%license LICENSE GOLANG-LICENSE vendor-licenses",
        ):
            self.assertIn(required, spec)
        self.assertNotIn("@VERSION@", spec)
        self.assertNotIn("curl ", spec)
        self.assertNotIn("wget ", spec)

    def test_pinned_toolchain_source_and_provenance(self):
        config, _ = copr.settings()
        toolchain = {**config["toolchain"], "sha256": hashlib.sha256(b"fixture").hexdigest()}
        network = Mock()
        network.download.side_effect = lambda url, path, **kw: path.write_bytes(b"fixture")
        with tempfile.TemporaryDirectory() as directory:
            plan = {"go_version": "1.27.0"}
            copr.toolchain_source(network, toolchain, plan, Path(directory))
            self.assertEqual(plan["toolchain_version"], toolchain["version"])
            self.assertEqual(plan["toolchain_sha256"], toolchain["sha256"])
            self.assertTrue(plan["toolchain_file"].endswith(".src.tar.gz"))
            self.assertEqual(
                network.download.call_args.kwargs["hosts"], {"go.dev", "dl.google.com"}
            )

    def test_toolchain_checksum_mismatch_or_newer_requirement_fails_closed(self):
        config, _ = copr.settings()
        network = Mock()
        network.download.side_effect = lambda url, path, **kw: path.write_bytes(b"corrupted")
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(repo.InvalidRepository):
                copr.toolchain_source(
                    network, config["toolchain"], {"go_version": "1.27.0"}, Path(directory)
                )
            network.reset_mock()
            with self.assertRaisesRegex(repo.InvalidRepository, "newer compiler"):
                copr.toolchain_source(
                    network, config["toolchain"], {"go_version": "1.28.0"}, Path(directory)
                )
            network.download.assert_not_called()

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


class FrontendTests(unittest.TestCase):
    def test_cold_offline_metadata_preserves_full_registry_record(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory)
            abbreviated = cache / "v11/metadata/registry.npmjs.org/@scope/package.jsonl"
            abbreviated.parent.mkdir(parents=True)
            abbreviated.write_text("fixture\n")
            metadata = {
                "name": "@scope/package",
                "versions": {
                    "1.0.0": {
                        "dist": {
                            "integrity": "fixture",
                            "attestations": {"provenance": {"predicateType": "fixture"}},
                        }
                    }
                },
                "time": {"1.0.0": "2026-01-01"},
            }
            network = Mock()
            network.download.side_effect = lambda url, path, **kw: path.write_text(
                json.dumps(metadata)
            )
            copr.complete_pnpm_metadata(network, cache)
            full = cache / "v11/metadata-full/registry.npmjs.org/@scope/package.jsonl"
            self.assertEqual(json.loads(full.read_text().splitlines()[1]), metadata)
            self.assertIn("@scope%2Fpackage", network.download.call_args.args[0])
            network.reset_mock()
            copr.complete_pnpm_metadata(network, cache)
            network.download.assert_not_called()

    def test_incomplete_offline_metadata_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory)
            abbreviated = cache / "v11/metadata/registry.npmjs.org/fixture.jsonl"
            abbreviated.parent.mkdir(parents=True)
            abbreviated.touch()
            network = Mock()
            network.download.side_effect = lambda url, path, **kw: path.write_text(
                '{"name":"wrong","versions":{},"time":{}}'
            )
            with self.assertRaisesRegex(repo.InvalidRepository, "Incomplete npm registry metadata"):
                copr.complete_pnpm_metadata(network, cache)

    def fixture(self, root, license="MIT"):
        config, recipe = copr.settings("autobrr")
        tool = io.BytesIO()
        with tarfile.open(fileobj=tool, mode="w:gz") as archive:
            content = b"MIT license fixture"
            info = tarfile.TarInfo("package/LICENSE")
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
        archive_bytes = tool.getvalue()
        integrity = "sha512-" + base64.b64encode(hashlib.sha512(archive_bytes).digest()).decode()
        recipe = {**recipe, "frontend": {**recipe["frontend"], "integrity": integrity}}
        source = root / "source"
        web = source / "web"
        web.mkdir(parents=True)
        (web / "package.json").write_text(
            json.dumps({"packageManager": "pnpm@11.24.0", "engines": {"node": ">=24.0"}})
        )
        (web / "pnpm-lock.yaml").write_text("fixture lockfile\n")
        package = web / "node_modules/.pnpm/fixture@1.0.0/node_modules/fixture"
        package.mkdir(parents=True)
        (package / "package.json").write_text(
            json.dumps({"name": "fixture", "version": "1.0.0", "license": license})
        )
        (package / "LICENSE").write_text("MIT license fixture")
        sources = root / "SOURCES"
        sources.mkdir()
        network = Mock()
        network.download.side_effect = lambda url, path, **kw: path.write_bytes(archive_bytes)
        return network, config, recipe, source, sources

    def test_frontend_inputs_verified_and_credentials_excluded(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(
                copr.subprocess,
                "run",
                return_value=types.SimpleNamespace(returncode=0, stdout="", stderr=""),
            ) as run,
            patch.dict(
                os.environ, {"COPR_CONFIG": "private fixture", "GH_TOKEN": "private fixture"}
            ),
        ):
            network, config, recipe, source, sources = self.fixture(Path(directory))
            plan = {"package": "autobrr", "version": "1.2.3"}
            copr.frontend_sources(network, config, recipe, plan, source, sources)
            self.assertTrue(plan["frontend"])
            self.assertEqual(run.call_count, 2)
            for call in run.call_args_list:
                self.assertIn("--ignore-scripts", call.args[0])
                self.assertIn("--frozen-lockfile", call.args[0])
                self.assertIn("--cpu=arm64", call.args[0])
                self.assertNotIn("COPR_CONFIG", call.kwargs["env"])
                self.assertNotIn("GH_TOKEN", call.kwargs["env"])
            bundle = sources / "autobrr-1.2.3-frontend.tar.gz"
            repo.verify_checksum(bundle, plan["frontend_sha256"])
            with tarfile.open(bundle) as archive:
                self.assertIn("frontend-licenses/fixture/1.0.0/LICENSE", archive.getnames())
                self.assertIn("pnpm-store", archive.getnames())
                self.assertIn("pnpm-cache", archive.getnames())

    def test_unknown_frontend_license_rejected(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(
                copr.subprocess,
                "run",
                return_value=types.SimpleNamespace(returncode=0, stdout="", stderr=""),
            ),
        ):
            network, config, recipe, source, sources = self.fixture(Path(directory), "UNLICENSED")
            with self.assertRaisesRegex(repo.InvalidRepository, "Review frontend license"):
                copr.frontend_sources(
                    network,
                    config,
                    recipe,
                    {"package": "autobrr", "version": "1.2.3"},
                    source,
                    sources,
                )

    def test_changed_pnpm_pin_or_checksum_never_executed(self):
        for kind in ("version", "checksum"):
            with (
                tempfile.TemporaryDirectory() as directory,
                patch.object(copr.subprocess, "run") as run,
            ):
                network, config, recipe, source, sources = self.fixture(Path(directory))
                if kind == "version":
                    (source / "web/package.json").write_text(
                        json.dumps({"packageManager": "pnpm@99.0.0", "engines": {"node": ">=24.0"}})
                    )
                else:
                    recipe["frontend"]["integrity"] = "sha512-" + "A" * 86 + "=="
                with self.assertRaises(repo.InvalidRepository):
                    copr.frontend_sources(
                        network,
                        config,
                        recipe,
                        {"package": "autobrr", "version": "1.2.3"},
                        source,
                        sources,
                    )
                run.assert_not_called()

    def test_web_recipes_build_both_layers_offline_and_ship_user_services(self):
        for package in ("autobrr", "qui"):
            _, recipe = copr.settings(package)
            spec = (copr.ROOT / recipe["spec"]).read_text()
            for value in (
                "install --offline --frozen-lockfile --ignore-scripts",
                "--config.cache-dir=",
                "--dir web run build",
                "go build -mod=vendor",
                "go test -mod=vendor -count=1 -short",
                "%{_userunitdir}",
            ):
                self.assertIn(value, spec)
            if package == "qui":
                self.assertIn("go test -mod=vendor -count=1 -short -parallel=1 ./...", spec)
            unit = (copr.ROOT / "packaging" / f"{package}.service").read_text()
            self.assertIn("NoNewPrivileges=true", unit)
            self.assertNotIn("User=root", unit)
            self.assertIn(f"ExecStart=/usr/bin/{package} serve", unit)


class CoprStateTests(unittest.TestCase):
    def test_copr_destination_validation(self):
        self.assertEqual(copr.target("fedora-user/autobrr-rpms"), ["fedora-user", "autobrr-rpms"])
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
                client.project_proxy.add.call_args.args[:2], ("fedora-user", "autobrr-rpms")
            )
            self.assertEqual(run.call_args_list[0].args[0][-2:], ["--env", "copr"])
            self.assertEqual(run.call_args_list[-1].args[0][-1], "fedora-user/autobrr-rpms")


if __name__ == "__main__":
    unittest.main()
