package com.hp.hpvolleyball.di

import android.app.Application

class Injector private constructor() {

    companion object {
        @JvmStatic
        lateinit var appComponent: AppComponent

        @Suppress("DEPRECATION")
        @JvmStatic
        internal fun initialize(application: Application) {
            appComponent = DaggerAppComponent
                .builder()
                .appModule(AppModule(application))
                .build()

        }

        @JvmStatic
        internal fun get(): AppComponent {
            return appComponent
        }
    }
}