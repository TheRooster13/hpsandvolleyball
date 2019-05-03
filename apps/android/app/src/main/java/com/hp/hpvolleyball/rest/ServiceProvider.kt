package com.hp.hpvolleyball.rest

import com.github.ajalt.timberkt.d
import com.hp.hpvolleyball.HpVolleyballService
import com.hp.hpvolleyball.di.ApplicationScoped
import okhttp3.OkHttpClient
import retrofit2.Retrofit
import javax.inject.Inject

@ApplicationScoped
class ServiceProvider @Inject constructor(
    private val builder: Retrofit.Builder,
    private val okHttpClient: OkHttpClient) {

    fun getServiceApi(): HpVolleyballService {
        val okHttpClientBuilder = okHttpClient.newBuilder()
        okHttpClientBuilder.addInterceptor { chain ->
            var request = chain.request()
            var requestBuilder = request.newBuilder()

            requestBuilder.addHeader("x-api-os", "ANDROID")
            request = requestBuilder.build()
            d { "Access Token and Region Token Headers: ${request.headers()}" }
            val response = chain.proceed(request)
            if (response.code() == 401) {
                d { "updateToken necessary: response is ${response.code()}" }
                    chain.proceed(request)
            } else {
                response
            }
        }
        val retrofit = builder
            .client(okHttpClientBuilder.build())
            .build()

        return retrofit.create(HpVolleyballService::class.java)
    }
}
