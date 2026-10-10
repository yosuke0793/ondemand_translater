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
    CROSS="${CROSS_COMPILE:-x86_64-w64-mingw32-}"
    export CC="${CROSS}gcc" CXX="${CROSS}g++" AR="${CROSS}ar" RANLIB="${CROSS}ranlib"
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
    cmake -S "$BUILD_DIR/harfbuzz" -B "$BUILD_DIR/harfbuzz-build" \
        -DCMAKE_INSTALL_PREFIX="$PREFIX" -DCMAKE_PREFIX_PATH="$PREFIX" \
        -DCMAKE_SYSTEM_NAME=Windows -DCMAKE_C_COMPILER="$CC" \
        -DCMAKE_CXX_COMPILER="$CXX" -DCMAKE_AR="$AR" -DCMAKE_RANLIB="$RANLIB" \
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
    CONFIGURE_ARGS+=(--extra-ldflags="-L$PREFIX/lib -static -static-libgcc -static-libstdc++")
fi
CONFIGURE_ARGS+=("${FFMPEG_TARGET_FLAGS[@]}")
printf '%q ' "${CONFIGURE_ARGS[@]}" > "$BUILD_DIR/ffmpeg-configure-args.txt"
printf '\n' >> "$BUILD_DIR/ffmpeg-configure-args.txt"
(cd "$BUILD_DIR/ffmpeg" && ./configure "${CONFIGURE_ARGS[@]}") \
    2>&1 | tee "$BUILD_DIR/ffmpeg-configure.log"
make -C "$BUILD_DIR/ffmpeg" -j"${BUILD_JOBS:-2}" ffmpeg ffprobe \
    2>&1 | tee "$BUILD_DIR/ffmpeg-build.log"
make -C "$BUILD_DIR/ffmpeg" install
