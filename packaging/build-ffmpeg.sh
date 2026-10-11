#!/bin/bash
set -euo pipefail

: "${SOURCE_DIR:?}" "${BUILD_DIR:?}" "${PREFIX:?}" "${TARGET:?}"
mkdir -p "$BUILD_DIR" "$PREFIX"
exec > >(tee "$BUILD_DIR/build.log") 2>&1
export PKG_CONFIG_LIBDIR="$PREFIX/lib/pkgconfig"
export PKG_CONFIG_PATH=
export CFLAGS="-O2 -fPIC -I$PREFIX/include ${CFLAGS:-}"
export LDFLAGS="-L$PREFIX/lib ${LDFLAGS:-}"
export CPPFLAGS="-I$PREFIX/include ${CPPFLAGS:-}"

extract_source() {
    local archive="$1" destination="$2"
    mkdir -p "$destination"
    tar -xf "$SOURCE_DIR/$archive" -C "$destination" --strip-components=1
}

build_autotools() {
    local archive="$1" dir="$2"
    extract_source "$archive" "$dir"
    shift 2
    (cd "$dir" && ./configure --prefix="$PREFIX" --disable-shared --enable-static "$@")
    make -C "$dir" -j"${BUILD_JOBS:-2}"
    make -C "$dir" install
}

if [[ "$TARGET" == macos-* ]]; then
    extract_source zlib-1.3.1.tar.gz "$BUILD_DIR/zlib"
    (cd "$BUILD_DIR/zlib" && ./configure --prefix="$PREFIX" --static)
    make -C "$BUILD_DIR/zlib" -j"${BUILD_JOBS:-2}"
    make -C "$BUILD_DIR/zlib" install
    build_autotools freetype-2.13.3.tar.xz "$BUILD_DIR/freetype" \
        --without-bzip2 --without-png --with-zlib=yes
    build_autotools fribidi-1.0.16.tar.xz "$BUILD_DIR/fribidi"
    extract_source harfbuzz-10.4.0.tar.xz "$BUILD_DIR/harfbuzz"
    cmake -S "$BUILD_DIR/harfbuzz" -B "$BUILD_DIR/harfbuzz-build" \
        -DCMAKE_INSTALL_PREFIX="$PREFIX" -DCMAKE_PREFIX_PATH="$PREFIX" \
        -DBUILD_SHARED_LIBS=OFF -DHB_HAVE_FREETYPE=ON -DHB_HAVE_GLIB=OFF \
        -DHB_HAVE_ICU=OFF -DHB_HAVE_CAIRO=OFF -DHB_HAVE_GRAPHITE2=OFF \
        -DHB_BUILD_UTILS=OFF -DHB_BUILD_SUBSET=OFF
    cmake --build "$BUILD_DIR/harfbuzz-build" --parallel "${BUILD_JOBS:-2}"
    cmake --install "$BUILD_DIR/harfbuzz-build"
    build_autotools libass-0.17.3.tar.xz "$BUILD_DIR/libass" --disable-fontconfig \
        --enable-coretext
    build_autotools lame-3.100.tar.gz "$BUILD_DIR/lame" \
        --disable-frontend --disable-decoder --disable-analyzer
    extract_source x264-c24e06c2e184345ceb33eb20a15d1024d9fd3497.tar.gz "$BUILD_DIR/x264"
    X264_HOST="x86_64-apple-darwin"
    [[ "$TARGET" == macos-arm64 ]] && X264_HOST="aarch64-apple-darwin"
    (cd "$BUILD_DIR/x264" && ./configure --prefix="$PREFIX" --host="$X264_HOST" --enable-static \
        --disable-shared --disable-cli --disable-asm --enable-pic)
    make -C "$BUILD_DIR/x264" -j"${BUILD_JOBS:-2}"
    make -C "$BUILD_DIR/x264" install
    sed -i.bak 's/ -ldl//g' "$PREFIX/lib/pkgconfig/x264.pc"
    rm -f "$PREFIX/lib/pkgconfig/x264.pc.bak"
    FFMPEG_TARGET_FLAGS=(--enable-audiotoolbox --enable-videotoolbox --enable-avfoundation)
elif [[ "$TARGET" == windows-x64 ]]; then
    export PATH="/mingw64/bin:/usr/bin:$PATH"
    CROSS="${CROSS_COMPILE:-}"
    export CC="${CROSS}gcc" CXX="${CROSS}g++" AR="${CROSS}ar" RANLIB="${CROSS}ranlib"
    for tool in "$CC" "$CXX" "$AR" "$RANLIB" cmake pkg-config make nasm cygpath; do
        command -v "$tool" >/dev/null || {
            echo "Required MINGW64 build tool is missing: $tool" >&2
            exit 1
        }
    done
    export CFLAGS="-O2 -I$PREFIX/include ${CFLAGS:-}"
    export LDFLAGS="-L$PREFIX/lib -static -static-libgcc -static-libstdc++ ${LDFLAGS:-}"
    extract_source zlib-1.3.1.tar.gz "$BUILD_DIR/zlib"
    (cd "$BUILD_DIR/zlib" && CHOST=x86_64-w64-mingw32 \
        ./configure --prefix="$PREFIX" --static)
    make -C "$BUILD_DIR/zlib" -j"${BUILD_JOBS:-2}"
    make -C "$BUILD_DIR/zlib" install
    build_autotools freetype-2.13.3.tar.xz "$BUILD_DIR/freetype" \
        --host=x86_64-w64-mingw32 --without-bzip2 --without-png --with-zlib=yes
    build_autotools fribidi-1.0.16.tar.xz "$BUILD_DIR/fribidi" --host=x86_64-w64-mingw32
    extract_source harfbuzz-10.4.0.tar.xz "$BUILD_DIR/harfbuzz"
    NATIVE_PREFIX="$(cygpath -m "$PREFIX")"
    CMAKE_CC="$(cygpath -m "$(command -v "$CC")")"
    CMAKE_CXX="$(cygpath -m "$(command -v "$CXX")")"
    CMAKE_AR="$(cygpath -m "$(command -v "$AR")")"
    CMAKE_RANLIB="$(cygpath -m "$(command -v "$RANLIB")")"
    CMAKE_MAKE="$(cygpath -m /usr/bin/make.exe)"
    CMAKE_CXXFLAGS="${CXXFLAGS:-}"
    CFLAGS="${CFLAGS//$PREFIX/$NATIVE_PREFIX}" \
    CXXFLAGS="${CMAKE_CXXFLAGS//$PREFIX/$NATIVE_PREFIX}" \
    LDFLAGS="${LDFLAGS//$PREFIX/$NATIVE_PREFIX}" \
    cmake -S "$BUILD_DIR/harfbuzz" -B "$BUILD_DIR/harfbuzz-build" \
        -G "Unix Makefiles" \
        -DCMAKE_MAKE_PROGRAM="$CMAKE_MAKE" \
        -DCMAKE_INSTALL_PREFIX="$NATIVE_PREFIX" -DCMAKE_PREFIX_PATH="$NATIVE_PREFIX" \
        -DCMAKE_SYSTEM_NAME=Windows -DCMAKE_C_COMPILER="$CMAKE_CC" \
        -DCMAKE_CXX_COMPILER="$CMAKE_CXX" -DCMAKE_AR="$CMAKE_AR" -DCMAKE_RANLIB="$CMAKE_RANLIB" \
        -DBUILD_SHARED_LIBS=OFF -DHB_HAVE_FREETYPE=ON -DHB_HAVE_GLIB=OFF \
        -DHB_HAVE_ICU=OFF -DHB_HAVE_CAIRO=OFF -DHB_HAVE_GRAPHITE2=OFF \
        -DHB_HAVE_DIRECTWRITE=ON -DHB_BUILD_UTILS=OFF -DHB_BUILD_SUBSET=OFF
    cmake --build "$BUILD_DIR/harfbuzz-build" --parallel "${BUILD_JOBS:-2}"
    cmake --install "$BUILD_DIR/harfbuzz-build"
    build_autotools libass-0.17.3.tar.xz "$BUILD_DIR/libass" \
        --host=x86_64-w64-mingw32 --disable-fontconfig --enable-directwrite
    build_autotools lame-3.100.tar.gz "$BUILD_DIR/lame" --host=x86_64-w64-mingw32 \
        --disable-frontend --disable-decoder --disable-analyzer
    extract_source x264-c24e06c2e184345ceb33eb20a15d1024d9fd3497.tar.gz "$BUILD_DIR/x264"
    (cd "$BUILD_DIR/x264" && ./configure --prefix="$PREFIX" --host=x86_64-w64-mingw32 \
        --cross-prefix="$CROSS" --enable-static --disable-shared --disable-cli --disable-asm)
    make -C "$BUILD_DIR/x264" -j"${BUILD_JOBS:-2}"
    make -C "$BUILD_DIR/x264" install
    sed -i.bak 's/ -ldl//g' "$PREFIX/lib/pkgconfig/x264.pc"
    rm -f "$PREFIX/lib/pkgconfig/x264.pc.bak"
    FFMPEG_TARGET_FLAGS=(--target-os=mingw32 --arch=x86_64 --cross-prefix="$CROSS")
else
    echo "Unsupported target: $TARGET" >&2
    exit 2
fi

extract_source ffmpeg-7.1.1.tar.xz "$BUILD_DIR/ffmpeg"
CONFIGURE_ARGS=(
    --prefix="$PREFIX" --disable-shared --enable-static --enable-pic
    --disable-debug --disable-doc --disable-ffplay --disable-autodetect
    --enable-gpl --enable-version3 --enable-libx264 --enable-libmp3lame --enable-libass
    --pkg-config-flags=--static --extra-cflags="-I$PREFIX/include"
    --extra-ldflags="-L$PREFIX/lib" --extra-libs=-lm
)
if [[ "$TARGET" != macos-* ]]; then
    CONFIGURE_ARGS+=(
        --extra-ldflags="-L$PREFIX/lib -static -static-libgcc -static-libstdc++"
        --extra-libs="-lm -lstdc++"
    )
fi
CONFIGURE_ARGS+=("${FFMPEG_TARGET_FLAGS[@]}")
printf '%q ' "${CONFIGURE_ARGS[@]}" > "$BUILD_DIR/ffmpeg-configure-args.txt"
printf '\n' >> "$BUILD_DIR/ffmpeg-configure-args.txt"
(cd "$BUILD_DIR/ffmpeg" && ./configure "${CONFIGURE_ARGS[@]}") \
    2>&1 | tee "$BUILD_DIR/ffmpeg-configure.log"
PROGRAM_SUFFIX=
[[ "$TARGET" == windows-x64 ]] && PROGRAM_SUFFIX=.exe
make -C "$BUILD_DIR/ffmpeg" -j"${BUILD_JOBS:-2}" "ffmpeg$PROGRAM_SUFFIX" "ffprobe$PROGRAM_SUFFIX" \
    2>&1 | tee "$BUILD_DIR/ffmpeg-build.log"
make -C "$BUILD_DIR/ffmpeg" install
