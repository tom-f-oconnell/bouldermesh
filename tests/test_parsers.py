"""Offline parser tests on fixture HTML / nodes.  Run: python3 tests/test_parsers.py"""
import io, json, os, sys, tempfile, time, urllib.error, urllib.request
import pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "backend"))
from climbs import common, harvest, mountainproject as mp, kaya, openbeta, htmlmini, adapter_for_url
from climbs.common import ApiError, find_videos, http, parse_latlng

# Trimmed from the live pages (Oct 2026), keeping the structures the parser depends on.
LD_CRUMBS = """<script type="application/ld+json">
{"@context": "http://schema.org", "@type": "BreadcrumbList", "itemListElement": [
 {"@type": "ListItem", "position": 1, "item": "https://www.mountainproject.com/route-guide", "name": "All Locations"},
 {"@type": "ListItem", "position": 2, "item": "https://www.mountainproject.com/area/105708959/california", "name": "California"},
 {"@type": "ListItem", "position": 3, "item": "https://www.mountainproject.com/area/107457415/yosemite-valley-bouldering", "name": "* Yosemite Valley Bouldering"},
 {"@type": "ListItem", "position": 4, "item": "https://www.mountainproject.com/area/105833471/camp-4-boulders", "name": "Camp 4 Boulders"}%s]}
</script>"""
MP = "https://www.mountainproject.com"
ASSET = "https://mountainproject.com/assets/photos/climb"

ROUTE_HTML = f"""<html><head><title>Climb Midnight Lightning, Yosemite National Park</title>
<meta property="og:image" content="{ASSET}/105857128_medium_1557864286.jpg?cache=1779390649"/></head>
<body>
<div class="mb-half small text-warm">
 <a href="{MP}/route-guide">All Locations</a> &gt; <a href="{MP}/area/105708959/california">California</a>
 &gt; <a href="{MP}/area/107457415/yosemite-valley-bouldering">Yosemite Valle&hellip;</a>
 &gt; <a href="{MP}/area/105833471/camp-4-boulders">Camp 4 Boulders</a>
 &gt; <a href="{MP}/area/109861444/big-columbia-boulder">Big Columbia Boulder</a>
</div>
<h1>
    Midnight Lightning
    <!--EDIT-Route-105835104--><a href="#" data-toggle="modal"><img src='/img/icons/edit_climb.svg' /></a>
</h1>
<h2 class="inline-block mr-2"><span class='rateYDS'>V8 <a href="{MP}/international-climbing-grades" class="font-body"><span class="small">YDS</span></a></span> <span class='rateFont'>7B <a href="{MP}/international-climbing-grades" class="font-body"><span class="small">Font</span></a></span></h2>
<span id="route-star-avg"><span class="scoreStars"></span>
    &nbsp;Avg: 3.9 from 117
 votes</span>
<div id="carousel-item-0" class="carousel-item" style="background-image: url('{ASSET}/105857128_medium_1557864286.jpg?cache=1779390649')">
  <a href="{MP}/photo/105857128/kurt-smith-1985" class="photo-link" onclick="return photoClicked(105857128)"></a>
</div>
<table class="description-details"><tr><td>Type:</td><td>
    Boulder
 </td></tr><tr><td>GPS:</td><td>
    37.74367, -119.59863
 </td></tr>
<tr><td>FA:</td><td>Ron Kauk, 1978</td></tr><tr><td class="pr-half text-nowrap">Page Views:</td><td>87,057 total &middot; 352/month</td></tr>
<tr><td class="pr-half text-nowrap">Shared By:</td><td><a href="{MP}/user/10116/peter-franzen">Peter Franzen</a> on Jun 21, 2006</td></tr></table>
<h2 class="mt-2">Description <a href="#">Suggest change</a></h2><div class="fr-view"><p>What can you say about such a classic boulder problem?</p><p>First sent by Ron Kauk.</p></div>
<h2 class="mt-2">Location <a href="#">Suggest change</a></h2><div class="fr-view">Right in Camp 4. Look for the chalked lightning bolt on the rock.</div>
<h2 class="mt-2">Protection <a href="#">Suggest change</a></h2><div class="fr-view">Crashpad, spotter</div>
<div class="col-xs-4 col-lg-3 card-with-photo">
 <a class="card-with-photo photo-card" href="{MP}/photo/105857128/kurt-smith-1985" onclick='return photoClicked(105857128);'>
  <div class="img-container position-relative"><img  class="lazy" data-src='{ASSET}/105857128_smallMed_1557864286.jpg?cache=1779390649' alt="Kurt Smith, 1985.<br>
 No pads or spotters!"></div></a>
 <div class="card-text small mb-2"><a class="card-with-photo photo-card" href="{MP}/photo/105857128/kurt-smith-1985"><div class="title-row text-truncate">Kurt Smith, 1985.</div></a></div>
</div>
<div class="col-xs-4 col-lg-3 card-with-photo">
 <a class="card-with-photo photo-card" href="{MP}/photo/106143224/jerad-friedrichs-on-midnight-lightning">
  <div class="img-container position-relative"><img  class="lazy" data-src='{ASSET}/106143224_smallMed_1559058594.jpg?cache=1' alt="Jerad Friedrichs"></div></a>
</div>
<a href="{MP}/photo/119694985/one-day">One day</a>
<div class="col-xs-6 col-sm-4 col-lg-3 video-tile" data-comment-id="107322305">
 <a href="#" class="video-thumb-link" data-toggle="modal" data-video-embed="https://www.youtube-nocookie.com/embed/H2EeJEvxpJI">
  <img class="video-thumb lazy" data-src="https://i.ytimg.com/vi/H2EeJEvxpJI/hqdefault.jpg"></a>
</div>
<div class="comment-body">Nice video: <a href="http://www.youtube.com/watch?v=H2EeJEvxpJI">http://www.youtube.com/watch?v=H2EeJEvxpJI</a> and https://youtu.be/VU473K7m_Ko?si=abc and https://vimeo.com/12345678</div>
{LD_CRUMBS % ', {"@type": "ListItem", "position": 5, "item": "https://www.mountainproject.com/area/109861444/big-columbia-boulder", "name": "Big Columbia Boulder"}'}
</body></html>"""

AREA_HTML = f"""<html><head><title>Big Columbia Boulder</title></head><body>
<div class="mb-half small text-warm"><a href="{MP}/route-guide">All Locations</a> &gt; <a href="{MP}/area/105708959/california">California</a>
 &gt; <a href="{MP}/area/107457415/yosemite-valley-bouldering">Yosemite Valle&hellip;</a> &gt; <a href="{MP}/area/105833471/camp-4-boulders">Camp 4 Boulders</a></div>
<h1>
    Big Columbia Boulder
            <span class="hidden-md-down">Climbing</span>
            <!--EDIT-Area-109861444-->
</h1>
<table class="description-details"><tr><td class="text-nowrap">Elevation:</td><td class="imperial">3,998 ft</td><td class="metric">1,219 m</td></tr>
<tr><td>GPS:</td><td>37.74172, -119.60355 <a href="https://maps.google.com/maps?q=37.74172,-119.60355">Google Map</a></td></tr></table>
<h2>Description <a>Suggest change</a></h2><div class="fr-view">Hosts the classic <a href="{MP}/route/105835104/midnight-lightning">Midnight Lightning</a>.</div>
<h2>Getting There <a>Suggest change</a></h2><div class="fr-view">Right in Camp 4.</div>
<table id="left-nav-route-table">
<tr><td><a href="{MP}/route/105835104/midnight-lightning">Midnight Lightning</a>
 <span class="route-type Boulder"><span class='rateYDS'>V8</span> <span class='rateFont'>7B</span></span></td></tr>
<tr><td><a href="{MP}/route/113075666/the-moffat-start">Moffat Start, The</a>
 <span class="route-type Boulder"><span class='rateYDS'>V10</span> <span class='rateFont'>7C+</span></span></td></tr></table>
<div class="lef-nav-row"><a href="{MP}/area/200000001/sub-block">Sub Block</a></div>
<a href="{MP}/map/109861444/big-columbia-boulder">Climbing Area Map</a>
<a class="card-with-photo photo-card" href="{MP}/photo/116495404/jerad-friedrichs-on-midnight-lightning">
 <div class="img-container"><img class="lazy" data-src='{ASSET}/106143224_smallMed_1559058594.jpg?cache=1' alt="Jerad Friedrichs on Midnight Lightning"></div></a>
<a href="{MP}/book/120346135"><img class="img-fluid lazy" data-src='{ASSET}/120346735_small_1614820219.jpg?cache=1'></a>
<form><table><tr><td>Type:</td><td>Rock Boulder Aid</td></tr><tr><td>Elevation:</td><td>Any</td></tr></table></form>
{LD_CRUMBS % ""}
</body></html>"""

r = mp.parse_route(ROUTE_HTML, "https://www.mountainproject.com/route/105835104/midnight-lightning")
d = r.to_dict()
assert d["name"] == "Midnight Lightning", d["name"]
assert d["grade"] == "V8 7B" and d["grades"] == {"vscale": "V8", "font": "7B"}, (d["grade"], d["grades"])
assert d["kind"] == "boulder"
assert (d["lat"], d["lng"]) == (37.74367, -119.59863), (d["lat"], d["lng"])
assert d["fa"] == "Ron Kauk, 1978"
assert d["rating"] == 3.9 and d["votes"] == 117
assert d["area_path"] == ["California", "* Yosemite Valley Bouldering", "Camp 4 Boulders", "Big Columbia Boulder"], d["area_path"]
assert d["boulder_name"] == "Big Columbia Boulder"
assert d["description"].startswith("What can you say"), d["description"]
assert d["protection"] == "Crashpad, spotter"
imgs = {m["url"]: m for m in d["images"]}
assert len(d["images"]) == 3, d["images"]
k1 = imgs["https://www.mountainproject.com/assets/photos/climb/105857128_medium_1557864286.jpg"]
assert k1["title"] == "Kurt Smith, 1985. No pads or spotters!" and k1["page"].endswith("/photo/105857128/kurt-smith-1985"), k1
assert "https://www.mountainproject.com/assets/photos/climb/106143224_medium_1559058594.jpg" in imgs, imgs
assert any(m["page"].endswith("/photo/119694985/one-day") and not m["url"] for m in d["images"])
vids = [m["url"] for m in d["videos"]]
assert vids == ["https://www.youtube.com/watch?v=H2EeJEvxpJI", "https://www.youtube.com/watch?v=VU473K7m_Ko", "https://vimeo.com/12345678"], vids
print("route OK:", json.dumps({k: d[k] for k in ("name","grade","lat","lng","fa","boulder_name","area_path")}))

a, routes, children = mp.parse_area(AREA_HTML, "https://www.mountainproject.com/area/109861444/big-columbia-boulder")
assert a.name == "Big Columbia Boulder" and a.kind == "area", a.name
assert (a.lat, a.lng) == (37.74172, -119.60355) and a.elevation_m == 1219.0, (a.lat, a.lng, a.elevation_m)
assert a.location == "Right in Camp 4."
assert a.area_path == ["California", "* Yosemite Valley Bouldering", "Camp 4 Boulders", "Big Columbia Boulder"], a.area_path
assert routes == [("https://www.mountainproject.com/route/105835104/midnight-lightning", "Midnight Lightning", "V8 7B"),
                  ("https://www.mountainproject.com/route/113075666/the-moffat-start", "The Moffat Start", "V10 7C+")], routes
assert children == [("https://www.mountainproject.com/area/200000001/sub-block", "Sub Block")], children
# photo page id != image file id on area pages; the guidebook cover isn't a photo
assert [(m.url, m.page) for m in a.images] == [("https://www.mountainproject.com/assets/photos/climb/106143224_medium_1559058594.jpg",
                                                "https://www.mountainproject.com/photo/116495404/jerad-friedrichs-on-midnight-lightning")], a.images
s = mp._stub(a, *routes[1])
assert (s.name, s.kind, s.grade, s.lat, s.coord_source, s.boulder_name, s.elevation_m, s.raw) == \
    ("The Moffat Start", "boulder", "V10 7C+", 37.74172, "parent-area", "Big Columbia Boulder", 1219.0, {"stub": True}), s
print("area OK:", a.name, routes, children)

# trimmed from the live /route-finder-export response; Location is bottom-up
CSV = """Route,Location,URL,"Avg Stars","Your Stars","Route Type",Rating,Pitches,Length,"Area Latitude","Area Longitude"
"Quinn's Slab","The Glass Pyramid > Camp 4 Boulders > * Yosemite Valley Bouldering > California",https://www.mountainproject.com/route/124539964/quinns-slab,2.1,-1,Boulder,"V0+ R",1,15,37.7426,-119.60284
"Crack Thing","Finger Crack Boulder > Camp 4 Boulders > California",https://www.mountainproject.com/route/1/crack-thing,-1,-1,"Trad, Boulder","5.10a/b V1",1,,37.74,-119.6
"""
mp.http.get_text = lambda url, **kw: CSV
q, ct = mp.export_area_csv(105833471)
del mp.http.get_text
assert (q.name, q.source_id, q.kind, q.grade, q.grades, q.rating, q.lat, q.coord_source) == \
    ("Quinn's Slab", "124539964", "boulder", "V0+ R", {"vscale": "V0+"}, 2.1, 37.7426, "area"), q
assert q.area_path == ["California", "* Yosemite Valley Bouldering", "Camp 4 Boulders", "The Glass Pyramid"], q.area_path
assert q.boulder_name == "The Glass Pyramid", q.boulder_name
assert (ct.grades, ct.rating, ct.boulder_name) == ({"vscale": "V1"}, None, "Finger Crack Boulder"), ct
print("csv OK:", q.name, q.boulder_name)

k = kaya.fetch("https://kaya-app.kayaclimb.com/location/Index-345070")[0]
assert k.name == "Index" and k.kind == "area" and k.source == "kaya"
ex = kaya.from_export([{"Climb": "The Egg", "Grade": "V11", "Area": "Index", "Latitude": "47.82", "Longitude": "-121.55", "Beta": "https://youtu.be/abcdefghijk"}])
assert ex[0].lat == 47.82 and ex[0].videos[0].url == "https://www.youtube.com/watch?v=abcdefghijk", ex[0].to_dict()
print("kaya OK")

node = {"uuid": "a1", "name": "Midnight Lightning", "fa": "Ron Kauk", "grades": {"vscale": "V8", "font": "7B"},
        "type": {"bouldering": True}, "metadata": {"lat": None, "lng": None, "mp_id": "105835104"},
        "content": {"description": "classic https://youtu.be/H2EeJEvxpJI"}, "pathTokens": ["USA", "California", "Camp 4", "Big Columbia Boulder", "Midnight Lightning"],
        "media": [{"mediaUrl": "/u/x/y.jpg", "width": 100, "height": 50, "username": "bob"}]}
area = {"uuid": "b2", "area_name": "Big Columbia Boulder", "pathTokens": ["USA", "California", "Camp 4", "Big Columbia Boulder"], "metadata": {"lat": 37.7, "lng": -119.6, "isBoulder": True, "mp_id": "109861444"}}
c = openbeta.climb_from_node(node, area)
assert c.grade == "V8 7B" and c.coord_source == "parent-area" and c.boulder_name == "Big Columbia Boulder" and c.cross_refs["mp_id"] == "105835104"
assert c.images[0].url == "https://media.openbeta.io/u/x/y.jpg" and c.videos[0].url.endswith("H2EeJEvxpJI")
assert c.area_path == ["USA", "California", "Camp 4", "Big Columbia Boulder"]
# as served live: V-grade repeated in yds, the area's coords copied onto the climb, "unknown" FA
c = openbeta.climb_from_node({**node, "fa": "unknown", "grades": {"vscale": "V10", "font": "7C+", "yds": "V10"},
                              "metadata": {"lat": 37.7, "lng": -119.6}}, area)
assert c.grade == "V10 7C+" and c.coord_source == "parent-area" and c.fa is None, (c.grade, c.coord_source, c.fa)
print("openbeta OK")
assert adapter_for_url("https://openbeta.io/climbs/x") is openbeta and adapter_for_url("https://mountainproject.com/route/1/x") is mp

# APIs that report failures in a 200 body: surfaced, retried (Commons), never left in the cache
BUSY = b'{"error": {"code": "cirrussearch-too-busy-error", "info": "Search is currently too busy."}}'
COMMONS = json.dumps({"query": {"pages": {"1": {"title": "File:A.jpg", "imageinfo": [
    {"url": "https://u/A.jpg?utm_source=commons.wikimedia.org&utm_content=original", "thumburl": "https://u/2048px-A.jpg", "thumbwidth": 2048, "thumbheight": 1536, "descriptionurl": "https://c/File:A.jpg",
     "extmetadata": {"Artist": {"value": "<a href='x'>Ann</a>"}, "LicenseShortName": {"value": "CC BY 4.0"}}}]}}}}).encode()
with tempfile.TemporaryDirectory() as tmp:
    common.CACHE_DIR = pathlib.Path(tmp)
    cp = http._cache_path("https://u/api")
    cp.parent.mkdir(parents=True)
    cp.write_bytes(BUSY)
    try:
        http.get_json("https://u/api", error=lambda r: (r.get("error") or {}).get("info"))
        assert False, "no ApiError"
    except ApiError as e:
        assert "too busy" in str(e) and not cp.exists(), (e, cp.exists())

logged, sleep = [], time.sleep
time.sleep = lambda s: None
responses = [BUSY, COMMONS]
http.get = lambda url, **kw: responses.pop(0)
imgs = harvest.commons_geosearch(37.7, -119.6, logged.append)
assert [(m.url, m.credit, m.license) for m in imgs] == [("https://u/2048px-A.jpg", "Ann", "CC BY 4.0")] and not responses, imgs
responses = [BUSY] * 3
assert harvest.commons_geosearch(37.7, -119.6, logged.append) == [] and "commons: failed: Search is currently too busy." in logged, logged
os.environ["FLICKR_KEY"] = "secret"
http.get = lambda url, **kw: b'{"stat": "fail", "code": 100, "message": "Invalid API Key"}'
assert harvest.flickr_search(37.7, -119.6, [], logged.append) == [] and "flickr: failed: Invalid API Key" in logged, logged
del os.environ["FLICKR_KEY"], http.get

# DuckDuckGo is a default source, HARVEST_DDG=0 keeps it off the network, and a failure only costs its photos
assert "ddg" in harvest.SOURCES and "HARVEST_DDG" not in os.environ and harvest.ddg_enabled()
http.get_text = lambda url, **kw: "vqd='4-123'"
http.get_json = lambda url, **kw: {"results": [{"image": "https://u/a.jpg", "url": "https://u/a", "title": "A"}]}
imgs = harvest.ddg_images(["q"], logged.append)
assert [(m.url, m.page, m.source) for m in imgs] == [("https://u/a.jpg", "https://u/a", "ddg")], imgs
http.get_text = lambda url, **kw: "the endpoint changed"
assert harvest.ddg_images(["q"], logged.append) == [] and any("no vqd token" in x for x in logged), logged
http.get_text = lambda url, **kw: 1 / 0
assert harvest.ddg_images(["q"], logged.append) == [] and any(x.startswith("ddg: 'q' failed") for x in logged), logged
os.environ["HARVEST_DDG"] = "0"
fetched = []
http.get_text = lambda url, **kw: fetched.append(url)
assert not harvest.ddg_enabled() and harvest.ddg_images(["q"], logged.append) == [] and not fetched, fetched
assert "ddg: skipped (HARVEST_DDG=0)" in logged, logged
del os.environ["HARVEST_DDG"], http.get_text, http.get_json

# Route Finder export: one request, asks for popularity order, and keeps it
EXPORT = ('Route,Location,URL,"Avg Stars","Your Stars","Route Type",Rating,Pitches,Length,"Area Latitude","Area Longitude"\n'
          'Second,"Slash Boulder > Western Belt > * JT Bouldering > Joshua Tree National Park > California",'
          'https://www.mountainproject.com/route/2/second,3.9,-1,Boulder,"V3 R",1,15,34.0,-116.1\n'
          'First,"Pig Boulder > Manx > * JT Bouldering > Joshua Tree National Park > California",'
          'https://www.mountainproject.com/route/1/first,3.1,-1,"Trad, Boulder",V4,,,34.1,-116.2\n')
urls = []
http.get_text = lambda url, **kw: urls.append(url) or EXPORT
recs = mp.export_area_csv("105720495", sort="popularity")
assert len(urls) == 1 and "selectedIds=105720495" in urls[0] and "sort1=popularity+desc" in urls[0], urls
assert [(c.name, c.source_id, c.grade, c.rating, c.raw["stub"]) for c in recs] == [("Second", "2", "V3 R", 3.9, True), ("First", "1", "V4", 3.1, True)], recs
assert recs[0].area_path[-2:] == ["Western Belt", "Slash Boulder"] and recs[0].area_path[0] == "California", recs[0].area_path
assert "sort1=area" in (mp.export_area_csv(1) and urls[-1]), urls
del http.get_text
assert mp.area_id("https://www.mountainproject.com/area/105720495/joshua-tree-national-park") == "105720495"
assert mp.area_id(" 105720495 ") == "105720495"
assert mp.area_id("https://openbeta.io/area/123") is None and mp.area_id("Joshua Tree") is None

# a 429 is retried once, after its Retry-After
http._robots["u"], slept = None, []
time.sleep = slept.append
replies = [urllib.error.HTTPError("https://u/x", 429, "Too Many Requests", {"Retry-After": "34"}, None), io.BytesIO(b"ok")]
urlopen, urllib.request.urlopen = urllib.request.urlopen, lambda req, timeout=0: (
    replies.pop(0) if isinstance(replies[0], io.BytesIO) else (_ for _ in ()).throw(replies.pop(0)))
assert http.get("https://u/x", use_cache=False) == b"ok" and 34 in slept, slept
urllib.request.urlopen = urlopen
time.sleep = sleep
print("api errors OK")
print("ALL PARSER TESTS PASSED")
