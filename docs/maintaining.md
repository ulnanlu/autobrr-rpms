# Maintenance

Recipes: `config/copr.json` and `packaging/`. Target: `ulnanlu/autobrr-rpms` on Fedora 44/45 and EL 9/10, x86_64/aarch64. COPR builds, signs, and hosts the packages; GitHub hosts packaging code only.

## Automation

`copr.yml` checks stable releases daily at **05:47 UTC**, on packaging changes, or on manual dispatch. Successful version/release/target combinations are skipped; active builds are watched and failed builds can be retried.

- Resolve upstream tags to commits and checksum their original source archives.
- Vendor Go dependencies with checksum verification; reject changes to `go.mod`/`go.sum` or unreviewed replacements.
- For web apps, vendor the pinned pnpm tool, both CPU architectures' dependencies, complete registry metadata, and license notices. Reject changed Node/pnpm requirements and unreviewed frontend licenses.
- Build the original frontend and Go binaries offline, run upstream short tests, then test a disposable-key-signed installation and live web assets.
- Submit SRPMs to COPR, which builds both architectures natively, signs the RPMs, and hosts binaries and sources.

Fedora 44 and EL 9/10 bootstrap checksum-pinned Go 1.27.3 source with their distribution compiler. Fedora 45 uses its distribution Go. Web builds use distribution Node 24 (the `nodejs:24` module on EL 9), frozen lockfiles, offline dependency installation, and no dependency install scripts. Private premium themes are not fetched.

EL builds use COPR's EPEL 9/10 targets. CI also builds and tests on AlmaLinux 9/10; compatible RHEL and Rocky Linux releases share the EL packages. EL 9 preparation needs Python 3.12, not its default Python 3.9. No Go or Node runtime is needed by the installed applications.

Build networking is disabled in COPR. Source/vendor/tool checksums are rechecked during `%prep`; compiler or package-manager requirement changes fail closed. Bump a recipe's `release` for packaging changes to an existing version. Review bundled licenses when updating recipes.

`sfvbrr` excludes only the reviewed historical tag `0.0.4-hotfix1` from stable-release selection. Any other unexpected tag still fails closed. CLI installation tests never run real `tqm` cleanup; `sfvbrr` must accept a valid SFV fixture and reject a corrupted one.

Validation does not upload duplicate SRPMs. Intermediate submission artifacts expire after one day and are deleted after successful publication.

## Credentials

Save the [COPR API configuration](https://copr.fedorainfracloud.org/api/) outside the checkout as `~/.config/copr`, mode `0600`. Never commit credentials.

```bash
uv run --with 'copr>=2.5,<3' python scripts/copr_sync.py init \
  --configure-github ulnanlu/autobrr-rpms
gh workflow run copr.yml --ref main
```

`init` uses the authenticated Fedora identity, checks existing project settings without overwriting them, stores `COPR_CONFIG` in the protected `copr` GitHub environment, and sets `COPR_PROJECT`. Restrict that environment to `main` and renew credentials before expiry. Pull requests receive no COPR credentials.

Force a checked rebuild with `gh workflow run copr.yml -f force=true`. COPR controls retention and availability; storage is not unlimited.

## Local checks

```bash
uv run python -m unittest discover -s tests -v
uvx ruff check scripts tests
uvx ruff format --check scripts tests
```

For a real build, use a disposable supported distribution container. For example:

```bash
podman run --rm -it -v "$PWD:/work:Z" -w /work registry.fedoraproject.org/fedora:44 bash
dnf -y install python3 golang nodejs24 systemd-rpm-macros rpm-build rpm-sign gnupg2 redhat-rpm-config git
python3 scripts/copr_sync.py prepare --package qui --output .work/qui
python3 scripts/copr_sync.py build --output .work/qui
python3 scripts/copr_sync.py test-install --output .work/qui
```

Use `mkbrr` or `autobrr` instead of `qui` as needed. Artifacts stay under gitignored `.work/`. Test installation uses an isolated RPM database and a disposable signing key, never a real publishing key or disabled signature checks. User services remain disabled until explicitly enabled.
