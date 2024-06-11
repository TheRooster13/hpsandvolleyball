import os
import io
import urllib
from datetime import datetime, timedelta, date, time
import string
import math
import random
import sys
import json
import re
from itertools import permutations
import csv
from werkzeug.utils import secure_filename
from functools import wraps
import pytz

import mailjet_rest
#from google.oauth2 import service_account

# For Google Calendar
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
#from google.oauth2 import id_token
#from google_auth_oauthlib.flow import Flow
#from google.oauth2.credentials import Credentials

from google.cloud import datastore, secretmanager
from google.cloud.datastore.query import PropertyFilter
from google.auth.transport import requests
from authlib.integrations.flask_client import OAuth

# These are needed to send mail from hp.com using O365
from msal import ConfidentialClientApplication
import requests as standard_requests
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import base64

from flask import Flask, request, render_template, jsonify, redirect, url_for, session, flash, make_response

# Globals - I want these eventually to go into a datastore per year so things can be different and configured per year.
# For now, hard-coded is okay.
numWeeks = 13
startdate = date(2024, 5, 20)
holidays = (2,1), (3,4), (5,3), (7,4), (8,3)  # ((week,slot),(week,slot),(week,slot)) - Memorial Day, Independence Day, BYITW Day
PLAYERS_PER_GAME = 8
SLOTS_IN_WEEK = 5
ELO_MARGIN = 75

SEND_INVITES = True
# How to team up the players for each of the three games
ms = ((0, 1, 0, 1, 1, 0, 1, 0), (0, 1, 1, 0, 0, 1, 1, 0), (0, 1, 1, 0, 1, 0, 0, 1))

random.seed(datetime.now())

google = None

calendar_service = build('calendar', 'v3')

# Google Cloud Secret Manager client
secret_client = secretmanager.SecretManagerServiceClient()

def access_secret_version(secret_id):
    secret_name = f"projects/{os.getenv('GOOGLE_CLOUD_PROJECT')}/secrets/{secret_id}/versions/latest"
    response = secret_client.access_secret_version(request={"name": secret_name})
    return response.payload.data.decode('UTF-8')

app = Flask(__name__)
app.secret_key = access_secret_version('flask_secret_key')
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=180)
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SECURE'] = True

# Load client ID and client secret from Secret Manager
client_id = access_secret_version("client_id")
client_secret = access_secret_version("client_secret")
mailjet_api = access_secret_version("Mailjet_api")
mailjet_secret = access_secret_version("Mailjet_secret")
o365_tenant_id = access_secret_version("o365_tenant_id")
o365_client_id = access_secret_version("o365_client_id")
o365_client_secret = access_secret_version("o365_client_secret")

oauth = OAuth(app)

google = oauth.register(
    'google',
    client_id=client_id,
    client_secret=client_secret,
    authorize_params=None,
    server_metadata_url='https://accounts.google.com/.well-known/openid-configuration',
    client_kwargs={'scope': 'openid email profile'}
)

@app.before_request
def before_request():
    # Allow cron jobs to bypass HTTP to HTTPS redirection
    if request.headers.get('X-Appengine-Cron'):
        return
    
    # Redirect HTTP to HTTPS
    if request.url.startswith('http://'):
        url = request.url.replace('http://', 'https://', 1)
        return redirect(url, code=301)

@app.route('/login')
def login():
    session['next_url'] = request.args.get('next') or request.referrer or url_for('main_page')
    redirect_uri = url_for('authorize', _external=True, _scheme='https')
    return google.authorize_redirect(redirect_uri, prompt='select_account')

@app.route('/authorize')
def authorize():
    token = google.authorize_access_token()
    user_info = google.get('https://www.googleapis.com/oauth2/v1/userinfo').json()
#    print(f"User info: {user_info}")
    session['profile'] = {
        'id': user_info['id'],
        'email': user_info['email'],
        'name': user_info.get('name', '')
    }
    next_url = session.pop('next_url', url_for('main_page'))
    return redirect(next_url)

@app.route('/logout')
def logout():
    session.pop('profile', None)
    return redirect(url_for('main_page'))

class User:
    def __init__(self, id, email, name):
        self.id = id
        self.email = email
        self.name = name
        self.admin = False

def get_current_user():
    user_info = session.get('profile')
    if not user_info:
        return None
    user = User(id=user_info['id'], email=user_info['email'], name=user_info.get('name'))
    user.admin = is_user_admin(user.id)
    session['is_admin'] = user.admin
    return user

def is_user_admin(user_id):
    name = f"projects/{os.getenv('GOOGLE_CLOUD_PROJECT')}/secrets/admin-ids/versions/latest"
    response = secret_client.access_secret_version(request={"name": name})
    admin_ids = response.payload.data.decode('UTF-8').split(',')
    return user_id in admin_ids

def get_login_info():
    logged_in = 'profile' in session
    return {
        'logged_in': logged_in,
        'url': "/logout" if logged_in else "/login",
        'linktext': 'Logout' if logged_in else 'Login'
    }

def get_year_string():
    now = datetime.today()
    return now.strftime("%Y")

def get_player(client, id, year):
    try:
        key = client.key('Player_List', f"year-{year}_player-{id}")
        player_data = client.get(key)
        return Player(player_data=player_data) if player_data else None
    except Exception as e:
        return None


class Player:
    def __init__(self, player_data={}):
        self.id = player_data.get('id')
        self.name = player_data.get('name')
        self.email = player_data.get('email')
        self.phone = player_data.get('phone')
        self.elo_score = player_data.get('elo_score', 1000)
        self.byes = 0  # Initialized to 0, as byes are not be directly stored in the datastore
        self.conflicts = []  # Initialized to empty, typically updated elsewhere in the application
        self.points = player_data.get('points', 0)
        self.wins = player_data.get('wins', 0)
        self.games = player_data.get('games', 0)
        self.points_per_game = player_data.get('points_per_game', 0)


def get_player_data(client, year=datetime.today().year, current_week=None):
    playerlist = []
    conflict_count = {}

    # Get player list
    player_query = client.query(kind='Player_List')
    player_query.add_filter(filter=PropertyFilter('year', '=', year))
    players = list(player_query.fetch())

    for player_entity in players:
        player = Player(player_data=player_entity)
        playerlist.append(player)
        conflict_count[player.id] = [0] * numWeeks  # Initialize conflict counts

    if current_week is not None:
        # Process past byes if applicable
        if current_week > 1:
            #print(f"The current week is {current_week}.")
            bye_query = client.query(kind='Schedule')
            bye_query.add_filter(filter=PropertyFilter('year', '=', year))
            bye_query.add_filter(filter=PropertyFilter('slot', '=', 0))
            #bye_query.add_filter(filter=PropertyFilter('week', '<', current_week))
            past_byes = list(bye_query.fetch())

            # Initialize a dictionary to track bye weeks per player
            player_bye_weeks = {}

            # Populate the dictionary with sets to track weeks
            for bye in past_byes:
                player_id = bye['id']
                if player_id not in player_bye_weeks:
                    player_bye_weeks[player_id] = set()
                player_bye_weeks[player_id].add(bye['week'])
                #print(f"Adding week {bye['week']} to {bye['name']}'s bye list.")

            # Count the unique bye weeks for each player
            for p in playerlist:
                if p.id in player_bye_weeks:
                    p.byes = len(player_bye_weeks[p.id])
                else:
                    p.byes = 0

        # Check future availability for byes
        conflict_query = client.query(kind='Availability')
        conflict_query.add_filter(filter=PropertyFilter('year', '=', year))
        conflicts = list(conflict_query.fetch())

        for conflict in conflicts:
            if conflict['week'] > current_week and conflict['id'] in conflict_count:
                conflict_count[conflict['id']][conflict['week'] - 1] += 1
                if conflict_count[conflict['id']][conflict['week'] - 1] == SLOTS_IN_WEEK:
                    for player in playerlist:
                        if player.id == conflict['id']:
                            player.byes += 1
                            break

            if conflict['week'] == current_week and conflict['id'] in conflict_count:
                for player in playerlist:
                    if player.id == conflict['id']:
                        print(f"{player.name} has a conflict on slot {conflict['slot']}")
                        player.conflicts.append(conflict['slot'])

    return playerlist



def pick_slots(tier_slot, tier, tier_slot_list):
    if tier >= len(tier_slot_list):
        return True  # We've iterated through all tiers, we're good.
    while len(tier_slot) < (tier + 1):
        tier_slot.append(0)  # Fill tier_slot with 0s. We'll fill this with the correct slots as we go.
    for x in tier_slot_list[tier]:  # Cycle through each possible slot for this tier
        if x not in tier_slot:  # If this slot hasn't been taken by another slot yet...
            tier_slot[tier] = x  # Claim the slot.
            # Recursively call the function again on the next tier. If it returns True...
            if pick_slots(tier_slot, tier + 1, tier_slot_list):
                return True  # ...then we can return true too.
    # If we get here, we've tried every slot in this tier's valid list and found nothing that isn't taken yet, so...
    tier_slot[tier] = 0  # ...reset this tier's slot to 0 so we can try again.
    return False  # We've failed. Back up and try another slot in the prior tier's list.


def find_smallest_set(set_list):
    smallest_set = len(set_list[1])
    smallest_set_pos = 1
    for p in range(2, len(set_list)):
        if len(set_list[p]) == smallest_set:
            # Randomly choose which of the two sets to return if there is a tie.
            # The randomness isn't evenly distributed though.
            smallest_set_pos = random.choice((smallest_set_pos, p))
        if len(set_list[p]) < smallest_set:
            smallest_set = len(set_list[p])
            smallest_set_pos = p
    return smallest_set_pos


# Define a function to get the list of workdays in a week
def get_workdays(start_date):
    workdays = []
    current_date = start_date
    for _ in range(5):  # Five workdays in a week (Mon to Fri)
        workdays.append(current_date)
        current_date += timedelta(days=1)
    return workdays



def create_entity(client, kind, identifier, data):
    """Creates a new entity with the specified data in the datastore."""
    key = client.key(kind, identifier)
    entity = datastore.Entity(key=key)
    entity.update(data)
    client.put(entity)
    return entity

def update_entity(client, kind, identifier, data):
    """Updates an existing entity with the specified data in the datastore."""
    key = client.key(kind, identifier)
    entity = client.get(key)
    if entity:
        entity.update(data)
        client.put(entity)
        return entity
    else:
        print(f"Attempted to update non-existent entity in {kind} with key: {identifier}")
        return None

def set_holidays(client, user_id):
    year = datetime.today().year
    player = get_player(client, user_id, year)
    if not player:
        return  # Exit if no player found

    # Fetch all existing holiday entities for the player and year
    query = client.query(kind='Availability')
    query.add_filter(filter=PropertyFilter('year', '=', year))
    query.add_filter(filter=PropertyFilter('id', '=', user_id))
    query.add_filter(filter=PropertyFilter('week', 'IN', [week for week, _ in holidays]))
    existing_entities = {f"{entity['week']}-{entity['slot']}": entity for entity in query.fetch()}

    to_put = []
    
    # Check existing records and create new ones if necessary
    for week, slot in holidays:
        key_str = f"{week}-{slot}"
        if key_str not in existing_entities:
            # Create and put a new entity only if it does not already exist
            key = client.key('Availability', f"year-{year}_player-{user_id}_week-{week}_slot-{slot}")
            new_conflict = datastore.Entity(key=key)
            new_conflict.update({
                'year': year,
                'id': user_id,
                'week': week,
                'slot': slot,
                'name': player.name
            })
            to_put.append(new_conflict)

    # Perform a batch put operation if there are new entities to store
    if to_put:
        client.put_multi(to_put)


def links2text(html_content):
    link_pattern = re.compile(r'<a[^>]*href="([^"]*)"[^>]*>(.*?)<\/a>', re.IGNORECASE)
    converted_text = re.sub(link_pattern, r'\2 (\1)', html_content)
    return converted_text


def send_email(subject, html, to):
    mailjet = mailjet_rest.Client(auth=(mailjet_api, mailjet_secret), version='v3.1')
    recipients = [
        {
            "Email": e,
        }
        for e in to
    ]
    data = {
      'Messages': [
        {
          "From": {
            "Email": "noreplyhpsandvolleyball@gmail.com",
          },
          "To": recipients,
          "Subject": subject,
          "HTMLPart": html
        }
      ]
    }
    result = mailjet.send.create(data=data)
    print(result.status_code)
    print(result.json())

def update_calendar_event(week, slot, player_out=None, player_in=None):
    calendar_id = 'aidl2j9o0310gpp2allmil37ak@group.calendar.google.com'

    # Calculate the start time for the event
    start_time = datetime.combine(startdate, datetime.min.time()) + timedelta(days=slot-1, weeks=week-1)
    time_min = start_time.isoformat() + 'Z'
    time_max = (start_time + timedelta(days=1)).isoformat() + 'Z'

    print(f"timeMin: {time_min}")
    print(f"timeMax: {time_max}")

    try:
        # Fetch the specific event by matching the start time
        events_result = calendar_service.events().list(
            calendarId=calendar_id,
            timeMin=time_min,
            timeMax=time_max,
            singleEvents=True,
            orderBy='startTime'
        ).execute()

        events = events_result.get('items', [])
        if not events:
            print("No events found.")
            return

        # Assuming there's only one event at the specific start time
        event = events[0]
        print(f"startTime: {event['start'].get('dateTime', event['start'].get('date'))}")

        # Update the event attendees if player_out and player_in are provided
        if player_out or player_in:
            attendees = event.get('attendees', [])
            if player_out:
                attendees = [att for att in attendees if att['email'] != player_out.email]
            if player_in:
                attendees.append({'email': player_in.email})
            event['attendees'] = attendees

        # Update the event description
        if player_out or player_in:
            substitutions = "\n\nSubstitution:"
            if player_out:
                substitutions += f"\nOut: {player_out.name}"
            if player_in:
                substitutions += f"\nIn: {player_in.name}"
            event['description'] = event.get('description', '') + substitutions

        # Update the event in the calendar
        calendar_service.events().update(calendarId=calendar_id, eventId=event['id'], body=event, sendUpdates='all').execute()
        print("Event updated successfully.")

    except HttpError as e:
        print(f"An error occurred: {e}")

def send_email_via_SMTP(subject, body, recipient, mailbox_email="hpsandvolleyball@hp.com"):
    # Function to get OAuth2 token
    def get_oauth2_token():
        app = ConfidentialClientApplication(
            client_id=o365_client_id,
            client_credential=o365_client_secret,
            authority=f"https://login.microsoftonline.com/{o365_tenant_id}"
        )

        token_response = app.acquire_token_for_client(scopes=["https://outlook.office365.com/.default"])
        if "access_token" in token_response:
            return token_response['access_token']
        else:
            raise Exception(f"Failed to get access token: {token_response.get('error_description')}")

    # Obtain the OAuth2 access token
    access_token = get_oauth2_token()

    # Create Email Content
    msg = MIMEMultipart()
    msg['From'] = mailbox_email
    msg['To'] = recipient
    msg['Subject'] = subject
    msg.attach(MIMEText(body, 'plain'))

    # SMTP Configuration
    smtp_server = "smtp.office365.com"
    smtp_port = 587

    # Send Email
    try:
        server = smtplib.SMTP(smtp_server, smtp_port)
        server.starttls()

        # Authenticate using the OAuth2 access token
        auth_string = f"user={mailbox_email}\1auth=Bearer {access_token}\1\1"
        auth_string = base64.b64encode(auth_string.encode()).decode()

        server.docmd("AUTH", "XOAUTH2 " + auth_string)
        server.sendmail(msg['From'], msg['To'], msg.as_string())
        server.quit()
        print("Email sent successfully")
        return "Email sent successfully"
    except Exception as e:
        print(f"Failed to send email: {e}")
        return f"Failed to send email: {e}"

    
##########################
###                    ###
###   Flask Handlers   ###
###                    ###
##########################

def admin_or_cron_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
       # Check for cron job header
        if request.headers.get('X-Appengine-Cron'):
            print("Cron job detected, executing task.")
            return f(*args, **kwargs)
        
        # Check for user profile in session
        if 'profile' not in session:
            print("User profile not in session, redirecting to login.")
            return redirect(url_for('login', next=request.url))
        
        # Check if user is an admin
        if not session.get('is_admin'):
            print("User is not admin, redirecting to main page.")
            return redirect(url_for('main_page'))
        
        return f(*args, **kwargs)
    
    return decorated_function


@app.route('/')
def main_page():
    login_info = get_login_info()
    user = get_current_user()
    if user:
        print(f"user={user.name}({user.id})")
    year = datetime.today().year
    client = datastore.Client()

    player = None
    if user:
        player = get_player(client, user.id, year)
    
    template_values = {
        'year': get_year_string(),
        'page': 'mainpage',
        'user': user,
        'is_signed_up': player is not None,
        'player': player,
        'login': login_info,
        'is_admin': user.admin if user else False,
    }

    os = request.headers.get('x-api-os')
    if os is not None:
        return jsonify(template_values)
    else:
        return render_template('mainpage.html', **template_values)


@app.post('/signup')
def signup_post():
    user = get_current_user()
    if user:
        print(f"user={user.name}({user.id})")
    now = datetime.today()
    year = now.year
    
    if user:
        client = datastore.Client()
        player_id = user.id
        player_name = request.form['name'] or user.name
        player_email = request.form['email'] or user.email

        # Prepare the player entity
        properties = {
            'year': year,
            'id': player_id,
            'name': player_name,
            'email': player_email,
            'elo_score': 800,  # Default ELO score
            'points': 0,
            'games': 0,
            'wins': 0,
            'points_per_game': 0.0
        }
        
        # Check previous years back to 2018 for player's data
        found = False
        for yr in range(year - 1, 2017, -1):
            query = client.query(kind='Player_List')
            query.add_filter('year', '=', yr)
            results = list(query.fetch())
            for previous_player in results:
                if previous_player['id'] == player_id or previous_player['name'] == player_name or previous_player['email'] == player_email:
                    properties['elo_score'] = int((previous_player['elo_score'] + 1000) / 2)
                    found = True
                    break
            if found:
                break

        if request.form.get('action') == "Commit":
            create_entity(client, 'Player_List', f"year-{year}_player-{player_id}", properties)
        set_holidays(client, user.id)

    return redirect(url_for('signup_get'))

@app.get('/signup')
def signup_get():
    now = datetime.today()
    year = now.year
    today = date.today()
    week = int((today - startdate).days // 7 + 1)

    client = datastore.Client()
    login_info = get_login_info()
    user = get_current_user()
    if user:
        print(f"user={user.name}({user.id})")
    player = get_player(client, user.id, year) if user else None
    player_list = get_player_data(client, year)
    active_schedule = check_for_active_schedule(client, year)

    template_values = {
        'year': str(year),
        'week': week,
        'page': 'signup',
        'user': user,
        'player_list': player_list,
        'is_signed_up': player is not None,
        'active_schedule': active_schedule,
        'is_admin': user.admin if user else False,
        'login': login_info,
        'player': player,
    }

    if request.headers.get('x-api-os'):
        return jsonify(template_values)
    else:
        return render_template('signup.html', **template_values)

def check_for_active_schedule(client, year):
    try:
        qry = client.query(kind='Schedule')
        qry.keys_only()
        qry.add_filter(filter=PropertyFilter('year', '=', year))
        results = list(qry.fetch(limit=1))
        return len(results) > 0
    except Exception as e:
        return False
        
@app.route('/unsignup', methods=['POST'])
def un_signup():
    user = get_current_user()
    if user:
        print(f"user={user.name}({user.id})")
        client = datastore.Client()
        year = datetime.today().year
        player_id = user.id
        key = client.key('Player_List', f"year-{year}_player-{player_id}")
        player = client.get(key)
        if player:
            client.delete(key)
    return redirect(url_for('signup_get'))

@app.route('/standings')
def standings():
    user = get_current_user()
    if user:
        print(f"user={user.name}({user.id})")
    now = datetime.now()
    today = date.today()
    week = int(((today - startdate).days + 2) // 7 + 1)
    week = max(1, min(week, numWeeks))
    year = int(request.args.get('y', now.year))
    login_info = get_login_info()

    client = datastore.Client()
    player_list = get_player_data(client, year)

    min_games = int(3 * (week // 2))
    sort_method = request.args.get('sort')
    if sort_method == 'elo':
        player_list.sort(key=lambda x: x.elo_score, reverse=True)
    else:
        player_list = [p for p in player_list if p.games >= min_games]
        player_list.sort(key=lambda x: x.points_per_game, reverse=True)

    win_percentage = {p.id: round(100 * float(p.wins) / p.games, 1) if p.games > 0 else 0 for p in player_list}

    template_values = {
        'current_year': now.year,
        'year': year,
        'page': 'standings',
        'player_list': player_list,
        'win_percentage': win_percentage,
        'min_games': min_games,
        'is_signed_up': user is not None,
        'login': login_info,
        'is_admin': user.admin if user else False,
    }

    if request.headers.get('x-api-os'):
        return jsonify(template_values)
    else:
        return render_template('standings.html', **template_values)

@app.route('/week', methods=['GET'])
def get_week():
    client = datastore.Client()
    today = date.today()
    year = int(request.args.get('y', today.year))
    user = get_current_user()
    if user:
        print(f"user={user.name}({user.id})")
    login_info = get_login_info()
    player = get_player(client, user.id, year) if user else None

    week = int(request.args.get('w', ((today - startdate).days + 2) // 7 + 1))
    week = max(1, min(week, numWeeks))

    player_data = get_player_data(client, year)
    slots = [startdate + timedelta(days=7 * (week - 1) + d) for d in range(SLOTS_IN_WEEK)]

    modal_data = {}
    slot = int(request.args.get('s', 0))
    sub_id = request.args.get('id')
    if slot > 0 and sub_id:
        print("Someone is trying to accept a sub request.")
        if not user:
            print("Redirecting for login.")
            return redirect(url_for('login', next=request.url))
        else:
            print(f"Fetching Schedule Data for week {week} and slot {slot}.")
            # Fetch schedule data for the specific slot
            query = client.query(kind='Schedule')
            query.add_filter(filter=PropertyFilter('year', '=', year))
            query.add_filter(filter=PropertyFilter('week', '=', week))
            query.add_filter(filter=PropertyFilter('slot', '=', slot))
            sr = list(query.fetch())
            sub_name = next((p.name for p in player_data if p.id == sub_id), None)
            day = slots[slot - 1].strftime("%A, %b %d")
            if any(s['id'] == sub_id for s in sr):
                modal_data = {
                    'title': 'Confirmation',
                    'message': f"Please confirm you would like to sub for {sub_name} on {day}.",
                    'button': 'Confirm'
                }
            else:
                modal_data = {
                    'title': 'Notice',
                    'message': f"{sub_name} is not currently scheduled to play on {day}. It is possible someone else already accepted the substitution request.",
                    'button': 'Close',
                    'url': 'week'
                }
            print(modal_data)
            
    # Fetch schedule data for the week
    query = client.query(kind='Schedule')
    query.add_filter(filter=PropertyFilter('year', '=', year))
    query.add_filter(filter=PropertyFilter('week', '=', week))
    schedule_data = list(query.fetch())
    schedule_data.sort(key=lambda x: (x['slot'], x['position']))

    active = []
    if user and year == today.year:
        for s in schedule_data:
            if s['id'] == user.id and s['slot'] != 0:
                deadline = startdate + timedelta(days=(7 * (week - 1)) + (s['slot'] - 1))
                print(f"{datetime.now()} < {datetime(deadline.year, deadline.month, deadline.day, 18)}?")
                if datetime.now() < datetime(deadline.year, deadline.month, deadline.day, 18):
                    print(f"Adding an active slot.")
                    active.append(s['slot'])

    template_values = {
        'current_year': today.year,
        'year': year,
        'page': 'week',
        'week': week,
        'slot': slot,
        'sub_id': sub_id,
        'modal_data': modal_data,
        'numWeeks': numWeeks,
        'slots': slots,
        'schedule_data': schedule_data,
        'active': active,
        'player': player,
        'is_signed_up': player is not None,
        'login': login_info,
        'is_admin': user.admin if user else False,
    }

    return render_template('week.html', **template_values)

@app.route('/week', methods=['POST'])
def post_week():
    user = get_current_user()
    if not user:
        print("Redirecting for login.")
        return redirect(url_for('login', next=request.url))
    print(f"user={user.name}({user.id})")
    client = datastore.Client()
    now = datetime.now()
    year = now.year
    week = int(request.form.get('w'))
    slot = int(request.form.get('s'))
    sub_id = request.form.get('id')
    action = request.form.get('action')
    player = get_player(client, user.id, year)
    player_data = get_player_data(client, year)

    # Query for the current schedule
    query = client.query(kind='Schedule')
    query.add_filter(filter=PropertyFilter('year', '=', year))
    query.add_filter(filter=PropertyFilter('week', '=', week))
    sr = list(query.fetch())
    
    response_data = {'title': 'Error', 'message': "Something went wrong. Please contact the administrator.", 'button': 'Close'}
    if player:
        print(f"player={player.name}({player.id})")
    else:
        print(f"Current user ({user.name}) is not a registered player.")
        response_data = {'title': 'Error', 'message': "You have logged in with a Google account that has not registered for the league. Please logout and try a different Google account.", 'button': 'Close'}
    print(f"week={week}, slot={slot}, sub_id={sub_id}, action={action}")
    
    if action == "Sub" and player is not None:
        sub_id = user.id
        notification_list = [
            p.email for p in player_data 
            if p.id not in {s['id'] for s in sr if s['slot'] == slot or (s['slot'] == 0 and s['position'] == 0)}
        ]
        if any(x['id'] == sub_id and x['slot'] == slot for x in sr):
            subject = f"{player.name} needs a Sub"
            html = ("<p>{0} needs a sub on {1}. This email is sent to everyone not already scheduled to play on that date. "
                    "If you are an alternate for this match and can play, please click <a href='http://hpsandvolleyball.appspot.com/week?w={2}&s={3}&id={4}'>this link</a>. "
                    "If there are no alternates for this match, or the alternates haven't accepted the invitation, you can go ahead and click the link. The first to accept the invitation will get to play.</p>"
                    ).format(player.name, (startdate + timedelta(days=(7 * (week - 1) + (slot - 1)))).strftime("%A %m/%d"), week, slot, sub_id)
            print(subject)
            print(html)
            print(f"sending to: {notification_list}")
            send_email(subject, html, ["brian.bartlow@hp.com"] + [player.email] + notification_list)
            response_data = {'title': 'Success', 'message': 'Sub request sent successfully', 'button': 'Close'}
        else:
            response_data = {'title': 'Failure', 'message': "It looks like you're not scheduled to play on the day you're requesting a sub. You may need to contact the administrator.", 'button': 'Close'}

    if action == "Confirm" and player is not None:
        print("executing substitution.")
        swap_id = player.id if any(x['id'] == sub_id and x['slot'] == slot for x in sr) else None
        if swap_id:
            player_list = [x['id'] for x in sr if x['id'] != sub_id and x['slot'] == slot]
            # Delete old schedule for slot because the positions may change based on the elo score of the swapping player
            keys_to_delete = [x.key for x in sr if x['slot'] == slot or (x['id'] == swap_id and x['slot'] == 0)]
            client.delete_multi(keys_to_delete)
            player_list.append(swap_id)
            player_list = sorted(player_list, key=lambda player_id: next(p.elo_score for p in player_data if p.id == player_id), reverse=True)
            # Save new slot schedule
            for idx, player_id in enumerate(player_list):
                key = client.key('Schedule', f"year-{year}_player-{player_id}_week-{week}_slot-{slot}_position-{idx+1}")
                new_schedule = datastore.Entity(key=key)
                new_schedule.update({
                    'year': year,
                    'id': player_id,
                    'name': next(p.name for p in player_data if p.id == player_id),
                    'week': week,
                    'slot': slot,
                    'position': idx + 1
                })
                client.put(new_schedule)
            player_in = next(p for p in player_data if p.id == swap_id)
            player_out = next(p for p in player_data if p.id == sub_id)
            # Store the subbed out player in the schedule as an alternate
            key = client.key('Schedule', f"year-{year}_player-{player_out.id}_week-{week}_slot-{0}_position-{slot}")
            new_bye = datastore.Entity(key=key)
            new_bye.update({
                'year': year,
                'id': player_out.id,
                'name': player_out.name,
                'week': week,
                'slot': 0,
                'position': slot
            })
            client.put(new_bye)
            
            #notification_list = [player_in.email, player_out.email]
            notification_list = [p.email for p in player_data]
            subject = f"Substitution Notification: OUT-{player_out.name}, IN-{player_in.name}"
            html = f"<p>{player_in.name} has substituted in for {player_out.name}. Please keep an eye out for an updated calendar invitation.</p>"
            print(subject)
            print(html)
            print(f"sending to: {notification_list}")
            send_email(subject, html, notification_list)

            update_calendar_event(week, slot, player_out, player_in)
            
            response_data = {'title': 'Success', 'message': 'Substitution successful', 'button': 'Close & Reload', 'url': 'week'}
        else:
            response_data = {'title': 'Failure', 'message': 'Substitution unsuccessful. It is possible someone else already accepted the substitution request.', 'button': 'Close', 'url': 'week'}

    print(response_data)
    return jsonify(response_data)

@app.route('/day', methods=['GET'])
def get_day():
    client = datastore.Client()
    today = date.today()
    year = int(request.args.get('y', today.year))

    login_info = get_login_info()
    user = get_current_user()
    player = None
    if user:
        print(f"user={user.name}({user.id})")
        player = get_player(client, user.id, year)

    week = int(request.args.get('w', ((today - startdate).days + 2) // 7 + 1))
    week = max(1, min(week, numWeeks))
    day = int(request.args.get('d', today.weekday() + 1))
    day = 1 if day > 5 else day

    schedule_day = startdate + timedelta(days=(7 * (week - 1) + (day - 1)))

    # Fetch schedule data for the given week and day
    query = client.query(kind='Schedule')
    query.add_filter(filter=PropertyFilter('year', '=', year))
    query.add_filter(filter=PropertyFilter('week', '=', week))
    query.add_filter(filter=PropertyFilter('slot', '=', day))
    schedule_data = list(query.fetch())
    schedule_data.sort(key=lambda x: x['position'])

    games = len(schedule_data) > 0

    game_team = [[[], []] for _ in range(3)]
    for p in schedule_data:
        for x in range(3):
            game_team[x][ms[x][p['position'] - 1]].append(p['name'])

    # Fetch scores
    query = client.query(kind='Scores')
    query.add_filter(filter=PropertyFilter('year', '=', year))
    query.add_filter(filter=PropertyFilter('week', '=', week))
    query.add_filter(filter=PropertyFilter('slot', '=', day))
    sr = list(query.fetch())

    score = [['', ''] for _ in range(3)]
    if sr:
        for s in sr:
            score[s['game'] - 1][0] = s['score1']
            score[s['game'] - 1][1] = s['score2']

    is_today = (today == schedule_day)

    template_values = {
        'current_year': today.year,
        'year': year,
        'page': 'day',
        'week': week,
        'day': day,
        'games': games,
        'score': score,
        'numWeeks': numWeeks,
        'schedule_day': schedule_day.strftime('%m/%d/%Y') if request.headers.get('x-api-os') else schedule_day,
        'is_today': is_today,
        'game_team': game_team,
        'is_signed_up': player is not None,
        'login': login_info,
        'is_admin': user.admin if user else False,
    }

    if request.headers.get('x-api-os'):
        return jsonify(template_values)
    else:
        return render_template('day.html', **template_values)

@app.route('/day', methods=['POST'])
def post_day():
    client = datastore.Client()
    now = datetime.today()
    user = get_current_user()
    if user:
        print(f"user={user.name}({user.id})")
    player = get_player(client, user.id, now.year)

    year = int(request.form.get('y', now.year))
    week = int(request.form.get('w'))
    day = int(request.form.get('d'))

    # Fetch all scores for the given year, week, and day
    query = client.query(kind='Scores')
    query.add_filter(filter=PropertyFilter('year', '=', year))
    query.add_filter(filter=PropertyFilter('week', '=', week))
    query.add_filter(filter=PropertyFilter('slot', '=', day))
    scores = list(query.fetch())

    # Convert fetched scores to a dictionary for easy lookup
    scores_dict = {score['game']: score for score in scores}

    updates = []  # List to collect entities for bulk update

    if request.form.get('action') == "Scores":
        if player:
            print(f"{player.name} is entering scores.")

        # Process scores for the given week, day, and game
        for g in range(1, 4):
            score_key = client.key('Scores', f"year-{year}_week-{week}_slot-{day}_game-{g}")
            score_entity = scores_dict.get(g)  # Get the existing score entity if it exists

            score1 = int(request.form.get(f"score-{g}-1") or 0)
            score2 = int(request.form.get(f"score-{g}-2") or 0)

            if not score_entity:
                score_entity = datastore.Entity(key=score_key)  # Create a new entity if it doesn't exist

            # Set the properties
            score_entity.update({
                'year': year,
                'week': week,
                'slot': day,
                'game': g,
                'score1': score1,
                'score2': score2
            })

            updates.append(score_entity)
            print(f"Game {g}: {score1} - {score2}")

        # Perform a bulk put operation for all updated and new entities
        if updates:
            client.put_multi(updates)
            print("Scores updated or added successfully.")
            return jsonify(title='Success', message='Scores saved successfully', button='Close')

@app.route('/profile', methods=['GET'])
def get_profile():
    client = datastore.Client()
    now = datetime.today()
    year = now.year
    user = get_current_user()
    if not user:
        print("Redirecting for login.")
        return redirect(url_for('login', next=request.url))
    print(f"user={user.name}({user.id})")
    pid = request.args.get('pid', user.id)
    player = get_player(client, pid, year)
    if player is None:
        return redirect(url_for('signup_get'))

    weeks = []
    for week_index in range(numWeeks):
        week_dates = []
        for date_index in range(5):  # 5 workdays in a week
            date1 = startdate + timedelta(days=(7 * week_index) + date_index)
            week_dates.append((week_index, date_index, date1.strftime("%b %d")))
        weeks.append(week_dates)

    query = client.query(kind='Availability')
    query.add_filter(filter=PropertyFilter('year', '=', year))
    query.add_filter(filter=PropertyFilter('id', '=', pid))
    results = list(query.fetch())
    fto_week = [[False]*5 for _ in range(numWeeks)]
    for entity in results:
        week = entity['week'] - 1
        slot = entity['slot'] - 1
        fto_week[week][slot] = True

    template_values = {
        'year': year,
        'page': 'profile',
        'user': user,
        'player': player,
        'is_signed_up': player is not None,
        'login': get_login_info(),
        'weeks': weeks,
        'fto_week': fto_week,
        'is_admin': user.admin if user else False,
    }
    return render_template('profile.html', **template_values)

@app.route('/profile', methods=['POST'])
def post_profile():
    client = datastore.Client()
    year = datetime.today().year
    user = get_current_user()
    if user:
        print(f"user={user.name}({user.id})")
    pid = request.form.get('pid', user.id)
    player = get_player(client, pid, year)

    # Update player name and/or email
    new_name = request.form.get('name', player.name)
    new_email = request.form.get('email', player.email)
    player_entity = client.get(client.key('Player_List', f"year-{year}_player-{pid}"))
    player_entity.update({
        'name': new_name,
        'email': new_email
    })
    client.put(player_entity)

    # Fetch all existing entities for this year and player
    query = client.query(kind='Availability')
    query.add_filter(filter=PropertyFilter('year', '=', year))
    query.add_filter(filter=PropertyFilter('id', '=', pid))
    existing_entities = {f"{entity['week']}-{entity['slot']}": entity for entity in query.fetch()}

    to_put = []
    to_delete = []

    # Process form data and compare to existing entities
    for week in range(1, numWeeks + 1):
        for slot in range(1, 6):
            key_name = f"{week}-{slot}"
            form_value = request.form.get(key_name, "False") == "True"
            entity = existing_entities.get(key_name)

            if form_value and not entity:
                # Create new entity if needed
                key = client.key('Availability', f"year-{year}_player-{pid}_week-{week}_slot-{slot}")
                new_entity = datastore.Entity(key=key)
                new_entity.update({
                    'year': year,
                    'id': pid,
                    'name': player.name,
                    'week': week,
                    'slot': slot
                })
                to_put.append(new_entity)
            elif not form_value and entity:
                # Schedule for deletion if no longer needed
                to_delete.append(entity.key)

    # Perform batch operations
    if to_put:
        client.put_multi(to_put)
    if to_delete:
        client.delete_multi(to_delete)
    set_holidays(client, pid)

    return jsonify({'title': 'Success', 'message': 'Profile updated successfully', 'button': 'Close'})

@app.route('/info')
def info():
    """
    Renders the Info page.
    """
    user = get_current_user()
    if user:
        print(f"user={user.name}({user.id})")
    client = datastore.Client()
    year = get_year_string()

    template_values = {
        'year': year,
        'page': 'info',
        'login': get_login_info(),
        'is_signed_up': get_player(client, user.id, year) is not None if user else False,
        'is_admin': user.admin if user else False,
    }

    if request.headers.get('x-api-os'):
        return jsonify(template_values)
    else:
        return render_template('info.html', **template_values)

@app.route('/admin', methods=['GET'])
@admin_or_cron_required
def admin_get():
    client = datastore.Client()
    user = get_current_user()
    if user:
        print(f"user={user.name}({user.id})")
#    if user is None:
#        return redirect(url_for('login', next=request.url))
#    if not user.admin:
#        return redirect(url_for('main_page'))  # Redirect if not an admin

    today = date.today()
    year = int(request.args.get('y', today.year))
    week = int(request.args.get('w', (((today - startdate).days + 3) / 7) + 1))
    week = max(0, min(week, numWeeks))
    print(f"Admin page: week {week}")
    players = get_player_data(client, year, week)

    valid_emails = [player.email for player in players if player.email]
    email_list = ";".join(valid_emails)
    mailto_link = "mailto:" + email_list
    login_info = get_login_info()

    template_values = {
        'year': year,
        'player_list': players,
        'mailto_link': mailto_link,
        'page': 'admin',
        'is_signed_up': True,
        'login': login_info,
        'is_admin': user.admin if user else False,
    }

    return render_template('admin.html', **template_values)

@app.route('/admin', methods=['POST'])
@admin_or_cron_required
def admin_post():
    client = datastore.Client()
#    user = get_current_user()
#    if user is None:
#        return redirect(url_for('login', next=request.url))
#    if not user.admin:
#        return redirect(url_for('main_page'))  # Redirect if not an admin

    year = int(request.form.get('y', datetime.today().year))
    action = request.form.get('action', '')
    response_data = {'title': 'Success', 'button': 'Close', 'message': 'Success'}

    # Fetch all players from Player_List for the given year
    player_query = client.query(kind='Player_List')
    player_query.add_filter(filter=PropertyFilter('year', '=', year))
    players = list(player_query.fetch())

    if action == "Submit":
        entities_to_put = []
        for player in players:
            updated = False
            for field in ['name', 'email', 'elo_score', 'points', 'wins', 'games', 'points_per_game']:
                form_value = request.form.get(f"{field}-{player['id']}")
                if form_value and form_value != str(player.get(field, '')):
                    player[field] = type(player[field])(form_value)
                    updated = True
                    print(f"{player['name']} {field} = {type(player[field])}({form_value})")
            if updated:
                entities_to_put.append(player)
        if entities_to_put:
            client.put_multi(entities_to_put)
        response_data['message'] = 'Data saved successfully'

    elif action == "Holidays":
        # Generate keys and prepare entities for all potential holiday slots for all players
        to_put = []
        for player in players:
            for week, slot in holidays:
                key = client.key('Availability', f"year-{year}_player-{player['id']}_week-{week}_slot-{slot}")
                new_conflict = datastore.Entity(key=key)
                new_conflict.update({
                    'year': year,
                    'id': player['id'],
                    'name': player['name'],
                    'week': week,
                    'slot': slot
                })
                to_put.append(new_conflict)
#                print(f"Preparing to add/update holiday for {player['name']} in week {week} at slot {slot}.")
        if to_put:
            client.put_multi(to_put)
#            print(f"Processed {len(to_put)} entities in Datastore.")
        response_data['message'] = 'Holidays updated successfully'

    return jsonify(response_data)

@app.route('/import_csv', methods=['POST'])
@admin_or_cron_required
def import_csv():
#    user = get_current_user()
#    if user is None or not user.admin:
#        return redirect(url_for('main_page'))
    if 'file' not in request.files:
        flash('No file part')
        return redirect(request.url)
    file = request.files['file']
    if file.filename == '':
        flash('No selected file')
        return redirect(request.url)

    if file:
        filename = secure_filename(file.filename)
        filepath = os.path.join('/tmp', filename)
        file.save(filepath)

        kind = request.form.get('kind')
        entities = []
        client = datastore.Client()

        field_types = {
            'id': str,
            'year': int,
            'email': str,
            'name': str,
            'elo_score': int,
            'points': int,
            'wins': int,
            'games': int,
            'points_per_game': float,
            'week': int,
            'slot': int,
            'game': int,
            'position': int,
            'score1': int,
            'score2': int,
        }

        with open(filepath, newline='', encoding='utf-8') as csvfile:
            reader = csv.DictReader(csvfile)
            for row in reader:
                key = create_key_from_row(client, kind, row)
                entity = datastore.Entity(key=key)
                for field, value in row.items():
                    if field in field_types and value:
                        try:
                            converted_value = field_types[field](value)
                            entity[field] = converted_value
                        except ValueError:
                            entity[field] = value
                            flash(f"Value conversion error for {field} with value {value}", "error")
                    else:
                        entity[field] = value
                entities.append(entity)

        if entities:
            client.put_multi(entities)

        os.remove(filepath)
        flash('Data imported successfully')

    return redirect(url_for('admin_get'))
    
def create_key_from_row(client, kind, row):
    year = row['year']
    if kind == 'Player_List':
        player_id = row['id']
        key = client.key('Player_List', f"year-{year}_player-{player_id}")
    elif kind == 'Availability':
        player_id = row['id']
        week = row['week']
        slot = row['slot']
        key = client.key('Availability', f"year-{year}_player-{player_id}_week-{week}_slot-{slot}")
    elif kind == 'Schedule':
        player_id = row['id']
        week = row['week']
        slot = row['slot']
        position = row['position']
        key = client.key('Schedule', f"year-{year}_player-{player_id}_week-{week}_slot-{slot}_position-{position}")
    elif kind == 'Scores':
        week = row['week']
        slot = row['slot']
        game = row['game']
        key = client.key('Scores', f"year-{year}_week-{week}_slot-{slot}_game-{game}")
    else:
        # Default fallback, creates an incomplete key
        key = client.key(kind)
    return key

@app.route('/export_csv', methods=['POST'])
@admin_or_cron_required
def export_csv():
    user = get_current_user()
    if user is None or not user.admin:
        return redirect(url_for('main_page'))
    print(f"user={user.name}({user.id})")
    kind = request.form['kind']
    years_input = request.form.get('years', '')
    years = []
    client = datastore.Client()
    query = client.query(kind=kind)
    if years_input:
        for part in years_input.split(','):
            if '-' in part:
                start_year, end_year = map(int, part.split('-'))
                years.extend(range(start_year, end_year + 1))
            else:
                years.append(int(part))
        query.add_filter('year', 'IN', years)
    else:
        years_input = "ALL"
    entities = list(query.fetch())

    if entities:
        field_names = entities[0].keys()
        proxy = io.StringIO()
        writer = csv.DictWriter(proxy, fieldnames=field_names)

        writer.writeheader()
        for entity in entities:
            row = {key: str(value) for key, value in entity.items()}
            writer.writerow(row)

        proxy.seek(0)
        output = make_response(proxy.getvalue())
        output.headers["Content-Disposition"] = f"attachment; filename={kind}_export_{years_input}.csv"
        output.headers["Content-type"] = "text/csv"
        return output

    flash(f"No data available to export for {kind} in the selected years.", "warning")
    return redirect(url_for('admin_get'))

def parse_years(input_str):
    # This function needs to be robust to parse single years, lists of years, or ranges
    years = []
    for part in input_str.split(','):
        if '-' in part:
            start_year, end_year = map(int, part.split('-'))
            years.extend(range(start_year, end_year + 1))
        else:
            years.append(int(part))
    return years



###################
#
# Tasks
#
###################

def split_people_into_tiers(player_data, num_tiers):
    player_data.sort(key=lambda p: p.elo_score, reverse=True)
    tier_size = len(player_data) // num_tiers
    extra_players = len(player_data) % num_tiers

    # Create tiers using slice notation, extra players go into upper tiers
    tiers = [player_data[i * tier_size + min(i, extra_players):(i + 1) * tier_size + min(i + 1, extra_players)]
             for i in range(num_tiers)]

    # Calculate cutoff ELO scores for each midpoint boundary between tiers
    cutoff_elo_scores = [(tiers[i][-1].elo_score + tiers[i+1][0].elo_score) / 2 for i in range(num_tiers - 1)]

    # Adjust membership based on cutoffs and ELO margins
    for i, cutoff_elo in enumerate(cutoff_elo_scores):
        upper_bound = cutoff_elo + ELO_MARGIN
        lower_bound = cutoff_elo - ELO_MARGIN
        for person in player_data:
            elo = person.elo_score
            if lower_bound <= elo <= cutoff_elo and person not in tiers[i]:
                tiers[i].append(person)
            elif cutoff_elo < elo <= upper_bound and person not in tiers[i + 1]:
                tiers[i + 1].append(person)

    # Sort tiers again in case any players were added out of initial order
    for tier in tiers:
        tier.sort(key=lambda p: p.elo_score, reverse=True)

    return tiers

def find_valid_schedule(tiers, day_permutations):
    most_byes = -1
    valid_schedule = None
    playing_people = []
    
    random.shuffle(day_permutations)
    for day_perm in day_permutations:
        tier_perms = list(permutations(tiers))
        random.shuffle(tier_perms)  # Shuffle tier permutations before iterating
        for tier_perm in tier_perms:
            slot_to_tier_mapping = dict(zip(day_perm, [list(tier) for tier in tier_perm]))
            bye_count = 0
            invited_people_by_slot = {}
            valid = True

            for slot, tier in slot_to_tier_mapping.items():
                available_people = [person for person in tier if slot not in person.conflicts]
                if len(available_people) < PLAYERS_PER_GAME:
                    valid = False
                    break
                else:
                    random.shuffle(available_people)
                    available_people.sort(key=lambda x: x.byes, reverse=True)
                    invited_people = []
                    for j in range(8):
                        invited_people.append(available_people[j])
                        for other_slot, other_tier in slot_to_tier_mapping.items():
                            if other_slot != slot and available_people[j] in other_tier:
                                other_tier.remove(available_people[j])
                        bye_count += available_people[j].byes
                    invited_people_by_slot[slot] = invited_people

            if valid and bye_count > most_byes:
                print(f"Found a better schedule. Most byes was {most_byes} and is now {bye_count}.")
                valid_schedule = slot_to_tier_mapping
                playing_people = invited_people_by_slot
                most_byes = bye_count

    for slot in playing_people:
        playing_people[slot].sort(key=lambda p: p.elo_score, reverse=True)

    return valid_schedule, playing_people

@app.route('/tasks/scheduler', methods=['GET'])
@admin_or_cron_required
def Scheduler():
    # This will run on Fridays to create the schedule for the next week
    # Filter for this year only
    today = date.today()
    year = today.year

    # Calculate what week# next week will be
    week = int(request.args.get('w', ((today - startdate).days + 3) // 7 + 1))
    week = max(1, week)
    if week > numWeeks:
        return
    print("Week %s Scheduler" % week)
    
    client = datastore.Client()
    temp_player_data = get_player_data(client, year, week) #player_data is a list of Player objects
    
    # Need to check for existing scores for this week. If there are scores for this week, we should abort.
    query = client.query(kind='Scores')
    query.add_filter(filter=PropertyFilter('year', '=', year))
    query.add_filter(filter=PropertyFilter('week', '=', week))
    results = list(query.fetch(limit=1))
    if results:
        print("There are scores in the system for this week. Aborting.")
        return

    # Create a list of players ids on bye this week because of FTO
    bye_list = []
    player_data = []
    
    for player in temp_player_data:
        if len(player.conflicts) < SLOTS_IN_WEEK:
            player_data.append(player)
        else:
            bye_list.append(player)
            print(f"{player.name} is on a bye this week.")
    
    # number of players not on a full week bye
    num_available_players = len(player_data)
    schedule = None
    valid_schedule_found = False
    for num_tiers in range(min(num_available_players // PLAYERS_PER_GAME, 5), 0, -1):
        #Not sure if we need to sort by ELO score every time through...
        player_data.sort(key=lambda p: p.elo_score, reverse=True)
        print("Trying to find a schedule with %d tiers." % num_tiers)
        tiers = split_people_into_tiers(player_data, num_tiers)
    
        day_permutations = list(permutations(range(1, SLOTS_IN_WEEK+1), num_tiers))
        schedule, playing_people = find_valid_schedule(tiers, day_permutations)
        if schedule:
            valid_schedule_found = True
            break
        
    # Check if there's a valid schedule and save it to Datastore
    if valid_schedule_found:
        print(f"We have a valid schedule!")
        # Delete any existing schedule for this week
        query = client.query(kind='Schedule')
        query.add_filter(filter=PropertyFilter('year', '=', year))
        query.add_filter(filter=PropertyFilter('week', '=', week))
        existing_schedules = list(query.fetch())

        # Use batch operation to delete existing schedules
        if existing_schedules:
            keys_to_delete = [entity.key for entity in existing_schedules]
            client.delete_multi(keys_to_delete)

        # Store the bye players in the database
        entities_to_store = []
        for player in bye_list:
            print(f"Adding {player.name} to the bye slot.")
            key = client.key('Schedule', f"year-{year}_player-{player.id}_week-{week}_slot-0_position-0")
            entity = datastore.Entity(key=key)
            entity.update({
                'year': year,
                'id': player.id,
                'name': player.name,
                'week': week,
                'slot': 0,
                'position': 0  # Using position 0 to denote bye
            })
            entities_to_store.append(entity)

        # Store the scheduled players and alternates
        for slot, tier in schedule.items():
            name_list = []
            email_list = []
            print(f"Slot {slot}")
            for player in tier:
                s = slot if player in playing_people[slot] else 0
                p = playing_people[slot].index(player)+1 if player in playing_people[slot] else slot
                key = client.key('Schedule', f"year-{year}_player-{player.id}_week-{week}_slot-{s}_position-{p}")
                entity = datastore.Entity(key=key)
                entity.update({
                    'year': year,
                    'id': player.id,
                    'name': player.name,
                    'week': week,
                    'slot': s,
                    'position': p
                })
                if player in playing_people[slot]:
                    name_list.append(player.name)
                    email_list.append(player.email)
                print(f"  {player.name} ({s}-{p})")
                entities_to_store.append(entity)

            
            if SEND_INVITES:

                # Calculate the date for this match
                match_date = startdate + timedelta(days=(7 * (week - 1) + (slot - 1)))
                start_time = datetime.combine(match_date, time(12, 0, 0))
                end_time = datetime.combine(match_date, time(13, 0, 0))
                # Convert times to America/Boise time zone
                #boise_tz = pytz.timezone('America/Boise')
                #start_time_boise = boise_tz.localize(start_time)
                #end_time_boise = boise_tz.localize(end_time)

                event = {
                    #'id': event_id,
                    'summary': f" Week {week} Sand Volleyball Match",
                    'location': 'N/S Sand Court',
                    'description': f"This is an automated invitation to your Week {week} Sand Volleyball Match. If you cannot make the match, DO NOT DECLINE. Instead, please go to https://hpsandvolleyball.appspot.com/week (make sure you are logged in) and click the \"I need a sub\" button. Once someone accepts the sub request, a new invitation will be sent and your invitation will be removed.",
                    'start': {
                        'dateTime': start_time.isoformat('T'),
                        'timeZone': 'America/Boise',
                    },
                    'end': {
                        'dateTime': end_time.isoformat('T'),
                        'timeZone': 'America/Boise',
                    },
                    'attendees': [{'email': e} for e in email_list],
                    'reminders': {
                        'useDefault': True,
                    },
                }
                try:
                    event = calendar_service.events().insert(
                        calendarId='aidl2j9o0310gpp2allmil37ak@group.calendar.google.com', 
                        body=event, 
                        sendNotifications=True
                    ).execute()
                    print(f"Event created: {event.get('htmlLink')}")
                except HttpError as e:
                    print(f"Failed to create event: {e.content.decode('utf-8')}")
                    print(f"Request body: {event}")
        
        # Use batch operation to put all new and updated entities
        if entities_to_store:
            print(f"Writing {len(entities_to_store)} entities to Schedule.")
            client.put_multi(entities_to_store)
    else:
        print("No valid schedule found. <sad face>")
    
    return render_template('scheduler.html')

@app.route('/tasks/elo')
@admin_or_cron_required
def elo():
    client = datastore.Client()
    today = date.today()
    year = today.year
    kfactor = 400  # Update the K-factor as per year specifics if necessary

    # Calculate which week it currently is, ensuring it falls within the allowed range
    week = int(request.args.get('w', ((today - startdate).days + 3) // 7 + 1))
    if 1 < week <= numWeeks+1:
        print("Week %s Elo Update" % week)

        # Fetch player data for the given year
        player_data = get_player_data(client, year)

        # Create a query for the schedule for the previous week
        query = client.query(kind='Schedule')
        query.add_filter('year', '=', year)
        query.add_filter('week', '=', week - 1)
        schedule_results = list(query.fetch())
        schedule_results.sort(key=lambda x: (x['slot'], x['position']))

        if schedule_results:
            player_elo = {player.id: player.elo_score for player in player_data}

            team_elo = [[[0, 0] for _ in range(3)] for _ in range(SLOTS_IN_WEEK)]  # List for Elo scores for each team in 3 games across all slots
            game_scores = [[[0, 0] for _ in range(3)] for _ in range(SLOTS_IN_WEEK)]  # Scores for each game across all slots
            player_count = [0] * SLOTS_IN_WEEK  # Player count for each slot

            # Calculate the average Elo scores for each team
            for p in schedule_results:
                if p['slot'] > 0:  # Ensure we're dealing with valid slots
                    slot_index = p['slot'] - 1  # Convert slot to 0-based index for list access
                    player_count[slot_index] += 1
                    for game_index in range(3):  # Three games per slot
                        # Add player's Elo to the appropriate team in the appropriate game
                        team_index = ms[game_index][p['position'] - 1]  # Determine team based on ms mapping
                        team_elo[slot_index][game_index][team_index] += float(player_elo[p['id']])

            # Compute average Elo scores by dividing total Elo by half the player count (since two teams)
            for slot_index in range(SLOTS_IN_WEEK):
                for game_index in range(3):
                    for team_index in range(2):
                        if player_count[slot_index] > 0:  # Only calculate if there are players
                            team_elo[slot_index][game_index][team_index] /= (player_count[slot_index] / 2)

            query = client.query(kind='Scores')
            query.add_filter('year', '=', year)
            query.add_filter('week', '=', week - 1)
            results = list(query.fetch())
            results.sort(key=lambda x: (x['slot'], x['game']))
            if results:
                for score in results:
                    game_scores[score['slot']-1][score['game'] - 1][0] = float(score.get('score1', 0))
                    game_scores[score['slot']-1][score['game'] - 1][1] = float(score.get('score2', 0))

            # Now iterate through each player and calculate their new Elo score based on
            # their old Elo score, the game scores, and the teams' average Elo scores.

            # Initialize dictionaries to hold the updated player stats
            new_elo = {p.id: p.elo_score for p in player_data}
            new_points = {p.id: p.points for p in player_data}
            new_wins = {p.id: p.wins for p in player_data}
            new_games = {p.id: p.games for p in player_data}

            # Iterate through each scheduled match to update player stats
            for s in schedule_results:
                if s['slot'] > 0:  # Ensure the player is not on a bye
                    slot_index = s['slot'] - 1
                    for g in range(3):  # Iterate through each game
                        player_id = s['id']
                        player = next((p for p in player_data if p.id == player_id), None)
                        if player:
                            team_index = ms[g][s['position'] - 1]
                            my_team_elo = team_elo[slot_index][g][team_index]
                            other_team_elo = team_elo[slot_index][g][1 - team_index]
                            my_team_score = game_scores[slot_index][g][team_index]
                            other_team_score = game_scores[slot_index][g][1 - team_index]
                            
                            if my_team_score > 0 or other_team_score > 0:
                                # Calculate the ELO change
                                expected_score_ratio = my_team_elo / (my_team_elo + other_team_elo)
                                actual_score_ratio = my_team_score / (my_team_score + other_team_score)
                                elo_change = round(kfactor * (actual_score_ratio - expected_score_ratio))
                                new_elo[player_id] += elo_change
                                new_games[player_id] += 1

                                # Print diagnostic information
                                print(f"{player.name} - {my_team_score} vs {other_team_score}")
                                print(f"{player.name}'s Elo score ({player.elo_score}) is now {new_elo[player_id]}")

                                # Update points and wins if this player's team won
                                if my_team_score > other_team_score:
                                    new_points[player_id] += other_team_elo
                                    new_wins[player_id] += 1
                                    print(f"{player.name}'s new points total is {new_points[player_id]}.")
            updates = []
            for player in player_data:
                if player.id in new_elo:
                    print(f"{player.name} - {new_elo[player.id]} - ")
                    key = client.key('Player_List', f"year-{year}_player-{player.id}")
                    entity = datastore.Entity(key=key)
                    entity.update({
                        'year': year,
                        'id': player.id,
                        'name': player.name,
                        'email': player.email,
                        'elo_score': new_elo[player.id],
                        'points': int(new_points[player.id]),
                        'wins': new_wins[player.id],
                        'games': new_games[player.id],
                        'points_per_game': round(float(new_points[player.id]/new_games[player.id]), 1) if new_games[player.id] > 0 else 0
                    })
                    updates.append(entity)
            # Perform a batch put operation to save the changes
            if updates:
                client.put_multi(updates)
    return render_template('scheduler.html')



@app.route('/tasks/notify')
@admin_or_cron_required
def notify():
    client = datastore.Client()
    today = date.today()
    year = today.year

    week = int(((today - startdate).days + 2)/ 7 + 1)
    day = today.isoweekday()

    to = ["brian.bartlow@hp.com"]
    subject = "Please Ignore"
    html = "<p>Please ignore this email.</p><p>I am testing new functionality on the website.</p>"

    player_data = get_player_data(client, year)

    sendit = True
    notification_list = []

    request_type = request.args.get('t')

    if request_type == "score":
        schedule_data = get_schedule_data(client, year, week, day)
        if schedule_data:
            sr = get_score_count(client, year, week, day)
            if sr < 3:  
                subject = "Reminder to submit scores"
                html = """<p>At the moment this email was generated, the scores haven't been entered for today's games. Please go to the <a href=\"http://hpsandvolleyball.appspot.com/day\">Score Page</a> and enter the scores. If someone has entered the scores by the time you check, or the games were not actually played, please disregard this reminder.</p>"""
                notification_list = [p.email for p in player_data if any(s['id'] == p.id for s in schedule_data)]
            else:
                sendit = False
        else:
            sendit = False

    elif request_type == "availability" and 0 <= week < numWeeks:
        subject = "Reminder to check and update your Availability/Conflicts for next week"
        html = """<p>Next week's schedule will be generated at 2:00pm. If there are any days next week where you cannot play at noon and possibly a bit beyond 1:00, please go to the <a href=\"http://hpsandvolleyball.appspot.com/profile\">Profile Page</a> and make sure those days are shown as unavailable (red).
        If that link doesn't work, please verify you are logged in with the Google account used when you signed up. Log in, then click on the Profile link at the top of the page. Then click the day(s) for any days that you cannot play and make sure they are red. NOTE: For those involved in the team league, this includes days you are scheduled to play with your team.</p>"""
        notification_list = [p.email for p in player_data if p.email]
        
    elif request_type == "test":
        sendit = False
        week = 2
        slot = 5
        email_list = ["brian.bartlow@hp.com"]
        match_date = startdate + timedelta(days=(7 * (week - 1) + (slot - 1)))
        start_time = datetime.combine(match_date, time(12, 0))
        end_time = datetime.combine(match_date, time(13, 0))

        # Create the calendar event
        event = {
            'summary': f"Week {week} Sand VolleyBall Match",
            'location': 'N/S Sand Court',
            'description': f"Week {week} Sand Volleyball Match",
            'start': {
                'dateTime': start_time.isoformat('T'),
                'timeZone': 'America/Boise',
            },
            'end': {
                'dateTime': end_time.isoformat('T'),
                'timeZone': 'America/Boise',
            },
            'attendees': [{'email': e} for e in email_list],
            'reminders': {
                'useDefault': True,
            },
        }

        # Insert the event
        event = calendar_service.events().insert(calendarId='aidl2j9o0310gpp2allmil37ak@group.calendar.google.com', body=event, sendNotifications=True).execute()
        return "Test calendar created."
        
    elif request_type == "sub":
        update_calendar_event(2, 5, Player({'email': 'brian.bartlow@hp.com', 'name': 'Brian Bartlow'}), Player({'email': 'brian.bartlow@gmail.com', 'name': 'Brian Bartlow'}))
        return "Substitution executed."
    
    elif request_type == "smtp":
        sendit = False
        subject = "Test Email from the App"
        body = "This is the body of the email. If you can read this, it worked!"
        recipient = "brian.bartlow@hp.com"
        return send_email_via_SMTP(subject, body, recipient)

    
    if sendit:
        for e in notification_list:
            to.append(str(e))
        send_email(subject, html, to)

    return "Notification sent successfully."

def get_schedule_data(client, year, week, day):
    # Fetch schedule data from Datastore for the specified year, week, and day
    qry = client.query(kind='Schedule')
    qry.add_filter('year', '=', year)
    qry.add_filter('week', '=', week)
    qry.add_filter('slot', '=', day)
    return list(qry.fetch())

def get_score_count(client, year, week, day):
    # Fetch score count from Datastore for the specified year, week, and day
    qry = client.query(kind='Scores')
    qry.add_filter('year', '=', year)
    qry.add_filter('week', '=', week)
    qry.add_filter('slot', '=', day)
    return len(list(qry.fetch()))


@app.route('/calendar', methods=['GET'])
@admin_or_cron_required
def calendar_get():
    user = get_current_user()
    if not user:
        print("Redirecting for login.")
        return redirect(url_for('login', next=request.url))

    calendar_id = 'aidl2j9o0310gpp2allmil37ak@group.calendar.google.com'

    try:
        calendar = calendar_service.calendars().get(calendarId=calendar_id).execute()
        events_result = calendar_service.events().list(calendarId=calendar_id).execute()
        events = events_result.get('items', [])

        for event in events:
            start_time = event.get('start', {}).get('dateTime') or event.get('start', {}).get('date')
            start_year = str(datetime.fromisoformat(start_time).year) if start_time else 'Unknown'
            event['start_year'] = start_year

        calendar['events'] = events

        template_values = {
            'calendars': [calendar],
            'login': get_login_info(),
            'is_admin': user.admin if user else False,
        }

        return render_template('calendar.html', **template_values)

    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/calendar', methods=['POST'])
@admin_or_cron_required
def calendar_post():
    user = get_current_user()
    if not user:
        print("Redirecting for login.")
        return redirect(url_for('login', next=request.url))

    calendar_id = request.form.get('calendar_id')
    event_id = request.form.get('event_id')
    action = request.form.get('action', '')

    if action == "Cancel" and calendar_id and event_id:
        try:
            calendar_service.events().delete(calendarId=calendar_id, eventId=event_id, sendUpdates='all').execute()
            response_data = {'title': 'Success', 'message': 'Event cancelled successfully', 'button': 'Close'}
        except Exception as e:
            response_data = {'title': 'Error', 'message': str(e), 'button': 'Close'}
    else:
        response_data = {'title': 'Error', 'message': 'Invalid action', 'button': 'Close'}

    return jsonify(response_data)




if __name__ == '__main__':
    app.run()
