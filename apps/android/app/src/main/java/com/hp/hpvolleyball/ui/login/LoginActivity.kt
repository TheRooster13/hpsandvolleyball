package com.hp.hpvolleyball.ui.login

import android.app.Activity
import android.arch.lifecycle.Observer
import android.arch.lifecycle.ViewModelProviders
import android.content.Intent
import android.os.Bundle
import android.support.annotation.StringRes
import android.support.v7.app.AppCompatActivity
import android.text.Editable
import android.text.TextWatcher
import android.view.View
import android.view.inputmethod.EditorInfo
import android.widget.Button
import android.widget.EditText
import android.widget.ProgressBar
import android.widget.Toast
import com.google.android.gms.auth.api.signin.GoogleSignIn
import com.google.android.gms.auth.api.signin.GoogleSignInClient

import com.hp.hpvolleyball.R
import com.google.android.gms.auth.api.signin.GoogleSignInOptions
import kotlinx.android.synthetic.main.activity_login.*
import com.google.android.gms.auth.api.signin.GoogleSignInAccount
import com.google.android.gms.tasks.Task
import com.github.ajalt.timberkt.d
import com.github.ajalt.timberkt.e
import com.github.ajalt.timberkt.w
import com.google.android.gms.common.api.ApiException
import com.hp.hpvolleyball.data.model.DailySchedule
import com.hp.hpvolleyball.di.ActivityModule
import com.hp.hpvolleyball.di.Injector
import com.hp.hpvolleyball.rest.ServiceProvider
import io.reactivex.Observable
import io.reactivex.ObservableEmitter
import io.reactivex.schedulers.Schedulers
import javax.inject.Inject


class LoginActivity : AppCompatActivity() {

    companion object {
        const val RC_SIGN_IN = 9
    }

    private lateinit var loginViewModel: LoginViewModel
    private var googleSignInClient: GoogleSignInClient? = null

    @Inject
    lateinit var serviceProvider: ServiceProvider

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        Injector.get().newActivityComponent(ActivityModule(this)).inject(this)

        setContentView(R.layout.activity_login)

        val username = findViewById<EditText>(R.id.username)
        val password = findViewById<EditText>(R.id.password)
        val login = findViewById<Button>(R.id.login)
        val loading = findViewById<ProgressBar>(R.id.loading)

// Configure sign-in to request the user's ID, email address, and basic
// profile. ID and basic profile are included in DEFAULT_SIGN_IN.
        val gso = GoogleSignInOptions.Builder(GoogleSignInOptions.DEFAULT_SIGN_IN)
            .requestIdToken(application.getString(R.string.id_token))
            .requestEmail()
            .build()

// Build a GoogleSignInClient with the options specified by gso.
        googleSignInClient = GoogleSignIn.getClient(this, gso)

        sign_in_button.setOnClickListener {
            startActivityForResult(googleSignInClient?.signInIntent, RC_SIGN_IN)
        }

        loginViewModel = ViewModelProviders.of(this, LoginViewModelFactory())
            .get(LoginViewModel::class.java)

        loginViewModel.loginFormState.observe(this@LoginActivity, Observer {
            val loginState = it ?: return@Observer

            // disable login button unless both username / password is valid
            login.isEnabled = loginState.isDataValid

            if (loginState.usernameError != null) {
                username.error = getString(loginState.usernameError)
            }
            if (loginState.passwordError != null) {
                password.error = getString(loginState.passwordError)
            }
        })

        loginViewModel.loginResult.observe(this@LoginActivity, Observer {
            val loginResult = it ?: return@Observer

            loading.visibility = View.GONE
            if (loginResult.error != null) {
                showLoginFailed(loginResult.error)
            }
            if (loginResult.success != null) {
                updateUiWithUser(loginResult.success)
            }
            setResult(Activity.RESULT_OK)

            //Complete and destroy login activity once successful
            finish()
        })

        username.afterTextChanged {
            loginViewModel.loginDataChanged(
                username.text.toString(),
                password.text.toString()
            )
        }

        password.apply {
            afterTextChanged {
                loginViewModel.loginDataChanged(
                    username.text.toString(),
                    password.text.toString()
                )
            }

            setOnEditorActionListener { _, actionId, _ ->
                when (actionId) {
                    EditorInfo.IME_ACTION_DONE ->
                        loginViewModel.login(
                            username.text.toString(),
                            password.text.toString()
                        )
                }
                false
            }

            login.setOnClickListener {
                loading.visibility = View.VISIBLE
                loginViewModel.login(username.text.toString(), password.text.toString())
            }
        }
    }

    public override fun onActivityResult(requestCode: Int, resultCode: Int, data: Intent?) {
        super.onActivityResult(requestCode, resultCode, data)

        // Result returned from launching the Intent from GoogleSignInClient.getSignInIntent(...);
        if (requestCode == RC_SIGN_IN) {
            // The Task returned from this call is always completed, no need to attach
            // a listener.
            val task = GoogleSignIn.getSignedInAccountFromIntent(data)
            handleSignInResult(task)
        }
    }

    private fun handleSignInResult(completedTask: Task<GoogleSignInAccount>) {
        try {
            val account = completedTask.result
            val idToken = account?.idToken
            val id = account?.id
            val email = account?.email
            val displayName = account?.displayName

            getDailySchedule("2018")
                .subscribeOn(Schedulers.io())
                .subscribe(
                    {
                        d { "schedule: ${it.schedule_day}"}
                        val day = it.day
                        val year = it.year
                    }
                )
//            getDailySchedule("2018")
//                .observeOn(AndroidSchedulers.mainThread())
//                .toObservable()
//                .subscribe(
//                )

            // Signed in successfully, show authenticated UI.
//            updateUI(account);
        } catch (e: ApiException) {
            // The ApiException status code indicates the detailed failure reason.
            // Please refer to the GoogleSignInStatusCodes class reference for more information.
            w { "signInResult:failed code=${e.statusCode}" }
//            updateUI(null);
        }
    }

    private fun getDailySchedule(year: String): Observable<DailySchedule> {
        return Observable.create { emitter: ObservableEmitter<DailySchedule> ->
            val dailyScheduleResponse = serviceProvider.getServiceApi().day(year).execute()
            when {
                dailyScheduleResponse.isSuccessful -> {
                    d { "Success" }
                    dailyScheduleResponse.body()?.let {
                        d { "${it.schedule_day}" }
                        emitter.onNext(it)
                        emitter.onComplete()
                    }
                }
                else -> {
                    e { "Failure" }
                    emitter.onComplete()
                }
            }

        }
    }
//    private fun getDailySchedule(year: String): Single<DailySchedule> {
//        return serviceProvider.getServiceApi().day(year)
//            .subscribeOn(Schedulers.io())
//    }

    private fun updateUiWithUser(model: LoggedInUserView) {
        val welcome = getString(R.string.welcome)
        val displayName = model.displayName
        // TODO : initiate successful logged in experience
        Toast.makeText(
            applicationContext,
            "$welcome $displayName",
            Toast.LENGTH_LONG
        ).show()
    }

    private fun showLoginFailed(@StringRes errorString: Int) {
        Toast.makeText(applicationContext, errorString, Toast.LENGTH_SHORT).show()
    }
}

/**
 * Extension function to simplify setting an afterTextChanged action to EditText components.
 */
fun EditText.afterTextChanged(afterTextChanged: (String) -> Unit) {
    this.addTextChangedListener(object : TextWatcher {
        override fun afterTextChanged(editable: Editable?) {
            afterTextChanged.invoke(editable.toString())
        }

        override fun beforeTextChanged(s: CharSequence, start: Int, count: Int, after: Int) {}

        override fun onTextChanged(s: CharSequence, start: Int, before: Int, count: Int) {}
    })
}
