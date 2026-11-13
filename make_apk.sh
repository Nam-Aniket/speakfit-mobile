#!/bin/bash
# Builds "build/$NAME.apk" with SDK tools only (aapt2, kotlinc, d8, zipalign, apksigner). No Gradle, no androidx.
# Same pattern as ~/Documents/health-dashboard/make_apk.sh. The sync token goes into an asset, never into git.
set -euo pipefail
cd "$(dirname "$0")"
export JAVA_HOME="${JAVA_HOME:-/opt/homebrew/opt/openjdk@17}"
SDK="$HOME/Library/Android/sdk"; BT="$SDK/build-tools/35.0.0"; JAR="$SDK/platforms/android-35/android.jar"
STDLIB="$(brew --prefix kotlin)/libexec/lib/kotlin-stdlib.jar"
KS_DIR="$HOME/Library/Application Support/SpeakFitMobile-build"; KS="$KS_DIR/speakfit-mobile.keystore"; KC="com.aniket.speakfitmobile.build"
OUT=build/apk; NAME=SpeakFitMobile; DEBUG=""
# --debug: emulator test build (debuggable, 20 s parts, Mac at the emulator host address). Never goes on the phone.
if [ "${1:-}" = "--debug" ]; then OUT=build/apk-debug; NAME=SpeakFitMobile-debug; DEBUG="--debug-mode"; fi
VERSION_CODE="${VERSION_CODE:-$(git rev-list --count HEAD)}"
rm -rf "$OUT"; mkdir -p "$OUT/classes" "$OUT/assets"

# shared upload token (Mac server reads the same file)
mkdir -p secrets; [ -f secrets/token ] || { openssl rand -hex 24 > secrets/token; chmod 600 secrets/token; }
cp secrets/token "$OUT/assets/token"
[ -n "$DEBUG" ] && { echo 20000 > "$OUT/assets/debug_part_ms"; echo 10.0.2.2:8770 > "$OUT/assets/debug_host"; }

# signing key: outside the repo, password in Keychain
mkdir -p "$KS_DIR"; chmod 700 "$KS_DIR"
if [ ! -f "$KS" ]; then
  KS_PASS="$(openssl rand -hex 24)"
  security add-generic-password -U -s "$KC" -a keystore_password -w "$KS_PASS" -T /usr/bin/security
  KS_PASS="$KS_PASS" "$JAVA_HOME/bin/keytool" -genkeypair -keystore "$KS" -storepass:env KS_PASS -keypass:env KS_PASS -alias sfm -keyalg RSA -keysize 2048 -validity 20000 -dname "CN=SpeakFit Mobile, O=Aniket" -noprompt 2>/dev/null
  security add-generic-password -U -s "$KC" -a keystore_backup_b64 -w "$(base64 < "$KS")" -T /usr/bin/security
  echo "Created APK signing key in $KS_DIR and backed it up in the Keychain"
fi
KS_PASS="$(security find-generic-password -s "$KC" -a keystore_password -w)"; export KS_PASS

"$BT/aapt2" compile --dir android/res -o "$OUT/res.zip"
"$BT/aapt2" link -I "$JAR" --manifest android/AndroidManifest.xml --min-sdk-version 29 --target-sdk-version 35 \
  --version-code "$VERSION_CODE" --version-name "0.1.$VERSION_CODE" -A "$OUT/assets" $DEBUG -o "$OUT/base.apk" "$OUT/res.zip"
kotlinc android/src/*.kt -cp "$JAR" -d "$OUT/classes.jar" -jvm-target 11 -no-reflect -nowarn 2>&1 | grep -v '^warning' || true
# d8 from build-tools 35 cannot parse Kotlin 2.4 metadata and prints ~1000 warnings; metadata is only for reflection, so the log is kept, not shown
"$BT/d8" --lib "$JAR" --min-api 29 --release --output "$OUT" "$OUT/classes.jar" "$STDLIB" > "$OUT/d8.log" 2>&1 || { cat "$OUT/d8.log"; exit 1; }
(cd "$OUT" && zip -q -j base.apk classes.dex)
"$BT/zipalign" -f -p 4 "$OUT/base.apk" "$OUT/aligned.apk"
"$BT/apksigner" sign --ks "$KS" --ks-pass env:KS_PASS --key-pass env:KS_PASS --out "build/$NAME.apk" "$OUT/aligned.apk"
"$BT/apksigner" verify "build/$NAME.apk"
rm -f "$OUT/assets/token" "$OUT/base.apk" "$OUT/aligned.apk"
ls -la "build/$NAME.apk"
