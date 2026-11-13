pluginManagement {
    repositories { google(); mavenCentral(); gradlePluginPortal() }
}
dependencyResolutionManagement {
    repositories { google(); mavenCentral() }
}
rootProject.name = "bouldermesh-app"
include(":shared", ":ios")
// the Android app only where there's an Android SDK to build it with: a Mac building the iPhone app needn't have one
if (System.getenv("ANDROID_HOME") != null || System.getenv("ANDROID_SDK_ROOT") != null || file("local.properties").exists()) include(":android")
