# Maintenance

For the source-built COPR pilot, see [copr.md](copr.md). This page covers the existing upstream-RPM Pages mirror.

## GitHub setup

Use a standalone public repository, not an upstream fork, with `main` as the default branch. Enable **Settings → Pages → GitHub Actions** and allow `main` in the `github-pages` environment. Protect the branch and restrict write access. Workflows detect the owner and project URL automatically.

## Signing key setup

On a trusted machine, generate a dedicated passphrase-protected RSA signing key:

```bash
umask 077
gpg --quick-generate-key 'mkbrr Unofficial Community RPM Repository <YOUR_EMAIL>' rsa4096 sign 5y
gpg --list-secret-keys --with-fingerprint
FINGERPRINT=YOUR_FULL_40_HEX_FINGERPRINT_WITHOUT_SPACES
gpg --armor --export "$FINGERPRINT" > config/signing-key.asc
gpg --show-keys --with-fingerprint config/signing-key.asc

REPOSITORY=$(gh repo view --json nameWithOwner --jq .nameWithOwner)
gpg --armor --export-secret-keys "$FINGERPRINT" |
  gh secret set RPM_SIGNING_KEY --repo "$REPOSITORY"
gh secret set RPM_SIGNING_PASSPHRASE --repo "$REPOSITORY"
gh variable set RPM_SIGNING_FINGERPRINT --body "$FINGERPRINT" --repo "$REPOSITORY"

git add config/signing-key.asc
git commit -m 'chore: update repository public signing key'
git push
```

Enter the same passphrase at the hidden `gh secret set` prompt. The private-key secret must be ASCII-armored, not base64. Commit only the public key; publish its fingerprint in the README and a separately trusted channel. CI uses isolated temporary GnuPG homes and refuses unsigned output or mismatched keys.

### Backup, renewal, and rotation

- Export directly to an encrypted offline volume: `gpg --armor --export-secret-keys "$FINGERPRINT" > /ENCRYPTED-BACKUP/mkbrr-private.asc`. Keep the passphrase separately and preserve `~/.gnupg/openpgp-revocs.d/FINGERPRINT.rev`. Never commit either backup.
- Renew before expiry with `gpg --quick-set-expire "$FINGERPRINT" 5y`, refresh the public export and private-key secret, and force a sync.
- For a new key, announce its fingerprint, replace the committed public key and all three GitHub values, and run with `force=true`. This re-signs all retained packages; the old signed state cannot be verified with the new key. Users must independently verify/import the new key and refresh DNF's package and metadata trust.
- If compromised, disable publishing, revoke the key, announce the incident, and rotate credentials before resuming.

## Synchronization

```bash
gh workflow run sync.yml --ref main
gh run list --workflow sync.yml --limit 5
gh run watch RUN_ID --exit-status
# Rebuild after a key change or repair a failed deployment:
gh workflow run sync.yml --ref main -f force=true
```

The workflow runs daily at **05:23 UTC** and on relevant pushes. It selects the latest three stable releases, verifies upstream checksums and RPM headers/integrity, signs packages, generates and signs `createrepo_c` metadata, validates with DNF, and deploys one complete Pages artifact. Signing secrets are unavailable to pull-request tests; deployment has separate permissions.

Signed state skips unchanged publications. Failed or incomplete releases leave the previous site intact. `force=true` bypasses the state comparison, not validation. Old packages and corresponding source expire together; generated binaries are never committed.

Upstream currently uses `vMAJOR.MINOR.PATCH` tags, `mkbrr_VERSION_linux_ARCH.rpm` assets, and `mkbrr_VERSION_checksums.txt`. The script discovers assets through the API and checks actual RPM architectures. Review upstream's [GoReleaser config](https://github.com/autobrr/mkbrr/blob/develop/.goreleaser.yml) and [release workflow](https://github.com/autobrr/mkbrr/blob/develop/.github/workflows/release.yml) if conventions change. Upstream checksum verification precedes independent signing; upstream signature status is recorded in provenance.

Enable Actions failure notifications. Check signing secrets, fingerprint formatting, key expiry, Pages settings, and upstream upload completeness before retrying. Never disable signature or checksum checks to work around a failure. GitHub may disable scheduled runs after 60 days of repository inactivity; monitor and re-enable them if needed.

## Local tests

Requires Python 3.11+, RPM tools, GnuPG, `createrepo_c`, and DNF. On Fedora:

```bash
sudo dnf install python3 rpm rpm-build rpm-sign createrepo_c gnupg2
uv run python -m unittest discover -s tests -v
uvx ruff check scripts tests
uvx ruff format --check scripts tests
```

For a real download/signing dry run without production secrets or publishing:

```bash
uv run python scripts/sync.py \
  --repository "$(gh repo view --json nameWithOwner --jq .nameWithOwner)" --dry-run --retain 1
uv run python scripts/sync.py --validate-site site
sudo "$(uv python find)" scripts/test_dnf.py --site site --install
```

Dry runs use disposable keys and never publish. DNF installation tests require root but use disposable install/cache roots, not the host RPM database; omit `--install` for unprivileged metadata validation. Tests cover signing, corruption rejection, state/idempotency, and failure handling. Native installation is tested on x86_64, not ARM; upgrade tests are no-op checks, not old-to-new upgrades.

CI signs on Ubuntu 24.04 and also tests Fedora 44 consumers. To use the signing environment locally without installing host tools:

```bash
podman run --rm -it -v "$PWD:/work:Z" -w /work ubuntu:24.04 bash
apt-get update
apt-get install -y python3 rpm createrepo-c gnupg dnf ca-certificates
python3 -m unittest discover -s tests -v
python3 scripts/sync.py --repository ulnanlu/mkbrr-dnf-repo --dry-run --retain 1
python3 scripts/test_dnf.py --site site --install
```

For local production sync, export `RPM_SIGNING_KEY`, `RPM_SIGNING_PASSPHRASE`, and `RPM_SIGNING_FINGERPRINT`, then omit `--dry-run`. Do not place secret values in shell history or logs. Optional `GH_TOKEN` avoids anonymous API limits and is sent only to GitHub's API.

## Storage and licensing

The site is capped at **800 MiB**, below Pages' 1 GB limit. Check `du -sh site`, [Pages limits](https://docs.github.com/en/pages/getting-started-with-github-pages/github-pages-limits), bandwidth, and Actions quotas. If necessary, deliberately lower `--retain` to 1 or 2 in the workflow; do not omit required source or architectures to fit.

Corresponding source archives, build scripts, and checksum-verified Go dependency sources accompany the RPMs under `sources/`, with commits and original/signed hashes in `provenance.json`. Preserve their licenses when redistributing. Source is offered alongside binaries under GPLv2's equivalent-access model, not a three-year written offer. Only repository automation is MIT-licensed.
