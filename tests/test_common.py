from __future__ import annotations

import tempfile
import unittest
import urllib.request
from pathlib import Path

from scripts import common as repo


class CommonTests(unittest.TestCase):
    def test_stable_release_selection_and_duplicate_rejection(self):
        releases = [
            {"tag_name": "v1.9.0"},
            {"tag_name": "v1.10.0"},
            {"tag_name": "v2.0.0-rc1", "prerelease": True},
            {"tag_name": "v3.0.0", "draft": True},
        ]
        self.assertEqual(repo.select_releases(releases)[0]["tag_name"], "v1.10.0")
        for invalid in (
            [],
            [{"tag_name": "v1.2"}],
            [{"tag_name": "v1.2.3"}, {"tag_name": "1.2.3"}],
        ):
            with self.assertRaises(repo.InvalidRepository):
                repo.select_releases(invalid)

    def test_checksum_rejects_corruption_and_bad_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "fixture"
            path.write_bytes(b"original")
            digest = repo.sha256(path)
            repo.verify_checksum(path, digest)
            path.write_bytes(b"modified")
            for value in (digest, "", "invalid", None):
                with self.assertRaises(repo.InvalidRepository):
                    repo.verify_checksum(path, value)

    def test_download_urls_are_https_and_allowlisted(self):
        self.assertEqual(
            repo.validate_url("https://api.github.com/repos/example/project", repo.HOSTS),
            "https://api.github.com/repos/example/project",
        )
        for url in (
            "http://api.github.com/",
            "https://evil.test/",
            "https://api.github.com:444/",
            "https://user:password@api.github.com/",
        ):
            with self.assertRaises(repo.InvalidRepository):
                repo.validate_url(url, repo.HOSTS)

    def test_redirects_drop_authorization_and_reject_unknown_hosts(self):
        handler = repo.SafeRedirect({"api.github.com", "codeload.github.com"})
        request = urllib.request.Request(
            "https://api.github.com/archive", headers={"Authorization": "Bearer fixture"}
        )
        redirect = handler.redirect_request(
            request, None, 302, "Found", {}, "https://codeload.github.com/archive"
        )
        self.assertIsNone(redirect.get_header("Authorization"))
        with self.assertRaises(repo.InvalidRepository):
            handler.redirect_request(request, None, 302, "Found", {}, "https://evil.test/")

    def test_github_credentials_cannot_be_sent_to_other_services(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(repo.InvalidRepository, "API token boundary"):
                repo.Network().download(
                    "https://registry.npmjs.org/pnpm",
                    Path(directory) / "file",
                    hosts={"registry.npmjs.org"},
                    api=True,
                )

    def test_names_and_github_repository_are_validated(self):
        self.assertEqual(repo.safe_name("pnpm-11.24.0.tgz"), "pnpm-11.24.0.tgz")
        for name in ("../escape", "nested/file", "/absolute", "", None):
            with self.assertRaises(repo.InvalidRepository):
                repo.safe_name(name)
        repo.validate_github_repository("ulnanlu/autobrr-rpms")
        for name in ("", "owner/repo/extra", "https://example.test/owner/repo"):
            with self.assertRaises(repo.InvalidRepository):
                repo.validate_github_repository(name)

    def test_command_failures_are_not_hidden(self):
        with self.assertRaises(repo.InvalidRepository):
            repo.run(["false"])


if __name__ == "__main__":
    unittest.main()
