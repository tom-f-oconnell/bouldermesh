plugins {
    kotlin("multiplatform") version "2.1.21" apply false
    kotlin("android") version "2.1.21" apply false
    kotlin("plugin.serialization") version "2.1.21" apply false
    kotlin("plugin.compose") version "2.1.21" apply false
    id("com.android.application") version "8.7.3" apply false
}

// build output somewhere other than the checkout, if asked: BOULDERMESH_APP_BUILD=/some/dir
System.getenv("BOULDERMESH_APP_BUILD")?.let { root -> allprojects { layout.buildDirectory.set(File(root, name)) } }
