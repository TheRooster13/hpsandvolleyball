package com.hp.hpvolleyball

import com.hp.hpvolleyball.data.model.DailySchedule
import io.reactivex.Completable
import io.reactivex.Observable
import io.reactivex.Single
import okhttp3.MultipartBody
import okhttp3.RequestBody
import retrofit2.Call
import retrofit2.Response
import retrofit2.http.*

interface HpVolleyballService {

    /*
    Daily Schedule
    * */
    @GET("day")
    fun day(
        @Query("y") year: String
    ): Call<DailySchedule>

    /*
    Weekly Schedule
    * */
    @GET("week")
    fun week(
        @Query("y") year: String
    ): Single<DailySchedule>

}

