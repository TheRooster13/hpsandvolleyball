package com.hp.hpvolleyball

import android.app.Application
import android.content.ContentProvider
import android.content.ContentValues
import android.database.Cursor
import android.net.Uri
import android.util.Log
import com.github.ajalt.timberkt.Timber
import com.github.ajalt.timberkt.d
import com.github.ajalt.timberkt.w
import com.hp.hpvolleyball.di.Injector
import io.reactivex.exceptions.UndeliverableException
import io.reactivex.plugins.RxJavaPlugins
import java.io.IOException
import java.io.InterruptedIOException
import java.net.SocketException

class AppInitializer : ContentProvider() {

    override fun delete(uri: Uri, selection: String?, selectionArgs: Array<String>?): Int {
        return 0
    }

    override fun getType(uri: Uri): String? {
        return null
    }

    override fun insert(uri: Uri, values: ContentValues?): Uri? {
        return null
    }

    override fun onCreate(): Boolean {
        // plant the timber tree
        if (BuildConfig.DEBUG) {
            Timber.plant(Timber.DebugTree())
        } else {
            Timber.plant(object: timber.log.Timber.DebugTree() {
                override fun isLoggable(tag: String?, priority: Int): Boolean {
                    return priority >= Log.ERROR
                }
            })
        }
        Injector.initialize(context!!.applicationContext as Application)
        RxJavaPlugins.setErrorHandler { ex ->
            var exception = ex
            if (ex is UndeliverableException) {
                d{"UndeliverableException: getting cause"}
                exception = ex.cause!!
            }
            if (exception is InterruptedIOException) {
                w(exception){"InterruptedIOException"}
            }else if (exception is IOException || exception is SocketException) {
                w(exception){"IOException || SocketException"}
            }else  if (exception is InterruptedException) {
                // fine, some blocking code was interrupted by a dispose call
            } else  if (exception is NullPointerException || exception is IllegalArgumentException) {
                // that's likely a bug in the application
                w(exception) { "NPE or IAE: " }
                Thread.currentThread().uncaughtExceptionHandler.uncaughtException(Thread.currentThread(), exception)
            }else if (exception is IllegalStateException) {
                // that's a bug in RxJava or in a custom operator
                w(exception) { "IllegalStateException: " }
                Thread.currentThread().uncaughtExceptionHandler.uncaughtException(Thread.currentThread(), exception)
            }else {
                w(exception) { "Undeliverable exception received, not sure what to do" }
            }
        }
        return true
    }

    override fun query(uri: Uri, projection: Array<String>?, selection: String?,
                       selectionArgs: Array<String>?, sortOrder: String?): Cursor? {
        return null
    }

    override fun update(uri: Uri, values: ContentValues?, selection: String?,
                        selectionArgs: Array<String>?): Int {
        return 0
    }
}
