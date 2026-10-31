# mkbrr Unofficial Community RPM Repository

An independently maintained DNF/YUM repository that redistributes the **official [autobrr/mkbrr release RPMs](https://github.com/autobrr/mkbrr/releases)**. It does not fork, patch, or rebuild mkbrr. Downloads are verified against upstream SHA-256 checksums **before** being independently signed, then published through GitHub Pages.

**Unofficial: not maintained, affiliated with, or endorsed by the autobrr team.** Installing these packages means trusting this third-party repository's signing key and maintainers, in addition to upstream and GitHub. Upstream checksums establish consistency with the release, not independent upstream identity authentication.

## Publication status and URLs

This checkout is configured for [`ulnanlu/mkbrr-dnf-repo`](https://github.com/ulnanlu/mkbrr-dnf-repo). Its intended Pages URL is **https://ulnanlu.github.io/mkbrr-dnf-repo/** and the intended configuration URL is **https://ulnanlu.github.io/mkbrr-dnf-repo/mkbrr-community.repo**.

**Production publication requires the maintainer's signing-key setup below. There is no production key or fingerprint in the initial checkout, and no unsigned or disposable-key repository will be deployed.** A successful Pages deployment in Actions, not the existence of this README, establishes that the repository is live. `config/signing-key.asc`, once added, is the public key and independent fingerprint reference; no production fingerprint is invented here.

When using this as a template under another account, all generated URLs come from `GITHUB_REPOSITORY` and the `actions/configure-pages` URL. No publishing code hardcodes an owner. For local commands use `gh repo view --json nameWithOwner --jq .nameWithOwner`. The examples below use the actual configured owner for convenience.

## Supported systems

- Fedora with current DNF5, and compatible RPM/DNF distributions such as RHEL, Rocky Linux, AlmaLinux, and CentOS Stream with DNF4. Compatibility still depends on the upstream binary, CPU, kernel, and distribution policies; this is not a claim that every distribution/version was installation-tested.
- Observed official RPM architectures: **x86_64** (asset `amd64`), **aarch64** (asset `arm64`), and **armv6hl** (asset `armv6`). Every discovered supported RPM is published to a single mixed-architecture repository; DNF selects compatible architectures. ARMv6 hard-float is not automatically compatible with all ARMv7 distributions, and current Fedora does not support ARMv6.
- Other RPM architectures will be published if upstream adds them and they pass the explicit asset-to-RPM architecture validation in `scripts/sync.py`. Unknown naming/architecture changes fail for review instead of being guessed or silently omitted.
- Requires HTTPS and GPG-aware DNF/YUM. DNF4 and DNF5 consumer tests are included. Very old YUM clients are not certified; use a supported distribution. No distribution-specific binaries are rebuilt.

## Install

### 1. Verify and import the repository key

Obtain the **full 40-hex fingerprint** through an independent trusted channel from the maintainer. Inspect the committed public key at [`config/signing-key.asc`](https://github.com/ulnanlu/mkbrr-dnf-repo/blob/main/config/signing-key.asc) once configured, preferably from a known-good checkout; a fingerprint downloaded from the same site as the key alone is not independent verification.

```bash
curl --fail --location --proto '=https' --proto-redir '=https' \
  https://ulnanlu.github.io/mkbrr-dnf-repo/RPM-GPG-KEY-mkbrr-community \
  -o /tmp/mkbrr-community.asc
gpg --show-keys --with-fingerprint /tmp/mkbrr-community.asc
# STOP unless the full fingerprint matches the independently obtained value.
sudo rpmkeys --import /tmp/mkbrr-community.asc
```

The primary signing key is RSA (at least 3072 bits; 4096 recommended). Package signatures and detached metadata signatures use SHA-256. DNF may ask separately to import the same key for its per-repository metadata keyring: compare its fingerprint too.

### 2. Add the repository

Current Fedora / DNF5 (install the distribution's `dnf5-plugins` if `config-manager` is unavailable):

```bash
sudo dnf config-manager addrepo --from-repofile=https://ulnanlu.github.io/mkbrr-dnf-repo/mkbrr-community.repo
```

DNF4 (`dnf-plugins-core` supplies `config-manager`):

```bash
sudo dnf config-manager --add-repo https://ulnanlu.github.io/mkbrr-dnf-repo/mkbrr-community.repo
```

Portable alternative (does not require the config-manager plugin):

```bash
sudo curl --fail --location --proto '=https' --proto-redir '=https' \
  https://ulnanlu.github.io/mkbrr-dnf-repo/mkbrr-community.repo \
  -o /etc/yum.repos.d/mkbrr-community.repo
```

### 3. Install and update

```bash
sudo dnf install mkbrr
sudo dnf upgrade mkbrr
```

Normal system upgrades also update mkbrr. Use DNF rather than mkbrr's built-in self-update command so that files remain tracked and signature-verified by RPM. To see retained versions: `dnf list --showduplicates mkbrr`. To downgrade when a compatible older version is retained: `sudo dnf downgrade mkbrr`.

The generated repository ID is `mkbrr-community`, description is `mkbrr Unofficial Community RPM Repository`, and both **`gpgcheck=1` and `repo_gpgcheck=1`** are mandatory. TLS verification remains enabled; repository errors are not silently ignored.

## Remove

```bash
sudo rm /etc/yum.repos.d/mkbrr-community.repo
sudo dnf clean all
# Optional; removing the repository does not uninstall the package:
sudo dnf remove mkbrr
```

To remove the trusted public key too, inspect `rpm -qa 'gpg-pubkey*'` and `rpm -qi gpg-pubkey-KEYID-CREATED`, match the displayed identity/fingerprint to the repository key, then remove **only that exact key package** with `sudo rpm -e gpg-pubkey-KEYID-CREATED`. Do not remove all RPM keys. DNF also maintains its own metadata key cache; `dnf clean all` clears metadata, and its key cache location varies by DNF version.

## Troubleshooting

- **404:** the first signed deployment may not have completed; check Actions and Settings → Pages. A project Pages URL must include `/mkbrr-dnf-repo/`, not just the owner's domain. Never substitute an unsigned download.
- **Bad/missing GPG signature:** stop. Check the announced fingerprint/rotation, system clock, and repository status. After independently checking the key, clear stale metadata with `sudo dnf clean all` and retry. Do not set either GPG option to zero or use `--nogpgcheck`.
- **Key rotation and cached metadata trust:** DNF has a separate metadata keyring. Check the new full fingerprint before accepting it; follow the maintainer's rotation announcement if stale cached keys must be removed. Do not globally delete keys or caches belonging to other repositories.
- **No package found:** check `uname -m`, `dnf repolist`, `dnf --refresh list --showduplicates mkbrr`, and upstream architecture support. An ARMv6 RPM is not a native ARMv7 build.
- **Repository temporarily unavailable:** the last successfully deployed complete site stays online when synchronization fails. GitHub Pages may cache an older response briefly. Retry after metadata refresh; never bypass verification.
- **Upstream package has empty License header:** this is currently upstream nFPM packaging metadata, not a license grant or permission to omit GPL compliance. The site supplies the matching source and LICENSE separately without rewriting the RPM payload/header identity.

---

# Maintainer guide

## Initial GitHub setup

The repository must be **standalone, not an upstream fork**, and public for free GitHub Pages hosting. From a fresh directory containing these files:

```bash
gh auth login
git init -b main
gh repo create "$(gh api user --jq .login)/mkbrr-dnf-repo" --public --source=. --remote=origin
git add .
git commit -m 'feat: implement verified signed mkbrr RPM repository'
git push -u origin main
```

For this checkout the GitHub repository and local Git initialization may already exist; inspect `git remote -v` and `gh repo view` rather than creating duplicates. The active account must be `ulnanlu` for the configured repository (`gh auth switch --user ulnanlu`).

Enable Settings → Pages → **Build and deployment → Source: GitHub Actions**, or:

```bash
REPOSITORY=$(gh repo view --json nameWithOwner --jq .nameWithOwner)
gh api --method POST "repos/$REPOSITORY/pages" -f build_type=workflow
# If Pages already exists, use --method PUT instead of POST.
```

Allow Actions to run and permit Pages deployments from the default branch. The `github-pages` deployment environment should permit only the default branch, with optional required reviewers. The upload/build job uses `contents:read` and `pages:read`; only the separate deployment job has `pages:write` and `id-token:write`. No PAT is needed in the workflow. Protect `main`, require validation/review where your account plan permits it, and review workflow/template changes as security-sensitive. There is no deployment or signing with production secrets in pull-request jobs.

## Signing key setup

**Do this on your own trusted machine. Never use a disposable test key in production.** These examples intentionally create a dedicated sign-only RSA primary key with no encryption subkeys; the implementation pins this exact key, not an automatically selected signing subkey.

```bash
umask 077
# GPG prompts for a strong passphrase; keep it in your password manager.
gpg --quick-generate-key \
  'mkbrr Unofficial Community RPM Repository <YOUR-MAINTAINER-EMAIL>' \
  rsa4096 sign 5y
gpg --list-secret-keys --keyid-format long --with-fingerprint
# Copy the dedicated key's full 40-hex fingerprint, without spaces:
FINGERPRINT=YOUR_FULL_40_HEX_FINGERPRINT
gpg --armor --export "$FINGERPRINT" > config/signing-key.asc
gpg --show-keys --with-fingerprint config/signing-key.asc
git add config/signing-key.asc
git commit -m 'chore: pin repository public signing key'
git push

REPOSITORY=$(gh repo view --json nameWithOwner --jq .nameWithOwner)
# Private material goes directly into GitHub's encrypted secret, not a disk file/log.
gpg --armor --export-secret-keys "$FINGERPRINT" |
  gh secret set RPM_SIGNING_KEY --repo "$REPOSITORY"
# Enter the same passphrase at the hidden GitHub CLI prompt, not on the command line:
gh secret set RPM_SIGNING_PASSPHRASE --repo "$REPOSITORY"
gh variable set RPM_SIGNING_FINGERPRINT --body "$FINGERPRINT" --repo "$REPOSITORY"
```

`RPM_SIGNING_KEY` is an **ASCII-armored complete private key**, not base64. The passphrase is read from a mode-0600 file inside a private temporary directory; it is not placed in command arguments. An empty passphrase is technically supported for existing keys but strongly discouraged. Fingerprints are public configuration, not secrets. Publish the full fingerprint in a separately trusted maintainer channel and, after configuring it, add it to your README/rotation announcements. The landing page publishes the actual imported key's fingerprint automatically.

The workflow and script fail closed if the private key, public key file, or matching fingerprint is missing/wrong, the passphrase cannot sign, or the key is unusable. Temporary isolated GnuPG homes and RPM trust databases are used; only the public export enters the site. GPG agents are stopped and temporary material removed in `finally` cleanup. GitHub runners are ephemeral; a hard-killed runner cannot execute cleanup, so never run production signing on an untrusted persistent runner.

### Backup and renewal

Keep an encrypted offline backup of the private key, its passphrase separately, and GnuPG's generated revocation certificate (`~/.gnupg/openpgp-revocs.d/FINGERPRINT.rev`). Never commit these files. Example to an **encrypted offline volume**:

```bash
gpg --armor --export-secret-keys "$FINGERPRINT" > /YOUR-ENCRYPTED-OFFLINE-VOLUME/mkbrr-signing-private.asc
gpg --armor --export "$FINGERPRINT" > /YOUR-ENCRYPTED-OFFLINE-VOLUME/mkbrr-signing-public.asc
```

Test restoration in an isolated GnuPG home, protect the GitHub account with strong MFA, restrict repository write access, and periodically review secret access. A key-expiration maintenance event is intentional: renew **before** expiry with `gpg --quick-set-expire "$FINGERPRINT" 5y`, update both the committed public export and private-key secret, and force a synchronization. The primary fingerprint stays the same.

## First deployment

```bash
gh workflow run sync.yml --ref main -f force=true
gh run list --workflow sync.yml --limit 5
gh run watch RUN_ID --exit-status
gh api "repos/$(gh repo view --json nameWithOwner --jq .nameWithOwner)/pages" --jq .html_url
```

Confirm the completed **deploy** job, public site, key fingerprint, downloadable `.repo`, and a client metadata refresh. Do not confuse a successful validation job with publication. The `force` input is also useful after a failed/canceled deployment or a key change. No generated RPMs or site data are committed to Git history.

## Publishing pipeline and retention

1. Daily at **05:23 UTC**, manual dispatch, or relevant pushes to `main`: check signing setup and run unit/signing tests. Scheduled Actions run only from the default branch; GitHub can delay schedules and may disable them after 60 days of inactivity in public repositories. Monitor Actions and re-enable if GitHub disables scheduling.
2. Query the paginated GitHub Releases API, exclude drafts/prereleases, and select the latest **three semantic stable versions**. No `/latest` filename assumptions; no fallback to an older release when the newest is incomplete.
3. Discover all RPM assets and the actual mkbrr SHA-256 manifest. Require the manifest and an exact match between its RPM entries and API assets. Validate uploaded states, download sizes, safe filenames, optional API SHA-256 digests, and original checksums. Although upstream currently provides checksums, their future absence is treated more strictly than a best-effort policy: **publication stops**, never silently weakens verification.
4. Reject changed package names, versions, epochs, incompatible architecture mappings, unexpected file paths/scriptlets, corrupted digests, missing architectures, or unexpected release naming. Use `rpmkeys` and actual queried RPM headers, not filenames alone. Upstream signatures (if any) are recorded; upstream checksums are the input verification authority. The inspected current upstream packages are unsigned.
5. Mirror a commit-pinned upstream source archive including the GPL license and build scripts, and all dependency `.zip` and `.mod` sources represented by root `go.sum`. Verify Go's `h1` content hashes and ensure every `go.mod` required module has full source. Shared module sources are deduplicated across retained versions. There is no Go compiler dependency and nothing is rebuilt; only a tiny synthetic RPM fixture is built by tests.
6. Re-sign each verified RPM using `rpmsign` with headless loopback GPG signing; verify signatures in an isolated RPM database that trusts only the repository public key. Re-check identity and integrity after signing. Record original and signed SHA-256 values separately.
7. Run `createrepo_c`, sign `repomd.xml`, provenance, and state with detached armored signatures. Validate metadata checksums, package coverage, every RPM signature, mirrored source digests, and real DNF metadata consumption before upload. XML DTDs/entities and path traversal are rejected.
8. Stage into a temporary directory and promote only a completely validated site. GitHub Pages deploys a single complete artifact; failed builds never upload. A concurrency group prevents overlapping publications. Cancellation is disabled for the publishing workflow. A separate least-privilege deploy job has no private key.

Signed `state.json` records release/asset IDs, sizes, update timestamps, optional API digests, source commits, repository URL, signing fingerprint, and implementation hash. An unchanged, correctly signed published state skips downloads/signing/deployment. Changed scripts, templates, upstream assets, source commits, URLs, or keys trigger regeneration. `--force` bypasses state comparison, but **never bypasses validation**. Failed deployment retries compare against the actually published state, not a prematurely saved cache. No large binary cache or branch is needed.

The latest three complete stable versions are regenerated from official assets for each changed publication. Old RPMs and their corresponding source are removed together; no unsupported architecture or individual RPM is silently dropped to fit a quota. Downgrades are supported while a version is retained. Requests using metadata cached before a replacement may reference an expired package; refresh client metadata after publication. Source and RPM retention are coupled, not a three-year written source offer.

The uncompressed site is capped at **800 MiB**, below GitHub Pages' 1 GiB published-site limit; an oversized site fails without replacing the last good site. Review [current Pages limits](https://docs.github.com/en/pages/getting-started-with-github-pages/github-pages-limits), Actions quotas, public distribution bandwidth (Pages documents a soft bandwidth limit), artifact size, and `du -sh site`. If source growth approaches the budget, deliberately lower `--retain` (1–3 supported) in the workflow, with documentation/review; do not remove corresponding source. GitHub Pages is not a general high-volume package CDN, and usage must comply with GitHub's terms. Monitor deployment errors and user reports.

## Local development and verification

Production needs Python **3.11+** and distribution packages: `rpm`, `rpmsign`, `rpmkeys`, `createrepo_c`, and GnuPG. `dnf` is needed for the final consumer test. Fedora development dependencies:

```bash
sudo dnf install python3 rpm rpm-build rpm-sign createrepo_c gnupg2
uv run python -m compileall -q scripts tests
uv run python -m unittest discover -s tests -v
uvx ruff check scripts tests
uvx ruff format --check scripts tests
```

The selected CI signing environment is **Ubuntu 24.04**, installing `rpm createrepo-c gnupg dnf` using apt. Its RPM 4.18 signing macro path is exercised with a passphrase-protected disposable RSA key; no assumptions about Fedora's newer signing internals are required for deployment. Fedora 44 DNF integration is also included using a disposable test container, not a production hosting requirement. To exercise the exact CI environment locally without modifying the host:

```bash
podman run --rm -it -v "$PWD:/work:Z" -w /work ubuntu:24.04 bash
# Inside the disposable container:
apt-get update
apt-get install -y python3 rpm createrepo-c gnupg dnf ca-certificates
python3 -m unittest discover -s tests -v
python3 scripts/sync.py --repository ulnanlu/mkbrr-dnf-repo --dry-run
python3 scripts/test_dnf.py --site site --install
```

Without production secrets or publication access, perform a real end-to-end dry run:

```bash
uv run python scripts/sync.py \
  --repository "$(gh repo view --json nameWithOwner --jq .nameWithOwner)" \
  --dry-run --retain 1
uv run python scripts/sync.py --validate-site site
sudo "$(uv python find)" scripts/test_dnf.py --site site --install
```

`--dry-run` downloads and verifies actual upstream assets and sources, creates a disposable passphrase-protected signing key, generates/validates the site, and marks the landing page **TEST ONLY**. **It never publishes.** Do not upload this output yourself. Each dry run has a fresh key and therefore deliberately cannot be byte-for-byte idempotent; production signing with a fixed key uses signed state to skip unchanged work. The generated directory is gitignored, and an existing unmarked directory is never overwritten.

`test_dnf.py` uses file URLs only within the test, isolated install/cache/trust roots, `gpgcheck=1`, `repo_gpgcheck=1`, and no host RPM database. With `--install`, it actually installs the native package in a disposable root, runs `mkbrr version`, checks a no-op upgrade, and proves tampered metadata signatures are rejected. DNF requires root for installation even with an isolated installroot, hence `sudo` above; prefer the disposable container example if you do not want to run the test as host root. Omit `--install` for unprivileged metadata-only validation. It requires native-architecture execution. It does not claim cross-architecture installation testing or a true old-to-new upgrade test.

Local production synchronization (do not paste private values into the command line or enable `set -x`):

```bash
# On your trusted machine with the dedicated key and passphrase available:
export RPM_SIGNING_FINGERPRINT="$FINGERPRINT"
export RPM_SIGNING_KEY="$(gpg --armor --export-secret-keys "$FINGERPRINT")"
read -r -s -p 'Signing passphrase: ' RPM_SIGNING_PASSPHRASE; echo
export RPM_SIGNING_PASSPHRASE
uv run python scripts/sync.py --repository "$(gh repo view --json nameWithOwner --jq .nameWithOwner)"
unset RPM_SIGNING_KEY RPM_SIGNING_PASSPHRASE
```

Optionally export a read-only `GH_TOKEN` to avoid GitHub's anonymous API limits. The script uses it only for `api.github.com` metadata and strips authorization on redirects; public release/source/dependency downloads receive no token. Standard-library Python is sufficient; no production Python dependencies are installed. Tests cover release selection, checksums, bad/missing assets, architecture/metadata validation, URL generation, signing, repository generation, idempotency and preservation on failure. Packaging tests explicitly skip on hosts lacking tools; CI installs those tools and must execute them.

## Key rotation

1. Announce the new full fingerprint through an independently trusted channel **before** switching. Keep an offline backup of the old public key and rotation notice. Existing installed package signatures remain associated with the old key until replaced.
2. Generate a new dedicated RSA sign-only key. Commit its public export as `config/signing-key.asc` and update `RPM_SIGNING_KEY`, `RPM_SIGNING_PASSPHRASE`, and `RPM_SIGNING_FINGERPRINT` together. Never expose secret exports in logs.
3. Manually run `sync.yml` with **`force=true`**. The old site's state is signed by the old key, so normal signature-checked state comparison intentionally refuses it. Force regenerates and re-signs **all retained packages and metadata** from verified upstream originals under the new key.
4. Users independently verify/import the new key, accept it for DNF metadata as appropriate, and refresh caches. This is an explicit trust transition, not a hidden zero-interaction rotation. The public key URL is stable, but a changed key must not be automatically trusted without checking the fingerprint.
5. If compromised, disable the publishing workflow, revoke the old key and announce the incident promptly. Rotate credentials/repository access too; merely replacing a secret does not restore compromised client trust. Resume only with a reviewed clean workflow and forced verified build.

## Recovery and monitoring

Read the failed job's diagnostic error, correct the actual cause, and retry the workflow. Common causes: secrets absent, wrong passphrase/fingerprint, expired key, disabled Pages/Actions, incomplete upstream release, API throttling, changed RPM layout, source-proxy outage, or size limits. The script retries transient downloads with bounded backoff; permission/validation failures are not masked. Never patch out integrity/signature checks to make a deployment green.

A partially uploaded upstream release leaves the prior site untouched. Retry manually when upstream completes upload or wait for the next daily run. If upstream deliberately changes package naming, license, architecture, scriptlets, or checksum conventions, review and update validation/tests. For an upstream release withdrawing an architecture, adjust the completeness policy only after confirming intent and documenting it. A same-version asset replacement is detected via asset state; provenance records the changed original checksum.

Enable GitHub Actions failure notifications and inspect scheduled-run history periodically, including after prolonged repository inactivity. `force=true` repairs a missing/corrupted site by rebuilding, but ordinary signed-state checks deliberately fail on tampering. Do not edit generated Pages files manually. Full local tests and CI validation should pass before trusting a production deployment.

## Licensing, source and provenance

Original automation/documentation: [MIT](LICENSE). mkbrr: [upstream GPL-2.0 LICENSE](https://github.com/autobrr/mkbrr/blob/develop/LICENSE). The RPM's upstream identity and payload remain unchanged; its signature/checksum changes because this repository independently signs it. Upstream maintainers do not control or endorse this signing key.

The site provides `provenance.json` and its signature, original upstream checksum manifests under `provenance/TAG/`, release/tag/source links, commit-pinned source tarballs with license/build scripts under `sources/TAG/`, and checksum-verified dependency sources under `sources/modules/`. Matching source is offered from the same site as binaries, following GPLv2 section 3's equivalent-access source distribution model, not just a link to a third-party server or a written offer. Module archives include their own upstream notices/licenses; preserve those when redistributing. The corresponding CLI source is mirrored; GUI frontend dependencies are not needed for these CLI RPMs. No claim of bit-reproducible upstream builds is made.

See [docs/upstream-inspection.md](docs/upstream-inspection.md) for the observed upstream release conventions and [docs/verification.md](docs/verification.md) for implementation-time test results.

## Source tree

```text
.github/
  dependabot.yml
  workflows/{sync,validate}.yml
config/
  index.html.in
  mkbrr-community.repo.in
  signing-key.asc                 # maintainer adds the real PUBLIC key
docs/
  upstream-inspection.md
  verification.md
scripts/
  __init__.py
  sync.py
  test_dnf.py
tests/
  test_sync.py
.gitignore
LICENSE
README.md
pyproject.toml
uv.lock
```

Generated (not committed):

```text
site/
  index.html
  mkbrr-community.repo
  RPM-GPG-KEY-mkbrr-community
  {state,provenance}.json[.asc]
  rpm/stable/{Packages/TAG/*.rpm,repodata/*}
  provenance/TAG/mkbrr_VERSION_checksums.txt
  sources/{README.txt,TAG/{mkbrr-TAG.tar.gz,LICENSE},modules/...}
```
