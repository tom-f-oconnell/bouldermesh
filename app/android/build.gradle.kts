plugins {
    id("com.android.application")
    kotlin("android")
    kotlin("plugin.serialization")
    kotlin("plugin.compose")
}

// One key signs every build that has it (CI's from the repo's secrets, app/ci; hal's from BM_SIGNING_*), so that each
// installs over the last and the app can update itself (Update.kt): Android only lets an app be replaced by one signed
// with the same key. Without it, the SDK's own debug key, which differs on every machine.
val signingKeystore = System.getenv("BM_SIGNING_KEYSTORE")?.takeIf { it.isNotBlank() && file(it).isFile }
val signingPassword = System.getenv("BM_SIGNING_PASSWORD")?.takeIf { it.isNotBlank() }
    ?: System.getenv("BM_SIGNING_PASSWORD_FILE")?.let { file(it).takeIf { f -> f.isFile }?.readText()?.trim() }
// which build this is, for Update.kt to tell a newer one: CI's run number (always rising), and the commit
val buildNumber = System.getenv("BM_VERSION_CODE")?.toIntOrNull() ?: 1
val buildCommit = System.getenv("BM_COMMIT") ?: providers.exec { commandLine("git", "rev-parse", "--short=7", "HEAD"); isIgnoreExitValue = true }
    .standardOutput.asText.get().trim().ifEmpty { "dev" }

android {
    namespace = "sh.boulderme.app"
    compileSdk = 35
    defaultConfig {
        applicationId = "sh.boulderme.app"
        minSdk = 26
        targetSdk = 35
        versionCode = buildNumber
        versionName = "0.1.0"
        buildConfigField("String", "COMMIT", "\"$buildCommit\"")
        buildConfigField("int", "BUILD", "$buildNumber")
    }
    signingConfigs {
        if (signingKeystore != null && signingPassword != null) create("shared") {
            storeFile = file(signingKeystore); storePassword = signingPassword
            keyAlias = "bouldermesh"; keyPassword = signingPassword
        }
    }
    buildTypes {
        release { isMinifyEnabled = false }
        getByName("debug") { signingConfigs.findByName("shared")?.let { signingConfig = it } }
    }
    compileOptions { sourceCompatibility = JavaVersion.VERSION_17; targetCompatibility = JavaVersion.VERSION_17 }
    kotlinOptions { jvmTarget = "17" }
    buildFeatures { compose = true; buildConfig = true }
    sourceSets["main"].java.srcDirs("src/main/kotlin")
}

dependencies {
    implementation(project(":shared"))
    implementation("org.jetbrains.kotlinx:kotlinx-serialization-json:1.7.3")
    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-android:1.9.0")
    implementation("androidx.core:core-ktx:1.15.0")
    implementation("androidx.activity:activity-compose:1.9.3")
    implementation("androidx.lifecycle:lifecycle-runtime-ktx:2.8.7")
    implementation(platform("androidx.compose:compose-bom:2024.12.01"))
    implementation("androidx.compose.ui:ui")
    implementation("androidx.compose.material3:material3")
    implementation("androidx.compose.foundation:foundation")
    implementation("androidx.exifinterface:exifinterface:1.3.7")
    implementation("com.google.ar:core:1.47.0")
    implementation("com.google.android.gms:play-services-code-scanner:16.1.0")     // Settings' Scan (scanQr())
}
