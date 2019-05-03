package com.hp.hpvolleyball.di

import android.app.Application
import android.app.NotificationManager
import android.content.Context
import android.support.v7.app.AppCompatActivity
import com.google.gson.Gson
import com.hp.hpvolleyball.common.Constants
import com.hp.hpvolleyball.common.HttpLoggingInterceptor
import com.hp.hpvolleyball.ui.login.LoginActivity
import dagger.Component
import dagger.Module
import dagger.Provides
import dagger.Subcomponent
import okhttp3.OkHttpClient
import retrofit2.Retrofit
import retrofit2.adapter.rxjava2.RxJava2CallAdapterFactory
import retrofit2.converter.gson.GsonConverterFactory
import java.lang.ref.WeakReference
import java.util.concurrent.TimeUnit
import javax.inject.Named
import javax.inject.Scope

@ApplicationScoped
@Component(modules = [AppModule::class, HttpModule::class, AbstractModule::class])
interface AppComponent {
    fun inject(application: Application): Application
    fun newActivityComponent(activityModule: ActivityModule): ActivityComponent  //subcomponent factory method
}

@Module
class HttpModule {

    private val okHttpClient: OkHttpClient = OkHttpClient.Builder()
        .addInterceptor(HttpLoggingInterceptor(HttpLoggingInterceptor.Level.BODY))
        .connectTimeout(Constants.Server.OK_HTTP_REQUEST_TIMEOUT, TimeUnit.SECONDS)
        .readTimeout(Constants.Server.OK_HTTP_REQUEST_TIMEOUT, TimeUnit.SECONDS)
        .writeTimeout(Constants.Server.OK_HTTP_WRITE_REQUEST_TIMEOUT, TimeUnit.SECONDS)
        .build()

    @Provides
    @ApplicationScoped
    fun provideOkHttpClientBuilder(): OkHttpClient.Builder {
        return okHttpClient.newBuilder()
    }

    @Provides
    @ApplicationScoped
    fun provideServer(): Retrofit.Builder {

        return Retrofit.Builder()
            .client(okHttpClient)
            .baseUrl(Constants.Server.BASE_URL)
            .addConverterFactory(GsonConverterFactory.create())
            .addCallAdapterFactory(RxJava2CallAdapterFactory.create())

    }

    @Provides
    @ApplicationScoped
    fun provideOkHttp(): OkHttpClient {
        return okHttpClient
    }

    @Provides
    @ApplicationScoped
    fun provideGson(): Gson {
        return Gson()
    }

}

@Scope
@kotlin.annotation.Retention(AnnotationRetention.RUNTIME)
annotation class ApplicationScoped

// objects marked with this will live for lifetime of activity
@Scope
annotation class ActivityScoped

@Module
class ActivityModule(private val activity: AppCompatActivity) {
}

@Subcomponent(modules = [ActivityModule::class])
@ActivityScoped
interface ActivityComponent {
    fun inject(activity: LoginActivity)
}

@Module
abstract class AbstractModule

@Module
class AppModule(val app: Application) {

    @Provides
    @ApplicationScoped
    fun provideContext(): Context {
        return app
    }

    @Provides
    @ApplicationScoped
    fun provideContextRef(): WeakReference<Context> {
        return WeakReference(app)
    }

    @Provides
    @ApplicationScoped
    fun provideApplication(): Application {
        return app
    }

    @Provides
    @ApplicationScoped
    fun getNM(context: Context): NotificationManager {
        return context.getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
    }

}
