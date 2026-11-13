# What passes between the phone apps and the server

Everything a phone learns goes in the photos themselves and in one record of the capture, so that a photo is still
worth its readings wherever it ends up. The server's side is `backend/captureplan.py`, `backend/phonedata.py` and
`capture_plan()`, `merge_capture()` in `backend/server.py`; the apps' is `shared/` (Plan.kt, GuideMesh.kt, Manifest.kt).
Each side's tests read files the other really wrote (`shared/src/jvmTest/resources`).

## From the server

    GET /api/capture/meshes               [{id, name, lat, lng, climbs: ["name grade"], holds, sets: [names]}]: current meshes a capture can add to
    GET /api/capture/plan/<mesh>          the plan below
    GET /api/capture/guide/<mesh>.bin     the guide below, at the plan's guide_url

**The plan** (JSON). Lengths are the mesh's own units unless they say metres; +Y is up.

    v, made, key          the format's version (3: sets, roles, need.detail and need.texture; a hold's sets, holds and detail_m); when it was made; what it was made from: a capture's record gives
                          its plan's key back, as a mesh's frame may have changed between the download and the upload
    mesh, name, lat, lng, heading_deg     the mesh, and where it is and faces as far as the server knows
    frame                 {up, ground_y, size, height, metres_per_unit, scale_guessed}: scale_guessed if its size isn't known
    guide                 {vertices, faces, patches, sha1} of the guide
    need                  {hold: {gsd_mm, views, ladder}, patch: {gsd_mm, views}, edge: {views}, view: {views}}: what makes
                          each kind of target done. ladder: on the way in to a hold, a photo wanted at each step that many
                          times coarser than gsd_mm, up to how its photos see it now
                          detail: {close_m, views, view_deg, ladder, sides_deg, ties}: the holds of the set of beta a capture
                          is for, in place of hold: from close enough that the hold (detail_m round it) fills the picture, no
                          nearer than close_m; views at least view_deg apart; besides straight on, one from its left, right,
                          above and below, sides_deg round, where a phone can get to; and ties steps of its ladder needed,
                          not only asked for on the way in: they tie the close photos to the rest
                          texture: {gsd_mm, views, facing_deg}: rock on no climb, in place of patch: one photo taken now,
                          within facing_deg of square on. Every patch wants one such, whatever its earlier photos saw
    sets                  [{id, name, climb, targets: [hold target ids]}]: the mesh's sets of beta (made on the site)
    roles                 {start, hand, finish, foot: "#rrggbb"}: what a hold of each role is drawn in, as the site draws it
    bridge                {within_m, deg}: how near a view target, and how nearly looking as it did, a photo counts as it
    view_deg, facing_deg  views closer together than view_deg are one; a target more than facing_deg off square isn't seen
    cameras               [{c, d}]: where its photos were taken, and which way they looked
    targets               [{id, kind, p, n, r, prio, role?, from?, out?, have_gsd, have_views, climbs}]
                          kind: hold (id h<i>), patch (p<k>, the guide's patch k), edge (e<i>, with out: the way past it)
                          or view (v<i>): where one of its photos (from) was taken (p) and the way it looked (n, not a
                          surface's normal), to photograph it from again first, which ties a capture's photos to the old
                          prio: 3 a hold, 2 rock on a climb or a view, 1 other rock, 0 the ground
                          have_gsd (units a pixel) and have_views: what its photos so far gave it
                          a hold: sets (the sets of beta it's in), holds (its ids in them, which outlast a plan and are
                          kept when a set is copied or carried to another mesh) and detail_m (metres round it that are the
                          hold, when it's wanted in detail)
    climbs                [{id, name, grade}]

**The guide** (`guide.bin`, little-endian): a copy of the mesh of at most 12,000 faces, to draw and to cast rays at.

    "BMG1", uint32 vertices, uint32 faces, uint32 patches
    float32 x, y, z                for each vertex
    uint32 a, b, c                 for each face
    uint16 patch                   for each face (65535: none)
    float32 finest view            for each face, units a pixel (0: never seen)
    uint8 views                    for each face

## To the server

    POST /api/projects {name}                         a project for the capture
    POST /api/projects/<id>/photos                    its photos, one a request, as a form upload
    POST /api/projects/<id>/capture                   the record below
    POST /api/projects/<id>/capture/points            the points below
    POST /api/projects/<id>/reconstruct               mesh it: with the photos of the mesh the record names, if it names one

**Each photo** is a JPEG stored upright: a phone's sensor reads its picture out on its side, and the app turns it on
the phone (by which way was down) and writes EXIF Orientation 1. So whatever reads it, whether or not it heeds that
tag, sees the same pixels, and the networks trained on upright pictures see one. Everything below that's in "the
stored pixels' axes" (`down`, `ar.q`, `k`) is for the picture as stored. It's named `cap_<capture>_<nnn>.jpg`, with the usual GPS tags (GPSImgDirection from true north, GPSHPositioningError) and, in
EXIF UserComment, `BMSH1:` followed by JSON in plain ASCII:

    v, app, device, cap, i, t   the format's version; the app and phone; the capture; which photo of it; when (Unix seconds)
    down                  gravity, a unit vector in the stored pixels' axes: x along the rows, y down the columns, z the
                          way the camera looks. down_src: "turn" (the phone's tracking, set right by the capture's turn on
                          the spot), "ar" (the tracking as it was: no turn) or "accel". down_track: the tracking's own, where
                          a turn changed it. down_acc: this photo's accelerometer alone, less what the turn found the phone adds
    turn                  how far clockwise (0, 90, 180, 270) the sensor's picture was turned to store it
    heading, heading_acc  where the camera pointed, degrees from true north, if the compass could be trusted
    gps                   {lat, lng, alt, acc, age}: the last fix, how far off it says it may be (m) and how old (s)
    ar                    {s, p, q, track}: the spell of tracking; the camera's place (m) and turn (w, x, y, z; image axes to
                          world) in its frame, +Y up; "normal" or "limited". s is the app's name for the capture's tracking,
                          with ".2", ".3"… after each time tracking was lost for over a second and came back: its world
                          may then be somewhere else, so places are only comparable within one s, and even there are a
                          strong hint rather than exact (tracking drifts by about a hundredth of the way walked)
    k                     {fx, fy, cx, cy, w, h}: the lens, for this picture's size
    exp                   {t, iso, focus_m}
    q                     {sharp, blur_px, clip_hi, clip_lo, ok}: how it was judged

A photo's blob may also have: `ref: "id1-card"` with `ref_hold` (a bank card, 85.60 by 53.98 mm, lies flat on the rock
beside that hold: for the hold's size, and to be left out of everything else); `still: 1` (a full-size still on a
phone whose other photos are its tracking's frames) with `ar.span: {dt_s, moved_m, turned_deg}` (its pose is between
the tracking's either side of it: how far apart those were), `ar.paused: 1` (tracking stopped for it: the pose is the
last before) and `k_src: "derived"` (its lens was worked out from the tracking camera's: a start, to be refined).

**The record** (`capture.json`, kept whole in the project):

    v, id, app, device, started, ended     v is 3 (1: no plan, level or down_agree_deg; one `ar.s` whatever happened; 2: no detail, licence or credit)
    plan                  {key, made, guide}: the plan the capture went by, and its guide's sha1
    mesh, climbs          the mesh it adds to (none: a rock with no mesh), and the climbs it was kept to, if any
    mode                  "plan" or "orbit"
    fit                   {scale, rot, shift, tilt_deg, taps, matched, rms_m}: the mesh-to-world fit used; tilt_deg is how
                          far off level the mesh turned out to be, and scale its metres a unit
    place                 {lat, lng, alt, acc_m, n, seconds}: its GPS fixes averaged, the phone moving about the rock
    level                 {down, tilt_deg, offset, offset_deg, spread_deg, sectors, samples}: which way is down in the
                          tracking's world, by a turn on the spot; how far the tracking's own was off; what the phone adds to
                          its accelerometer (m/s2, in the axes of the picture as the sensor reads it: before any photo's
                          `turn`) and how many degrees that's worth; and how far the
                          directions faced disagreed, which is how far to trust it. None if the turn was skipped
    down_agree_deg        how far each kept photo's down was from its own accelerometer's, in the middle
    calibration           {mag_offset, mag_field_ut, mag_rms_ut}
    skipped               {target: why}
    detail                {set, holds: [{target, ids, p, r_m, gsd_mm, views, views_of, sides, sides_of, ties, ties_of, card,
                          done}]}: the set of beta it was for, if one was chosen, and what each of its holds got: its finest
                          view, the directions that count of those needed, the sides with a close view (front, left, right,
                          above, below) of those wanted, the tying photos, and whether one has a card beside it. p is in the
                          plan's mesh's coordinates; ids are the holds' own (betasets.json)
    licence, credit       what its photos are given under by whoever took them ("CC-BY-4.0") and who to credit; each
                          photo's blob says the same (lic, by). The server keeps both with the mesh's record of its photos
    progress              {total, done, skipped, holds, holds_done}
    shots                 [{name, blob, kept, why, notes, helped}]: every photo taken, kept or not, with its readings

**The points** (`points.bin`, kept in the project as `capture-points.bin`): what the phone's tracking found of the world,
one to each 5 cm, 20,000 at most to a spell. Noisy to a centimetre or so, except from a lidar.

    "BMP1", uint32 how many
    float32 x, y, z, uint16 spell      for each: where, in the tracking's world (metres, +Y up), and which spell of
                                       tracking found it: 1 the first (photos' `ar.s` with no ".n"), 2 the one after ".2"

## On the server, once a capture is meshed

A capture's mesh is kept beside the mesh it added to (`meshes.adds_to`), never in its place, and tied to it by
`frame.json` in its folder: how the former mesh's coordinates sit in its own. `backend/frames.py` has the format and
the one function that finds it from the cameras two meshes share; the server writes it for a mesh made by meshing
every photo afresh (`how: "cameras"`), and the incremental pipeline (experiments/mapper/incr) for its own (`"add"`,
`"add+relevel"`, `"refit"`), so that either kind is treated alike.

    v, of, how, made, note
    status                "tied", or "untied" with why
    scale, rot, shift     x_this = scale * rot @ x_of + shift   (rot: 3 rows)
    shared, of_shared     the cameras that agreed, of those in common
    rms, rms_m            how far those cameras are from where the tie puts them (this mesh's units; metres if known)
    tilt_deg, rot_deg     the angle between the two meshes' ups; and the whole turn
    carried               what was brought over from the former mesh through it (marked holds, climbs' views)

Which of the tied meshes their climbs open is an editor's choice (`POST /api/meshes/<id>/prefer`, `meshes.preferred`),
the newest until one is made. What took each upload's photos is kept as `projects.device` (and `app`): from the
record for a capture, else from the photos' own EXIF.
