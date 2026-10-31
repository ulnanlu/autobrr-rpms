# mkbrr RPM repository

[![Validate](https://github.com/ulnanlu/mkbrr-dnf-repo/actions/workflows/validate.yml/badge.svg?branch=main)](https://github.com/ulnanlu/mkbrr-dnf-repo/actions/workflows/validate.yml)
[![Publish](https://github.com/ulnanlu/mkbrr-dnf-repo/actions/workflows/sync.yml/badge.svg?branch=main)](https://github.com/ulnanlu/mkbrr-dnf-repo/actions/workflows/sync.yml)
[![Upstream release](https://img.shields.io/github/v/release/autobrr/mkbrr?label=upstream)](https://github.com/autobrr/mkbrr/releases/latest)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

Official [mkbrr release RPMs](https://github.com/autobrr/mkbrr/releases), checksum-verified and independently signed for DNF. **Unofficial; not affiliated with or endorsed by autobrr.** Packages are not rebuilt or patched.

[Repository](https://ulnanlu.github.io/mkbrr-dnf-repo/) · [.repo file](https://ulnanlu.github.io/mkbrr-dnf-repo/mkbrr-community.repo) · [Actions](https://github.com/ulnanlu/mkbrr-dnf-repo/actions)

## Supported systems

Fedora and compatible DNF-based distributions. Architectures: **x86_64**, **aarch64**, **armv6hl**. ARMv6 requires distribution support; it is not a native ARMv7 build. Installation is tested on x86_64 with DNF4 and DNF5; ARM packages are integrity/signature-verified, not execution-tested.

## Install

### 1. Verify and import the repository key

You are trusting this repository's signing key. Compare the full fingerprint with a trusted copy of the [committed public key](config/signing-key.asc) or another independent maintainer channel:

```text
EB74D9B819729F12F360B24EA07E0E6DD4BBEF5C
```

```bash
curl --fail --location --proto '=https' --proto-redir '=https' \
  https://ulnanlu.github.io/mkbrr-dnf-repo/RPM-GPG-KEY-mkbrr-community \
  -o /tmp/mkbrr-community.asc
gpg --show-keys --with-fingerprint /tmp/mkbrr-community.asc
# Import only after checking the fingerprint.
sudo rpmkeys --import /tmp/mkbrr-community.asc
```

### 2. Add the repository

DNF5 (`dnf5-plugins` provides `config-manager`):

```bash
sudo dnf config-manager addrepo --from-repofile=https://ulnanlu.github.io/mkbrr-dnf-repo/mkbrr-community.repo
```

DNF4 (`dnf-plugins-core`):

```bash
sudo dnf config-manager --add-repo https://ulnanlu.github.io/mkbrr-dnf-repo/mkbrr-community.repo
```

Without either plugin:

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

Use DNF rather than mkbrr's self-updater. The latest three stable versions are retained; use `sudo dnf downgrade mkbrr` for a retained older version. Both package and metadata GPG checks are enabled.

## Remove

```bash
sudo rm /etc/yum.repos.d/mkbrr-community.repo
sudo dnf clean all
```

Optionally uninstall with `sudo dnf remove mkbrr`. To remove the trusted key, identify its exact entry with `rpm -qa 'gpg-pubkey*'` and `rpm -qi`, then remove only that entry with `sudo rpm -e`.

## Troubleshooting

- **Signature errors:** check the fingerprint and system clock, then try `sudo dnf clean all`. DNF may request the same key separately for metadata. Never disable GPG checks.
- **No package found:** check `uname -m`, `dnf repolist`, and `dnf --refresh list --showduplicates mkbrr`.
- **404 or unavailable:** check the [publishing workflow](https://github.com/ulnanlu/mkbrr-dnf-repo/actions/workflows/sync.yml). Failed syncs leave the previous repository online.

## Maintenance

See [docs/maintaining.md](docs/maintaining.md) for setup, signing keys, synchronization, and tests.

## License and source

Repository automation: [MIT](LICENSE). mkbrr: [GPL-2.0](https://github.com/autobrr/mkbrr/blob/develop/LICENSE). [Signed provenance](https://ulnanlu.github.io/mkbrr-dnf-repo/provenance.json) records upstream tags, source links, and original checksums. Matching source, build scripts, licenses, and dependency sources are served alongside binaries; see the [source instructions](https://ulnanlu.github.io/mkbrr-dnf-repo/sources/README.txt).
