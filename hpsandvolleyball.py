import os
import urllib
import datetime
import logging
import string
import math
import random
import sys
import json

# For Google Calendar
from apiclient.discovery import build

from google.appengine.api import users
from google.appengine.ext import ndb

import jinja2
import webapp2

# using SendGrid's Python Library
# https://github.com/sendgrid/sendgrid-python
import keys
import sendgrid
from sendgrid.helpers.mail import *

# from google.
# Globals - I want these eventually to go into a datastore per year so things can be different and configured per year. For now, hard-coded is okay.
numWeeks = 14
startdate = datetime.date(2019, 5, 20)
holidays = ((2, 1), (3, 4), (7, 4))  # Memorial Day, BYITW Day?, Independance Day
ms = ((0, 1, 0, 1, 1, 0, 1, 0), (0, 1, 1, 0, 0, 1, 1, 0),
      (0, 1, 1, 0, 1, 0, 0, 1))  # How to team up the players for each of the three games

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
    if pid == None:
        if user:
            pid = user.user_id()
    if pid != None:
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
    pl = {}
    fto_count = {}
    # Get player list
    qry = Player_List.query(ancestor=db_key(now.year))
    qry = qry.order(Player_List.schedule_rank)
    plr = qry.fetch(100)
    for player in plr:
        pl[player.id] = Player()
        pl[player.id].name = player.name
        pl[player.id].email = player.email
        pl[player.id].phone = player.phone
        pl[player.id].rank = player.schedule_rank
        pl[player.id].score = player.elo_score

        # Need a dict of lists to count the conflicts for each week per player. Initialized with zeros.
        fto_count[player.id] = [0] * numWeeks

    # Check previous schedules for byes or alternates
    if current_week > 1:
        qry = Schedule.query(ancestor=db_key(year))
        qry = qry.filter(Schedule.tier == 0)
        past_byes = qry.fetch()
        if past_byes:
            for bye in past_byes:
                if bye.week < current_week:  # in the past
                    pl[bye.id].byes += 1

    # Check future fto for byes
    qry = Fto.query(ancestor=db_key(year))
    fto = qry.fetch()
    if fto:
        for f in fto:
            if f.week > current_week and f.user_id in fto_count:
                fto_count[f.user_id][f.week - 1] += 1
                if fto_count[f.user_id][
                    f.week - 1] == 4:  # Once we reach 4 conflicts in a week, that's a bye week. Don't want to double-count on the 5th conflict.
                    pl[f.user_id].byes += 1
            if f.week == current_week:  # To make things easy, we can populate the weekly conflicts while iterating through the fto list.
                pl[f.user_id].conflicts.append(f.slot)

    return pl


def pick_slots(tier_slot, tier, tier_slot_list):
    if tier >= len(tier_slot_list): return True  # We've iterated through all tiers, we're good.
    while len(tier_slot) < (tier + 1): tier_slot.append(
        0)  # Fill tier_slot with 0s. We'll fill this with the correct slots as we go.
    for x in tier_slot_list[tier]:  # Cycle through each possible slot for this tier
        if x not in tier_slot:  # If this slot hasn't been taken by another slot yet...
            tier_slot[tier] = x  # Claim the slot.
            if pick_slots(tier_slot, tier + 1,
                          tier_slot_list):  # Recursively call the function again on the next tier. If it returns True...
                return True  # ...then we can return true too.
    # If we get here, we've tried every slot in this tier's valid list and found nothing that isn't taken yet, so...
    tier_slot[tier] = 0  # ...reset this tier's slot to 0 so we can try again.
    return False  # We've failed. Back up and try another slot in the prior tier's list.


def find_smallest_set(set_list):
    smallest_set = len(set_list[1])
    smallest_set_pos = 1
    for p in range(2, len(set_list)):
        if len(set_list[p]) == smallest_set:
            smallest_set_pos = random.choice((smallest_set_pos,
                                              p))  # Randomly choose which of the two sets to return if there is a tie. The randomness isn't evenly distributed though.
        if len(set_list[p]) < smallest_set:
            smallest_set = len(set_list[p])
            smallest_set_pos = p
    return smallest_set_pos


def remove_conflicts(player_ids, player_data, self, count=1):
    if count > 20: return []
    slots = range(1, 6)
    y = 0
    for p in player_ids:
        y += 1
        if y > 8: break  # use the data from the first 8 players in the tier
        for s in player_data[p].conflicts:
            if s in slots:
                slots.remove(s)
        # for z in player_ids:
        # logging.info(" %s - %s" % (player_data[z].name, player_data[z].conflicts))
    if (len(slots) == 0) and (len(
            player_ids) > 8):  # If there are no valid slots for this group of 8 to play, and there are more than 8 people in the tier, randomly shuffle the players and tray again.
        logging.info("No slot for this tier, shuffling and trying again. Count=%s" % count)
        random.shuffle(player_ids)
        return remove_conflicts(player_ids, player_data, self, count + 1)
    random.shuffle(slots)  # randomize the order of the available slots
    return slots


class Player(object):
    def __init__(self):
        self.name = None
        self.email = None
        self.phone = None
        self.rank = None
        self.score = 1000
        self.byes = 0
        self.conflicts = []


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


class PlayerStandings(ndb.Model):
    """
    A model for tracking player standings (resets each year)
    """
    id = ndb.StringProperty(indexed=True)
    name = ndb.StringProperty(indexed=True)
    points = ndb.StringProperty(indexed=True)
    games = ndb.StringProperty(indexed=True)


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
        }

        os = self.request.headers['x-api-os']
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
            player.phone = str(self.request.get('phonenumber')).translate(None, string.punctuation)
            player.schedule_rank = int(self.request.get('count'))
            # Check to see if player played previously, if so, import ELO score
            tp = None
            tp = get_player(self, player.id, (now.year - 1))
            if tp:
                player.elo_score = int((tp.elo_score + 1000) / 2)
            else:
                player.elo_score = 0
            if player.name == "":
                player.name = user.nickname()
            if player.email == "":
                player.email = user.email()
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
        }

        os = self.request.headers['x-api-os']
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
        }

        os = self.request.headers['x-api-os']
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
        }

        os = self.request.headers['x-api-os']
        if os is not None:
            json_data = json.dumps(template_values, indent=4)
            self.response.write(json_data)
        else:
            template = JINJA_ENVIRONMENT.get_template('ftolog.html')
            self.response.write(template.render(template_values))


class FTO(webapp2.RequestHandler):
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

        # Add new slot entries
        for week in range(numWeeks):
            for slot in range(5):
                checkbox_name = str(week + 1) + "-" + str(slot + 1)
                if self.request.get(checkbox_name):
                    fto = Fto(parent=db_key(year))
                    fto.user_id = pid
                    fto.name = player.name
                    fto.week = int(week + 1)
                    fto.slot = int(slot + 1)

                    match_found = False
                    for fto_entry in fto_data:
                        if fto_entry == fto:
                            match_found = True
                    if match_found is False:
                        fto.put()
        # delete removed slot entries
        for fto_entry in fto_data:
            checkbox_name = str(fto_entry.week) + "-" + str(fto_entry.slot)
            if self.request.get(checkbox_name):
                pass
            else:
                fto_entry.key.delete()
        if pid == user.user_id():
            url = "fto"
        else:
            url = "fto?pid=%s" % pid
        self.redirect(str(url))

    def get(self):
        # Filter for this year only
        now = datetime.datetime.today()

        # See if user is logged in and signed up
        login_info = get_login_info(self)
        user = users.get_current_user()

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
            self.redirect('/')

        set_holidays(self)

        # Fill an array with the weeks of the season
        weeks = list()
        for x in range(numWeeks):
            date1 = startdate + datetime.timedelta(days=(7 * x))
            date2 = startdate + datetime.timedelta(days=(4 + 7 * x))
            weeks.append(date1.strftime("%b %d") + " - " + date2.strftime("%b %d"))

        # build a 2D array for the weeks and slots (all False)
        fto_week = list()
        fto_slot = list()
        for w in range(numWeeks):
            for s in range(5):
                fto_slot.append(False)
            fto_week.append(list(fto_slot))

        # Get FTO data
        if pid:
            qry_f = Fto.query(ancestor=db_key(now.year))
            qry_f = qry_f.filter(Fto.user_id == pid)
            fto_data = qry_f.fetch(100)

            # for each set of FTO data, change the array item to True
            for entry in fto_data:
                fto_week[(entry.week - 1)][(entry.slot - 1)] = True
                # logging.info("Week: "+str(entry.week)+" Slot: "+str(entry.slot)+" = "+str(fto_week[(entry.week-1)][(entry.slot-1)]))

        template_values = {
            'year': get_year_string(),
            'page': 'fto',
            'user': user,
            'player': player,
            'is_signed_up': player is not None,
            'login': login_info,
            'weeks': weeks,
            'fto_week': fto_week,
        }

        os = self.request.headers['x-api-os']
        if os is not None:
            json_data = json.dumps(template_values, indent=4)
            self.response.write(json_data)
        else:
            template = JINJA_ENVIRONMENT.get_template('fto.html')
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
                player.phone = str(self.request.get('phone-' + player.id)).translate(None,
                                                                                     string.punctuation).translate(None,
                                                                                                                   string.whitespace)
                player.schedule_rank = int(self.request.get('rank-' + player.id))
                player.elo_score = int(self.request.get('score-' + player.id))
                print("%s is now rank %s" % (player.name, player.schedule_rank))
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
        }

        template = JINJA_ENVIRONMENT.get_template('admin.html')
        self.response.write(template.render(template_values))


class Scheduler(webapp2.RequestHandler):
    # This will run on Fridays for the next week
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
        if week < 1: week = 1
        player_data = get_player_data(week, self)
        old_player_list = player_data.keys()
        #		old_player_list = sorted(old_player_list, key=lambda k: player_data[k].rank)
        old_player_list = sorted(old_player_list, key=lambda k: player_data[k].score, reverse=True)
        logging.info("Week %s Scheduler" % week)

        # If there is no existing schedule for this week, we know it is the first time the scheduler has run for this week
        # So we should first reorder the players based on the previous week's results. Unless this is week 1
        #		if week > 1:
        #			qry = Schedule.query(ancestor=db_key(year))
        #			qry = qry.filter(Schedule.week == week)
        #			if qry.count() == 0: #Check if there is already a schedule for this week, if there isn't, we must reorder the player list based on last week's scores
        #				qry = Schedule.query(ancestor=db_key(year))
        #				qry = qry.filter(Schedule.week == (week-1))
        #				qry = qry.order(-Schedule.tier, Schedule.position)
        #				schedule_results = qry.fetch()
        #				tiers = schedule_results[0].tier
        #				tier_position = []
        #				for x in range(tiers+1):
        #					tier_position.append([])
        #				on_bye = []
        #				for p in schedule_results:
        #					tier_position[p.tier].append([p.id, 0]) # Fill with the player IDs
        #					if p.tier == 0:
        #						on_bye.append(p.id)
        #				qry = Scores.query(ancestor=db_key(year))
        #				qry = qry.filter(Scores.week == (week-1))
        #				qry = qry.order(Scores.tier, Scores.game)
        #				results = qry.fetch()
        #				if results:
        #					for score in results:
        #						if score.game == 1:
        #							tier_position[score.tier][0][1] += (score.score1-score.score2)
        #							tier_position[score.tier][2][1] += (score.score1-score.score2)
        #							tier_position[score.tier][5][1] += (score.score1-score.score2)
        #							tier_position[score.tier][7][1] += (score.score1-score.score2)
        #							tier_position[score.tier][1][1] += (score.score2-score.score1)
        #							tier_position[score.tier][3][1] += (score.score2-score.score1)
        #							tier_position[score.tier][4][1] += (score.score2-score.score1)
        #							tier_position[score.tier][6][1] += (score.score2-score.score1)
        #						if score.game == 2:
        #							tier_position[score.tier][0][1] += (score.score1-score.score2)
        #							tier_position[score.tier][3][1] += (score.score1-score.score2)
        #							tier_position[score.tier][4][1] += (score.score1-score.score2)
        #							tier_position[score.tier][7][1] += (score.score1-score.score2)
        #							tier_position[score.tier][1][1] += (score.score2-score.score1)
        #							tier_position[score.tier][2][1] += (score.score2-score.score1)
        #							tier_position[score.tier][5][1] += (score.score2-score.score1)
        #							tier_position[score.tier][6][1] += (score.score2-score.score1)
        #						if score.game == 3:
        #							tier_position[score.tier][0][1] += (score.score1-score.score2)
        #							tier_position[score.tier][3][1] += (score.score1-score.score2)
        #							tier_position[score.tier][5][1] += (score.score1-score.score2)
        #							tier_position[score.tier][6][1] += (score.score1-score.score2)
        #							tier_position[score.tier][1][1] += (score.score2-score.score1)
        #							tier_position[score.tier][2][1] += (score.score2-score.score1)
        #							tier_position[score.tier][4][1] += (score.score2-score.score1)
        #							tier_position[score.tier][7][1] += (score.score2-score.score1)
        #					for t in range(1, tiers+1):
        #						tier_position[t] = sorted(tier_position[t], key=lambda k: k[1], reverse=True)
        #						logging.info("Tier %s Results - Up(%s, %s), Down (%s, %s)" % (t, player_data[tier_position[t][0][0]].name, player_data[tier_position[t][1][0]].name, player_data[tier_position[t][6][0]].name, player_data[tier_position[t][7][0]].name))
        #					temp_rank_list = []
        #					for x in range(1,tiers+1):
        #						if x == 1: # If the top tier, top performers move to the top
        #							temp_rank_list.append(tier_position[x][0][0])
        #							temp_rank_list.append(tier_position[x][1][0])
        #						temp_rank_list.append(tier_position[x][2][0])
        #						temp_rank_list.append(tier_position[x][3][0])
        #						if x > 1: # If not the top tier, bottom performers from tier above move down here
        #							temp_rank_list.append(tier_position[x-1][6][0])
        #							temp_rank_list.append(tier_position[x-1][7][0])
        #						if x < tiers: # If not the bottom tier, top performers from tier below move up here
        #							temp_rank_list.append(tier_position[x+1][0][0])
        #							temp_rank_list.append(tier_position[x+1][1][0])
        #						temp_rank_list.append(tier_position[x][4][0])
        #						temp_rank_list.append(tier_position[x][5][0])
        #						if x == tiers: # If the bottom tier, bottom performers move to the bottom
        #							temp_rank_list.append(tier_position[x][6][0])
        #							temp_rank_list.append(tier_position[x][7][0])
        #					player_list = []
        #					for p in range(len(old_player_list)):
        #						if old_player_list[p] in on_bye:
        #							player_list.append(old_player_list[p])
        #						elif len(temp_rank_list):
        #							player_list.append(temp_rank_list.pop(0))
        #						else:
        #							player_list.append(old_player_list[p])
        #					# Store the new ranks in the database
        #					qry = Player_List.query(ancestor=db_key(year))
        #					pr = qry.fetch()
        #					for p in pr:
        #						for i,x in enumerate(player_list):
        #							if x == p.id:
        #								p.schedule_rank = i
        #								p.put()
        #
        #				else:
        #					player_list = old_player_list
        #			else:
        #				player_list = old_player_list
        #		else:
        #			player_list = old_player_list
        player_list = old_player_list

        # Need to check for existing scores for this week. If there are scores for this week, we should abort.
        qry = Scores.query(ancestor=db_key(year))
        qry = qry.filter(Scores.week == (week))
        if qry.count() == 0:

            # Create a list of players ids on bye this week because of FTO
            bye_list = list()
            bye_list.append([])  # Add a list for true bye players (4+ days of conflicts this week)
            if player_list:
                for p in player_list:
                    if len(player_data[p].conflicts) >= 4:
                        bye_list[0].append(p)
                        logging.info("%s is on bye." % player_data[p].name)
            num_available_players = int(len(player_list) - len(bye_list[0]))  # number of players not on an FTO bye
            slots_needed = math.floor(
                num_available_players / 8)  # Since we are automatically reducing the slots required if we fail at finding a valid schedule, we only need a minimum of 8 players per tier.
            if slots_needed > 5: slots_needed = 5  # Max of 5 matches per week. We only have 5 slots available.

            valid_schedule = False
            while valid_schedule == False:
                if slots_needed == 0: break  # Cannot create a schedule (too few players or an incredible number of conflicts)
                tier_list = list()  # List of player ids per tier
                tier_slot_list = list()  # List of available slots per tier after removing conflicts for each player in the tier
                tier_slot = list()  # List of the slot each tier will play in

                players_per_slot = float(num_available_players) / float(
                    slots_needed)  # Put this many players into each tier
                counter = 0
                tier_list.append([])  # Add list for tier 0 (bye players)
                tier_list.append([])  # Add list for tier 1 (top players)
                for p in player_list:
                    if p in bye_list[0]:  # player is on a bye and should be added to tier 0
                        pass
                    #					tier_list[0].append(p) #add a player to the bye tier
                    else:  # player is elligible to play and
                        # This code allocated player slots to the tiers when the players_per_slot number isn't an integer (like 9.5 players per tier)
                        counter += 1
                        if counter > players_per_slot and len(tier_list) < slots_needed + 1:
                            counter -= players_per_slot
                            tier_list.append([])  # Add another tier
                        tier_list[len(tier_list) - 1].append(p)  # Add a player to the current tier

                tier_slot_list.append([])  # empty set for tier 0 (byes)
                for x in range(1, len(tier_list)):
                    logging.info("Tier %s: Size %s" % (x, len(tier_list[x])))
                    random.shuffle(tier_list[x])  # randomly shuffle the list so ties in byes are ordered randomly
                    tier_list[x] = sorted(tier_list[x], key=lambda k: player_data[k].byes,
                                          reverse=True)  # order based on byes (decending order). Future orders will be random.
                    tier_slot_list.append(remove_conflicts(tier_list[x], player_data, self))

                for i in range(50):  # Try this up to X times.
                    if not pick_slots(tier_slot, 1,
                                      tier_slot_list):  # iterate through the slots per tier until a solution is found for every tier.
                        # We couldn't find a schedule that works so go back and shuffle the most restrictive player list to get a new set of 8
                        #					stc = find_smallest_set(tier_slot_list) #stc = set to cycle ---- This could cause us to not find a solution. ----
                        stc = random.randint(1, len(
                            tier_slot_list))  # choose a random tier to shuffle. --- We don't know which tier is causing problems, so shuffle one at random ---
                        while stc == len(tier_slot_list):  # Just in case the random choice equals the top limit.
                            stc = random.randint(1, len(tier_slot_list))  # Shuffle again
                        logging.info(
                            "Could not find a valid schedule. Shuffling tier %s and trying again. Count=%s/50" % (
                            stc, i + 1))
                        random.shuffle(tier_list[stc])  # Shuffle the players in a random tier
                        tier_slot_list[stc] = remove_conflicts(tier_list[stc], player_data, self)
                    else:
                        break

                for x in range(1, len(tier_list)):
                    bye_list.append([])
                    for p in range(len(tier_list[x]) - 1, 7, -1):
                        bye_list[x].append(tier_list[x][p])  # Add alternate players to bye list
                        tier_list[x].remove(tier_list[x][p])  # Remove alternate players from the tier list
                    tier_list[x] = sorted(tier_list[x],
                                          key=lambda k: player_data[k].rank)  # Sort the 8 players in each tier by rank

                # Check to see if we have a valid schedule
                valid_schedule = True
                for x in range(1, len(tier_slot)):
                    if not tier_slot[x]:  # No valid slots for this tier - bad news
                        valid_schedule = False
                if valid_schedule == False:  # clear the lists, reduce the number of matches, and try again
                    logging.info("No valid schedule. Dropping from %s matches to %s and trying again." % (
                    slots_needed, slots_needed - 1))
                    del tier_list[:]
                    del tier_slot_list[:]
                    del tier_slot[:]
                    for x in range(len(bye_list) - 1, 0, -1):
                        del bye_list[x]
                    slots_needed -= 1

            # If we reach this point, we have a valid schedule! Save it to the database.
            # First delete any existing schedule for this week (in case the scheduler runs more than once)
            qry = Schedule.query(ancestor=db_key(year))
            qry = qry.filter(Schedule.week == week)
            results = qry.fetch()
            for r in results:
                r.key.delete()

            # Store the bye players and alternate players in the database
            for x in range(len(bye_list)):
                z = 0
                for p in bye_list[x]:
                    s = Schedule(parent=db_key(year))  # database entry
                    s.id = p
                    s.name = player_data[p].name
                    s.week = week
                    s.slot = 0
                    s.tier = 0
                    s.position = tier_slot[
                        x]  # using the position variable to store the slot this player can be an alternate for
                    s.put()
            # store the scheduled players in the database and create calendar events with notifications
            y = 0
            for x in tier_list:
                z = 0
                name_list = list()
                email_list = list()
                for p in x:
                    z += 1
                    s = Schedule(parent=db_key(year))  # database entry
                    s.id = p
                    s.name = player_data[p].name
                    s.week = week
                    s.slot = tier_slot[y]
                    s.tier = y
                    s.position = z  # 1-8
                    s.put()  # Stores the schedule data in the database

                    # Add the player names and emails to some lists for creating and sending an iCalendar event
                    name_list.append(player_data[p].name)
                    email_list.append(player_data[p].email)

                if y > 0:  # If this isn't tier 0 (players on bye)...
                    # Calculate the date for this match
                    match_date = startdate + datetime.timedelta(days=(7 * (week - 1) + (tier_slot[y] - 1)))
                    start_time = datetime.datetime.combine(match_date, datetime.time(12, 0, 0))
                    end_time = datetime.datetime.combine(match_date, datetime.time(13, 0, 0))

                    service = build('calendar', 'v3')
                    event = {
                        'summary': 'Sand VolleyBall Match',
                        'location': 'N/S Sand Court',
                        'description': "Week %s Sand Volleyball Match" % week,
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
                    event = service.events().insert(calendarId='brianbartlow@gmail.com', body=event,
                                                    sendNotifications=True).execute()
                y += 1
        else:
            logging.info("There are scores in the system for this week. Aborting.")

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
        kfactor = 500  # 2018 = 200, 2019 = ?

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
        qry = qry.order(-Schedule.tier,
                        Schedule.position)  # Order based on decending tier so we can get a count of how many tiers there were this week.
        schedule_results = qry.fetch()
        tiers = schedule_results[0].tier  # The number of tiers is equal to the highest tier from the schedule.

        # Set up lists to store the average team Elo and scores for each game(3) in each tier(variable).
        team_elo = []
        scores = []
        for x in range(tiers + 1):
            team_elo.append([[0, 0], [0, 0], [0, 0]])
            scores.append([[0, 0], [0, 0], [0, 0]])

        # Calculate the average Elo scores for each team
        for p in schedule_results:
            if p.tier > 0:
                for x in range(3):
                    team_elo[p.tier][x][team_map[x][p.position - 1]] += float(
                        player_data[p.id].score)  # Add elo_score to team_elo[tier][game][team]
        # logging.info("team_elo = %s (tier %s, game %s, team %s)" % (team_elo[p.tier][x][team_map[x][p.position-1]], p.tier, x+1, team_map[x][p.position-1]+1))
        for x in range(tiers + 1):
            for y in range(3):
                for z in range(2):
                    team_elo[x][y][z] /= float(4)  # Average Elo_Score for each team
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

        # Now iterate through each player on the schedule and calculate their new Elo score based on their old Elo score, the game scores, and the teams' average Elo scores.
        new_elo = {}
        for p in schedule_results:
            if p.tier > 0:
                new_elo[p.id] = player_data[p.id].score
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
        # Store the new Elo scores in the database
        qry = Player_List.query(ancestor=db_key(year))
        pr = qry.fetch()
        for p in pr:
            if p.id in new_elo:  # Only store new scores if there is a new score to store :)
                p.elo_score = new_elo[p.id]
                p.put()


class Standings(webapp2.RequestHandler):
    def get(self):
        users.get_current_user()
        now = datetime.datetime.today()
        if self.request.get('y'):
            year = int(self.request.get('y'))
        else:
            year = now.year
        login_info = get_login_info(self)
        player = get_player(self)

        # Get player list
        qry_p = Player_List.query(ancestor=db_key(year))
        qry_p = qry_p.order(-Player_List.elo_score)
        player_list = qry_p.fetch()

        template_values = {
            'current_year': now.year,
            'year': year,
            'page': 'admin',
            'player_list': player_list,
            'is_signed_up': player is not None,
            'login': login_info,
        }

        os = self.request.headers['x-api-os']
        if os is not None:
            json_data = json.dumps(template_values, indent=4)
            self.response.write(json_data)
        else:
            template = JINJA_ENVIRONMENT.get_template('standings.html')
            self.response.write(template.render(template_values))


class Sub(webapp2.RequestHandler):
    def get(self):
        user = users.get_current_user()
        now = datetime.datetime.today()
        get_login_info(self)
        get_player(self)
        week = int(self.request.get('w'))
        sub_id = self.request.get('id')
        player_data = get_player_data(week, self)

        qry = Schedule.query(ancestor=db_key(now.year))
        qry = qry.filter(Schedule.week == week)
        sr = qry.fetch()
        swap_id = None
        player_list = []
        success = "n"

        # Check to make sure the sub_id is a currently active player (otherwise, someone else may have already accepted the sub request.)
        for x in sr:
            if x.id == sub_id:
                if x.slot != 0:
                    # Check to make sure the logged in player is an alternate for the sub requester's slot
                    slot = x.slot
                    tier = x.tier
                    if user:
                        for y in sr:
                            if y.id == user.user_id():
                                if y.position == slot:
                                    # Make the swap
                                    swap_id = y.id
        if swap_id:
            success = "y"
            for x in sr:
                if x.slot == slot and x.id != sub_id:
                    player_list.append(
                        x.id)  # Add everyone already in this slot to a list except the player being subbed out.
            player_list.append(swap_id)  # Then add the player being swapped in.
            player_list = sorted(player_list, key=lambda k: player_data[k].rank)  # Sort the list by rank
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

            sg = sendgrid.SendGridAPIClient(apikey=keys.API_KEY)
            from_email = Email("noreply@hpsandvolleyball.appspot.com")
            to_email = Email("brian.bartlow@hp.com")
            subject = "Substitution Successful"
            content = Content("text/html",
                              "This notice is to inform you that the substitution has been completed successfully. Since the system doesn't automatically update the meeting invites, %s, please forward your meeting invitation to <a href=\"mailto:%s\">%s</a>." % (
                                  player_data[sub_id].name, player_data[swap_id].email, player_data[swap_id].name))
            mail = Mail(from_email, subject, to_email, content)
            personalization = Personalization()
            personalization.add_to(Email(player_data[sub_id].email))
            personalization.add_to(Email(player_data[swap_id].email))
            mail.add_personalization(personalization)
            sg.client.mail.send.post(request_body=mail.get())

        self.redirect("week?w=%s&m=%s" % (week, success))


class WeeklySchedule(webapp2.RequestHandler):
    def post(self):
        sg = sendgrid.SendGridAPIClient(apikey=keys.API_KEY)
        user = users.get_current_user()
        now = datetime.datetime.today()
        get_login_info(self)
        get_player(self)
        week = int(self.request.get('w'))
        player_data = get_player_data(week, self)

        from_email = Email("noreply@hpsandvolleyball.appspot.com")
        to_email = Email("brian.bartlow@hp.com")

        if self.request.get('action') == "Sub":
            if user:
                sub_id = user.user_id()
                qry = Schedule.query(ancestor=db_key(now.year))
                qry = qry.filter(Schedule.week == week)
                sr = qry.fetch()
                notification_list = []
                sendit = False

                for x in sr:
                    if x.id == sub_id:  # Find the slot of the person needing a sub
                        if x.slot != 0:
                            slot = x.slot
                            for y in sr:
                                if y.slot == 0 and y.position == slot:  # find the alternates for the player needing a sub
                                    notification_list.append(player_data[y.id].email)
                                    sendit = True
                            break

                subject = "%s needs a Sub" % player_data[sub_id].name
                content = Content("text/html",
                                  "<p>%s needs a sub on %s. If you can play, please click <a href = \"http://hpsandvolleyball.appspot.com/sub?w=%s&id=%s\">this link</a>. The first to accept the invitation will get to play.</p><strong>NOTE: The system is not currently able to update the invitations, so please remember to check the website for the official schedule.</strong>" % (
                                      player_data[sub_id].name,
                                      (startdate + datetime.timedelta(days=(7 * (week - 1) + (slot - 1)))).strftime(
                                          "%A %m/%d"), week, sub_id))
                logging.info(subject)
                logging.info("sending to: %s" % notification_list)
                if sendit:
                    mail = Mail(from_email, subject, to_email, content)
                    if len(notification_list):
                        personalization = Personalization()
                        for e in notification_list:
                            personalization.add_to(Email(e))
                        mail.add_personalization(personalization)
                    sg.client.mail.send.post(request_body=mail.get())

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
        slots = []
        for d in range(5):
            slots.append(startdate + datetime.timedelta(days=(7 * (week - 1) + d)))

        qry = Schedule.query(ancestor=db_key(year))
        qry = qry.filter(Schedule.week == week)
        qry = qry.order(Schedule.slot, Schedule.position)
        schedule_data = qry.fetch()

        active = 0
        if user:
            for s in schedule_data:
                if s.id == user.user_id() and s.slot != 0:
                    deadline = startdate + datetime.timedelta(days=(7 * (week - 1)) + (s.slot - 1))
                    if datetime.datetime.today() < datetime.datetime(deadline.year, deadline.month, deadline.day,
                                                                     18):  # noon Mountain time on the day of the match
                        active = 1

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
        }

        os = self.request.headers['x-api-os']
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
        if today == schedule_day or not score[2][1]:
            is_today = True

        os = self.request.headers['x-api-os']
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
        }

        if os is not None:
            json_data = json.dumps(template_values, indent=4)
            self.response.write(json_data)
        else:
            template = JINJA_ENVIRONMENT.get_template('day.html')
            self.response.write(template.render(template_values))


class Notify(webapp2.RequestHandler):
    def get(self):
        sg = sendgrid.SendGridAPIClient(apikey=keys.API_KEY)

        today = datetime.date.today()
        year = today.year

        # Calculate what week and day it is
        week = int(math.floor(int((today - startdate).days) / 7) + 1)
        day = today.isoweekday()

        from_email = Email("noreply@hpsandvolleyball.appspot.com")
        # to_email = Email("")
        to_email = Email("brian.bartlow@hp.com")
        subject = "Please Ignore"
        content = Content("text/html", "Please ignore this email, I am testing new functionality on the website.")

        player_data = get_player_data(1, self)
        sendit = False
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
                    subject = "Reminder to submit scores"
                    content = Content("text/html",
                                      "At the moment this email was generated, the scores haven't been entered for today's games. Please go to the <a href=\"http://hpsandvolleyball.appspot.com/day\">Score Page</a> and enter the scores. If someone has entered the scores by the time you check, or the games were not actually played, please disregard.")
                    sendit = True
                    for s in schedule_data:
                        # pass
                        notification_list.append(player_data[s.id].email)
            else:
                logging.info("There are no games scheduled for today.")

        elif self.request.get('t') == "fto":
            subject = "Reminder to check and update your FTO/Conflicts for next week"
            content = Content("text/html", """Next week's schedule will be generated at 2:00pm. Please go to the <a href=\"http://hpsandvolleyball.appspot.com/fto\">FTO Page</a> and check to make sure your schedule is up-to-date for next week.
            If that link doesn't work, please log in with the Google account used when you signed up.""")
            sendit = True
            for p in player_data:
                # logging.info("%s - %s" % (player_data[p].name,player_data[p].email))
                if player_data[p].email:
                    # pass
                    notification_list.append(player_data[p].email)

        elif self.request.get('t') == "test":
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

        elif self.request.get('t') == "sms":
            from googlevoice import Voice
            from googlevoice.util import input

            voice = Voice()
            voice.login()

            phone_number = '2082831663'
            text = 'Test SMS from hpsandvolleyball'

            voice.send_sms(phone_number, text)

        elif self.request.get('t') == "log":
            logging.info('This is an info log message')
            self.response.out.write('Logging example.')

        if sendit:
            mail = Mail(from_email, subject, to_email, content)
            if len(notification_list):
                personalization = Personalization()
                for e in notification_list:
                    personalization.add_to(Email(e))
                mail.add_personalization(personalization)
            response = sg.client.mail.send.post(request_body=mail.get())
            print(response.status_code)
            print(response.body)
            print(response.headers)


app = webapp2.WSGIApplication([
    ('/', MainPage),
    ('/signup', Signup),
    ('/unsignup', Unsignup),
    ('/info', Info),
    ('/ftolog', Ftolog),
    ('/fto', FTO),
    ('/week', WeeklySchedule),
    ('/day', DailySchedule),
    ('/standings', Standings),
    ('/sub', Sub),
    ('/admin', Admin),
    ('/tasks/notify', Notify),
    ('/tasks/scheduler', Scheduler),
    ('/tasks/elo', Elo),
], debug=True)
