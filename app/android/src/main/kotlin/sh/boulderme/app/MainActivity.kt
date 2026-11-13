package sh.boulderme.app

import android.Manifest
import android.content.pm.ApplicationInfo
import android.content.pm.PackageManager
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.BackHandler
import androidx.activity.compose.setContent
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.safeDrawingPadding
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.material3.darkColorScheme
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.core.content.ContextCompat

/** Where the app is. */
sealed interface Screen {
    data object Home : Screen
    /** getFirst: download the first mesh listed without being asked (for trying it in an emulator). */
    data class Browse(val getFirst: Boolean = false) : Screen
    data object Settings : Screen
    data object Calibrate : Screen
    /** mesh: the mesh being added to, or null for a rock with none. */
    data class Capture(val mesh: String?, val name: String) : Screen
    data class Done(val id: String) : Screen
}

class MainActivity : ComponentActivity() {
    lateinit var store: Store
    lateinit var api: Api
    lateinit var sensors: PhoneSensors
    private var screen by mutableStateOf<Screen>(Screen.Home)

    fun has(permission: String) = ContextCompat.checkSelfPermission(this, permission) == PackageManager.PERMISSION_GRANTED

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        store = Store(this); api = Api(store); sensors = PhoneSensors(this)
        if (applicationInfo.flags and ApplicationInfo.FLAG_DEBUGGABLE != 0) tryOut()
        setContent {
            MaterialTheme(colorScheme = darkColorScheme()) {
                Surface(Modifier.fillMaxSize()) {
                    val go: (Screen) -> Unit = { screen = it }
                    BackHandler(enabled = screen != Screen.Home && screen !is Screen.Capture) { screen = Screen.Home }
                    when (val s = screen) {
                        is Screen.Capture -> CaptureScreen(this, s, go)       // the camera, edge to edge
                        else -> Box(Modifier.fillMaxSize().safeDrawingPadding()) {
                            when (s) {
                                Screen.Home -> HomeScreen(this@MainActivity, go)
                                is Screen.Browse -> BrowseScreen(this@MainActivity, s.getFirst, go)
                                Screen.Settings -> SettingsScreen(this@MainActivity, go)
                                Screen.Calibrate -> CalibrateScreen(this@MainActivity, go)
                                is Screen.Done -> DoneScreen(this@MainActivity, s.id, go)
                                is Screen.Capture -> {}
                            }
                        }
                    }
                }
            }
        }
    }

    /**
     * In a debug build, for trying it in an emulator with nobody to press anything (app/ci/screens-android.sh): extras
     * on the intent that starts it set the server (`--es server <address>`) and open a screen straight away (`--es
     * screen browse|settings|calibrate|capture`, with `--ez get true` to download the first mesh listed, and `--es
     * mesh <id>` for the mesh to capture: the first downloaded if not given, `none` for a rock with no mesh).
     */
    private fun tryOut() {
        intent.getStringExtra("server")?.let { store.server = it }
        screen = when (intent.getStringExtra("screen")) {
            "browse" -> Screen.Browse(intent.getBooleanExtra("get", false))
            "settings" -> Screen.Settings
            "calibrate" -> Screen.Calibrate
            "capture" -> {
                val asked = intent.getStringExtra("mesh")
                val mesh = asked?.takeIf { it != "none" } ?: store.downloaded().firstOrNull()?.mesh?.takeIf { asked == null }
                Screen.Capture(mesh, mesh?.let { store.plan(it)?.name } ?: "A rock")
            }
            else -> Screen.Home
        }
    }

    override fun onResume() {
        super.onResume()
        sensors.start(has(Manifest.permission.ACCESS_FINE_LOCATION))
    }

    override fun onPause() {
        sensors.stop()
        super.onPause()
    }
}
