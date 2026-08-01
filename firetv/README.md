# NostalgiaBox for Fire TV

This focused Android TV client connects to one NostalgiaBox server on a trusted
LAN. It direct-plays normalized H.264/AAC MP4 media and uses the server's HLS
fallback for media that requires remuxing or transcoding.

## Kid mode

After the first successful server connection, the app requires a four-digit
parent PIN and then enters kid mode:

- Up/down changes channels and select/play pauses or resumes playback.
- Back stays inside NostalgiaBox.
- A short Menu press does nothing. Hold Menu for three seconds to enter the
  parent PIN.
- Parent mode can open Fire TV Home, change the NostalgiaBox server or PIN, or
  relock immediately.
- Returning to NostalgiaBox always relocks parent mode.
- Leaving for Fire TV Home stops playback and releases the server session;
  returning creates exactly one fresh live session.
- Channel changes use the same retro presentation as the web player: a
  top-left monospace channel bug, program title, brief static flash, scanlines,
  and a subtle CRT-style vignette.

The PIN is stored locally as a salted derived hash, not as plaintext. Five
failed attempts impose a 30-second delay. If the PIN is forgotten, clear the
NostalgiaBox application data from Fire TV settings and repeat setup.

The app requests a best-effort launch after boot and after an APK update. Fire
OS firmware can block apps from opening activities in the background, so this
must be tested on the physical television. NostalgiaBox does not replace the
Amazon launcher and cannot intercept the system-controlled Home button.

## Build

Install Android SDK 35 and use Java 17 or newer, then run:

```bash
export ANDROID_HOME="$HOME/Library/Android/sdk" # macOS default
./gradlew testDebugUnitTest lintDebug assembleDebug
```

The debug APK is written to `app/build/outputs/apk/debug/app-debug.apk`.

## Brand assets

The TV client packages `nostalgiabox.png` as density-specific Android launcher
icons. It also uses an opaque 16:9 banner for the Fire TV launcher. Regenerate
both, plus the matching Amazon Appstore upload asset, from the repository root:

```bash
python3 firetv/scripts/generate-brand-assets.py
```

The generated files are:

- `app/src/main/res/mipmap-*/ic_launcher.png`: icons embedded in the APK.
- `app/src/main/res/drawable-nodpi/tv_banner.png`: Fire TV launcher banner.
- `store-assets/fire-tv-app-icon-1280x720.png`: Amazon Appstore listing image.

The generator requires Pillow and uses the bundled VT323 font. The app also
packages that font under `app/src/main/res/font` for the channel overlay. Store
signing, screenshots, and submission metadata remain separate release steps.

## Sideload

On the Fire TV:

1. Open **Settings > My Fire TV** or **Device & Software > About**.
2. Select the TV name seven times to reveal **Developer Options** if it is
   hidden.
3. Open **Developer Options** and enable **ADB Debugging**.
4. Open **About > Network** and note the TV's LAN IP address.

Then run on the Mac:

```bash
"$ANDROID_HOME/platform-tools/adb" connect FIRE_TV_IP:5555
"$ANDROID_HOME/platform-tools/adb" install -r app/build/outputs/apk/debug/app-debug.apk
"$ANDROID_HOME/platform-tools/adb" shell am start \
  -n com.nostalgiabox.tv/.MainActivity
```

Approve the ADB connection prompt on the television. On first launch, enter
`http://192.168.1.121:8080`, create and confirm the parent PIN, and verify
channel playback.

Record the actual device platform before testing boot behavior:

```bash
"$ANDROID_HOME/platform-tools/adb" shell getprop ro.product.model
"$ANDROID_HOME/platform-tools/adb" shell getprop ro.build.version.sdk
"$ANDROID_HOME/platform-tools/adb" shell getprop ro.build.version.release
"$ANDROID_HOME/platform-tools/adb" shell getprop ro.build.fingerprint
"$ANDROID_HOME/platform-tools/adb" shell cmd package resolve-activity \
  -a android.intent.action.MAIN -c android.intent.category.HOME
```

Reboot once from Fire TV settings and observe whether NostalgiaBox launches
without interaction. If it does not, pin NostalgiaBox in the Fire TV app row;
do not disable or replace the Amazon launcher.

Finally, enable the television's native parental controls under **Settings >
Preferences > Parental Controls**, including PIN protection for app launches
when the firmware offers it. This protects other applications after a child
presses the system-controlled Home button.

The client intentionally permits cleartext HTTP because the server is a
trusted-LAN appliance. Do not expose the server to the internet.
