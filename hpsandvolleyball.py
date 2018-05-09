import os
import urllib
import datetime
import logging
# This is needed for timezone conversion (but not part of standard lib)
#import dateutil

from google.appengine.api import users
from google.appengine.ext import ndb

import jinja2
import webapp2

# Globals - I want these eventually to go into a datastore per year so things can be different and configured per year. For now, hard-coded is okay.
numWeeks = 14
startdate = datetime.date(year, 5, 21)

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
    result = Entry()
    if user:
        qry = Entry.query(ancestor=db_key(now.year))
        qry = qry.filter(Entry.player.identity == user.user_id())
        result = qry.get()
    if result:
        return result.player
    else:
        return None

def set_holidays(x):
	# Check and set holidays to unavailable
	now = datetime.datetime.today()
    login_info = get_login_info(x)
    user = users.get_current_user()
	qry_f = Fto.query(ancestor=db_key(now.year))
	qry_f = qry_f.filter(Fto.user_id == user.user_id())
	fto_data = qry_f.fetch(100)
	player = get_player(x)
	if player:
		holidays = ((2,1),(3,4),(7,3))
		
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
		
class Player(ndb.Model):
    """
    Sub model for representing a player.
    """
    identity = ndb.StringProperty(indexed=True)
    email    = ndb.StringProperty(indexed=False)
    name     = ndb.StringProperty(indexed=False)
    phone    = ndb.StringProperty(indexed=False)
    
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
   
class Entry(ndb.Model):
    """
    A main model for representing an individual player entry.
    """
    player    = ndb.StructuredProperty(Player)
    comment   = ndb.StringProperty(indexed=False)
    committed = ndb.BooleanProperty(indexed=True)
    date      = ndb.DateTimeProperty(auto_now_add=True)

	
class Schedule(ndb.Model):
	"""
	A model for tracking the weekly and daily schedule
	"""
	id			= ndb.StringProperty(indexed=True)
	name		= ndb.StringProperty(indexed=True)
	week		= ndb.IntegerProperty(indexed=True)
	slot		= ndb.IntegerProperty(Indexed=True)
	tier		= ndb.IntegerProperty(Indexed=True)
	position	= ndb.IntegerProperty(Indexed=True)
	
class Player_List(ndb.Model):
	"""
	A model for tracking the ordered list for scheduling
	"""
	id				= ndb.StringProperty(indexed=False)
	name			= ndb.StringProperty(indexed=False)
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
        if user:
            entry = Entry(parent=db_key(now.year))
            entry.player = Player(identity=user.user_id(), email=self.request.get('email'), name=self.request.get('name'), phone=self.request.get('phonenumber') )
            if entry.player.name == "":
                entry.player.name = user.nickname()
            if entry.player.email == "":
                entry.player.email = user.email()
            entry.comment = ""
            if self.request.get('action') == "Commit":
                entry.committed = True
            else:
                entry.committed = False
            entry.put()
            self.redirect('signup')
	
    def get(self):
        now = datetime.datetime.today()
        
        # Get committed entries list
        qry_c = Entry.query(ancestor=db_key(now.year))
        qry_c = qry_c.filter(Entry.committed == True)
        qry_c = qry_c.order(Entry.date)
        entries_c = qry_c.fetch(100)

        # See if user is logged in and signed up
        login_info = get_login_info(self)
        user = users.get_current_user()
        player = get_player(self)
        template_values = {
            'year': get_year_string(),
            'page': 'signup',
            'user': user,
            'entries_c': entries_c,
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
            qry = Entry.query(ancestor=db_key(now.year))
            entries = qry.fetch(100)
            for entry in entries:
                if entry.player.identity == user.user_id(): 
                    entry.key.delete()
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
        entries = qry.fetch()

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

        qry_p = Entry.query(ancestor=db_key(now.year))
        qry_p = qry_p.filter(Entry.player.identity == user.user_id())
        entry = qry_p.get()
        
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
                    fto.name = entry.player.name
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
        
        # Get committed entries list
        qry_c = Entry.query(ancestor=db_key(now.year))
        qry_c = qry_c.filter(Entry.committed == True)
        qry_c = qry_c.order(Entry.date)
        entries_c = qry_c.fetch(100)

        # See if user is logged in and signed up
        login_info = get_login_info(self)
        user = users.get_current_user()
        player = get_player(self)
        if player is None:
            self.redirect('/')
			
		set_holiday(self)
        
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

class Schedule(webapp2.RequestHandler):
	def get(self):
        # Filter for this year only
        now = datetime.datetime.today()
        year = now.year		
		
		
		
app = webapp2.WSGIApplication([
    ('/',           		MainPage),
	('/signup',				Signup),
    ('/unsignup', 			Unsignup),
    ('/info',     			Info),
    ('/ftolog',      		Ftolog),
    ('/fto',     	    	FTO),
	('/tasks/schedule',		Schedule),
], debug=True)