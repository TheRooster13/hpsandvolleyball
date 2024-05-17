# HP Volleyball

A website to admninstrate HP's Summer Sand Volleyball League using Google App Engine.

#### Overview

This website contains:

An introductory page with information about the league.
A signup page where players can sign up for the league, and by doing so, consent to whatever legalese HP requires.
An Info page  that discusses some of the details and rules for the league.
An Availability page where players can adjust their availability to play on all of the days in the league season.
A Weekly schedule page where players can see who is scheduled to play when.
A Daily schedule page where players can see who plays on which team for each game, and enter the scores for those games.
A Standings page where players are listed and ordered by ELO rank or by Points-Per-Game.

The app is built with Python 3.9 utilizing Flask, Datastore, Mailjet, and the Google calendar API. ChatGPT helped with a lot of syntax and some scheduling logic.

The scheduling system is quite robust and can handle a relatively large number of players, each having their own availability restrictions and ELO ranking.

The goals of the scheduler are:
1. Schedule as many games as possible so as many people can play as possible.
2. Group players into similar ELO skill tiers for scheduling. Players should play against other of similar skill.
3. Prioritize scheduling players who have relatively more byes so that everyone is scheduled a similar number of times.

The scheduler will absolutely not schedule any player on a date where they are unavailable.

Currently, the available time slots for matches are from 12-1 Monday-Friday.

The scheduler is setup to handle 4-on-4 matches, where the teams rotate each game. Every player has a "nemesis" that they play against every game.

Players are required to use a Google account to sign up, and then their Google ID is used to identify players in the system.

#### Usage

The Google App Engine (GAE) is used for this application.  The `update.bat`
file is used to push the changes to the GAE server.  Use the `runserver.bat`
file to run a local server and test changes prior to updating.

#### Links

* [Google Developers Console](https://console.developers.google.com/iam-admin/projects)
* [Python API](https://cloud.google.com/appengine/docs/python/)
* [Dashboard](https://console.cloud.google.com/appengine?src=ac&project=hpsandvolleyball)
* [Live Application](http://hpsandvolleyball.appspot.com)

