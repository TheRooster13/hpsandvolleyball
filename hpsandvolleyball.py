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

def get_vball_type(h):
    comps = h.request.path.split("/")
    logging.info("comps: %s" % comps)
    return comps[1]

class Player(ndb.Model):
    """
    Sub model for representing a player.
    """
    identity = ndb.StringProperty(indexed=False)
    email    = ndb.StringProperty(indexed=False)
    name     = ndb.StringProperty(indexed=False)

class Entry(ndb.Model):
    """
    A main model for representing an individual player entry.
    """
    player    = ndb.StructuredProperty(Player)
    comment   = ndb.StringProperty(indexed=False)
    committed = ndb.BooleanProperty(indexed=True)
    date      = ndb.DateTimeProperty(auto_now_add=True)

class ChatEntry(ndb.Model):
    """
    Model for representing an chat comment.
    """
    identity = ndb.StringProperty(indexed=False)
    email    = ndb.StringProperty(indexed=False)
    name     = ndb.StringProperty(indexed=False)
    comment  = ndb.StringProperty(indexed=False)
    date     = ndb.DateTimeProperty(auto_now_add=True)

class ChatEntryLocal(object):
    """
    To covert datetime to local timezone
    """

    def __init__(self, entry):
        self.identity = entry.identity
        self.email    = entry.email
        self.name     = entry.name
        self.comment  = entry.comment
        self.date     = self.utc_to_local(entry.date)

    def utc_to_local(self, utc_dt):
        # Becase GAE doesn't have dateutil
        utc_offset = -6 # Through testing - not sure about DST
        hour = int(utc_dt.strftime("%H"))
        min  = int(utc_dt.strftime("%M"))
        hour = ((25 + hour + utc_offset) % 24) - 1
        ampm = "PM" if (hour > 11) else "AM"
        if hour > 12:
            hour = hour - 12
        return "%d:%02d %s" % (hour, min, ampm)
        
        ## Define zones
        #utc_zone = dateutil.tz.gettz('UTC')
        #mtn_zone = dateutil.tz.gettz('America/Boise')
        ## Tell the datetime object that it's in UTC time zone since 
        ## datetime objects are 'naive' by default
        #utc_dt = utc_dt.replace(tzinfo=utc_zone)
        ## Convert time zone to mountain
        #return utc_dt.astimezone(mtn_zone)

class MainPage(webapp2.RequestHandler):
    """
    Reads the database and creates the data for rendering the signup list
    """
    def get(self):
        # Filter for this year only
        now = datetime.datetime.today()

        # Get committed entries list
        qry_c = Entry.query(ancestor=db_key(now.year))
        qry_c = qry_c.filter(Entry.committed == True)
        qry_c = qry_c.order(Entry.date)
        entries_c = qry_c.fetch(100)

        # See if user is logged in and signed up
        login_info = get_login_info(self)
        user = users.get_current_user()
        is_signed_up = False
        signed_up_entry = None
        if user:
            for entry in entries_c + entries_m:
                if entry.player.identity == user.user_id():
                    is_signed_up = True
                    signed_up_entry = entry

        template_values = {
            'year': get_year_string(),
            'page': 'signup',
            'user': user,
            'entries_c': entries_c,
           'is_signed_up': is_signed_up,
            'signed_up_entry': signed_up_entry,
            'login': login_info,
        }

        template = JINJA_ENVIRONMENT.get_template('signup.html')
        self.response.write(template.render(template_values))

class Chat(webapp2.RequestHandler):
    """
    Manages adding a chat message.
    """
    def post(self):
        user = users.get_current_user()
        now = datetime.datetime.today()
        if user:
            entry = ChatEntry(parent=chat_db_key(now.year))
            entry.identity = user.user_id()
            entry.email    = user.email()
            entry.name     = user.nickname()
            entry.comment  = self.request.get('comment')
            entry.put()
            self.redirect('/')

class Signup(webapp2.RequestHandler):
    """
    Manages adding a new player to the signup list for this season.
    """
    def post(self):
        user = users.get_current_user()
        now = datetime.datetime.today()
        if user:
            entry = Entry(parent=db_key(now.year))
            entry.player = Player(identity=user.user_id(), email=user.email(), name=user.nickname())
            entry.comment = ""
            if self.request.get('action') == "Commit":
                entry.committed = True
            else:
                entry.committed = False
            entry.put()
            self.redirect('/')

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
        self.redirect('/')

class Info(webapp2.RequestHandler):
    """
    Renders Info page
    """
    def get(self):
        login_info = get_login_info(self)
        template_values = { 
            'year': get_year_string(),
            'page' : 'info', 
            'login': login_info 
        }
        template = JINJA_ENVIRONMENT.get_template('info.html')
        self.response.write(template.render(template_values))

class Log(webapp2.RequestHandler):
    """
    Renders Log page (hidden)
    """
    def get(self):
        now = datetime.datetime.today()
        qry = Entry.query(ancestor=db_key(now.year))
        qry = qry.order(-Entry.date)
        entries = qry.fetch()

        login_info = get_login_info(self)
        template_values = {
            'year': get_year_string(),
            'page': 'log', 
            'login': login_info, 
            'entries': entries,
        }
        template = JINJA_ENVIRONMENT.get_template('log.html')
        self.response.write(template.render(template_values))

app = webapp2.WSGIApplication([
    ('/',           	MainPage),
	('/signup',			Signup),
    ('/unsignup', 		Unsignup),
    ('/info',     		Info),
    ('/log',      		Log),
    ('/chat',     		Chat),
], debug=True)