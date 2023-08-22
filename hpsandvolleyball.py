import os
import urllib
import datetime
import logging
import string
import math
import random
import sys
import json
import re
import itertools

# For Google Calendar
from apiclient.discovery import build

from google.appengine.api import users
from google.appengine.ext import ndb

import jinja2
import webapp2

from google.appengine.api import mail

# Globals - I want these eventually to go into a datastore per year so things can be different and configured per year.
# For now, hard-coded is okay.
numWeeks = 6
startdate = datetime.date(2023, 8, 28)
holidays = ()  # ((week,slot),(week,slot),(week,slot)) - Memorial Day, Independence Day, BYITW Day
PLAYERS_PER_GAME = 8
SLOTS_IN_WEEK = 5
ELO_MARGIN = 75

SEND_INVITES = False
# How to team up the players for each of the three games
ms = ((0, 1, 0, 1, 1, 0, 1, 0), (0, 1, 1, 0, 0, 1, 1, 0), (0, 1, 1, 0, 1, 0, 0, 1))

random.seed(datetime.datetime.now())

JINJA_ENVIRONMENT = jinja2.Environment(
    loader=jinja2.FileSystemLoader(os.path.dirname(__file__)),
    extensions=['jinja2.ext.autoescape'],
    autoescape=True)


def db_key(db_name):
    """
    Constructs a Datastore key for a player list.
    We use the list name as the key.
    """
    return ndb.Key("Entries", db_name)


def get_login_info(h):
    user = users.get_current_user()
    if user:
        logged_in = True
        url = users.create_logout_url(h.request.uri)
        linktext = 'Logout'
    else:
        logged_in = False
        url = users.create_login_url(h.request.uri)
        linktext = 'Login'
    info = {
        'logged_in': logged_in,
        'url': url,
        'linktext': linktext,
    }
    return info


def get_year_string():
    now = datetime.datetime.utcnow()
    return now.strftime("%Y")


def get_player(x, pid=None, year=datetime.datetime.today().year):
    # Get committed entries list
    get_login_info(x)
    user = users.get_current_user()
    result = None
    if pid is None:
        if user:
            pid = user.user_id()
    if pid is not None:
        qry = Player_List.query(ancestor=db_key(year))
        qry = qry.filter(Player_List.id == pid)
        result = qry.get()
    return result


def set_holidays(x):
    # Check and set holidays to unavailable
    now = datetime.datetime.today()
    year = now.year
    get_login_info(x)
    user = users.get_current_user()
    player = get_player(x)
    if player:
        qry_f = Fto.query(ancestor=db_key(year))
        qry_f = qry_f.filter(Fto.user_id == user.user_id())
        fto_data = qry_f.fetch(100)
        for week_slot in holidays:
            fto = Fto(parent=db_key(year))
            fto.user_id = user.user_id()
            fto.name = player.name
            fto.week = week_slot[0]
            fto.slot = week_slot[1]

            match_found = False
            for fto_entry in fto_data:
                if fto_entry == fto:
                    match_found = True
            if match_found is False:
                fto.put()


def get_player_data(current_week, self):
    now = datetime.datetime.today()
    year = now.year
    pl = []
    fto_count = {}
    # Get player list
    qry = Player_List.query(ancestor=db_key(year))
    qry = qry.order(Player_List.schedule_rank)
    plr = qry.fetch(100)
    for player in plr:
        p = Player()
        p.id = player.id
        p.name = player.name
        p.email = player.email
        p.phone = player.phone
        p.rank = player.schedule_rank
        p.score = player.elo_score
        p.points = player.points
        p.wins = player.wins
        p.games = player.games
        p.points_per_game = player.points_per_game
        p.conflicts = []
        pl.append(p)

        # Need a dict of lists to count the conflicts for each week per player. Initialized with zeros.
        fto_count[player.id] = [0] * numWeeks

    # Check previous schedules for byes or alternates
    if current_week > 1:
        qry = Schedule.query(ancestor=db_key(year))
        qry = qry.filter(Schedule.slot == -1)
        past_byes = qry.fetch()
        if past_byes:
            bye_weeks = set()
            for entry in past_byes:
                if entry.slot == 0:
                    bye_weeks.add((entry.id, entry.week))
                
            for p in pl:
                p.byes = sum(1 for (id, _) in bye_weeks if id == p.id)
            
    # Check future fto for byes
    qry = Fto.query(ancestor=db_key(year))
    fto = qry.fetch()
    if fto:
        for f in fto:
            if f.week > current_week and f.user_id in fto_count:
                fto_count[f.user_id][f.week - 1] += 1
                if fto_count[f.user_id][f.week - 1] == SLOTS_IN_WEEK: # Once we reach 5 conflicts in a week, that's a bye week.
                    for player in pl:
                        if player.id == f.user_id:
                            player.byes += 1
                            break
            # To make things easy, we can populate the weekly conflicts while iterating through the fto list.
            if f.week == current_week and f.user_id in fto_count:
                for player in pl:
                    if player.id == f.user_id:
                        player.conflicts.append(f.slot)
                        break

    return pl


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


def remove_conflicts(player_ids, player_data, self, count=1):
    if count > 20:
        return []
    slots = range(1, 6)
    y = 0
    for p in player_ids:
        y += 1
        if y > 8:
            break  # use the data from the first 8 players in the tier
        for s in player_data[p].conflicts:
            if s in slots:
                slots.remove(s)
        # for z in player_ids:
        # logging.info(" %s - %s" % (player_data[z].name, player_data[z].conflicts))
    # If there are no valid slots for this group of 8 to play,
    # and there are more than 8 people in the tier,
    # randomly shuffle the players and try again.
    if (len(slots) == 0) and (len(player_ids) > 8):
        logging.info("No slot for this tier, shuffling and trying again. Count=%s" % count)
        random.shuffle(player_ids)
        return remove_conflicts(player_ids, player_data, self, count + 1)
    random.shuffle(slots)  # randomize the order of the available slots
    return slots

# Define a function to get the list of workdays in a week
def get_workdays(start_date):
    workdays = []
    current_date = start_date
    for _ in range(5):  # Five workdays in a week (Mon to Fri)
        workdays.append(current_date)
        current_date += datetime.timedelta(days=1)
    return workdays

def links2text(html_content):
    link_pattern = re.compile(r'<a[^>]*href="([^"]*)"[^>]*>(.*?)<\/a>', re.IGNORECASE)
    converted_text = re.sub(link_pattern, r'\2 (\1)', html_content)
    return converted_text

class Player(object):
    def __init__(self):
        self.id = None
        self.name = None
        self.email = None
        self.phone = None
        self.rank = None
        self.score = 1000
        self.byes = 0
        self.conflicts = []
        self.points = 0
        self.wins = 0
        self.games = 0
        self.points_per_game = 0


class Fto(ndb.Model):
    """
    A model for storing conflicting days per player.
    """
    user_id = ndb.StringProperty(indexed=True)
    week = ndb.IntegerProperty(indexed=True)
    slot = ndb.IntegerProperty(indexed=True)
    name = ndb.StringProperty(indexed=True)

    def __eq__(self, other):
        """Overrides the default implementation"""
        if isinstance(self, other.__class__):
            return self.user_id == other.user_id and self.week == other.week and self.slot == other.slot
        return NotImplemented


class Schedule(ndb.Model):
    """
    A model for tracking the weekly and daily schedule
    """
    id = ndb.StringProperty(indexed=True)
    name = ndb.StringProperty(indexed=True)
    week = ndb.IntegerProperty(indexed=True)
    slot = ndb.IntegerProperty(indexed=True)
    tier = ndb.IntegerProperty(indexed=True)
    position = ndb.IntegerProperty(indexed=True)


class Player_List(ndb.Model):
    """
    A model for tracking the ordered list for scheduling
    """
    id = ndb.StringProperty(indexed=True)
    email = ndb.StringProperty(indexed=False)
    name = ndb.StringProperty(indexed=True)
    phone = ndb.StringProperty(indexed=False)
    schedule_rank = ndb.IntegerProperty(indexed=True)
    elo_score = ndb.IntegerProperty(indexed=True)
    points = ndb.IntegerProperty(indexed=True)
    wins = ndb.IntegerProperty(indexed=True)
    games = ndb.IntegerProperty(indexed=True)
    points_per_game = ndb.FloatProperty(indexed=True)


class AddRandomPlayers(webapp2.RequestHandler):
    def get(self):
        num_players = 20  # Number of random players to add

        for i in range(num_players):
            player = Player_List(parent=db_key(datetime.datetime.now().year))
            player.id = ''.join(random.choice(string.digits) for _ in range(10))  # Random player ID
            player.name = "Player"+str(i+1)
            player.email = player.name+"@example.com"
            player.schedule_rank = i+1
            player.elo_score = random.randint(700, 1300)  # Random ELO score
            player.points = random.randint(0, 0)
            player.games = random.randint(0, 0)
            player.wins = random.randint(0, player.games)
            player.points_per_game = player.points / player.games if player.games > 0 else 0

            player.put()  # Save the player to the database

        logging.info("Added %d random players to the database." % num_players)


class Scores(ndb.Model):
    """
    A model for tracking game scores
    """
    week = ndb.IntegerProperty(indexed=True)
    tier = ndb.IntegerProperty(indexed=True)
    slot = ndb.IntegerProperty(indexed=True)
    game = ndb.IntegerProperty(indexed=True)
    score1 = ndb.IntegerProperty(indexed=False)
    score2 = ndb.IntegerProperty(indexed=False)


class MainPage(webapp2.RequestHandler):
    """
    Reads the database and creates the data for rendering the signup list
    """

    def get(self):
        # Filter for this year only
        datetime.datetime.today()

        # See if user is logged in and signed up
        login_info = get_login_info(self)
        user = users.get_current_user()
        player = get_player(self)
        template_values = {
            'year': get_year_string(),
            'page': 'mainpage',
            'user': user,
            'is_signed_up': player is not None,
            'player': player,
            'login': login_info,
            'is_admin': users.is_current_user_admin(),
        }

        os = self.request.headers.get('x-api-os')
        if os is not None:
            json_data = json.dumps(template_values, indent=4)
            self.response.write(json_data)
        else:
            template = JINJA_ENVIRONMENT.get_template('mainpage.html')
            self.response.write(template.render(template_values))


class Signup(webapp2.RequestHandler):
    """
    Manages adding a new player to the signup list for this season.
    """

    def post(self):
        user = users.get_current_user()
        now = datetime.datetime.today()
        # get the number of currently signed up players

        if user:
            player = Player_List(parent=db_key(now.year))
            player.id = user.user_id()
            player.name = self.request.get('name')
            player.email = self.request.get('email')
            player.schedule_rank = int(self.request.get('count'))
            
             # Check previous years back to 2016 for player's ELO score
            player.elo_score = 800  # Default ELO score if no previous data found

            for year in range(now.year - 1, 2015, -1):  # Iterate from (current year - 1) to 2016
                tp = get_player(self, player.id, year)
                if tp and tp.elo_score:  # Check if player and ELO score data exist for that year
                    player.elo_score = int((tp.elo_score + 1000) / 2)
                    break  # If data found, stop checking previous years
                    
            if player.name == "":
                player.name = user.nickname()
            if player.email == "":
                player.email = user.email()
            player.points = 0
            player.games = 0
            player.wins = 0
            player.points_per_game = 0

            if self.request.get('action') == "Commit":
                player.put()
            set_holidays(self)
        self.redirect('signup')

    def get(self):
        now = datetime.datetime.today()
        today = datetime.date.today()
        week = int(math.floor(int(((today - startdate).days) + 3) / 7) + 1)

        # Get committed entries list
        qry_p = Player_List.query(ancestor=db_key(now.year))
        qry_p = qry_p.order(Player_List.name)
        player_list = qry_p.fetch(100)

        # See if user is logged in and signed up
        login_info = get_login_info(self)
        user = users.get_current_user()
        player = get_player(self)

        qry = Schedule.query(ancestor=db_key(now.year))
        active_schedule = qry.count() > 0

        template_values = {
            'year': get_year_string(),
            'week': week,
            'page': 'signup',
            'user': user,
            'player_list': player_list,
            'is_signed_up': player is not None,
            'active_schedule': active_schedule,
            'player': player,
            'login': login_info,
            'is_admin': users.is_current_user_admin(),
        }

        os = self.request.headers.get('x-api-os')
        if os is not None:
            json_data = json.dumps(template_values, indent=4)
            self.response.write(json_data)
        else:
            template = JINJA_ENVIRONMENT.get_template('signup.html')
            self.response.write(template.render(template_values))


class Unsignup(webapp2.RequestHandler):
    """
    Manages removing the current logged in user from the signup
    sheet (for this season).
    """

    def post(self):
        user = users.get_current_user()
        if user:
            now = datetime.datetime.today()
            qry = Player_List.query(ancestor=db_key(now.year))
            player_list = qry.fetch(100)
            for player in player_list:
                if player.id == user.user_id():
                    player.key.delete()
        self.redirect('signup')


class Info(webapp2.RequestHandler):
    """
    Renders Info page
    """

    def get(self):
        login_info = get_login_info(self)
        template_values = {
            'year': get_year_string(),
            'page': 'info',
            'login': login_info,
            'is_signed_up': get_player(self) is not None,
            'is_admin': users.is_current_user_admin(),
        }

        os = self.request.headers.get('x-api-os')
        if os is not None:
            json_data = json.dumps(template_values, indent=4)
            self.response.write(json_data)
        else:
            template = JINJA_ENVIRONMENT.get_template('info.html')
            self.response.write(template.render(template_values))


class Ftolog(webapp2.RequestHandler):
    """
    Renders Log page (hidden)
    """

    def get(self):
        now = datetime.datetime.today()
        player = get_player(self)
        if player:
            pass
        else:
            self.redirect('/')
        qry = Fto.query(ancestor=db_key(now.year))
        qry = qry.order(Fto.name).order(Fto.week, Fto.slot)
        entries = qry.fetch()

        login_info = get_login_info(self)
        template_values = {
            'year': get_year_string(),
            'page': 'log',
            'login': login_info,
            'entries': entries,
            'is_signed_up': player is not None,
            'is_admin': users.is_current_user_admin(),
        }

        os = self.request.headers.get('x-api-os')
        if os is not None:
            json_data = json.dumps(template_values, indent=4)
            self.response.write(json_data)
        else:
            template = JINJA_ENVIRONMENT.get_template('ftolog.html')
            self.response.write(template.render(template_values))


class Availability(webapp2.RequestHandler):
    """
    Renders Schedule page
    """

    def post(self):
        now = datetime.datetime.today()
        year = now.year

        get_login_info(self)
        user = users.get_current_user()

        if self.request.get('pid'):
            pid = self.request.get('pid')
        else:
            pid = user.user_id()
        player = get_player(self, pid)

        qry_f = Fto.query(ancestor=db_key(now.year))
        qry_f = qry_f.filter(Fto.user_id == pid)
        fto_data = qry_f.fetch(100)

        # Add new slot entries by iterating through the form
        for week in range(1, numWeeks + 1):
            for slot in range(1, 5+1):
                cell_name = str(week)+"-"+str(slot)
#                logging.info(cell_name)
                cell_value = self.request.get(cell_name)
#                logging.info(cell_value)
                if cell_value == "True":
                    fto = Fto(parent=db_key(year))
                    fto.user_id = pid
                    fto.name = player.name
                    fto.week = week
                    fto.slot = slot

                    match_found = False
                    for fto_entry in fto_data:
                        if fto_entry == fto:
                            match_found = True
                    if match_found is False:
                        fto.put()
        # delete removed slot entries
        for fto_entry in fto_data:
            cell_name = str(fto_entry.week)+"-"+str(fto_entry.slot)
            cell_value = self.request.get(cell_name)
            if cell_value == "True":
                pass
            else:
                fto_entry.key.delete()

        response_data = {'status': 'success', 'message': 'Availability saved successfully'}
        self.response.headers['Content-Type'] = 'application/json'
        self.response.write(json.dumps(response_data))


#        if pid == user.user_id():
#            url = "availability?saved=true"
#        else:
#            url = "availability?pid=%s&saved=true" % pid
#        self.redirect(str(url))

    def get(self):
        # Filter for this year only
        now = datetime.datetime.today()

        # See if user is logged in and signed up
        login_info = get_login_info(self)
        user = users.get_current_user()
        if not user:
            self.redirect(users.create_login_url(self.request.uri))

        if self.request.get('pid'):
            pid = self.request.get('pid')
        elif user:
            pid = user.user_id()
        else:
            pid = None
        print("pid=%s" % pid)
        player = get_player(self, pid)
        print("player=%s" % player)

        if player is None:
            print("Redirecting")
            self.redirect("/signup")

        set_holidays(self)

        # Fill a list with the workdays for each week of the season
        weeks = []
        for week_index in range(numWeeks):
            week_dates = []
            for date_index in range(5):  # 5 workdays in a week
                date1 = startdate + datetime.timedelta(days=(7 * week_index) + date_index)
                week_dates.append((week_index, date_index, date1.strftime("%b %d")))
            weeks.append(week_dates)

        # Initialize the 2D array with False values
        fto_week = [[False for _ in range(5)] for _ in range(numWeeks)]

        # Get FTO data
        if pid:
            qry_f = Fto.query(ancestor=db_key(now.year))
            qry_f = qry_f.filter(Fto.user_id == pid)
            fto_data = qry_f.fetch(100)

            # for each set of FTO data, change the array item to True
            for entry in fto_data:
                fto_week[(entry.week - 1)][(entry.slot - 1)] = True
                # logging.info("Week: "+str(entry.week)+
                # " Slot: "+str(entry.slot)+" = "+str(fto_week[(entry.week-1)][(entry.slot-1)]))

        saved = self.request.get('saved')
        logging.info("save=" + saved)

        template_values = {
            'year': get_year_string(),
            'page': 'availability',
            'user': user,
            'player': player,
            'is_signed_up': player is not None,
            'login': login_info,
            'weeks': weeks,
            'fto_week': fto_week,
            'saved': saved,
            'is_admin': users.is_current_user_admin(),
        }

        os = self.request.headers.get('x-api-os')
        if os is not None:
            json_data = json.dumps(template_values, indent=4)
            self.response.write(json_data)
        else:
            template = JINJA_ENVIRONMENT.get_template('availability.html')
            self.response.write(template.render(template_values))


class Admin(webapp2.RequestHandler):
    def post(self):
        users.get_current_user()
        now = datetime.datetime.today()
        if self.request.get('y'):
            year = int(self.request.get('y'))
        else:
            year = now.year

        # Get player list
        qry_p = Player_List.query(ancestor=db_key(year))
        qry_p = qry_p.order(Player_List.schedule_rank)
        player_list = qry_p.fetch()

        if self.request.get('action') == "Submit":
            for player in player_list:
                player.name = self.request.get('name-' + player.id)
                player.email = self.request.get('email-' + player.id)
                player.schedule_rank = int(self.request.get('rank-' + player.id))
                player.elo_score = int(self.request.get('score-' + player.id))
                player.points = int(self.request.get('points-' + player.id))
                player.wins = int(self.request.get('wins-' + player.id))
                player.games = int(self.request.get('games-' + player.id))
                player.points_per_game = float(self.request.get('points_per_game-' + player.id))
#                print("%s is now rank %s" % (player.name, player.schedule_rank))
                player.put()


        if self.request.get('action') == "Holidays":
            for player in player_list:
                # Add holidays for all players.
                qry_f = Fto.query(ancestor=db_key(year))
                qry_f = qry_f.filter(Fto.user_id == player.id)
                fto_data = qry_f.fetch()
                for week_slot in holidays:
                    fto = Fto(parent=db_key(year))
                    fto.user_id = player.id
                    fto.name = player.name
                    fto.week = week_slot[0]
                    fto.slot = week_slot[1]

                    match_found = False
                    for fto_entry in fto_data:
                        if fto_entry == fto:
                            match_found = True
                    if match_found is False:
                        fto.put()

        self.redirect('admin')

    def get(self):
        # Filter for this year only
        now = datetime.datetime.today()
        if self.request.get('y'):
            year = int(self.request.get('y'))
        else:
            year = now.year
        login_info = get_login_info(self)

        # Get player list
        qry_p = Player_List.query(ancestor=db_key(year))
        qry_p = qry_p.order(Player_List.schedule_rank)
        player_list = qry_p.fetch()

        template_values = {
            'year': year,
            'page': 'admin',
            'player_list': player_list,
            'is_signed_up': True,
            'login': login_info,
            'is_admin': users.is_current_user_admin(),
        }

        os = self.request.headers.get('x-api-os')
        if os is not None:
            json_data = json.dumps(template_values, indent=4)
            self.response.write(json_data)
        else:
            template = JINJA_ENVIRONMENT.get_template('admin.html')
            self.response.write(template.render(template_values))


def split_people_into_tiers(player_data, num_tiers):
    tiers = [[] for _ in range(num_tiers)]
    tier_size = len(player_data) // num_tiers
    remaining_people = len(player_data) % num_tiers
    
    current_tier = 0
    for person in player_data:
        tiers[current_tier].append(person)
        if remaining_people > 0 and len(tiers[current_tier]) >= tier_size + 1:
            remaining_people -= 1
            current_tier = min(current_tier + 1, num_tiers - 1)
        elif len(tiers[current_tier]) >= tier_size:
            current_tier = min(current_tier + 1, num_tiers - 1)

    
    # Calculate the cutoff ELO scores for each tier
    cutoff_elo_scores = []
    for i in range(num_tiers - 1):
        upper_tier = tiers[i]
        lower_tier = tiers[i + 1]
        lowest_elo_upper = upper_tier[-1].score
        highest_elo_lower = lower_tier[0].score
        cutoff_elo = (lowest_elo_upper + highest_elo_lower) / 2
        cutoff_elo_scores.append(cutoff_elo)
        
    # Add players to multiple tiers based on the cutoff ELO scores
    for i, cutoff_elo in enumerate(cutoff_elo_scores):
        upper_bound = cutoff_elo + ELO_MARGIN
        lower_bound = cutoff_elo - ELO_MARGIN
        for person in player_data:
            elo = person.score
            if lower_bound <= elo <= cutoff_elo and person not in tiers[i]:
                tiers[i].append(person)
            elif cutoff_elo <= elo <= upper_bound and person not in tiers[i + 1]:
                tiers[i + 1].append(person)

    # Sort each tier by ELO score of people within the tier because people will have been added out of order.
    for tier in tiers:
        tier.sort(key=lambda p: p.score, reverse=True)

    return tiers

def find_valid_schedule(tiers, day_permutations):
    most_byes = -1
    valid_schedule = None
    playing_people = []  # List to store invited people for each slot
    random.shuffle(day_permutations) # We don't want any bias on which days get scheduled for when there are ties in most_byes
    for permutation in day_permutations:
        slot_to_tier_mapping = dict(zip(permutation, [tier[:] for tier in tiers]))  # Make a copy of tiers
        bye_count = 0
        invited_people_by_slot = {}
        
        # Check if each tier has at least 8 people available on the assigned slot
        valid = True
        for slot, tier in slot_to_tier_mapping.items():
            available_people = [person for person in tier if slot not in person.conflicts]
            if len(available_people) < PLAYERS_PER_GAME:
                valid = False
                break
            else:
                available_people.sort(key=lambda x: x.byes, reverse=True)
                invited_people = []
                for j in range(8):
                    invited_people.append(available_people[j])
                    # Temporarily remove invited people from other tiers
                    for other_slot, other_tier in slot_to_tier_mapping.items():
                        if other_slot != slot and available_people[j] in other_tier:
                            other_tier.remove(available_people[j])
                    bye_count += available_people[j].byes
                invited_people_by_slot[slot] = invited_people  # Store invited people for this slot
        
        if valid:
            if bye_count > most_byes:
                logging.info("Found a better schedule. most_byes was %d and is now %d." % (most_byes, bye_count))
                valid_schedule = slot_to_tier_mapping
                playing_people = invited_people_by_slot
                most_byes = bye_count
            else:
                logging.info("Found a schedule, but it isn't better. %d vs %d" % (bye_count, most_byes))

    for slot in playing_people:
        playing_people[slot].sort(key=lambda p: p.score, reverse=True)
#        logging.info("Slot %d:" % slot)
#        for p in playing_people[slot]:
#            logging.info("  %s" % p.name)
    return {"schedule": valid_schedule, "playing_people": playing_people}

class Scheduler(webapp2.RequestHandler):
    # This will run on Fridays to create the schedule for the next week
    def get(self):
        # Filter for this year only
        today = datetime.date.today()
        year = today.year
        random.seed(datetime.datetime.now())

        # Calculate what week# next week will be
        if self.request.get('w'):
            week = int(self.request.get('w'))
        else:
            week = int(math.floor(int(((today - startdate).days) + 3) / 7) + 1)
        if week < 1:
            week = 1
        if week > numWeeks:
            return
        logging.info("Week %s Scheduler" % week)
        
        
        player_data = get_player_data(week, self) #player_data is a list of Player objects
        
        # Need to check for existing scores for this week. If there are scores for this week, we should abort.
        qry = Scores.query(ancestor=db_key(year))
        qry = qry.filter(Scores.week == (week))
        if qry.count() != 0:
            logging.info("There are scores in the system for this week. Aborting.")
            return

        # Create a list of players ids on bye this week because of FTO
        bye_list = []
        if player_data:
            for p in player_data:
                if len(p.conflicts) >= SLOTS_IN_WEEK:
                    bye_list.append(p)
                    logging.info("%s is on a bye this week." % p.name)
                    
        # Remove players on bye from the player_data list            
        player_data = [p for p in player_data if p not in bye_list]
        
        # number of players not on a full week bye
        num_available_players = len(player_data)
        
        for num_tiers in range(num_available_players // PLAYERS_PER_GAME, 1, -1):
            #Not sure if we need to sort by ELO score every time through...
            player_data.sort(key=lambda p: p.score, reverse=True)
            logging.info("Trying to find a schedule with %d tiers." % num_tiers)
            tiers = split_people_into_tiers(player_data, num_tiers)
        
            day_permutations = list(itertools.permutations(range(1, SLOTS_IN_WEEK+1), num_tiers))
            result = find_valid_schedule(tiers, day_permutations)
            
            if result["schedule"]:
                valid_schedule_found = True
                valid_schedule = result["schedule"]
                playing_people = result["playing_people"]


                # If we reach this point, we have a valid schedule! Save it to the database.
                # First delete any existing schedule for this week (in case the scheduler runs more than once)
                qry = Schedule.query(ancestor=db_key(year))
                qry = qry.filter(Schedule.week == week)
                results = qry.fetch()
                for r in results:
                    r.key.delete()

        # Store the bye players in the database
        for p in bye_list:
            logging.info("Adding %s to the bye slot." % p.name)
            s = Schedule(parent=db_key(year))  # database entry
            s.id = p.id
            s.name = p.name
            s.week = week
            s.slot = 0
            s.tier = 0
            # using the position variable to store the slot this player can be an alternate for
            s.position = 0
            s.put()
        
        # store the scheduled players and alternates in the database and create calendar events with notifications            
        for slot, tier in valid_schedule.items():
            name_list = []
            email_list = []
            logging.info("Slot %d" % slot)
            for p in tier:
                s = Schedule(parent=db_key(year))  # database entry
                s.id = p.id
                s.name = p.name
                s.week = week
                s.tier = 0 # is the tier neccessary? There currently isn't a good way to find the tier.
                if p in playing_people[slot]: # for players scheduled to play
                    s.slot = slot
                    s.position = playing_people[slot].index(p)+1
                    name_list.append(p.name)
                    email_list.append(p.email)
                    logging.info("  %s (%d)" % (s.name, s.position))
                else: # for alternates
                    s.slot = 0
                    s.position = slot
                s.put()
            
            if SEND_INVITES:
                # Calculate the date for this match
                match_date = startdate + datetime.timedelta(days=(7 * (week - 1) + (slot - 1)))
                start_time = datetime.datetime.combine(match_date, datetime.time(12, 0, 0))
                end_time = datetime.datetime.combine(match_date, datetime.time(13, 0, 0))

                service = build('calendar', 'v3')
                event = {
                    'summary': 'Sand VolleyBall Match',
                    'location': 'N/S Sand Court',
                    'description': "Week %s Sand Volleyball Match. If you cannot make the match, please go to https://hpsandvolleyball.appspot.com/week (make sure you are logged in) and click the \"I need a sub\" button." % week,
                    'start': {
                        #						'dateTime': '2018-05-28T12:00:00-06:00',
                        'timeZone': 'America/Boise',
                    },
                    'end': {
                        #						'dateTime': '2018-05-28T13:00:00-06:00',
                        'timeZone': 'America/Boise',
                    },
                    'attendees': [
                        #					{'email': 'brian.bartlow@hp.com'},
                    ],
                    'reminders': {
                        'useDefault': True,
                    },
                }
                event['start']['dateTime'] = start_time.isoformat('T')
                event['end']['dateTime'] = end_time.isoformat('T')
                for e in email_list:
                    event['attendees'].append({'email': e})
                event = service.events().insert(calendarId='brianbartlow@gmail.com', body=event, sendNotifications=True).execute()
        
        sys.stdout.flush()
        template = JINJA_ENVIRONMENT.get_template('scheduler.html')
        self.response.write(template.render({}))


class Elo(webapp2.RequestHandler):
    def get(self):
        today = datetime.date.today()
        datetime.datetime.today()
        year = today.year
        random.seed(datetime.datetime.now())
        team_map = ((0, 1, 0, 1, 1, 0, 1, 0), (0, 1, 1, 0, 0, 1, 1, 0), (0, 1, 1, 0, 1, 0, 0, 1))
        kfactor = 400  # 2018 = 200, 2019 = 400, 2023 = 400

        # Calculate what week# next week will be
        if self.request.get('w'):
            week = int(self.request.get('w'))
        else:
            week = int(math.floor(int((today - startdate).days + 3) / 7) + 1)
        if week < 1: week = 1
        logging.info("Week %s Elo Update" % week)
        player_data = get_player_data(week, self)

        qry = Schedule.query(ancestor=db_key(year))
        qry = qry.filter(Schedule.week == (week - 1))
        # Order based on decending tier so we can get a count of how many tiers there were this week.
        qry = qry.order(-Schedule.tier, Schedule.position)
        schedule_results = qry.fetch()
        if schedule_results:
            tiers = schedule_results[0].tier  # The number of tiers is equal to the highest tier from the schedule.

            # Set up lists to store the average team Elo and scores for each game(3) in each tier(variable).
            team_elo = []
            scores = []
            player_count = []
            for x in range(tiers + 1):
                team_elo.append([[0, 0], [0, 0], [0, 0]])
                scores.append([[0, 0], [0, 0], [0, 0]])
                player_count.append(0)

            # Calculate the average Elo scores for each team
            for p in schedule_results:
                if p.tier > 0:
                    player_count[p.tier] += 1
                    for x in range(3):
                        # Add elo_score to team_elo[tier][game][team]
                        team_elo[p.tier][x][team_map[x][p.position - 1]] += float(player_data[p.id].score)
            # logging.info("team_elo = %s (tier %s, game %s, team %s)" % (team_elo[p.tier][x][team_map[x][p.position-1]], p.tier, x+1, team_map[x][p.position-1]+1))
            for x in range(tiers + 1):
                for y in range(3):
                    for z in range(2):
                        if player_count[x]:
                            team_elo[x][y][z] /= float(player_count[x]/2)  # Average Elo_Score for each team
            # logging.info("team_elo = %s (tier %s, game %s, team %s)" % (team_elo[x][y][z], x, y+1, z+1))

        # Grab the scores from the database and store them in a list.
        qry = Scores.query(ancestor=db_key(year))
        qry = qry.filter(Scores.week == (week - 1))
        qry = qry.order(Scores.tier, Scores.game)
        results = qry.fetch()
        if results:
            for score in results:
                scores[score.tier][score.game - 1][0] = float(score.score1)
                scores[score.tier][score.game - 1][1] = float(score.score2)

        # Now iterate through each player on the schedule and calculate their new Elo score based on
        # their old Elo score, the game scores, and the teams' average Elo scores.
        new_elo = {}
        new_points = {}
        new_wins = {}
        new_games = {}
        for p in schedule_results:
            if p.tier > 0:
                if p.id not in new_elo:
                    new_elo[p.id] = player_data[p.id].score
                    new_points[p.id] = player_data[p.id].points
                    new_wins[p.id] = player_data[p.id].wins
                    new_games[p.id] = player_data[p.id].games
                for g in range(3):
                    my_team_elo = float(team_elo[p.tier][g][team_map[g][p.position - 1]])
                    other_team_elo = float(team_elo[p.tier][g][1 - team_map[g][p.position - 1]])
                    my_team_score = float(scores[p.tier][g][team_map[g][p.position - 1]])
                    other_team_score = float(scores[p.tier][g][1 - team_map[g][p.position - 1]])
                    logging.info("%s - %s vs %s" % (player_data[p.id].name, my_team_score, other_team_score))
                    # Take the old Elo score and add the modifier to it. We'll store it later.
                    if my_team_score > 0 or other_team_score > 0:
                        new_elo[p.id] += int(round(float(kfactor * (
                                (my_team_score / (my_team_score + other_team_score)) - (
                                my_team_elo / (my_team_elo + other_team_elo))))))  # Elo magic here
                        logging.info("%s's Elo score(%s) is now %s" % (
                            player_data[p.id].name, player_data[p.id].score, new_elo[p.id]))
                        new_games[p.id] += 1
                    # If this player's team won, add the ELO score of the losing team
                    if my_team_score > other_team_score:
                        new_points[p.id] += other_team_elo
                        new_wins[p.id] += 1
                        
        # Store the new Elo scores, PPG, and ranks in the database
        qry = Player_List.query(ancestor=db_key(year))
        pr = qry.fetch()
        for p in pr:
            if p.id in new_elo:  # Only store new scores if there is a new score to store
                p.elo_score = new_elo[p.id]
                p.points = int(new_points[p.id])
                p.wins = new_wins[p.id]
                p.games = new_games[p.id]
                if p.games == 0:
                    p.points_per_game = 0
                else:
                    p.points_per_game = round(float(new_points[p.id]/new_games[p.id]),1)
#                p.put()
        # Sort the player list by their new ELO scores.
        pr.sort(key=lambda p: p.elo_score, reverse=True)
        for rank, player in enumerate(pr_sorted):
            player.schedule_rank = rank
            player.put()        

class Standings(webapp2.RequestHandler):
    def get(self):
        users.get_current_user()
        now = datetime.datetime.today()
        today = datetime.date.today()
        week = int(math.floor(int((today - startdate).days + 3) / 7))
        if week < 1: week = 1
        if self.request.get('y'):
            year = int(self.request.get('y'))
        else:
            year = now.year
        login_info = get_login_info(self)
        player = get_player(self)

        # Get player list
        qry_p = Player_List.query(ancestor=db_key(year))
        if self.request.get('sort') == 'ppg':
            qry_p = qry_p.filter(Player_List.games >= int(3*week/2))
            player_list = qry_p.fetch()
            player_list = sorted(player_list, key=lambda k: k.points_per_game, reverse=True)
        else:
            qry_p = qry_p.order(-Player_List.elo_score)
            player_list = qry_p.fetch()

        win_percentage = {}
        for p in player_list:
            if p.games > 0:
                win_percentage[p.id] = round(100 * float(p.wins) / float(p.games), 1)
            else:
                win_percentage[p.id] = 0

        template_values = {
            'current_year': now.year,
            'year': year,
            'page': 'standings',
            'player_list': player_list,
            'win_percentage': win_percentage,
            'min_games': int(3*week/2),
            'is_signed_up': player is not None,
            'login': login_info,
            'is_admin': users.is_current_user_admin(),
        }

        os = self.request.headers.get('x-api-os')
        if os is not None:
            json_data = json.dumps(template_values, indent=4)
            self.response.write(json_data)
        else:
            template = JINJA_ENVIRONMENT.get_template('standings.html')
            self.response.write(template.render(template_values))


class Sub(webapp2.RequestHandler):
    def get(self):
        user = users.get_current_user()
        if not user:
            self.redirect(users.create_login_url(self.request.uri))
            
        now = datetime.datetime.today()
        get_login_info(self)
        get_player(self)
        week = int(self.request.get('w'))
        slot = int(self.request.get('s'))
        tier = int(self.request.get('t'))
        sub_id = self.request.get('id')
        player_data = get_player_data(week, self)
        player = get_player(self)

        qry = Schedule.query(ancestor=db_key(now.year))
        qry = qry.filter(Schedule.week == week)
        sr = qry.fetch()
        swap_id = None
        player_list = []
        success = "n"

        # Check to make sure the sub_id is a currently active player
        # (otherwise, someone else may have already accepted the sub request.)
        for x in sr:
            if x.id == sub_id and x.slot == slot and x.tier == tier:
                if x.slot != 0:
                    if player:
                        # Make the swap
                        swap_id = player.id
        if swap_id is not None:
            success = "y"
            for x in sr:
                if x.slot == slot and x.id != sub_id:
                    # Add everyone already in this slot to a list except the player being subbed out.
                    player_list.append(x.id)
            player_list.append(swap_id)  # Then add the player being swapped in.
            player_list = sorted(player_list, key=lambda k: player_data[k].score, reverse=True)  # Sort the list by elo
            # delete the existing schedule for this slot
            qry = Schedule.query(ancestor=db_key(now.year))
            qry = qry.filter(Schedule.week == week, Schedule.slot == slot)
            results = qry.fetch()
            for r in results:
                r.key.delete()
            # Then save the new slot schedule.
            z = 0
            for p in player_list:
                z += 1
                s = Schedule(parent=db_key(now.year))  # database entry
                s.id = p
                s.name = player_data[p].name
                s.week = week
                s.slot = slot
                s.tier = tier
                s.position = z  # 1-8
                s.put()  # Stores the schedule data in the database

            # delete the swapping player from the schedule where it shows as alternate.
            qry = Schedule.query(ancestor=db_key(now.year))
            qry = qry.filter(Schedule.week == week, Schedule.id == swap_id, Schedule.slot == 0)
            results = qry.fetch()
            if results:
                results[0].key.delete()
            # add the subbed out player to the alternate list.
            s = Schedule(parent=db_key(now.year))
            s.id = sub_id
            s.name = player_data[sub_id].name
            s.week = week
            s.slot = 0
            s.tier = 0
            s.position = slot
            s.put()

#            sg = sendgrid.SendGridAPIClient(apikey=keys.API_KEY)
            # Send an email confirmation out to the admin and the substituting players
            message = mail.EmailMessage()
            message.sender = "noreply@hpsandvolleyball.appspotmail.com"
            message.to = [str(player_data[sub_id].email), str(player_data[swap_id].email), "brian.bartlow@hp.com"]
            message.subject = "Substitution Successful"
#            message.body = "This notice is to inform you that the substitution has been completed successfully. Since the system doesn't automatically update the meeting invites, %s, please forward your meeting invitation to %s at %s." % (
#                            player_data[sub_id].name, player_data[swap_id].name, player_data[swap_id].email)
            message.html = "This notice is to inform you that the substitution has been completed successfully. Since the system doesn't automatically update the meeting invites, %s, please forward your meeting invitation to <a href=\"mailto:%s\">%s</a>." % (
                            player_data[sub_id].name, player_data[swap_id].email, player_data[swap_id].name)
            message.body = links2text(message.html)
            message.check_initialized()
            message.send()
            
#            from_email = Email("noreply@hpsandvolleyball.appspotmail.com")
#            to_email = Email("brian.bartlow@hp.com")
#            subject = "Substitution Successful"
#            content = Content("text/html",
#                              "This notice is to inform you that the substitution has been completed successfully. Since the system doesn't automatically update the meeting invites, %s, please forward your meeting invitation to <a href=\"mailto:%s\">%s</a>." % (
#                                  player_data[sub_id].name, player_data[swap_id].email, player_data[swap_id].name))
#            mail = Mail(from_email, subject, to_email, content)
#            personalization = Personalization()
#            personalization.add_to(Email(player_data[sub_id].email))
#            personalization.add_to(Email(player_data[swap_id].email))
#            mail.add_personalization(personalization)
#            sg.client.mail.send.post(request_body=mail.get())

        self.redirect("week?w=%s&m=%s" % (week, success))


class WeeklySchedule(webapp2.RequestHandler):
    def post(self):
#        sg = sendgrid.SendGridAPIClient(apikey=keys.API_KEY)
        user = users.get_current_user()
        now = datetime.datetime.today()
        get_login_info(self)
        player = get_player(self)
        week = int(self.request.get('w'))
        slot = int(self.request.get('s'))
        player_data = get_player_data(week, self)


#        from_email = Email("noreply@hpsandvolleyball.appspot.com")
#        to_email = Email("brian.bartlow@hp.com")

        if self.request.get('action') == "Sub" and user and player is not None:
            sub_id = user.user_id()
            qry = Schedule.query(ancestor=db_key(now.year))
            qry = qry.filter(Schedule.week == week)
            sr = qry.fetch()
            notification_list = []
            sendit = False

            for x in sr:
                if x.id == sub_id:
                    if x.slot == slot:
                        tier = x.tier
                        notification_list.append(player_data[x.id].email)
                        for y in sr:
                            # send to everyone not already playing in this slot or on a bye week
                            if y.slot != slot and y.position != 0 and player_data[y.id].email not in notification_list:
                                notification_list.append(player_data[y.id].email)
                                sendit = True
                        break

            message = mail.EmailMessage()
            message.sender = "noreply@hpsandvolleyball.appspotmail.com"
            message.to = ["brian.bartlow@hp.com"]
            message.subject = "%s needs a Sub" % player_data[sub_id].name
#            message.body = """%s needs a sub on %s. This email is sent to everyone not already scheduled to play on that date. If you are an alternate for this match and can play, please click this link http://hpsandvolleyball.appspot.com/sub?w=%s&s=%s&t=%s&id=%s. If you are not an alternate for this match, you can still sub, but you should wait long enough for the alternates to be able to accept first. If there are no alternates for this match, and you can play, go ahead and click the link. The first to accept the invitation will get to play.
#                                NOTE: The system is not able to update the calendar invitations, so please remember to check the website for the official schedule.""" % (player_data[sub_id].name, (startdate + datetime.timedelta(days=(7 * (week - 1) + (slot - 1)))).strftime("%A %m/%d"), week, slot, tier, sub_id)
            message.html = "<p>%s needs a sub on %s. This email is sent to everyone not already scheduled to play on that date. If you are an alternate for this match and can play, please click <a href = \"http://hpsandvolleyball.appspot.com/sub?w=%s&s=%s&t=%s&id=%s\">this link</a>. If you are not an alternate for this match, you can still sub, but you should wait long enough for the alternates to be able to accept first. If there are no alternates for this match, and you can play, go ahead and click the link. The first to accept the invitation will get to play.</p><strong>NOTE: The system is not able to update the calendar invitations, so please remember to check the website for the official schedule.</strong>" % (player_data[sub_id].name, (startdate + datetime.timedelta(days=(7 * (week - 1) + (slot - 1)))).strftime("%A %m/%d"), week, slot, tier, sub_id)
            message.body = links2text(message.html)

            if sendit:
                logging.info(message.subject)
                logging.info(message.html)
                logging.info("sending to: %s" % notification_list)
#                mail = Mail(from_email, subject, to_email, content)
                for e in notification_list:
                    message.to.append(str(e))
#                    personalization = Personalization()
#                    for e in notification_list:
#                        personalization.add_to(Email(e))
#                    mail.add_personalization(personalization)
                message.check_initialized()
                message.send()
#                response = sg.client.mail.send.post(request_body=mail.get())
#                print(response.status_code)
#                print(response.body)
#                print(response.headers)
        self.redirect("week?w=%s&m=rs" % week)

    def get(self):
        today = datetime.date.today()
        if self.request.get('y'):
            year = int(self.request.get('y'))
        else:
            year = today.year
        # See if user is logged in and signed up
        login_info = get_login_info(self)
        user = users.get_current_user()
        player = get_player(self)
        success = self.request.get('m')

        # Calculate what week# next week will be
        if self.request.get('w'):
            week = int(self.request.get('w'))
        else:
            week = int(math.floor(int((today - startdate).days + 2) / 7) + 1)
        if week < 1:
            week = 1
        if week > numWeeks:
            week = numWeeks

        os = self.request.headers.get('x-api-os')
        slots = []
        for d in range(SLOTS_IN_WEEK):
            if os is None:
                slots.append(startdate + datetime.timedelta(days=(7 * (week - 1) + d)))
            else:
                slots.append((startdate + datetime.timedelta(days=(7 * (week - 1) + d))).strftime('%m/%d/%Y'))

        qry = Schedule.query(ancestor=db_key(year))
        qry = qry.filter(Schedule.week == week)
        qry = qry.order(Schedule.slot, Schedule.position)

        if os is None:
            schedule_data = qry.fetch()
        else:
            schedule_data = ""

        active = []
        if user and year == today.year:
            for s in schedule_data:
                if s.id == user.user_id() and s.slot != 0:
                    deadline = startdate + datetime.timedelta(days=(7 * (week - 1)) + (s.slot - 1))
                    # noon Mountain time on the day of the match
                    if datetime.datetime.today() < datetime.datetime(deadline.year, deadline.month, deadline.day, 18):
                        active.append(s.slot)

        template_values = {
            'current_year': today.year,
            'year': year,
            'page': 'week',
            'week': week,
            'numWeeks': numWeeks,
            'slots': slots,
            'schedule_data': schedule_data,
            'success': success,
            'active': active,
            'player': player,
            'is_signed_up': player is not None,
            'login': login_info,
            'is_admin': users.is_current_user_admin(),
        }

        if os is not None:
            json_data = json.dumps(template_values, indent=4)
            self.response.write(json_data)
        else:
            template = JINJA_ENVIRONMENT.get_template('week.html')
            self.response.write(template.render(template_values))


class DailySchedule(webapp2.RequestHandler):
    def post(self):
        users.get_current_user()
        now = datetime.datetime.today()
        # See if user is logged in and signed up
        get_login_info(self)
        users.get_current_user()
        player = get_player(self)

        if self.request.get('y'):
            year = int(self.request.get('y'))
        else:
            year = now.year
        week = int(self.request.get('w'))
        day = int(self.request.get('d'))
        tier = int(self.request.get('t'))

        if self.request.get('action') == "Scores":
            if player is not None:
                logging.info("%s is entering scores." % player.name)
            qry = Scores.query(ancestor=db_key(year))
            qry = qry.filter(Scores.week == week, Scores.slot == day)
            sr = qry.fetch()
            for s in sr:
                s.key.delete()  # Delete the old scores

            for g in range(1, 4):
                score = Scores(parent=db_key(year))
                score.week = week
                score.slot = day
                score.tier = tier
                score.game = g
                if self.request.get("score-%s-1" % g):
                    score.score1 = int(self.request.get("score-%s-1" % g))
                else:
                    score.score1 = 0
                if self.request.get("score-%s-2" % g):
                    score.score2 = int(self.request.get("score-%s-2" % g))
                else:
                    score.score2 = 0
                if score.score1 or score.score2:
                    logging.info("Game %s: %s - %s" % (g, score.score1, score.score2))
                    score.put()  # Save the new scores

        self.redirect("day?w=%s&d=%s" % (week, day))

    def get(self):
        today = datetime.date.today()
        if self.request.get('y'):
            year = int(self.request.get('y'))
        else:
            year = today.year
        # See if user is logged in and signed up
        login_info = get_login_info(self)
        users.get_current_user()
        player = get_player(self)

        day = 0
        # Calculate what week and day it is
        if self.request.get('w'):
            week = int(self.request.get('w'))
        else:
            week = int(math.floor(int((today - startdate).days) / 7) + 1)
        if week < 1:
            week = 1
            day = 1
        if week > numWeeks:
            week = numWeeks

        if self.request.get('d'):
            day = int(self.request.get('d'))
        else:
            if not day:
                day = today.weekday() + 1
        if day > 5:
            day = 1
            week += 1

        schedule_day = startdate + datetime.timedelta(days=(7 * (week - 1) + (day - 1)))

        qry = Schedule.query(ancestor=db_key(year))
        qry = qry.filter(Schedule.week == week, Schedule.slot == day)
        qry = qry.order(Schedule.position)
        schedule_data = qry.fetch()
        if len(schedule_data) > 0:
            games = True
            tier = schedule_data[0].tier
        else:
            games = False
            tier = 0

        game_team = [[], []], [[], []], [[], []]
        for p in schedule_data:
            for x in range(3):
                game_team[x][ms[x][p.position - 1]].append(p.name)

        qry = Scores.query(ancestor=db_key(year))
        qry = qry.filter(Scores.week == week, Scores.slot == day)
        sr = qry.fetch()

        score = [['', ''], ['', ''], ['', '']]
        if sr:
            for s in sr:
                score[s.game - 1][0] = s.score1
                score[s.game - 1][1] = s.score2

        is_today = False
#        if today == schedule_day or not score[2][1]:
#        if today > schedule_day and not score[2][1]:
#            # If a match gets rescheduled and played later.
#            is_today = True
        if today == schedule_day:
            is_today = True

        os = self.request.headers.get('x-api-os')
        day_schedule = schedule_day
        if os is not None:
            day_schedule = schedule_day.strftime('%m/%d/%Y')

        template_values = {
            'current_year': today.year,
            'year': year,
            'page': 'day',
            'week': week,
            'day': day,
            'tier': tier,
            'games': games,
            'score': score,
            'numWeeks': numWeeks,
            'schedule_day': day_schedule,
            'is_today': is_today,
            'game_team': game_team,
            'is_signed_up': player is not None,
            'login': login_info,
            'is_admin': users.is_current_user_admin(),
        }

        if os is not None:
            json_data = json.dumps(template_values, indent=4)
            self.response.write(json_data)
        else:
            template = JINJA_ENVIRONMENT.get_template('day.html')
            self.response.write(template.render(template_values))


class Notify(webapp2.RequestHandler):
    def get(self):
#        sg = sendgrid.SendGridAPIClient(apikey=keys.API_KEY)

        today = datetime.date.today()
        year = today.year

        # Calculate what week and day it is
        week = int(math.floor(int((today - startdate).days) / 7) + 1)
        day = today.isoweekday()

        message = mail.EmailMessage()
        message.sender = "noreply@hpsandvolleyball.appspotmail.com"
        message.to = ["brian.bartlow@hp.com"]
        message.subject = "Please Ignore"
#        message.body = "Please ignore this email. I am testing new functionality on the website."
        message.html = "<p>Please ignore this email.</p><p>I am testing new functionality on the website.</p>"
        message.body = links2text(message.html)

#        from_email = Email("noreply@hpsandvolleyball.appspot.com")
#        # to_email = Email("")
#        to_email = Email("brian.bartlow@hp.com")
#        subject = "Please Ignore"
#        content = Content("text/html", "Please ignore this email, I am testing new functionality on the website.")

        player_data = get_player_data(0, self)
        sendit = True
        notification_list = []

        if self.request.get('t') == "score":
            # Check to see if there is a match scheduled for today
            qry = Schedule.query(ancestor=db_key(year))
            qry = qry.filter(Schedule.week == week, Schedule.slot == day)
            qry = qry.order(Schedule.position)
            schedule_data = qry.fetch()
            logging.info("Week %s, Slot %s" % (week, day))
            if schedule_data:  # If there is a match scheduled for today
                # Check to see if scores have been entered for today's match
                logging.info("There are games scheduled today.")
                qry = Scores.query(ancestor=db_key(year))
                qry = qry.filter(Scores.week == week, Scores.slot == day)
                sr = qry.count()
                logging.info("%s scores have been entered today." % sr)
                if sr < 3:  # If no scores have been entered for today's match, email all of today's players to remind them to enter the score.
                    logging.info("Sending email reminder to enter scores.")
                    message.subject = "Reminder to submit scores"
#                    message.body = "At the moment this email was generated, the scores haven't been entered for today's games. Please go to the Score Page (http://hpsandvolleyball.appspot.com/day) and enter the scores. If someone has entered the scores by the time you check, or the games were not actually played, please disregard."
                    message.html = "At the moment this email was generated, the scores haven't been entered for today's games. Please go to the <a href=\"http://hpsandvolleyball.appspot.com/day\">Score Page</a> and enter the scores. If someone has entered the scores by the time you check, or the games were not actually played, please disregard."
                    message.body = links2text(message.html)
                    sendit = True
                    for s in schedule_data:
                        # pass
                        notification_list.append(player_data[s.id].email)
                else:
                    logging.info("The scores were already entered today.")
                    sendit = False
            else:
                logging.info("There are no games scheduled for today.")
                sendit = False

        elif self.request.get('t') == "fto" and week >= 0 and week < numWeeks:
            message.subject = "Reminder to check and update your FTO/Conflicts for next week"
#            message.body = """Next week's schedule will be generated at 2:00pm. If there are any days next week where you cannot play at noon, please go to the FTO Page (http://hpsandvolleyball.appspot.com/availability) and check to make sure those days are checked off as unavailable.
#            If that link doesn't work, please verify you are logged in with the Google account used when you signed up. Log in, then click on the FTO link at the top of the page. Then click the checkbox for any days that you cannot play at noon."""
            message.html = """Next week's schedule will be generated at 2:00pm. If there are any days next week where you cannot play at noon, please go to the <a href=\"http://hpsandvolleyball.appspot.com/availability\">FTO Page</a> and check to make sure those days are checked off as unavailable.
            If that link doesn't work, please verify you are logged in with the Google account used when you signed up. Log in, then click on the FTO link at the top of the page. Then click the checkbox for any days that you cannot play at noon."""
            message.body = links2text(message.html)
            sendit = True
            for p in player_data:
                logging.info("%s - %s" % (player_data[p].name,player_data[p].email))
                if player_data[p].email:
                    # pass
                    notification_list.append(player_data[p].email)

        elif self.request.get('t') == "test":
            sendit = False
            week = 2
            slot = 4
            email_list = ["brian.bartlow@hp.com"]
            match_date = startdate + datetime.timedelta(days=(7 * (week - 1) + (slot - 1)))
            start_time = datetime.datetime.combine(match_date, datetime.time(12, 0, 0))
            end_time = datetime.datetime.combine(match_date, datetime.time(13, 0, 0))

            service = build('calendar', 'v3')
            event = {
                'summary': "Week %s Sand VolleyBall Match" % week,
                'location': 'N/S Sand Court',
                'description': "Week %s Sand Volleyball Match" % week,
                'start': {
                    'timeZone': 'America/Boise',
                },
                'end': {
                    'timeZone': 'America/Boise',
                },
                'attendees': [
                ],
                'reminders': {
                    'useDefault': True,
                },
            }
            event['start']['dateTime'] = start_time.isoformat('T')
            event['end']['dateTime'] = end_time.isoformat('T')
            for e in email_list:
                event['attendees'].append({'email': e})
            event = service.events().insert(calendarId='aidl2j9o0310gpp2allmil37ak@group.calendar.google.com',
                                            body=event, sendNotifications=True).execute()

        elif self.request.get('t') == "log":
            sendit = False
            logging.info('This is an info log message')
            self.response.out.write('Logging example.')

        if sendit:
#            mail = Mail(from_email, subject, to_email, content)
            for e in notification_list:
                message.to.append(str(e))
                
            logging.info(message.to)
#                personalization = Personalization()
#                for e in notification_list:
#                    personalization.add_to(Email(e))
#                mail.add_personalization(personalization)
#            response = sg.client.mail.send.post(request_body=mail.get())
#            print(response.status_code)
#            print(response.body)
#            print(response.headers)
            message.check_initialized()
            message.send()


app = webapp2.WSGIApplication([
    ('/', MainPage),
    ('/signup', Signup),
    ('/unsignup', Unsignup),
    ('/info', Info),
    ('/ftolog', Ftolog),
    ('/availability', Availability),
    ('/week', WeeklySchedule),
    ('/day', DailySchedule),
    ('/standings', Standings),
    ('/sub', Sub),
    ('/admin', Admin),
    ('/tasks/notify', Notify),
    ('/tasks/scheduler', Scheduler),
    ('/tasks/elo', Elo),
    ('/add_random_players', AddRandomPlayers),
], debug=True)
