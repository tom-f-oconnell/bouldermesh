# BoulderMesh phone apps

Android and iPhone apps for photographing a boulder so that its mesh comes out whole, level, the right size and in the
right place. They show what's still to photograph over the camera's picture, take each photo when it's worth taking,
judge it, and record with it which way was down, where the phone was and which way it faced. **Neither has run on a
phone yet**: see [What's been checked](#whats-been-checked).

## Using it

1. **While you have signal**, open *Get meshes…* and get the boulders you'll visit (nearest first; *Get the N within
   2 km* takes them all). They're kept on the phone, with their holds, and work with no signal.
2. **Once for a phone**, *Calibrate*: about a minute, as the screen says. Do the compass part again at the rock if the
   phone complains of it.
3. **At the rock**, press *Capture* on its mesh. A small model of the mesh appears in front of you:
   - tap a spot on the model that you can find on the rock, then tap the same spot on the real rock;
   - do that once more, with a spot well to one side of the first;
   - the mesh is drawn over the rock. If it sits on it, press *It fits: start*. If not, *Add a spot* or *Start again*.
4. **Follow the words at the top.** Red parts of the mesh still need photos, amber ones are part done, green ones
   done; rings mark holds. The app takes a photo by itself when you're holding still somewhere useful, and says so
   when one came out blurred or badly lit. *Skip this* leaves what can't be reached (too high, the back). *Take* takes
   one whatever.
5. **Finish**, then *Upload* once you have signal. The server meshes your photos together with the mesh's own.

A rock with no mesh: *Capture a new rock…*, tap its middle, and walk round as much of it as you can; *Can't reach that
side* for the rest, and *Mark a hold* for anything that needs close-ups.

Uploading needs the site's editing token (*Settings*: paste it, or the link from the admin page's *Pair a phone*).

## More

- **What counts as done** comes with each mesh from the server (`need` in its plan): a hold seen at 0.5 mm of rock a
  pixel from 4 directions at least 15° apart, other rock on a climb at 1.5 mm from 3, and each edge where the mesh
  ends from 3. What the mesh's existing photos already gave counts. *Doing: its climbs / whole rock* switches between
  holds and the rock on climbs, and everything but the ground. A hold too high to be had that finely from the ground
  asks for the best a phone held up can do.
- **A photo isn't kept** if the phone's own motion would have smeared it by more than 1.5 pixels (from its gyroscope,
  its speed and the shutter time), if it's much softer than this capture's others, if more than 5% of it is blown out
  or 25% black, or if the phone had lost track of where it was.
- **Each photo carries** its readings in its EXIF (`BMSH1:` in UserComment, and the usual GPS tags), and the capture a
  record (`capture.json`): [FORMAT.md](FORMAT.md). The server uses them to level the mesh, size it (from how far the
  phone moved between photos), turn it to north and put it on the map.
- **Android photos are small**: ARCore only gives the frames it tracks with (1920 x 1080 on most phones), so a hold
  needs photographing from about 70 cm or nearer. An iPhone gives its full 12 megapixels while tracking, and its
  shutter is kept to 1/120 s or shorter.
- A capture's mesh is new, beside the one it adds to, and linked to the same climbs: which to keep is yours.

## Building

Android, anywhere with a JDK (17 or later) and the Android SDK (`ANDROID_HOME`, or `sdk.dir` in `local.properties`):

    ./gradlew :android:assembleDebug        # android/build/outputs/apk/debug/android-debug.apk
    adb install -r android/build/outputs/apk/debug/android-debug.apk

iPhone, on a Mac with Xcode 15 or later:

    brew install xcodegen
    cd ios/xcode && xcodegen && open BoulderMesh.xcodeproj     # set your team under Signing, then run on a phone

The iPhone app is Kotlin too (`ios/src/iosMain`, on UIKit, ARKit and SceneKit), started by a few lines of Swift, so
all but its linking is checked on any machine: `./gradlew :ios:compileKotlinIosArm64`.

[design.md](../design.md#the-phone-apps-app) has why it's built this way.

    ./gradlew :shared:jvmTest               # the logic both apps share
    python3 ../tests/test_capture.py        # the server's side, and that each side still reads what the other writes

### What's been checked

| | |
|---|---|
| the shared logic (`shared/`): lining up, coverage, hints, photo and sensor judging, a whole capture | tested (JVM) |
| the server: plans, guides, readings in photos, size and place from them, captures meshed with their mesh | tested |
| the files that pass between them | each side's tests read what the other really writes |
| the Android app | compiles to an APK; never run |
| the iPhone app's Kotlin | compiles for iPhone and simulator; never linked or run |
| the iPhone app's Swift and Xcode project | written blind; `ios/xcode/github-workflow-ios.yml` would build it on GitHub's Macs |

The thresholds for sharpness are guesses until they've met real photos.
