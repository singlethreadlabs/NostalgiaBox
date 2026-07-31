# NostalgiaBox for Fire TV

This focused Android TV client connects to one NostalgiaBox server on a trusted
LAN. It direct-plays normalized H.264/AAC MP4 media and uses the server's HLS
fallback for media that requires remuxing or transcoding.

## Build

Install Android SDK 35 and use Java 17 or newer, then run:

```bash
export ANDROID_HOME="$HOME/Library/Android/sdk" # macOS default
./gradlew testDebugUnitTest lintDebug assembleDebug
```

The debug APK is written to `app/build/outputs/apk/debug/app-debug.apk`.

## Sideload

Enable developer options and ADB debugging on the Fire TV, note its LAN IP,
and run:

```bash
"$ANDROID_HOME/platform-tools/adb" connect FIRE_TV_IP:5555
"$ANDROID_HOME/platform-tools/adb" install -r app/build/outputs/apk/debug/app-debug.apk
```

On first launch, enter the NostalgiaBox server URL, including port 8080. The
app validates the server and remembers the address. Use D-pad up/down to change
channels, select or play/pause to control playback, and the menu/settings key
to change the server address.

The client intentionally permits cleartext HTTP because the server is a
trusted-LAN appliance. Do not expose the server to the internet.
