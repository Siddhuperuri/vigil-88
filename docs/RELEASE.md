# Releasing VIGIL-88

**Nothing built today is distributable.** Four owner decisions are open, and the repository is set up so a
build cannot be mistaken for a release while any of them is.

## What is enforced

| Mechanism | Effect |
|---|---|
| `release/decisions.yaml` | The four decisions, each `undecided` until the owner records it |
| `scripts/release_check.py` | Reads the decisions and the evidence each one needs; a decision written down without its evidence is still a blocker |
| Installer name | Every build is `VIGIL-88-DEV-ONLY-Setup-<version>.exe` and writes `build\installer\RELEASE-STATUS.txt` listing why |
| `package.ps1 -Release` | Refused before anything is built while a decision is open; afterwards verifies the installer's Authenticode signature |
| `scripts/gpu_runtime.py` | Downloads the NVIDIA runtime only if decision 2 says so, the manifest is complete, and `--yes` is given |
| `.github/workflows/release.yml` | Manual workflow that runs `release_check.py --release`; it fails while anything is open |
| `tests/unit/test_release_gates.py` | The repository must report all four blockers today, and the installer must keep its development name |

## The four decisions

### 1. Project licence

**Decide:** which licence the project is released under. Nothing in the repository grants anyone rights
until this is answered. The licence also has to be compatible with what is linked (no AGPL or GPL is).

**Record:** `licence: {status: decided, spdx: <identifier>}`, add a `LICENSE` file at the root, and name the
identifier in `pyproject.toml`. All three are checked.

### 2. NVIDIA runtime (about 2.4 GB)

**Decide:** CPU-only releases, or an explicit hash-verified download of the CUDA and cuDNN libraries, and
whether NVIDIA's redistribution terms permit that for this project.

**Record:** `gpu_runtime: {status: not_shipped}` for CPU only. For a download, `status: download` and
`release/gpu-runtime.manifest.json`:

```json
{ "files": [ { "name": "...", "url": "https://...", "sha256": "<64 hex>", "size_bytes": 123,
               "licence_review": { "approved_by": "<name>", "date": "YYYY-MM-DD" } } ] }
```

**Prepared:** the downloader (`scripts/gpu_runtime.py`) is written and tested against a local server. It has
never fetched a real NVIDIA file, and no manifest exists.

### 3. Code signing

**Decide:** which certificate (and whether it is an EV certificate, which SmartScreen treats better), who
holds the key, and where signing runs.

**Record:** `signing: {status: signed, subject: "<certificate subject>"}`. `package.ps1 -Release` then runs
`Get-AuthenticodeSignature` on the built installer and requires `Valid` and the recorded subject.

**Not prepared:** electron-builder's signing configuration. It needs the certificate's details, and guessing
them would be improvising. A self-signed certificate is not an option (ADR 0019).

### 4. Updates

**Decide:** where updates are hosted, who holds the update-signing key, and how the key is rotated.

**Record:** `updater: {status: not_shipped}` for a release with no updater. For one, `status: enabled`,
`feed_url` (https) and `public_key_fingerprint`.

**Guard:** while the decision is open, a dependency on an updater package in `desktop/package.json` is itself a
blocker, so an updater cannot arrive before its key and host do.

## Making a release, once the decisions are made

```powershell
powershell -File scripts/package.ps1 -Release
```

It fails at the first unresolved decision and says which. It has never succeeded, because it cannot yet.
