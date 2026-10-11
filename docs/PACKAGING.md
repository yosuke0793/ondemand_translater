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
revision is meaningful. Builds from a corresponding-source archive (which has
no `.git`) record the SHA-256 of its verified application source manifest
instead; `TRANSLATESERVICE_SOURCE_REVISION` may supply an explicit Git-style
revision when needed. Never put credentials, models, user media or databases
in packaging inputs.

macOS prerequisites: native CPython 3.12 including Tcl/Tk (python.org recommended),
Xcode command-line tools (`clang`, `make`, `codesign`, `otool`), CMake, pkg-config,
and system `hdiutil`/`ditto`. Intel builds also require NASM. For example,
`brew install cmake pkg-config nasm` supplies build tools, not the FFmpeg binary
or its linked third-party libraries. The wrapper can install missing CMake
through an existing Homebrew installation; otherwise it fails explicitly.
GitHub Actions prepares CMake, pkg-config and NASM before invoking the macOS
wrapper on both native runners. Local Intel builds must also install NASM;
re-running an older workflow revision does not pick up this preparation step.

```bash
PYTHON312=/absolute/path/to/python3.12 bash packaging/build-macos.sh
```

Windows prerequisites: CPython 3.12 x64 with Tcl/Tk, Inno Setup 6, and MSYS2
installed at `C:\msys64`. The wrapper updates that MSYS2 installation and installs
the MINGW64 GCC/CMake/pkgconf/make toolchain plus NASM and MSYS make using pacman.
The native MINGW64 build uses `gcc`, `g++`, `ar` and `ranlib` from
`C:\msys64\mingw64\bin`, rather than assuming cross-prefixed binutils aliases
exist. That directory is placed before Git/Visual Studio tools on the build
process PATH. The Windows HarfBuzz build explicitly uses Unix Makefiles.
Its CMake compiler, archiver and ranlib settings use absolute Windows paths
resolved from the selected MINGW64 tools. The make program is explicitly
MSYS2's `C:\msys64\usr\bin\make.exe`, so CMake cannot choose Strawberry Perl's
`gmake.exe`. Prefix paths in CMake's inherited compiler/linker flags are also
converted from MSYS paths to native Windows paths.
The repository's `.gitattributes` fixes text checkout line endings to LF,
including hash-pinned Piper license evidence and corresponding-source recipes.
Do not replace the pinned hashes with Windows CRLF hashes or bypass verification.
FFmpeg's make program targets are `ffmpeg.exe` and `ffprobe.exe` on Windows,
and `ffmpeg` and `ffprobe` on macOS. The recipe selects the suffix per target;
the installed executable names and bundle layout remain unchanged.
The Windows final static link explicitly includes `-lstdc++` in `--extra-libs`.
HarfBuzz is C++, so `-static-libstdc++` alone does not add the runtime when
FFmpeg links with the C compiler. Keep the runtime after dependent libraries
and retain the static-link flags to avoid a new runtime DLL requirement.
This changes the build machine's MSYS2 environment; use a dedicated build machine
or the clean GitHub runner, not a shared development installation. A clean
GitHub Windows runner provides Inno Setup; locally install it yourself and, if
needed, set `$env:ISCC` to its `ISCC.exe`.

```powershell
./packaging/build-windows.ps1 -Python312 "C:\Path\To\Python312\python.exe"
```

Both wrappers delete/recreate **only** `packaging/.venv-build`, install the
hash-locked native dependency set, run `pip check`, and build in
`packaging/.work`. They do not reuse/copy the development `.venv`, existing app
bundles, source data or old platform launchers. All build caches and scratch
files stay under `packaging/.work`. Downloads are from public PyPI and the pinned
upstream source URLs; no source or credentials are uploaded by local build scripts.
Before freezing, each wrapper builds the pinned PyAV source against managed
shared FFmpeg libraries, force-installs that locally built wheel without
dependency resolution or index access, then verifies its linkage and wheel
hash. The upstream PyAV wheel hashes remain in the locked dependency graph for
faster-whisper's dependency resolution, but the wheel is replaced before
PyInstaller runs and the build aborts unless the installed extensions link to
the locally built shared libraries.
On Windows, `.dll.a` files are MinGW import libraries, not static FFmpeg
archives; they are required for linking but are not distributed as runtime
libraries. True static `.a` libraries are rejected. The pinned setuptools
toolchain runs `build_ext --compiler=mingw32` before `bdist_wheel`; the build
first compiles, links, and imports a small extension in the selected CPython
3.12 x64 interpreter. An incompatible compiler, Python import library, or
runtime fails this preflight rather than falling back to MSVC or an upstream
PyAV wheel.
The dedicated environment is required by `build.py build`; running that command
from an unrelated environment fails.

The bundle gate also asks the frozen worker to decode the H.264 fixture with
library-path overrides removed. This checks PyInstaller's relocated DLL/dylib
loading in addition to decoding in the source-build interpreter; presence of
library files alone is not accepted as proof. On macOS the source prefix's
library directory is temporarily hidden during that frozen-worker check and
restored on success or failure, preventing absolute build paths from masking
missing or incorrectly relocated bundled libraries. The corresponding-source archive
contains the PyAV extension compilation log as well as the FFmpeg build logs.

The wrappers require several GB of disk and may download substantial PyTorch,
OpenCV and speech/translation dependencies. The bundled faster-whisper VAD
ONNX assets are included; large Whisper transcription model weights are **not**
downloaded during packaging. The pinned Piper LJ Speech female and John male voices are included for
offline synthesis. Azure AI Speech, Argos language packs and optional Ollama
remain separate runtime features; "self-contained" means
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
DMG creation retries only `hdiutil` failures reporting `Resource busy`, at most
three attempts with 5- and 10-second waits. Every failure is logged; other errors
and an exhausted retry limit fail the build. A successful image must still pass
the mounted bundle verification. This does not identify or forcibly release
the resource causing a busy error.
Drag the app into Applications. The executable layout is:

```text
Windows: TranslateService/{TranslateService.exe,TranslateWorker.exe,ffmpeg.exe,ffprobe.exe}
macOS:   TranslateService.app/Contents/MacOS/{TranslateService,TranslateWorker,ffmpeg,ffprobe}
```

PyInstaller's internal resource root contains `BUNDLED`, `VERSION`, `version.json`,
`docs/`, all `lecture_poc/prompts/` files and `third-party-notices/`. Both analyses include
`source/worker` in their module search paths; the GUI analysis is lightweight
and the worker analysis owns the conversion dependencies. Platform-specific
keyring backends and `piper.espeakbridge` have explicit hidden imports. Compiled
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

FFmpeg/ffprobe are now built from source for Windows x64 and both Mac CPUs.
`packaging/ffmpeg.json` records exact source archives and SHA-256 hashes;
downloads fail closed on hash mismatch. The build recipe uses a minimal GPLv3
configuration with H.264/libx264, AAC, MP3 and libass subtitles. Nonfree
components and unrequested optional codecs are excluded.

PyAV 15.1.0 is also built from its pinned PyPI source distribution, not retained
from the upstream binary wheel. `packaging/pyav.json` records and hash-pins that
sdist and FFmpeg 7.1.1. A separate recipe builds only shared FFmpeg libraries
for PyAV, disables GPL/nonfree/version-3 components, encoders, muxers, network
protocols and static libraries, and uses FFmpeg's LGPL-2.1-or-later terms.
It does not build or enable x264/x265. The DLL/dylib dependencies are checked
against PyAV's native extensions and must be included in the frozen bundle.
The packaging run creates an H.264 fixture with the separately built GPL
FFmpeg executable and verifies that the installed PyAV extension decodes an
actual frame through these managed shared libraries.

The PyAV binding itself is BSD-3-Clause; the linked shared FFmpeg libraries
are LGPL-2.1-or-later. Their exact source archive hashes, build recipe,
configure/build records and license texts are included in
`TranslateService-<version>-<target>-pyav-source.tar.gz`. The bundle carries
PyAV's BSD notice and FFmpeg's LGPL text under `third-party-notices/pyav/`.
Publish that archive alongside both the GPL FFmpeg source archive and the
Piper/application source archive. The release gate requires all three for
every platform and rejects changed pins, license/build-policy violations,
missing LGPL notices or absent decode-smoke evidence. These source archives
are separate because the standalone GPL FFmpeg executable and PyAV's LGPL
shared libraries are distinct builds with different license obligations.

The former `eugeneware/ffmpeg-static` Apple Silicon binary explicitly reports
`Therefore it is not legally redistributable` under `-L`. Do not distribute
existing installers/DMGs containing that binary, even for test users. Removing
that message or copying different LICENSE text is not a remedy: rebuild from
the permitted source configuration.

Do **not** replace these with copied Homebrew executables: those often depend
on developer-local dylibs. macOS staging rejects any linkage except `/usr/lib/`
and `/System/Library/`. Each tool is launched natively during staging. Final
bundle tests run with the bundled executable directory first and only OS tools
in PATH, then encode/probe a synthetic H.264/AAC video and verify the `subtitles`
filter. Native staging also compares raw-frame hashes with and without subtitles
to require actual text rendering, checks the H.264/AAC codecs and 1.5-second
duration, and encodes MP3. `drawtext` is not required because OCR text replacement
is omitted. Windows tools are statically linked from the same pinned sources;
their DLL imports must be OS libraries and actual execution is checked in the
clean hosted build.
The Windows OS-library allowlist includes `avicap32.dll`, the system Video for
Windows capture library documented by Microsoft. This does not allow external
MinGW runtime DLLs or separately installed FFmpeg DLLs.

Bundle checks invoke both tools with `-version` and `-L`, reject nonfree or
unredistributable reports, and require the configured GPLv3 license. Source
builds capture the tool versions, actual build configuration, linkage and
license texts. Each target produces a **corresponding-source archive** next to
the binaries, containing exact original source archives of FFmpeg and enabled
libraries, the recipe/manifest and actual build records.

Publish `TranslateService-<version>-<target>-ffmpeg-source.tar.gz` with that
target's installer/DMG and keep it accessible for the binary recipients. It is
not the GitHub automatically generated application "Source code (zip)".
Checksums cover it; the release gate refuses missing/mismatched source archives
or nonfree build metadata. The gate also inspects the archive without extracting
it: original source hashes, exact tracked manifest/recipes, target/version,
binary-hash records and configure/build logs must agree. Review the source contents and
[FFmpeg legal guidance](https://ffmpeg.org/legal.html) before publishing.
An archive/checksum gate detects technical omissions, not every legal issue:
obligations for enabled libraries, codec patents and other application
dependencies still require review. GPL does not automatically change the
license of the separately executed application into GPL.

Python package inventory, wheel license/notice files, dependency lock and the
application MIT license remain in every bundle. Review wheels missing required
license texts. Unofficial Edge TTS and XTTS have been removed; Azure AI Speech
uses the customer's official Speech resource. Production signing/notarization
and review of other dependencies remain separate requirements.

## Local Piper and official Azure Speech

The default voice is `en_US-ljspeech-medium`, used by pinned `piper-tts` 1.3.0.
The model, its JSON configuration, model card and license-evidence text are bundled and SHA-256
verified. The model creator, Bryce Beattie, labels this LJ Speech model public
domain on the [creator's distribution page](https://brycebeattie.com/files/tts/).
The dataset's public-domain statement and the model creator's statement are
distinct pieces of provenance; retain both. Do not substitute another Piper
voice based on a repository-level MIT tag: each voice has its own model card
and may have research-only or other restrictions.

The male alternative is `en_US-john-medium` (John), explicitly described by
the same creator as a US English male voice with public-domain licensing and
LibriVox training recordings. Download its original model/config from the
creator's URLs, verify the pinned SHA-256 values and retain the John-specific
license evidence; do not assume another male model has the same permissions.
Both voices are included in new frozen builds and both must pass offline
synthesis self-tests. Selecting John loads its own model/config, not a
pitch-shifted copy of LJ Speech. The default female voice and existing
female-voice jobs remain unchanged.

Piper and its embedded eSpeak NG are GPL-3.0-or-later. Because the frozen
application imports Piper, do not rely on an independent-process exception or
describe the combined frozen application as MIT-only. Provide GPLv3 terms and
the corresponding application/Piper/eSpeak source needed to rebuild the
distributed combination, while retaining the original MIT copyright/license
notices for application code. University-internal, non-selling use does not
justify ignoring obligations when copies are distributed outside the
organization. The GPL does not automatically license the generated lecture
video; permissions for lecture content and participants are separate.

Publish `TranslateService-<version>-<target>-piper-source.tar.gz` alongside each
binary and the FFmpeg source archive. It includes the exact pinned Piper and
embedded eSpeak sources, build instructions and application build inputs.
The release validator checks original source hashes, runtime/model provenance
and matching manifest records, not just the outer archive checksum. The
offline worker self-test synthesizes a short sentence into a nonempty WAV;
model files alone are not proof that phonemization or inference works.

In source mode, the pinned voice is prepared on first use in a per-user cache
outside the checkout. `PIPER_MODEL_DIR` can point to a preprepared directory
containing the exact verified model/config/card/license-evidence files. Frozen builds use only bundled
assets; they do not download a missing/damaged model or silently select another.

The alternative backend is the official Azure AI Speech REST API with the
user's Speech resource key and region. Keys are not bundled, passed on command
lines, or written to job metadata. GUI jobs inject only the selected service's
credentials into the worker environment. Azure requests send English text for
synthesis and may be billed; no automatic retry after ambiguous failures and
no fallback to unofficial endpoints is performed. Azure live acceptance testing
requires an authorized resource and is distinct from mocked request tests.
See the [beginner configuration steps](INSTALL_BUNDLED.md#音声合成を選ぶpiperとazure-ai-speech).

XTTS and unofficial Edge TTS are removed, including optional requirements,
worker dispatch, fallback paths and locks. An old development environment may
still contain packages installed by older versions; use a new dedicated build
environment, rather than assuming installed-but-unused packages are absent.

### Rebuilding the media tools from the corresponding source

The archive contains `ffmpeg.json`, `sources/` (eight original archives: FFmpeg
7.1.1, x264, LAME, libass, FreeType, FriBidi, HarfBuzz and zlib),
`build-ffmpeg.sh`, `ffmpeg_source.py`, `build-info.json`, and `build/` records.
Copyright/license texts remain in the original archives and are also copied
into the application's `third-party-notices/ffmpeg/` directory. FreeType's FTL
and GPL texts are included along with the distribution acknowledgement.

Extract the source archive into a new directory. Install the native build
prerequisites above, read `build-info.json` for the original configuration,
and run the supplied shell recipe from the extracted directory:

```bash
# macOS: use macos-x64 on an Intel Mac.
SOURCE_DIR="$PWD/sources" BUILD_DIR="$PWD/rebuild" PREFIX="$PWD/result" \
  TARGET=macos-arm64 BUILD_JOBS=4 bash ./build-ffmpeg.sh
```

For Windows, open an **MSYS2 MINGW64** terminal, change to the extracted
directory, and run the same command with `TARGET=windows-x64`. Ensure the
MINGW64 compiler/CMake/pkgconf and MSYS make/NASM are installed first. Do not
run the recipe in PowerShell or Git Bash. The result is `result/bin/ffmpeg`
and `result/bin/ffprobe` (`.exe` on Windows). No original source patches are
applied; the recipe records build-time adjustments, including x264 pkg-config
flags. The archived Python module records the full application staging process;
it is not a standalone replacement for the shell recipe outside the checkout.

Run both tools with `-version` and `-L`, and retain the original source archive
with any binary distribution. Rebuilding with different compiler/SDK versions
or paths is not guaranteed to produce byte-identical executables.

## Dependency lock maintenance

`packaging/requirements.in` constrains the worker requirements plus freezing
tools. Native lock files are generated with **uv 0.8.22**, contain exact
transitive versions and SHA-256 hashes, and are committed under `packaging/locks`.
Torch 2.2.2 and NumPy 1.26.4 intentionally preserve Intel macOS wheels/ABI;
review/test those constraints before upgrades. No unresolved runtime transitive
version ranges remain in the committed native locks. All locked packages except
Argos's strictly required `sacremoses==0.0.53` use binary wheels only. PyAV's
upstream wheel is installed only as a dependency-resolution placeholder and is
replaced before freezing by the hash-pinned source build described above;
`cython==3.1.4` is an explicit, per-platform hash-locked build dependency.
The Argos upstream version has no wheel, so its hash-pinned pure-Python sdist is built
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

Distribution checks reject job databases (including WAL/SHM sidecars), job/log/
queue/processing directories, user settings and custom glossary files anywhere
in the bundle. Only explicitly selected application resources are collected.
`build.py validate` and `build.py build` also reject any indexed files under
`source/worker/data/` and any corresponding-source inventory entry in that
runtime-data tree. Source-archive builds without Git use the source manifest for
revision identity and do not require a `.git` directory.
Existing history on a test PC comes from its external user data directory and
is intentionally preserved across installations; do not clear it during build
or app startup. Use a fresh user account/data directory for first-run testing.

The worker spec collects faster-whisper package data, including both Silero VAD
ONNX assets. These small voice-activity models are bundled; unlike Whisper
transcription weights they are not first-run downloads. Worker self-tests check
both files and create the CPU ONNX sessions via `get_vad_model()` without network
access, so missing package data fails before an installer is produced.

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
If a frozen GUI or worker self-test fails, the build logs its JSON report before
propagating the failure. If startup fails before a report is written, the log
explicitly says no report was produced; captured process output is also logged.
Failed Actions builds preserve existing smoke-test JSON files in a separate
`TranslateService-<target>-diagnostics` artifact. These synthetic-test reports
contain no API keys or user videos and are not distribution artifacts.

Successful output under ignored `packaging/artifacts/` uses names such as:

```text
TranslateService-0.1.0-windows-x64-setup.exe
TranslateService-0.1.0-windows-x64-portable.zip
TranslateService-0.1.0-macos-arm64.dmg
TranslateService-0.1.0-macos-x64.dmg
TranslateService-0.1.0-<target>-build-info.json
TranslateService-0.1.0-<target>-ffmpeg-source.tar.gz
TranslateService-0.1.0-<target>-pyav-source.tar.gz
TranslateService-0.1.0-<target>-piper-source.tar.gz
TranslateService-0.1.0-<target>-SHA256SUMS.txt
```

Build information records the Git revision or source-manifest SHA-256,
native Python, lock/FFmpeg/PyAV manifest hashes, corresponding-source
archive/hash and license policy, the PyAV H.264 decode smoke report, and frozen
smoke reports.
Checksums cover each target's distributables, sources and build information. Before handing binaries to users, verify hashes on a clean machine
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
