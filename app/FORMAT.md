# What passes between the phone apps and the server

Everything a phone learns goes in the photos themselves and in one record of the capture, so that a photo is still
worth its readings wherever it ends up. The server's side is `backend/captureplan.py`, `backend/phonedata.py` and
`capture_plan()`, `merge_capture()` in `backend/server.py`; the apps' is `shared/` (Plan.kt, GuideMesh.kt, Manifest.kt).
Each side's tests read files the other really wrote (`shared/src/jvmTest/resources`).

## From the server

    GET /api/capture/meshes               [{id, name, lat, lng, climbs: ["name grade"], holds}]: current meshes a capture can add to
    GET /api/capture/plan/<mesh>          the plan below
    GET /api/capture/guide/<mesh>.bin     the guide below, at the plan's guide_url

**The plan** (JSON). Lengths are the mesh's own units unless they say metres; +Y is up.

    v, made, key          the format's version; when it was made; what it was made from
    mesh, name, lat, lng, heading_deg     the mesh, and where it is and faces as far as the server knows
    frame                 {up, ground_y, size, height, metres_per_unit, scale_guessed}: scale_guessed if its size isn't known
    guide                 {vertices, faces, patches, sha1} of the guide
    need                  {hold: {gsd_mm, views}, patch: {gsd_mm, views}, edge: {views}}: what makes each kind of target done
    view_deg, facing_deg  views closer together than view_deg are one; a target more than facing_deg off square isn't seen
    cameras               [{c, d}]: where its photos were taken, and which way they looked
    targets               [{id, kind, p, n, r, prio, role?, from?, out?, have_gsd, have_views, climbs}]
                          kind: hold (id h<i>), patch (p<k>, the guide's patch k) or edge (e<i>, with out: the way past it)
                          prio: 3 a hold, 2 rock on a climb, 1 other rock, 0 the ground
                          have_gsd (units a pixel) and have_views: what its photos so far gave it
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
    POST /api/projects/<id>/reconstruct               mesh it: with the photos of the mesh the record names, if it names one

**Each photo** is a JPEG with its pixels as the sensor read them (EXIF Orientation says how to show it upright), named
`cap_<capture>_<nnn>.jpg`, with the usual GPS tags (GPSImgDirection from true north, GPSHPositioningError) and, in
EXIF UserComment, `BMSH1:` followed by JSON in plain ASCII:

    v, app, device, cap, i, t   the format's version; the app and phone; the capture; which photo of it; when (Unix seconds)
    down                  gravity, a unit vector in the stored pixels' axes: x along the rows, y down the columns, z the
                          way the camera looks. down_src: "ar" (the phone's tracking) or "accel"; down_acc: the accelerometer's own
    heading, heading_acc  where the camera pointed, degrees from true north, if the compass could be trusted
    gps                   {lat, lng, alt, acc, age}: the last fix, how far off it says it may be (m) and how old (s)
    ar                    {s, p, q, track}: the tracking session; the camera's place (m) and turn (w, x, y, z; image axes to
                          world) in its frame, +Y up; "normal" or "limited"
    k                     {fx, fy, cx, cy, w, h}: the lens, for this picture's size
    exp                   {t, iso, focus_m}
    q                     {sharp, blur_px, clip_hi, clip_lo, ok}: how it was judged

**The record** (`capture.json`, kept whole in the project):

    v, id, app, device, started, ended
    mesh, climbs          the mesh it adds to (none: a rock with no mesh), and the climbs it was kept to, if any
    mode                  "plan" or "orbit"
    fit                   {scale, rot, shift, tilt_deg, taps, matched, rms_m}: the mesh-to-world fit used; tilt_deg is how
                          far off level the mesh turned out to be, and scale its metres a unit
    place                 {lat, lng, alt, acc_m, n, seconds}: its GPS fixes averaged, the phone moving about the rock
    calibration           {accel_bias, mag_offset, mag_field_ut, mag_rms_ut}
    skipped               {target: why}
    progress              {total, done, skipped, holds, holds_done}
    shots                 [{name, blob, kept, why, notes, helped}]: every photo taken, kept or not, with its readings
