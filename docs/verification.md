# Verification record

Verified on 2026-10-31. These are executed tests, not a proposed test plan.

| Check | Result |
| --- | --- |
| `python -m compileall -q scripts tests` | Passed |
| Ruff lint and formatting checks | Passed |
| `actionlint` 1.7.12 | Both workflows passed |
| Standard-library unittest suite in Ubuntu 24.04 with packaging tools | **24 tests passed, zero skips** |
| Passphrase-protected disposable RSA signing | Passed with Ubuntu RPM 4.18, `rpmsign`, `rpmkeys`, GnuPG |
| Imported ASCII-armored private key and fixed-key idempotency | Passed using a disposable test key, isolated homes/databases |
| Unsigned/corrupted RPM and tampered detached signature rejection | Passed |
| Original checksum failures and incomplete manifests rejected before signing | Passed |
| Go source `h1` verification and corruption rejection | Passed |
| Failed builds preserve previous generated output; unrelated output refused | Passed |
| Real upstream stable release synchronization | **v1.27.0, v1.26.0, v1.25.1; 9 RPMs; all three upstream architectures** |
| Generated repository metadata, signatures and source digests | Passed; complete dry-run site approximately **96.6 MiB** |
| Ubuntu 24.04 DNF4 consumer | Read all 9 packages; installed native x86_64 1.27.0 in a disposable root; ran `mkbrr version`; no-op upgrade succeeded |
| Fedora 44 DNF5 consumer | Same metadata/install/version/no-op-upgrade checks passed in a disposable Fedora container |
| DNF4 and DNF5 tampered metadata rejection | Passed with `repo_gpgcheck=1` and `skip_if_unavailable=0` |
| Generated project URL | `https://ulnanlu.github.io/mkbrr-dnf-repo/rpm/stable/` |
| Package and metadata verification options | `gpgcheck=1`, `repo_gpgcheck=1`, TLS verification enabled |

The test harness explicitly sets `skip_if_unavailable=0`: DNF4 otherwise may
report a successful makecache command after skipping an invalid repository.
The distributed repository configuration also fails closed in this way.
DNF's installation subcommand requires root even with a disposable installroot;
the GitHub-hosted Ubuntu test uses `sudo`, and local installation tests run as
container root. Metadata-only consumption works without elevated privileges.

The host is Fedora 44 and has no host `rpmsign`/`createrepo_c` installation;
packaging verification was performed in a disposable Ubuntu 24.04 container
matching the selected CI environment, without installing tools or packages
into the host. Fedora consumer testing used the Fedora project's Fedora 44
container image. The GitHub validation workflow repeats these checks.

Only native **x86_64** installation/execution was tested. ARM packages were
downloaded, checksum/header/integrity/signature-verified, and represented in
metadata, but were not installed or executed on ARM hardware. A real
old-to-new application upgrade was not tested; the upgrade check used the
already installed current version. Synthetic RPMs are used only for hermetic
unit fixtures; the application itself was never rebuilt.

## GitHub configuration

- Standalone public repository: <https://github.com/ulnanlu/mkbrr-dnf-repo>.
- Pages configured via API with `build_type=workflow` and HTTPS enforced.
- `github-pages` deployment environment permits only the `main` branch.
- The maintainer configured the production signing secrets and committed the
  public key. The fingerprint variable was corrected to contain 40 contiguous
  hexadecimal characters rather than GPG's space-grouped display format.
- Production fingerprint: `EB74D9B819729F12F360B24EA07E0E6DD4BBEF5C`.
- [First production publication](https://github.com/ulnanlu/mkbrr-dnf-repo/actions/runs/41166767778)
  succeeded on 2026-10-31, including signing, DNF consumption, artifact upload,
  and deployment. **No disposable signing key was published.**
- Live site: <https://ulnanlu.github.io/mkbrr-dnf-repo/>. Its index, `.repo`,
  public key, metadata and detached signature, provenance and signed state were
  downloaded successfully over HTTPS. All detached signatures were verified
  against the committed production public key; a downloaded x86_64 RPM also
  passed signed-checksum, RPM signature and integrity verification. The live
  repository contains 9 RPMs across v1.27.0, v1.26.0 and v1.25.1.

See [README.md](../README.md#signing-key-setup) for exact setup commands and the
[validation workflow](https://github.com/ulnanlu/mkbrr-dnf-repo/actions/workflows/validate.yml)
for subsequent CI results.
