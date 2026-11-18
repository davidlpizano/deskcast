#!/usr/bin/env bash
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SDK="${ANDROID_SDK_ROOT:-$HOME/.local/share/android-sdk}"
BT="$SDK/build-tools/34.0.0"
PLATFORM="$SDK/platforms/android-34/android.jar"

BUILD="$HERE/build"
rm -rf "$BUILD"
mkdir -p "$BUILD/gen" "$BUILD/obj" "$BUILD/dex" "$BUILD/res_flat"

"$BT/aapt2" compile --dir "$HERE/res" -o "$BUILD/res_flat"
"$BT/aapt2" link \
  -o "$BUILD/unaligned.apk" \
  -I "$PLATFORM" \
  --manifest "$HERE/AndroidManifest.xml" \
  --java "$BUILD/gen" \
  "$BUILD/res_flat"/*.flat

find "$HERE/src" "$BUILD/gen" -name "*.java" > "$BUILD/sources.txt"
javac --release 11 -classpath "$PLATFORM" -d "$BUILD/obj" @"$BUILD/sources.txt"

"$BT/d8" --min-api 26 --lib "$PLATFORM" --output "$BUILD/dex" $(find "$BUILD/obj" -name "*.class")

(cd "$BUILD/dex" && zip -qj "$BUILD/unaligned.apk" classes.dex)

"$BT/zipalign" -f 4 "$BUILD/unaligned.apk" "$BUILD/aligned.apk"

KEYSTORE="$HERE/debug.keystore"
if [ ! -f "$KEYSTORE" ]; then
  keytool -genkeypair -v -keystore "$KEYSTORE" -storepass android -keypass android \
    -alias androiddebugkey -keyalg RSA -keysize 2048 -validity 10000 \
    -dname "CN=DeskCast,O=OpenSource,C=ES" >/dev/null 2>&1
fi

"$BT/apksigner" sign \
  --ks "$KEYSTORE" --ks-pass pass:android --key-pass pass:android \
  --out "$HERE/DeskCast.apk" "$BUILD/aligned.apk"

rm -rf "$BUILD" "$HERE/DeskCast.apk.idsig"
ls -lh "$HERE/DeskCast.apk"
