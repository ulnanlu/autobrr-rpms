# COPR source-build pilot

The pilot packages **mkbrr** from unmodified upstream source for **Fedora 44 and 45, x86_64 and aarch64**. Fedora 44 bootstraps Go 1.27.3 from checksum-pinned source using the distribution's Go 1.26; Fedora 45 uses its distribution compiler. Both build offline. Other applications, ARMv6, and EPEL targets are not packaged yet.

COPR publication requires your Fedora account/API credentials. GitHub authentication is separate. Until configured and a COPR build succeeds, Pages remains the live installation channel.

## Activate

1. Create/sign in with your [Fedora account](https://accounts.fedoraproject.org/) at [COPR](https://copr.fedorainfracloud.org/).
2. Copy your configuration from [the COPR API page](https://copr.fedorainfracloud.org/api/) to `~/.config/copr`, outside this repository. Run `chmod 600 ~/.config/copr`. Never commit or paste the token into chat.
3. From this checkout, create the project and configure GitHub:

```bash
uv run --with 'copr>=2.5,<3' python scripts/copr_sync.py init \
  --configure-github ulnanlu/mkbrr-dnf-repo
gh workflow run copr.yml --ref main
gh run list --workflow copr.yml --limit 5
gh run watch RUN_ID --exit-status
```

`init` authenticates your Fedora identity, creates `YOUR_FEDORA_USER/autobrr-community`, and validates its targets and disabled build networking. It stores `COPR_CONFIG` in GitHub's protected `copr` environment and sets `COPR_PROJECT` to the actual Fedora owner/project. Existing projects are checked, not overwritten. Keep the API token renewed before its expiry. For another GitHub repository, first create that environment and restrict it to the default branch; this repository is already configured.

After a successful COPR build, inspect its project signing key and verify its fingerprint. COPR uses its own project key, **not** the existing Pages key. On a supported target:

```bash
sudo dnf copr enable YOUR_FEDORA_USER/autobrr-community
sudo dnf install mkbrr
# If mkbrr is already installed:
sudo dnf upgrade mkbrr
```

Keep package signature checking enabled. The COPR plugin is supplied by `dnf5-plugins` or `dnf-plugins-core`, depending on the distribution. Only remove `/etc/yum.repos.d/mkbrr-community.repo` after verifying the replacement; this setup does not disable or modify Pages.

## Pipeline

`copr.yml` checks stable upstream releases daily at **05:47 UTC**, on manual dispatch, and on packaging changes. It is skipped until `COPR_PROJECT` is configured; manual runs without configuration fail safely.

- Resolve the release to a commit and checksum the unmodified source archive.
- Run `go mod vendor` with Go's checksum database enabled. Reject changes to upstream `go.mod`/`go.sum`.
- Put dependency source, license notices, checksums, and provenance into an SRPM.
- Compile locally, run upstream short tests, and test-install in a disposable root.
- Include checksum-pinned Go source for Fedora 44's compiler bootstrap; never lower upstream's Go requirement.
- Upload the SRPM to COPR and wait for all four targets to succeed. COPR signs and hosts the resulting RPMs.

The RPM build runs with network access disabled, `GOTOOLCHAIN=local`, and `GOPROXY=off`; no upstream application or compiler binaries are downloaded during the build. Dependency resolution happens beforehand. All source archives are included in the SRPM and rechecked during `%prep`. Go source version/checksum pins live in `config/copr.json`; newer upstream compiler requirements fail closed until reviewed. The optional upstream PGO profile is not used.

Successful version/release/target combinations are skipped. In-flight builds are watched rather than duplicated; failed builds can be retried. Bump `release` in `config/copr.json` for packaging changes to an existing version. Release 2 supersedes the upstream RPM's release 1 without an Epoch change. Review bundled licenses when updating recipes; the upstream README grants GPL-2.0-or-later, and dependency notices are installed with the package.

The secret is available only in the default-branch submission job, which uses an isolated temporary config. Pull-request validation has no COPR credentials. Force a rebuild with `gh workflow run copr.yml -f force=true`; this never bypasses checks.

## Local verification

Use either target release without installing build tools on the host:

```bash
podman run --rm -it -v "$PWD:/work:Z" -w /work registry.fedoraproject.org/fedora:44 bash
dnf -y install python3 golang rpm-build rpm-sign gnupg2 redhat-rpm-config git
python3 -m unittest discover -s tests -p test_copr_sync.py -v
python3 scripts/copr_sync.py prepare
python3 scripts/copr_sync.py build
python3 scripts/copr_sync.py test-install
```

These commands build an **unsigned local RPM**, not a published COPR package. `test-install` signs a temporary copy with a disposable key, verifies it, and uses an isolated RPM database/installroot; it never disables signature checks. Artifacts under `.work/copr/` are gitignored. `copr-validate.yml` tests both Fedora releases without credentials; COPR builds both architectures natively.

To submit locally after activation, install `copr-cli` or use `uv run --with copr`, then run `python scripts/copr_sync.py submit --target YOUR_FEDORA_USER/autobrr-community`. COPR controls build availability and retention; it is not unlimited storage. Applications with missing redistribution licenses remain excluded.
