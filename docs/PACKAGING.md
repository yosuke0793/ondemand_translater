# Native developer distribution guide

This is the **new self-contained distribution path**, independent of the existing
`mac/` and `win/` source/environment installers. It builds a Tk GUI and a separate
console worker using PyInstaller. End users do not need Python, pip, FFmpeg or
an activated venv. This is not a claim that an unsigned build is production-ready.

## Supported native build matrix

| Target | Build host in Actions | Output |
| --- | --- | --- |
| Windows x64 | `windows-2022`, Python 3.12.10 x64 | per-user Inno Setup `.exe`, portable `.zip` |
| macOS Apple Silicon | `macos-15`, Python 3.12.10 arm64 | `.dmg` |
| macOS Intel | `macos-15-intel`, Python 3.12.10 x64 | `.dmg` |

These are explicit currently supported hosted-runner labels, not moving `latest`
aliases. Intel is a separate native build, not Rosetta or a universal2 package.
Retest the matrix when GitHub retires a runner. Building on macOS 15 does not
certify compatibility with earlier macOS releases; the supported minimum OS must
be established by clean-machine testing. Windows ARM/Linux are not distribution
targets.

## Local build

Use a clean checkout. Commit all intended changes before a release so the build
revision is meaningful. Never put credentials, models, user media or databases
in packaging inputs.

macOS prerequisites: native CPython 3.12 including Tcl/Tk (python.org recommended),
Xcode command-line tools (`codesign`, `otool`), and system `hdiutil`/`ditto`.

```bash
PYTHON312=/absolute/path/to/python3.12 bash packaging/build-macos.sh
```

Windows prerequisites: CPython 3.12 x64 with Tcl/Tk and Inno Setup 6. A clean
GitHub Windows runner provides Inno Setup; locally install it yourself and, if
needed, set `$env:ISCC` to its `ISCC.exe`.

```powershell
./packaging/build-windows.ps1 -Python312 "C:\Path\To\Python312\python.exe"
```

Both wrappers delete/recreate **only** `packaging/.venv-build`, install the
hash-locked native dependency set, run `pip check`, and build in
`packaging/.work`. They do not reuse/copy the development `.venv`, existing app
bundles, source data or old platform launchers. All build caches and scratch
files stay under `packaging/.work`. Downloads are from public PyPI/upstream
binary URLs; no source or credentials are uploaded by local build scripts.
The dedicated environment is required by `build.py build`; running that command
from an unrelated environment fails.

The wrappers require several GB of disk and may download substantial PyTorch,
OpenCV and speech/translation dependencies. They do **not** download model
weights. Online speech/model services, Argos language packs, optional Ollama,
XTTS remain separate runtime features; "self-contained" means
the default application/runtime dependencies and media executables, not every
optional service/model. CUDA drivers/libraries are not bundled; CPU is the
portable baseline. Network/provider functionality needs separate acceptance
testing using your own credentials.

OCR-based video text replacement is omitted from all bundled distributions.
OpenCV and pytesseract are excluded from the frozen executables (the shared
build dependency locks still contain them). Bundled workers reject
`--video-text-overlay*` arguments explicitly. The source-mode optional feature
is unchanged and requires its own Tesseract and compatible FFmpeg installation.

## Runtime layout and resources

Windows portable ZIP contains `TranslateService/`; the installer copies that
same tested tree into `%LOCALAPPDATA%\Programs\TranslateService` without admin
rights and offers a Start Menu shortcut (desktop shortcut optional). Uninstall
does not delete user jobs/settings or OS credential-store entries.

macOS disk image contains `TranslateService.app` and an `/Applications` link.
Drag the app into Applications. The executable layout is:

```text
Windows: TranslateService/{TranslateService.exe,TranslateWorker.exe,ffmpeg.exe,ffprobe.exe}
macOS:   TranslateService.app/Contents/MacOS/{TranslateService,TranslateWorker,ffmpeg,ffprobe}
```

PyInstaller's internal resource root contains `BUNDLED`, `VERSION`, `version.json`, all
`lecture_poc/prompts/` files and `third-party-notices/`. Both analyses include
`source/worker` in their module search paths; the GUI analysis is lightweight
and the worker analysis owns the conversion dependencies. Platform-specific
keyring backends and `edge_tts.__main__` have explicit hidden imports. Compiled
Python modules and native dependency libraries are necessarily inside the
bundle; the raw checkout/venv/tests/runtime data are not copied.
The `BUNDLED` marker distinguishes this distribution from the legacy frozen
source-mode Windows launcher, which intentionally still uses an external venv.

`packaging/VERSION` is the single numeric `MAJOR.MINOR.PATCH` source (currently
`0.1.0`). `build.py` generates `bundle_version.py`, Windows PE version-resource
data and `version.json`; the spec also sets macOS plist versions. Runtime version
introspection reads the bundled `VERSION`. No developer must edit generated
version files.

## Media binaries and license provenance

`packaging/ffmpeg.json` pins uncompressed FFmpeg and ffprobe release assets from
`eugeneware/ffmpeg-static` **b6.1.1**, for all three targets, with release-provided
SHA-256 digests. The asset release label is not a promise about every underlying
FFmpeg revision: captured `-version`/configuration output records actual binary
provenance. Downloads fail closed on checksum mismatch.

Do **not** replace these with copied Homebrew executables: those often depend
on developer-local dylibs. macOS staging rejects any linkage except `/usr/lib/`
and `/System/Library/`. Each tool is launched natively during staging. Final
bundle tests run with the bundled executable directory first and only OS tools
in PATH, then encode/probe a synthetic H.264/AAC video and verify the `subtitles`
filter. `drawtext` is not required because OCR text replacement is omitted.
Windows statically linked upstream media tools
are checked through actual execution in the clean hosted build.

Upstream FFmpeg platform LICENSE/README, manifest hashes, captured build
configuration/linkage, Python package inventory, wheel license/notice files,
the dependency lock and application MIT license are retained in every bundle.
**FFmpeg builds contain GPL components.** Their license does not become MIT
because this application is MIT. Before publishing, arrange compliant
**corresponding source** availability for the precise redistributed FFmpeg and
enabled codec-library revisions, including build recipes/configuration. Merely
pointing at latest FFmpeg source is insufficient. Review the pinned upstream
release/build repository and [FFmpeg legal guidance](https://ffmpeg.org/legal.html);
obtain/archive the exact sources from the binary provider if necessary. Source
compliance is a maintainer release gate, not an automated claim by this pipeline.
Review any wheel that lacks complete bundled license texts as well.

## Dependency lock maintenance

`packaging/requirements.in` constrains the worker requirements plus freezing
tools. Native lock files are generated with **uv 0.8.22**, contain exact
transitive versions and SHA-256 hashes, and are committed under `packaging/locks`.
Torch 2.2.2 and NumPy 1.26.4 intentionally preserve Intel macOS wheels/ABI;
review/test those constraints before upgrades. No unresolved runtime transitive
version ranges remain in the committed native locks. All packages except
Argos's strictly required `sacremoses==0.0.53` use binary wheels only. That
upstream version has no wheel, so its hash-pinned pure-Python sdist is built
without build isolation using the hash-locked setuptools/wheel bootstrap
environment; no unpinned build dependencies are fetched. A missing native wheel
fails rather than silently compiling an uncontrolled native toolchain.

After changing worker requirements, pins or Python:

```bash
# Use a separate developer environment; this installs the resolver, not app packages.
python3 -m pip install uv==0.8.22
mkdir -p packaging/.work/tool-tmp
export TMPDIR="$PWD/packaging/.work/tool-tmp"
export UV_CACHE_DIR="$PWD/packaging/.work/uv-cache"
uv pip compile packaging/requirements.in --python-version 3.12 --python-platform x86_64-pc-windows-msvc --generate-hashes -o packaging/locks/windows-x64.txt
uv pip compile packaging/requirements.in --python-version 3.12 --python-platform aarch64-apple-darwin --generate-hashes -o packaging/locks/macos-arm64.txt
uv pip compile packaging/requirements.in --python-version 3.12 --python-platform x86_64-apple-darwin --generate-hashes -o packaging/locks/macos-x64.txt
uv pip compile packaging/bootstrap.in --python-version 3.12 --generate-hashes -o packaging/locks/bootstrap.txt
```

Resolve with `--only-binary PACKAGE` for each locked package other than
`sacremoses` when certifying wheel availability, then compare the selected pins
against the committed locks. The initial locks were checked this way on all
three target platform tags; cross-platform resolution alone is not a native
execution test.

Rebuild/test **all** targets after regeneration. The pip/setuptools/wheel bootstrap
is hash-locked in `packaging/locks/bootstrap.txt`, and uv's
resolver version are pinned; the Inno Setup version supplied by the hosted
runner, OS/Xcode image and CPython local patch version can still change.
PyInstaller/installer timestamps also vary. This is a reproducible dependency
graph, **not a byte-identical/reproducible-build guarantee**. Actions pins Python
3.12.10 and action commits; review them for security updates.

## Validation and artifacts

The Windows bundled worker writes stdout/stderr as UTF-8, including when
redirected to build checks or job logs. Frozen worker checks explicitly decode
UTF-8. If a checked command fails, captured stdout/stderr are printed before
the build aborts; use that diagnostic rather than only the exit-code annotation.
The workflow's checkout/setup-python pins use Node.js 24.

Lightweight configuration tests (no application dependency install):

```bash
python3 -m unittest discover -s packaging/tests -v
python3 packaging/build.py validate --expected-tag v0.1.0
bash -n packaging/build-macos.sh
```

Each native build validates two frozen roles without opening a display:

```text
TranslateService[.exe] --bundle-self-test /absolute/path/gui-report.json
TranslateWorker[.exe] --bundle-self-test /absolute/path/worker-report.json
TranslateWorker[.exe] --help
```

Report files are required for the windowed Windows GUI (no stdout console).
JSON must have `status: ok`, the expected `role` and version and both media-tool
version strings. The checks import default conversion dependencies, validate
prompts, construct the secure credential backend without retrieving/storing
secrets, and run media tools. They do not use API keys, a display or model
downloads. A copied/extracted ZIP is re-tested on Windows; a read-only mounted
DMG is re-tested on macOS. Actions also silently installs, tests and uninstalls
the per-user Windows installer in an isolated runner directory.

Successful output under ignored `packaging/artifacts/` uses names such as:

```text
TranslateService-0.1.0-windows-x64-setup.exe
TranslateService-0.1.0-windows-x64-portable.zip
TranslateService-0.1.0-macos-arm64.dmg
TranslateService-0.1.0-macos-x64.dmg
TranslateService-0.1.0-<target>-build-info.json
TranslateService-0.1.0-<target>-SHA256SUMS.txt
```

Build information records commit, native Python, lock/FFmpeg manifest hashes,
and frozen smoke reports. Checksums cover each target's distributables and build
information. Before handing binaries to users, verify hashes on a clean machine
without developer Python/FFmpeg, test GUI launch and credential-store behavior,
and exercise a real conversion with provider access separately.

## GitHub Actions and release gates

`.github/workflows/package-release.yml` has two entry modes:

* **Run workflow** manually: all matrix builds/tests; upload only distribution
  artifacts (14-day retention). No GitHub release is created, even if a tag was
  selected for the manual run.
* Push an explicitly prepared **`vMAJOR.MINOR.PATCH` tag**: the tag must exactly
  match `packaging/VERSION` before dependency installation. Only after **all**
  matrix jobs succeed does a separate, minimally privileged job download all
  artifacts, validate the complete asset/checksum/report set and create a
  **draft** GitHub Release. Existing releases are not overwritten.

Normal jobs have repository read permission. Only the tag release job gets
`contents: write` using GitHub's scoped automatic token; no user/API/signing
secrets are required. Artifact uploads happen only in the configured workflow,
not during local development. Nothing has to be uploaded to a third-party build
or scanning service.

Never claim unattended production publication. A maintainer must review the
draft, exact-source/license obligations, checksums, clean-machine acceptance and
security/signing before pressing Publish. If one architecture fails, no release
is created; fix the build and rerun failed jobs rather than silently omitting
that architecture. Do not reuse/move a published version tag.

## Optional signing/notarization

Default Windows binaries/installers are unsigned (SmartScreen may warn).
macOS builds are ad-hoc signed **only for local execution/integrity**, not
Developer ID signed/notarized; Gatekeeper can reject downloaded apps.
`signed_for_distribution: false` in build metadata is intentional.

For a production pipeline, opt in explicitly: use a protected release
environment, import a Developer ID certificate into an isolated temporary
keychain located under `packaging/.work`, sign all nested executables/libraries
inside-out with appropriate hardened-runtime entitlements, sign the app/DMG,
notarize with `xcrun notarytool`, and staple/validate the result. Windows requires
Authenticode signing of GUI/worker and the final Inno installer with timestamped
`signtool` verification. Do not invent/download signing credentials or embed
private keys in this repository. Signing must happen **before** final artifact
checksums; rerun relocated/mounted/installed smoke tests and update metadata
only after signature verification. This optional production flow is documented,
not enabled by the unsigned default workflow.
