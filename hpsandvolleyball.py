import os
import urllib
import datetime
import logging
import string
import math
import random
# This is needed for timezone conversion (but not part of standard lib)
#import dateutil

from google.appengine.api import users
from google.appengine.ext import ndb

import jinja2
import webapp2

# Globals - I want these eventually to go into a datastore per year so things can be different and configured per year. For now, hard-coded is okay.
numWeeks = 14
startdate = datetime.date(2018, 5, 21)
holidays = ((2,1),(3,4),(7,3)) #Memorial Day, BYITW Day, Independance Day

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

def chat_db_key(db_name):
    """
    Constructs a Datastore key for a chat comment list.
    We use the list name as the key.
    """
    return ndb.Key("ChatEntries", db_name)

def get_login_info(h):
    user = users.get_current_user()
    if user:
        logged_in = True
        url       = users.create_logout_url(h.request.uri)
        linktext  = 'Logout'
    else:
        logged_in = False
        url       = users.create_login_url(h.request.uri)
        linktext  = 'Login'
    info = {
        'logged_in': logged_in, 
        'url': url, 
        'linktext': linktext,
    }
    return info

def get_year_string():
    now = datetime.datetime.utcnow()
    return now.strftime("%Y")

def get_player(x):
    # Get committed entries list
    now = datetime.datetime.today()
    login_info = get_login_info(x)
    user = users.get_current_user()
    result = Player_List()
    if user:
        qry = Player_List.query(ancestor=db_key(now.year))
        qry = qry.filter(Player_List.id == user.user_id())
        result = qry.get()
        if result:
            return result
    return None

def set_holidays(x):
	# Check and set holidays to unavailable
    now = datetime.datetime.today()
    year = now.year
    login_info = get_login_info(x)
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
                            
            matchFound = False
            for fto_entry in fto_data:
                if fto_entry == fto:
                    matchFound = True
            if matchFound == False:
                fto.put()

def assign_byes(week, player_list):
    # Find players who are on a bye. If they have four or more bad days they will be automatically placed on a bye.
    now = datetime.datetime.today()
    year = now.year
    bye_list = list()
    for player in player_list:
        qry = Fto.query(ancestor=db_key(year))
        qry=qry.filter(Fto.user_id == player.id)
        qry = qry.filter(FTO.week == week)
        if qry.count() >= 4:
            bye_list.append(player.id)
    return bye_list

def get_player_data(current_week):
    now = datetime.datetime.today()
    year = now.year
    pl = {}
    fto_count = {}
    # Get player list
    qry = Player_List.query(ancestor=db_key(now.year))
    plr = qry.fetch(100)
    for each player in plr:
        pl[player.id] = Player()
        pl[player.id].name = player.name
        pl[player.id].email = player.email
        pl[player.id].phone = player.phone
        pl[player.id].rank = player.schedule_rank
        pl[player.id].score = player.elo_score
        
        # Need a dict of lists to count the conflicts for each week per player. Initialized with zeros.
        fto_count[player.id]=[0] * numWeeks
     
    # Check previous schedules for byes or alternates
    qry = Schedule.query(ancestor=db_key(year))
    qry = qry.filter(Schedule.tier == 0)
    past_byes = qry.fetch()
    if past_byes:
        for bye in past_byes:
            if bye.week < current_week: #in the past
                pl[bye.user_id].byes += 1

    # Check future fto for byes
    qry = Fto.query(ancestor=db_key(year))
    fto = qry.fetch()
    if fto:
        for f in fto:
            fto_count[f.user_id][f.week-1]+=1
            if fto_count[f.user_id][f.week-1] == 4: #Once we reach 4 conflicts in a week, that's a bye week. Don't want to double-count on the 5th conflict.
                pl[f.user_id].byes += 1
            if f.week = current_week: #To make things easy, we can populate the weekly conflicts while iterating through the fto list.
                pl[player.id].conflicts.append(f.slot)
            

def pick_slot(a, b, c):
    if b >= len(c): return True
    while len(a)<b+1:a.append(0)
    a[b]=0
    for x in c[b]:
        if x not in a:
            a[b] = x
            if pick_slot(a, b+1, c):
                return True
    return False

def find_smallest_set(set_list):
    smallest_set = len(set_list[1])
    smallest_set_pos = 1
    for p in range(2,len(set_list):
        if len(set_list[p]) < smallest_set:
            smallest_set = len(set_list[p])
            smallest_set_pos = p
    return smallest_set_pos

def remove_conflicts(player_ids, player_data, count=1):
    if count > 20: return []
    slots = range(1,6)
    y=0
    for p in player_ids:
        y+=1
        if y > 8: break # use the data from the first 8 players in the tier
        for s in player_data[p].conflicts:
            if s in slots:
                slots.remove(s)
    if len(slots) == 0:
        random.shuffle(player_ids)
        return remove_conflicts(player_ids, player_data, count+1)
    random.shuffle(tier_slot_list[x]) #randomize the order of the available slots
    return slots
    
class Player(object):
    def __init__(self):
        self.name = None
        self.email = None
        self.phone = None
        self.rank = None
        self.score = 1000
        self.byes = None
        self.conflicts = []
    
   
class Fto(ndb.Model):
    """
    A model for storing conflicting days per player.
    """
    user_id     = ndb.StringProperty(indexed=True)
    week        = ndb.IntegerProperty(indexed=True)
    slot        = ndb.IntegerProperty(indexed=True)
    name        = ndb.StringProperty(indexed=True)
 
    def __eq__(self, other):
        """Overrides the default implementation"""
        if isinstance(self, other.__class__):
            return (self.user_id == other.user_id and self.week == other.week and self.slot == other.slot)
        return NotImplemented
   
class Schedule(ndb.Model):
    """
    A model for tracking the weekly and daily schedule
    """
    id			= ndb.StringProperty(indexed=True)
    name		= ndb.StringProperty(indexed=True)
    week		= ndb.IntegerProperty(indexed=True)
    slot		= ndb.IntegerProperty(indexed=True)
    tier		= ndb.IntegerProperty(indexed=True)
    position	= ndb.IntegerProperty(indexed=True)
	
class Player_List(ndb.Model):
    """
    A model for tracking the ordered list for scheduling
    """
    id				= ndb.StringProperty(indexed=True)
    email           = ndb.StringProperty(indexed=False)
    name			= ndb.StringProperty(indexed=True)
    phone           = ndb.StringProperty(indexed=False)
    schedule_rank	= ndb.IntegerProperty(indexed=True)
    elo_score		= ndb.IntegerProperty(indexed=True)

class Scores(ndb.Model):
    """
    A model for tracking game scores
    """
    week			= ndb.IntegerProperty(indexed=True)
    slot			= ndb.IntegerProperty(indexed=True)
    game			= ndb.IntegerProperty(indexed=True)
    team1_score		= ndb.IntegerProperty(indexed=False)
    team2_score		= ndb.IntegerProperty(indexed=False)
	
    
class MainPage(webapp2.RequestHandler):
    """
    Reads the database and creates the data for rendering the signup list
    """
    def get(self):
        # Filter for this year only
        now = datetime.datetime.today()
        
        set_holidays(self)

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
            player.elo_score = 1000
            if player.name == "":
                player.name = user.nickname()
            if player.email == "":
                player.email = user.email()
            if self.request.get('action') == "Commit":
                player.put()
            self.redirect('signup')
	
    def get(self):
        now = datetime.datetime.today()
        
        # Get committed entries list
        qry_p = Player_List.query(ancestor=db_key(now.year))
        qry_p = qry_p.order(Player_List.name)
        player_list = qry_p.fetch(100)

        # See if user is logged in and signed up
        login_info = get_login_info(self)
        user = users.get_current_user()
        player = get_player(self)
        template_values = {
            'year': get_year_string(),
            'page': 'signup',
            'user': user,
            'player_list': player_list,
            'is_signed_up': player is not None,
            'player': player,
            'login': login_info,
        }

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
            logging.info("in user section")
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
            'page' : 'info', 
            'login': login_info,
            'is_signed_up': get_player(self) is not None,
        }
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
        entries = qry.fetch(500)

        login_info = get_login_info(self)
        template_values = {
            'year': get_year_string(),
            'page': 'log', 
            'login': login_info, 
            'entries': entries,
            'is_signed_up': player is not None,
        }
        template = JINJA_ENVIRONMENT.get_template('ftolog.html')
        self.response.write(template.render(template_values))
      
class FTO(webapp2.RequestHandler):
    """
    Renders Schedule page
    """
    def post(self):
        now = datetime.datetime.today()
        year = now.year
        
        login_info = get_login_info(self)
        user = users.get_current_user()

        player = get_player(self)
        
        qry_f = Fto.query(ancestor=db_key(now.year))
        qry_f = qry_f.filter(Fto.user_id == user.user_id())
        fto_data = qry_f.fetch(100)
        
        # Add new slot entries
        for week in range(numWeeks):
            for slot in range(5):
                checkbox_name = str(week+1)+"-"+str(slot+1)
                if self.request.get(checkbox_name):
                    fto = Fto(parent=db_key(year))
                    fto.user_id = user.user_id()
                    fto.name = player.name
                    fto.week = int(week+1)
                    fto.slot = int(slot+1)
                    
                    matchFound = False
                    for fto_entry in fto_data:
                        if fto_entry == fto:
                            matchFound = True
                    if matchFound == False:
                        fto.put()
        # delete removed slot entries
        for fto_entry in fto_data:
            checkbox_name = str(fto_entry.week)+"-"+str(fto_entry.slot)
            if self.request.get(checkbox_name):
                pass
            else:
                fto_entry.key.delete()
            
        self.redirect("fto")
    
    def get(self):
        # Filter for this year only
        now = datetime.datetime.today()
        year = now.year
        
        # See if user is logged in and signed up
        login_info = get_login_info(self)
        user = users.get_current_user()
        player = get_player(self)
        if player is None:
            self.redirect('/')

        set_holidays(self)

        # Fill an array with the weeks of the season
        weeks = list()
        for x in range(numWeeks):
            date1 = startdate + datetime.timedelta(days=(7*x))
            date2 = startdate + datetime.timedelta(days=(4+7*x))
            weeks.append(date1.strftime("%b %d") + " - " + date2.strftime("%b %d"))

        # build a 2D array for the weeks and slots (all False)
        fto_week = list()
        fto_slot = list()
        for w in range(numWeeks):
            for s in range(5):
                fto_slot.append(False)
            fto_week.append(list(fto_slot))
            
        # Get FTO data
        if user:
            qry_f = Fto.query(ancestor=db_key(now.year))
            qry_f = qry_f.filter(Fto.user_id == user.user_id())
            fto_data = qry_f.fetch(100)
        
            # for each set of FTO data, change the array item to True
            for entry in fto_data:
                fto_week[(entry.week-1)][(entry.slot-1)] = True
#                self.response.out.write("Week: "+str(entry.week)+" Slot: "+str(entry.slot)+" = "+str(fto_week[(entry.week-1)][(entry.slot-1)]))
        
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

        template = JINJA_ENVIRONMENT.get_template('fto.html')
        self.response.write(template.render(template_values))

class Admin(webapp2.RequestHandler):
    def post(self):
        user = users.get_current_user()
        now = datetime.datetime.today()
        
        # Get player list
        qry_p = Player_List.query(ancestor=db_key(now.year))
        qry_p = qry_p.order(Player_List.schedule_rank)
        player_list = qry_p.fetch(100)         
        
        for player in player_list:
            player.name = self.request.get('name-'+player.id)
            player.email = self.request.get('email-'+player.id)
            player.phone = str(self.request.get('phone-'+player.id)).translate(None, string.punctuation).translate(None, string.whitespace)
            player.schedule_rank = int(self.request.get('rank-'+player.id))
            player.elo_score = int(self.request.get('score-'+player.id))

            if self.request.get('action') == "Submit":
                player.put()
        self.redirect('admin')   

    def get(self):
        # Filter for this year only
        now = datetime.datetime.today()
        year = now.year
        login_info = get_login_info(self)

        # Get player list
        qry_p = Player_List.query(ancestor=db_key(now.year))
        qry_p = qry_p.order(Player_List.schedule_rank)
        player_list = qry_p.fetch()   
        
        template_values = {
            'year': get_year_string(),
            'page': 'admin',
            'player_list': player_list,
            'is_signed_up': True,
            'login': login_info,
        }

        template = JINJA_ENVIRONMENT.get_template('admin.html')
        self.response.write(template.render(template_values))

class Schedule(webapp2.RequestHandler):
    # This will run on Fridays for the next week
    def get(self):
        # Filter for this year only
        today = datetime.datetime.today()
        year = today.year

        # Calculate what week# next week will be
        week = math.floor(int(startdate - today)/7)+1
        week = 1 if week < 1
        # Now that we know what week it will be next, get the players who will definitely be on bye
        player_data = get_player_data(week)
        # Create a list of players ids on bye this week because of FTO
        bye_list = []
        for p in player_data:
            if len(player_data[p].conflicts)>= 4: bye_list.append(p)
        num_available_players = int(len(player_list) - len(bye_list)) #number of players not on bye
        slots_needed = math.floor(num_available_players / 9) # Should always have at least one alternate per tier
        slots_needed = 4 if slots_needed > 4 # Max of 4 matches per week.  -------Check This-------
        players_per_slot = float(num_available_players) / float(slots_needed) #Put this many players into each tier
        tier_list = list() # List of a list of player ids per tier
        counter = 0
        tier_list.append([]) #Add list for tier 0 (bye players)
        tier_list.append([]) #Add list for tier 1 (top players)
        for p in player_data:
            if player_data[p].id is in bye_list: # player is on a bye and should be added to tier 0
                tier_list[0].append(player_data[p].id) #add a player to the bye tier
            else: #player is elligible to play and 
                # This code allocated player slots to the tiers when the players_per_slot number isn't an integer (like 9.5 players per tier)
                counter += 1
                if counter > players_per_slot:
                    counter -= players_per_slot
                    tier_list.append([]) #Add another tier
                tier_list[len(tier_list)-1].append(player_data[p].id) #Add a player to the current tier
        
        tier_slot_list = list() # Create a list of available slots per tier (8 players combined)
        tier_slot_list.append([]) # empty set for tier 0 (byes)
        for x in range(1,len(tier_list)):
            random.shuffle(tier_list[x]) #randomly shuffle the list so ties in byes are ordered randomly
            tier_list[x] = sorted(tier_list[x], key=lambda k:player_data[k].byes, reverse) #order based on byes (decending order). Future orders will be random.
            tier_slot_list.append(remove_conflicts(tier_list[x], player_data))
        
        tier_slot=list() # Create a list to store the slot where each tier will play.
        for i in range(50): # Try this up to X times.
            if not pick_slots(tier_slot, 1, tier_slot_list):
                # We couldn't find a schedule that works so go back and shuffle the most restrictive player list to get a new set of 8
                stc = find_smallest_set(tier_slot_list) #stc = set to cycle
                random.shuffle(tier_list[stc]) # Shuffle the players in the most restrictive tier.
                tier_slot_list[stc] = remove_conflicts(tier_list[x], player_data)
            else:
                break
        
        for x in range(1, len(tier_list)):
            for p in range(9, len(tier_slot_list[x])):
                bye_list.append(p) # Add alternate players to bye list
                tier_slot_list[x].remove(p) # Remove alternate players from the tier list
            tier_slot_list[x] = sorted(tier_slot_list[x], key lambda k:player_data[k].rank) # Sort the 8 players in each tier by rank
        
        print(tier_slot_list)
		
app = webapp2.WSGIApplication([
    ('/',           		MainPage),
	('/signup',				Signup),
    ('/unsignup', 			Unsignup),
    ('/info',     			Info),
    ('/ftolog',      		Ftolog),
    ('/fto',     	    	FTO),
    ('/admin',              Admin),
	('/tasks/schedule',		Schedule),
], debug=True)