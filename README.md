# HP volleyball

Simple pickup volleyball signup page (Google App Engine).

#### Overview

A simple signup page is displayed to the user.  If the user is logged in,
they may commit to playing or indicate that they may be able to play.
These choices are made via `Commit/Maybe` buttons. Users that are not 
logged in can only see the current tally and a `Login` button.

A *Comments* secion is shown below the signup tally.  It can be used to
indicate start times, or other issues (like who is brining the ball).

The `Info` tab provides general information (rules, location, etc.)

#### Usage

The Google App Engine (GAE) is used for this application.  The `update.bat`
file is used to push the changes to the GAE server.  Use the `runserver.bat`
file to run a local server and test changes prior to updating.


