"""Offline parser tests on fixture HTML / nodes.  Run: python3 tests/test_parsers.py"""
import json, sys
import pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "backend"))
from climbs import mountainproject as mp, kaya, openbeta, htmlmini, adapter_for_url
from climbs.common import find_videos, parse_latlng

ROUTE_HTML = """<html><head><title>Climb Midnight Lightning, Yosemite National Park</title>
<meta property="og:title" content="Climb Midnight Lightning, Yosemite National Park">
<meta property="og:image" content="https://mountainproject.com/assets/photos/climb/105857128_medium_1557864286.jpg?cache=1779390649"></head>
<body>
<div class="mb-half small text-warm">
 <a href="/route-guide">All Locations</a> &gt; <a href="/area/105708959/california">California</a> &gt;
 <a href="/area/105833381/yosemite-national-park">Yosemite NP</a> &gt; <a href="/area/105833388/yosemite-valley">Yosemite Valley</a> &gt;
 <a href="/area/107457415/yosemite-valley-bouldering">Yosemite Valle&hellip;</a> &gt; <a href="/area/105833471/camp-4-boulders">Camp 4 Boulders</a> &gt;
 <a href="/area/109861444/big-columbia-boulder">Big Columbia Boulder</a></div>
<h1>Midnight Lightning <a class="small" href="#">Suggest change</a></h1>
<h2 class="inline-block mr-2"><span class="rateHueco">V8</span> <a href="/international">YDS</a> <span class="rateFont">7B</span> <a href="/international">Font</a></h2>
<span id="route-star-avg"><span class="scoreStars"></span> Avg: 3.9 from 117 votes</span>
<table class="description-details"><tr><td>Type:</td><td>Boulder</td></tr><tr><td>GPS:</td><td>37.74367, -119.59863 <a href="http://maps.google.com/maps?q=37.74367,-119.59863">Google Map</a></td></tr>
<tr><td>FA:</td><td>Ron Kauk, 1978</td></tr><tr><td>Page Views:</td><td>87,031 total &middot; 352/month</td></tr><tr><td>Shared By:</td><td><a href="/user/1/peter">Peter Franzen</a> on Jun 21, 2006</td></tr></table>
<h2 class="mt-2">Description <a href="#">Suggest change</a></h2><div class="fr-view"><p>What can you say about such a classic boulder problem?</p><p>First sent by Ron Kauk.</p></div>
<h2 class="mt-2">Location <a href="#">Suggest change</a></h2><div class="fr-view">Right in Camp 4. Look for the chalked lightning bolt on the rock.</div>
<h2 class="mt-2">Protection <a href="#">Suggest change</a></h2><div class="fr-view">Crashpad, spotter</div>
<div class="photos">
 <a href="/photo/105857128/kurt-smith-1985"><img src="https://mountainproject.com/assets/photos/climb/105857128_smallMed_1557864286.jpg" alt="Kurt Smith 1985"></a>
 <a href="/photo/106143224/jerad-friedrichs-on-midnight-lightning"><img data-src="https://mountainproject.com/assets/photos/climb/106143224_smallMed_1559058594.jpg" alt="Jerad Friedrichs"></a>
 <a href="/photo/119694985/one-day">One day</a>
</div>
<div class="comment-body">Nice video: <a href="http://www.youtube.com/watch?v=H2EeJEvxpJI">http://www.youtube.com/watch?v=H2EeJEvxpJI</a> and https://youtu.be/VU473K7m_Ko?si=abc and https://vimeo.com/12345678</div>
</body></html>"""

AREA_HTML = """<html><head><title>Big Columbia Boulder</title></head><body>
<div class="mb-half small text-warm"><a href="/route-guide">All Locations</a> &gt; <a href="/area/105708959/california">California</a> &gt; <a href="/area/105833471/camp-4-boulders">Camp 4 Boulders</a></div>
<h1>Big Columbia Boulder <a href="#">Suggest change</a></h1>
<table class="description-details"><tr><td>Elevation:</td><td>3,998 ft 1,219 m</td></tr><tr><td>GPS:</td><td>37.74172, -119.60355</td></tr></table>
<h2>Description <a>Suggest change</a></h2><div class="fr-view">Hosts the classic <a href="/route/105835104/midnight-lightning">Midnight Lightning</a>.</div>
<h2>Getting There <a>Suggest change</a></h2><div class="fr-view">Right in Camp 4.</div>
<table id="left-nav-route-table"><tr><td><a href="/route/105835104/midnight-lightning">Midnight Lightning</a></td><td><span class="rateHueco">V8</span> <span class="rateFont">7B</span></td></tr>
<tr><td><a href="/route/108080257/after-midnight">After Midnight</a></td><td><span class="rateHueco">V10</span></td></tr></table>
<div class="lef-nav-row"><a href="/area/200000001/sub-block">Sub Block</a></div>
<a href="/map/109861444/big-columbia-boulder">Climbing Area Map</a>
</body></html>"""

r = mp.parse_route(ROUTE_HTML, "https://www.mountainproject.com/route/105835104/midnight-lightning")
d = r.to_dict()
assert d["name"] == "Midnight Lightning", d["name"]
assert d["grade"] == "V8 7B", d["grade"]
assert d["kind"] == "boulder"
assert (d["lat"], d["lng"]) == (37.74367, -119.59863), (d["lat"], d["lng"])
assert d["fa"] == "Ron Kauk, 1978"
assert d["rating"] == 3.9 and d["votes"] == 117
assert d["area_path"][-1] == "Big Columbia Boulder" and d["area_path"][0] == "California", d["area_path"]
assert d["boulder_name"] == "Big Columbia Boulder"
assert d["description"].startswith("What can you say"), d["description"]
assert d["protection"] == "Crashpad, spotter"
imgs = {m["url"] for m in d["images"]}
assert "https://www.mountainproject.com/assets/photos/climb/105857128_medium_1557864286.jpg" in imgs, imgs
assert "https://www.mountainproject.com/assets/photos/climb/106143224_medium_1559058594.jpg" in imgs, imgs
assert any(m["page"].endswith("/photo/119694985/one-day") and not m["url"] for m in d["images"])
vids = [m["url"] for m in d["videos"]]
assert vids == ["https://www.youtube.com/watch?v=H2EeJEvxpJI", "https://www.youtube.com/watch?v=VU473K7m_Ko", "https://vimeo.com/12345678"], vids
print("route OK:", json.dumps({k: d[k] for k in ("name","grade","lat","lng","fa","boulder_name","area_path")}))

a, routes, children = mp.parse_area(AREA_HTML, "https://www.mountainproject.com/area/109861444/big-columbia-boulder")
assert a.name == "Big Columbia Boulder" and a.kind == "area"
assert (a.lat, a.lng) == (37.74172, -119.60355) and a.elevation_m == 1219.0, (a.lat, a.lng, a.elevation_m)
assert a.location == "Right in Camp 4."
assert routes[0][1] == "Midnight Lightning" and routes[0][2] == "V8 7B", routes
assert routes[1][2] == "V10"
assert children == [("https://www.mountainproject.com/area/200000001/sub-block", "Sub Block")], children
print("area OK:", a.name, routes, children)

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
print("openbeta OK")
assert adapter_for_url("https://openbeta.io/climbs/x") is openbeta and adapter_for_url("https://mountainproject.com/route/1/x") is mp
print("ALL PARSER TESTS PASSED")
