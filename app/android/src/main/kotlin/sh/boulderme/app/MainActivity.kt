package sh.boulderme.app

import android.Manifest
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
    data object Browse : Screen
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
                                Screen.Browse -> BrowseScreen(this@MainActivity, go)
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

    override fun onResume() {
        super.onResume()
        sensors.start(has(Manifest.permission.ACCESS_FINE_LOCATION))
    }

    override fun onPause() {
        sensors.stop()
        super.onPause()
    }
}
