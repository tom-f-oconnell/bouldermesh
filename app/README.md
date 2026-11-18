# BoulderMesh phone apps

Android and iPhone apps for photographing a boulder so that its mesh comes out whole, level, the right size and in the
right place. They show what's still to photograph over the camera's picture, take each photo when it's worth taking,
judge it, and record with it which way was down, where the phone was and which way it faced. **Neither has run on a
phone yet**: see [What's been checked](#whats-been-checked).

## Using it

1. **While you have signal**, open *Get meshes…* and get the boulders you'll visit (nearest first; *Get the N within
   2 km* takes them all). It asks whether to keep each with its textures or without (shown as a wireframe), saying
   what each takes. They're kept on the phone, with their holds, for capturing and for the viewer, and work with no
   signal.
2. **Before a capture**, *Calibrate* the compass: half a minute of turning the phone every which way, away from metal.
3. **At the rock**, press *Capture* on its mesh. It first asks for **one slow turn all the way round on the spot**, with
   the phone held upright as for a photo: that's how it finds which way is down (below). Then a small model of the
   mesh appears in front of you:
   - tap a spot on the model that you can find on the rock, then tap the same spot on the real rock;
   - do that once more, with a spot well to one side of the first;
   - the mesh is drawn over the rock. If it sits on it, press *It fits: start*. If not, *Add a spot* or *Start again*.
4. **Follow the words at the top.** It first walks you to a few rings in the air: where earlier photos of the rock
   were taken, to take one again from each, which is what ties your photos to those. Then red parts of the mesh still
   need photos, amber ones are part done, green ones done; rings mark holds. On the way in to a hold it asks you to
   stop for a photo at each halving of the distance. The app takes a photo by itself when you're holding still somewhere useful, and says so
   when one came out blurred or badly lit. *Skip this* leaves what can't be reached (too high, the back). *Take* takes
   one whatever is in view, as soon as you hold still (*Hold still…* while it waits, up to 8 s).
5. **Finish**, then *Upload* once you have signal. The server meshes your photos together with the mesh's own.
6. **Once it's meshed**, *View mesh* on its capture shows it in the site's own viewer. Home asks the server how each
   upload stands whenever it comes up (*Check if meshed* asks again); press a capture for what it holds, grouped by
   what each photo was for (each climb, the card, tying to the old photos, the scenery), with what's been sent ticked.
   *View* on a downloaded mesh shows it the same way. With no signal, the viewer shows the copy kept on the phone (as
   a wireframe if it was kept without textures); an upload's mesh can be kept too (*Keep for viewing with no signal*,
   on its screen).

A rock with no mesh: *Capture a new rock…*, tap its middle, and walk round as much of it as you can; *Can't reach that
side* for the rest, and *Mark a hold* for anything that needs close-ups.

*Mark a hold* (with or without a mesh): tap a hold on the rock, and it's asked for close up. Once the capture is meshed,
the holds you marked are on the mesh as a set of beta of yours, *Marked on the phone* (each a hand hold: change the
roles in the site's *Beta* tab), and on the mesh you were adding to as well.

*Another ring* (with or without a mesh) asks for the rock all round again from another height: hold the phone at least
0.9 m higher or lower than before (crouched, or up on a pole), and keep it near that height all the way round; the
mesh's own targets come back after it. For a phone out of sight, *Settings* has two sounds, both off at first: a bell
when a photo is kept, and a tick over and over while a photo from where the phone is would add nothing. With Auto on,
move until the ticking stops, hold still, and wait for the bell.

Uploading needs an account or the site's editing token. On an Android phone signed in to the site, your account's
*Sign the app in* opens the app signed in, with your photos credited to your account's name. Otherwise, in *Settings*,
paste an invite link, a code from *Sign in another device* on the site, or the link from the admin page's *Pair a
phone*. The app's own *Sign in another device* (in *Settings*) gives a code for signing in on a computer: there, *Edit…*,
*Sign in with a code*.

**A phone with the lower half of its screen broken:** give its account the role *george* (admin page, *People*; or
invite as one). Signed in with it, both apps draw everything, dialogs too, in the top half of the screen, from the
next start (or at once when signing in). The phone's own keyboard still comes up at the bottom.

## More

- **What counts as done** comes with each mesh from the server (`need` in its plan): a hold seen at 0.5 mm of rock a
  pixel from 4 directions at least 15° apart, other rock on a climb at 1.5 mm from 3, and each edge where the mesh
  ends from 3. What the mesh's existing photos already gave counts. *Doing: its climbs / whole rock* switches between
  holds and the rock on climbs, and everything but the ground. A hold too high to be had that finely from the ground
  asks for the best a phone held up can do.
- **Which way is down** sets how steep the mesh's walls are, and no one sensor reading can be trusted for it: an
  accelerometer's zero is typically off by a degree or so, and it doesn't sit exactly square to the camera. Both are
  fixed to the phone, though, and gravity to the world: so over a turn on the spot the phone's errors point every way in
  turn and cancel, as a spirit level's does when it's turned end for end. Nothing level or plumb is needed. The screen
  says afterwards how well the twelve directions agreed (under 0.4° is needed), how far the phone's own tracking had
  been off, and how far one reading would have been.
  During the turn, a ring shows each direction (green once read), a bar how still the phone is now against what it
  should stay under, and how well the directions read so far agree. From five directions on, one that spoils the rest
  is cleared as soon as it shows (orange on the ring, with which way to turn to face it again); the rest are kept. Each photo then
  carries down as the turn found it, and its own accelerometer's beside it, and the screen says if the two drift apart.
  In simulation this finds down to under 0.1°; on a phone it hasn't been tried.
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

or `./apk.sh` (`--install` to put it on the phone `adb` sees), which keeps a copy named by its commit in `dist/`. Every
push that changes `app/` also builds one on GitHub, kept 30 days as the run's `android-debug-<commit>` artifact, and
on main, when the Android app's own code or build changed since the last (not for docs, tests, CI's scripts or the
iPhone app alone, so phones aren't offered updates that change nothing), as the release [android-latest](https://github.com/tom-f-oconnell/bouldermesh/releases/tag/android-latest):
open that on the phone, tap the `.apk` and allow installing it, with no USB and no sign-in. Its `bouldermesh.apk` is the same file
under a name that stays, a direct download: https://github.com/tom-f-oconnell/bouldermesh/releases/download/android-latest/bouldermesh.apk
(the volunteer page's *Data collection* links to it, with how testers install, sign in and use the apps).

Every build is signed with one key, so each installs over the last, keeping captures not yet sent, and the app updates
itself: it looks for a newer build each time it comes to the front, at most every 10 minutes (the release's title says
its build number, CI's run number, which is its `versionCode`), and offers *Update* on its Home screen and in *Settings*
(which shows the build and commit installed, and has *Check for updates*), which downloads it and hands it to Android's installer (the
person confirms; the first time, Android asks them to let the app install apps). The key is the repo's secrets
`ANDROID_SIGNING_KEYSTORE` (base64 of a PKCS12 file, alias `bouldermesh`) and `ANDROID_SIGNING_PASSWORD`; its copy on hal
is in `/data/tom/android-toolchain/signing/` (`signing.env` gives Gradle `BM_SIGNING_KEYSTORE` and
`BM_SIGNING_PASSWORD_FILE`), and should be kept somewhere else safe too: a build signed with another key can't update an
installed one, so losing it means every phone uninstalls once. Without the key, a build is signed with the SDK's debug
key, which installs over nothing else.

iPhone, on a Mac with Xcode 15 or later (no Android SDK needed):

    brew install xcodegen && brew install --cask temurin       # XcodeGen, and a JDK for the Kotlin build Xcode runs
    cd ios/xcode && xcodegen && open BoulderMesh.xcodeproj     # set your team under Signing, then run on a phone

Without a Mac, or for someone else's iPhone: TestFlight, below. The icon is the site's favicon
(`make_icons.py`, which makes the Android app's too).

### TestFlight: an iPhone without a Mac

`.github/workflows/testflight.yml` builds the iPhone app on GitHub's Macs, signs it for the App Store and uploads it to
TestFlight, from which any tester's iPhone installs it: no Mac, no cable, no registering the phone. It runs on every
push to main that changes the iPhone app's own code or build (`app/shared/src/commonMain`, `app/ios`, the Gradle files;
not docs, tests, CI's scripts or the Android app), or `gh workflow run testflight.yml`, each upload numbered by the run. Until its
secrets are set it only says so. Setting them up, once, with an Apple developer account:

1. [App Store Connect](https://appstoreconnect.apple.com) → *Users and Access* → *Integrations* → *App Store Connect
   API*: generate a key (role *App Manager*), note its issuer ID, and download `AuthKey_<key id>.p8` (only once).
2. [Identifiers](https://developer.apple.com/account/resources/identifiers/list): register an App ID with the bundle ID
   `sh.boulderme.app` (explicit; no capabilities needed).
3. A distribution certificate, made without a Mac: `app/ci/ios-signing.sh csr`, upload the `.csr` it makes as an
   [*Apple Distribution*](https://developer.apple.com/account/resources/certificates/add) certificate, download it,
   then `app/ci/ios-signing.sh p12 <downloaded .cer>`. Its key stays in `~/.bouldermesh-ios-signing`: keep it private.
4. [Profiles](https://developer.apple.com/account/resources/profiles/add): *App Store Connect* distribution, for
   `sh.boulderme.app`, with that certificate; download it.
5. App Store Connect → *Apps* → *+* → *New App*: iOS, bundle ID `sh.boulderme.app`, any SKU; a name not taken on the App
   Store (it needn't be the one under the icon).
6. `app/ci/ios-signing.sh secrets AuthKey_<key id>.p8 <issuer id> <team id> <profile>.mobileprovision` (the team ID is
   under *Membership details*) sets the workflow's seven secrets with `gh`; then `gh workflow run testflight.yml`.

**Giving it to the people you invite:** in App Store Connect's *TestFlight* tab, make an *external* group, add a build
to it (Apple reviews the first build of each version, usually within a day), and turn on its *Public Link*. Put that
link (`https://testflight.apple.com/join/…`) in the admin page's *The iPhone app's TestFlight public link*: the site
then shows it, with the Android app's latest build, to whoever joins from an invite (right after joining) and under
their name at the top of the page. All they do: install TestFlight, open the link on the phone, *Accept*, *Install*.

A build shows in App Store Connect's *TestFlight* tab a few minutes after the run, once Apple has processed it. To put it
on someone's iPhone, add them as a tester: an *internal* tester (a user of your App Store Connect team, up to 100)
gets builds at once; an *external* one (any email, in a group) after Apple's beta review of the first build of each
version, usually within a day. They install Apple's *TestFlight* app and accept the invitation; later builds reach
them by themselves. Each build lasts 90 days. The camera screen needs a real iPhone (ARKit doesn't run in a
simulator); what it does there comes back to the server with each capture (`capture-log.jsonl`), and TestFlight sends
crash reports to App Store Connect.

To try either against a test server rather than the site, set its address in *Settings*, under *Advanced*. Plain `http://` works for one
on your own network (a debug build on Android; local addresses on an iPhone); anything else has to be https.

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
| the Android app's camera screen | starts ARCore and draws the camera's picture in the emulator; tracking never begins there (too few frames a second), so lining up, the overlay and taking photos are unchecked |
| the Android app | never run on a phone |
| the iPhone app | built and linked for the simulator on GitHub's Macs; never run on an iPhone |
| both apps starting up, and every screen but the camera's | run in a simulator and an emulator on GitHub against a test server, a picture kept of each screen (`.github/workflows/apps.yml`): see its last run |

The thresholds for sharpness are guesses until they've met real photos.
