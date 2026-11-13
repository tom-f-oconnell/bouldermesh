plugins {
    kotlin("multiplatform")
    kotlin("plugin.serialization")
}

// The iPhone app, in Kotlin: built into BoulderMeshKit.framework, which the Xcode project in xcode/ links and starts.
kotlin {
    listOf(iosArm64(), iosSimulatorArm64(), iosX64()).forEach {
        it.binaries.framework { baseName = "BoulderMeshKit"; isStatic = true }
    }
    sourceSets {
        iosMain.dependencies {
            implementation(project(":shared"))
            implementation("org.jetbrains.kotlinx:kotlinx-serialization-json:1.7.3")
        }
        all {
            languageSettings.optIn("kotlinx.cinterop.ExperimentalForeignApi")
            languageSettings.optIn("kotlinx.cinterop.BetaInteropApi")
        }
    }
}
