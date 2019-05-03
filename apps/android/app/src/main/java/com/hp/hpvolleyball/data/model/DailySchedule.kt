package com.hp.hpvolleyball.data.model

import com.google.gson.annotations.Expose
import com.google.gson.annotations.SerializedName

class DailySchedule {
    @SerializedName("week")
    @Expose
    var week: Int? = null
    @SerializedName("numWeeks")
    @Expose
    var numWeeks: Int? = null
    @SerializedName("schedule_day")
    @Expose
    var schedule_day: String? = null
    @SerializedName("is_today")
    @Expose
    var is_today: Boolean? = null
    @SerializedName("year")
    @Expose
    var year: Int? = null
    @SerializedName("tier")
    @Expose
    var tier: Int? = null
    @SerializedName("day")
    @Expose
    var day: Int? = null
}