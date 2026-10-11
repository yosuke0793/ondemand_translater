#!/bin/bash
set -euo pipefail

: "${SOURCE_DIR:?}" "${BUILD_DIR:?}" "${PREFIX:?}" "${TARGET:?}"
mkdir -p "$BUILD_DIR" "$PREFIX"
exec > >(tee "$BUILD_DIR/build.log") 2>&1

extract_source() {
    local archive="$1" destination="$2"
    mkdir -p "$destination"
    tar -xf "$SOURCE_DIR/$archive" -C "$destination" --strip-components=1
}

FFMPEG_TARGET_FLAGS=()
if [[ "$TARGET" == macos-* ]]; then
    export CFLAGS="-O2 -fPIC -I$PREFIX/include ${CFLAGS:-}"
    export LDFLAGS="-L$PREFIX/lib -Wl,-rpath,@loader_path ${LDFLAGS:-}"
    if [[ "$TARGET" == macos-arm64 ]]; then
        FFMPEG_TARGET_FLAGS+=(--arch=aarch64)
    else
        FFMPEG_TARGET_FLAGS+=(--arch=x86_64)
    fi
elif [[ "$TARGET" == windows-x64 ]]; then
    export PATH="/mingw64/bin:/usr/bin:$PATH"
    CROSS="${CROSS_COMPILE:-}"
    export CC="${CROSS}gcc" CXX="${CROSS}g++" AR="${CROSS}ar" RANLIB="${CROSS}ranlib"
    for tool in "$CC" "$CXX" "$AR" "$RANLIB" make objdump; do
        command -v "$tool" >/dev/null || {
            echo "Required MINGW64 build tool is missing: $tool" >&2
            exit 1
        }
    done
    export CFLAGS="-O2 -I$PREFIX/include ${CFLAGS:-}"
    export LDFLAGS="-L$PREFIX/lib -static-libgcc -static-libstdc++ ${LDFLAGS:-}"
    FFMPEG_TARGET_FLAGS+=(--target-os=mingw32 --arch=x86_64 --cross-prefix="$CROSS")
else
    echo "Unsupported target: $TARGET" >&2
    exit 2
fi

extract_source ffmpeg-7.1.1.tar.xz "$BUILD_DIR/ffmpeg"
CONFIGURE_ARGS=(
    --prefix="$PREFIX"
    --disable-gpl --disable-nonfree --disable-version3
    --disable-static --enable-shared
    --disable-programs --disable-doc --disable-debug --disable-autodetect
    --disable-network --disable-encoders --disable-muxers --disable-devices
    --enable-protocol=file,pipe,data
    --enable-pic --disable-x86asm
)
CONFIGURE_ARGS+=("${FFMPEG_TARGET_FLAGS[@]}")
printf '%q ' "${CONFIGURE_ARGS[@]}" > "$BUILD_DIR/ffmpeg-configure-args.txt"
printf '\n' >> "$BUILD_DIR/ffmpeg-configure-args.txt"
(cd "$BUILD_DIR/ffmpeg" && ./configure "${CONFIGURE_ARGS[@]}") \
    2>&1 | tee "$BUILD_DIR/ffmpeg-configure.log"
make -C "$BUILD_DIR/ffmpeg" -j"${BUILD_JOBS:-2}" 2>&1 | tee "$BUILD_DIR/ffmpeg-build.log"
make -C "$BUILD_DIR/ffmpeg" install

for library in avformat avcodec avdevice avutil avfilter swscale swresample; do
    if ! compgen -G "$PREFIX/lib/lib${library}*" >/dev/null; then
        echo "FFmpeg did not install shared library $library" >&2
        exit 1
    fi
done
if find "$PREFIX/lib" -maxdepth 1 \( -name 'libav*.a' -o -name 'libsw*.a' \) \
    ! -name '*.dll.a' | grep -q .; then
    echo "Static FFmpeg libraries are forbidden in the managed PyAV build" >&2
    exit 1
fi
