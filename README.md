# autobrr RPMs

Unofficial, source-built Fedora packages for [autobrr](https://github.com/autobrr). **Not affiliated with or endorsed by upstream.**

[COPR repository](https://copr.fedorainfracloud.org/coprs/ulnanlu/autobrr-rpms/) · [Packaging CI](https://github.com/ulnanlu/autobrr-rpms/actions/workflows/copr-validate.yml) · [Maintenance](docs/maintaining.md)

| Package | COPR build |
| --- | --- |
| [autobrr](https://github.com/autobrr/autobrr) | [![autobrr build](https://copr.fedorainfracloud.org/coprs/ulnanlu/autobrr-rpms/package/autobrr/status_image/last_build.png)](https://copr.fedorainfracloud.org/coprs/ulnanlu/autobrr-rpms/package/autobrr/) |
| [mkbrr](https://github.com/autobrr/mkbrr) | [![mkbrr build](https://copr.fedorainfracloud.org/coprs/ulnanlu/autobrr-rpms/package/mkbrr/status_image/last_build.png)](https://copr.fedorainfracloud.org/coprs/ulnanlu/autobrr-rpms/package/mkbrr/) |
| [qui](https://github.com/autobrr/qui) | [![qui build](https://copr.fedorainfracloud.org/coprs/ulnanlu/autobrr-rpms/package/qui/status_image/last_build.png)](https://copr.fedorainfracloud.org/coprs/ulnanlu/autobrr-rpms/package/qui/) |

**Fedora 44/45 · x86_64/aarch64.** Daily stable-release updates; unmodified application source and offline RPM builds.

## Install

```bash
sudo dnf copr enable ulnanlu/autobrr-rpms
sudo dnf install autobrr mkbrr qui  # choose the packages you need
```

Confirm the project signing key: **`AED00F97B6F0520E98B18F4B20995ABDC582B8D3`**. Keep package GPG checks enabled. The COPR plugin is supplied by `dnf5-plugins` or `dnf-plugins-core`.

Update with DNF, not the applications' self-updaters. Large `.src.rpm` files contain build inputs, not normal installation payloads.

## Optional user services

```bash
systemctl --user daemon-reload
systemctl --user enable --now autobrr.service  # or qui.service
```

Services are not started on installation. Configuration: `~/.config/autobrr/` and `~/.config/qui/`; qui data: `~/.local/share/qui/`. To run while logged out, enable lingering with `sudo loginctl enable-linger "$USER"`.

## License

Packaging automation: [MIT](LICENSE). Applications: GPL-2.0-or-later, with bundled dependency notices and source provenance installed alongside them. Matching source RPMs are hosted by COPR.
