package sh.boulderme.core

/**
 * The terms the apps show before anything else, and won't go on without: the server's own (backend/accounts.py TERMS,
 * which tests/test_accounts.py checks this against), kept here so that they can be read and accepted with no signal.
 * An account's acceptance is sent when the phone next uploads.
 */
const val TERMS_VERSION = 1
const val TERMS = "BoulderMesh is a community project: using it to capture or add to a rock means sharing what you add.\n\n" +
    "Everything you capture with the app or upload to the site (photos, videos, the readings stored with them, and beta you choose to share), and whatever is made from it alone (meshes, textures, models of holds), is published under the Creative Commons Attribution 4.0 International licence (CC BY 4.0): the site and anyone else may copy, change and reuse it, for any purpose, as long as they credit you by the name you give.\n\n" +
    "Only add what is yours to license this way. What you add is reviewed before it is shown, and may be declined or removed. Meshes that also draw on other people's photos stay under those photos' own terms."
