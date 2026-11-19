package sh.boulderme.core

import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertNotNull
import kotlin.test.assertNull
import kotlin.test.assertTrue

/** The viewer kept on the phone: what each file goes by, and keeping a mesh's. */
class OfflineTest {
    private val server = "https://boulderme.sh"

    @Test fun whatAFileGoesBy() {
        assertEquals("/meshes/ab/mesh.glb?lod=preview&v=1", Offline.key("https://boulderme.sh/meshes/ab/mesh.glb?v=1&lod=preview", server), "its query in order")
        assertEquals("/meshes/ab/mesh.glb?lod=preview&v=1", Offline.key("/meshes/ab/mesh.glb?lod=preview&v=1", server))
        assertEquals("/meshes/ab/mesh 0.jpg?max=2048", Offline.key("/meshes/ab/mesh%200.jpg?max=2048", server), "decoded")
        assertEquals("/m/ab", Offline.key("https://boulderme.sh/m/ab?notex=1#x", server), "the page, by its path alone")
        assertEquals("/m/ab", Offline.key("bmsh://offline/m/ab?notex=1", server), "under iOS's own scheme")
        assertEquals("https://cdn.jsdelivr.net/npm/three@0.170.0/build/three.module.js",
            Offline.key("bmsh://offline/cdn/cdn.jsdelivr.net/npm/three@0.170.0/build/three.module.js", server), "a CDN's file, made the page's own on iOS")
        assertEquals("https://tile.openstreetmap.org/1/2/3.png", Offline.key("https://tile.openstreetmap.org/1/2/3.png", server))
        val files = mapOf("/api/climbs?limit=100&offset=0" to KeptFile("a"), "/api/status" to KeptFile("b"))
        assertEquals("a", Offline.find(files, "/api/climbs?limit=200&offset=0")?.file, "the API's list, however long it's asked for")
        assertEquals("b", Offline.find(files, "/api/status?x=1")?.file)
        assertNull(Offline.find(files, "/meshes/ab/other.glb"))
        assertTrue("src=\"/cdn/cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.js\"" in
            Offline.ownCdns("<script src=\"https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.js\">"))
        assertEquals("12 MB", Offline.size(12_300_000)); assertEquals("1.5 MB", Offline.size(1_480_000)); assertEquals("850 KB", Offline.size(850_000))
    }

    @Test fun whatALibraryImports() {
        val url = "https://cdn.jsdelivr.net/npm/three@0.170.0/examples/jsm/loaders/GLTFLoader.js"
        val js = "import { Mesh } from 'three';\nimport { toTrianglesDrawMode } from '../utils/BufferGeometryUtils.js';\nconst m = import(\"./x/y.js\");\n// import('https://my-cdn.example/model.bin')"
        assertEquals(listOf("https://cdn.jsdelivr.net/npm/three@0.170.0/examples/jsm/utils/BufferGeometryUtils.js",
            "https://cdn.jsdelivr.net/npm/three@0.170.0/examples/jsm/loaders/x/y.js"), Offline.imports(url, js), "bare names are the import map's")
    }

    @Test fun keepingAMesh() {
        val bundle = OfflineBundle("ab", "v1", "/m/ab", listOf(
            OfflineFile("/m/ab"), OfflineFile("https://cdn.example/lib/a.js", lib = true), OfflineFile("/api/meshes/ab/cleanup"),
            OfflineFile("/meshes/ab/mesh.glb?lod=preview&v=v1", 1000), OfflineFile("/meshes/ab/t.jpg?max=2048", 5000, texture = true)),
            OfflineSizes(1000, 6000))
        val asked = ArrayList<String>()
        val answers = mapOf(
            "https://boulderme.sh/m/ab" to Triple(200, "text/html", "<html>"),
            "https://cdn.example/lib/a.js" to Triple(200, "text/javascript", "import x from './b.js'"),
            "https://cdn.example/lib/b.js" to Triple(200, "text/javascript", "export default 1"),
            "https://boulderme.sh/api/meshes/ab/cleanup" to Triple(404, "application/json", "{}"),
            "https://boulderme.sh/meshes/ab/mesh.glb?lod=preview&v=v1" to Triple(200, "model/gltf-binary", "glb"),
            "https://boulderme.sh/meshes/ab/t.jpg?max=2048" to Triple(200, "image/jpeg", "jpg"))
        val libs = HashMap<String, Pair<KeptFile, ByteArray?>>()
        val said = ArrayList<Double>()
        fun run(textures: Boolean, fail: String? = null): Pair<OfflineIndex?, String?> {
            var out: Pair<OfflineIndex?, String?>? = null
            said.clear()
            OfflineFetch(bundle, textures, server,
                fetch = { url, got, done ->
                    asked.add(url); val a = answers[url]!!
                    if (url == fail) done(0, null, null, "no signal") else { got(400); got(800); done(a.first, a.second, a.third.encodeToByteArray(), null) }
                },
                lib = { libs[it] },
                keep = { key, lib, status, type, body -> KeptFile(if (lib) "lib-${libs.size}" else "f${key.length}", status, type).also { if (lib) libs[key] = it to body } },
                now = 1.0, done = { i, why -> out = i to why }, progress = { said.add(it) }).start()
            return assertNotNull(out)
        }
        val (plain, why) = run(false)
        assertNull(why)
        // by the sized files' bytes: the mesh's 1000 is all of it without textures, and comes 400 and 800 at a time
        assertEquals(listOf(0.0, 0.4, 0.8, 1.0), said.map { kotlin.math.round(it * 100) / 100 }, "how far it's got")
        assertTrue(said.zipWithNext().all { (a, b) -> b > a }, "only ever growing")
        assertTrue(plain!!.files.keys.none { "t.jpg" in it } && !plain.textures, "without its textures")
        assertTrue("https://cdn.example/lib/b.js" in plain.files && plain.files["/api/meshes/ab/cleanup"]!!.status == 404, "what the library imports; the API as it answered")
        asked.clear()
        val (textured, _) = run(true)
        assertEquals(1.0, said.last())
        fun near(x: Double) = said.any { kotlin.math.abs(it - x) < 0.006 }
        assertTrue(near(1000.0 / 6000) && near(1800.0 / 6000), "the textures' bytes count too: $said")
        assertTrue(textured!!.textures && "/meshes/ab/t.jpg?max=2048" in textured.files)
        assertTrue(asked.none { "cdn.example" in it }, "libraries kept already aren't fetched again: $asked")
        assertTrue(run(true, "https://boulderme.sh/meshes/ab/mesh.glb?lod=preview&v=v1").second!!.contains("no signal"), "the mesh has to come")
    }

    @Test fun saysWhetherAKeptMeshIsCurrent() {
        val got = Plan.json.decodeFromString(kotlinx.serialization.builtins.MapSerializer(kotlinx.serialization.serializer<String>(), MeshFresh.serializer()),
            """{"a": {"key": "k1"}, "b": {"key": "k2"}, "c": {"newer": "c2", "newer_name": "Wall"}, "d": {"gone": true}}""")
        assertNull(got["a"]!!.said("k1")); assertTrue(!got["a"]!!.stale("k1"), "as the phone has it")
        assertTrue(got["b"]!!.stale("k1") && got["b"]!!.said("k1")!!.startsWith("Changed") && got["b"]!!.action == "Update", "its holds or beta changed")
        assertTrue(got["c"]!!.stale("k1") && got["c"]!!.action == "Get the newer mesh", "rebuilt")
        assertTrue(!got["d"]!!.stale("k1") && got["d"]!!.said("k1")!!.startsWith("No longer"), "gone: nothing to get, the copy kept")
        assertTrue(MeshFresh.mayDrop("old", listOf("other", null)) && !MeshFresh.mayDrop("old", listOf("old")), "kept while a capture to send goes by it")
    }
}
