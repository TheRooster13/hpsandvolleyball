import os
import urllib
import datetime
import logging
import string
import math
import random
import sys

#RICH ADD START
#import icalendar
#import uuid
#import email.MIMEBase
#from email.MIMEMultipart import MIMEMultipart
#The following won't be needed if we send via sendgrid
#import smtplib
#RICH ADD END

# This is needed for timezone conversion (but not part of standard lib)
#import dateutil

from google.appengine.api import users
from google.appengine.ext import ndb

import jinja2
import webapp2

# using SendGrid's Python Library
# https://github.com/sendgrid/sendgrid-python
import keys
import sendgrid
from sendgrid.helpers.mail import *

# Globals - I want these eventually to go into a datastore per year so things can be different and configured per year. For now, hard-coded is okay.
numWeeks = 14
startdate = datetime.date(2018, 5, 21)
holidays = ((2,1),(3,4),(7,3)) #Memorial Day, BYITW Day, Independance Day
ms = ((0,1,0,1,1,0,1,0),(0,1,1,0,0,1,1,0),(0,1,1,0,1,0,0,1))

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
		url	   = users.create_logout_url(h.request.uri)
		linktext  = 'Logout'
	else:
		logged_in = False
		url	   = users.create_login_url(h.request.uri)
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
		fto_count[player.id]=[0] * numWeeks
	 
	# Check previous schedules for byes or alternates
	if current_week > 1:
		qry = Schedule.query(ancestor=db_key(year))
		qry = qry.filter(Schedule.tier == 0)
		past_byes = qry.fetch()
		if past_byes:
			for bye in past_byes:
				if bye.week < current_week: #in the past
					pl[bye.id].byes += 1

	# Check future fto for byes
	qry = Fto.query(ancestor=db_key(year))
	fto = qry.fetch()
	if fto:
		for f in fto:
			if f.week > current_week and f.user_id in fto_count:
				fto_count[f.user_id][f.week-1]+=1
				if fto_count[f.user_id][f.week-1] == 4: #Once we reach 4 conflicts in a week, that's a bye week. Don't want to double-count on the 5th conflict.
					pl[f.user_id].byes += 1
			if f.week == current_week: #To make things easy, we can populate the weekly conflicts while iterating through the fto list.
				pl[f.user_id].conflicts.append(f.slot)
#				self.response.out.write("%s's conflicts: " % f.name)
#				self.response.out.write(pl[f.user_id].conflicts)
#				self.response.out.write("<br>")
				
	return pl
			

def pick_slots(tier_slot, tier, tier_slot_list):
	if tier >= len(tier_slot_list): return True #We've iterated through all tiers, we're good.
	while len(tier_slot)<(tier+1):tier_slot.append(0) #Fill tier_slot with 0s. We'll fill this with the correct slots as we go.
	for x in tier_slot_list[tier]: #Cycle through each possible slot for this tier
		if x not in tier_slot: #If this slot hasn't been taken by another slot yet...
			tier_slot[tier] = x #Claim the slot.
			if pick_slots(tier_slot, tier+1, tier_slot_list): #Recursively call the function again on the next tier. If it returns True...
				return True #...then we can return true too.
	#If we get here, we've tried every slot in this tier's valid list and found nothing that isn't taken yet, so...
	tier_slot[tier]=0 #...reset this tier's slot to 0 so we can try again.
	return False #We've failed. Back up and try another slot in the prior tier's list.

def find_smallest_set(set_list):
	smallest_set = len(set_list[1])
	smallest_set_pos = 1
	for p in range(2,len(set_list)):
		if len(set_list[p]) == smallest_set:
			smallest_set_pos = random.choice((smallest_set_pos,p)) # Randomly choose which of the two sets to return if there is a tie. The randomness isn't evenly distributed though.
		if len(set_list[p]) < smallest_set:
			smallest_set = len(set_list[p])
			smallest_set_pos = p
	return smallest_set_pos

def remove_conflicts(player_ids, player_data, self, count=1):
	if count > 20: return []
	slots = range(1,6)
	y=0
	for p in player_ids:
		y+=1
		if y > 8: break # use the data from the first 8 players in the tier
		for s in player_data[p].conflicts:
			if s in slots:
				slots.remove(s)
	for z in player_ids:
		self.response.out.write(" %s " % player_data[z].name)
		self.response.out.write(player_data[z].conflicts)
	self.response.out.write("<br>")
	if (len(slots) == 0) and (len(player_ids) > 8):
		self.response.out.write("8 players, no valid slot, shuffling and trying again. Count=%s<br>" % count)
		random.shuffle(player_ids)
		return remove_conflicts(player_ids, player_data, self, count+1)
	random.shuffle(slots) #randomize the order of the available slots
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
	user_id	 = ndb.StringProperty(indexed=True)
	week		= ndb.IntegerProperty(indexed=True)
	slot		= ndb.IntegerProperty(indexed=True)
	name		= ndb.StringProperty(indexed=True)
 
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
	email		   = ndb.StringProperty(indexed=False)
	name			= ndb.StringProperty(indexed=True)
	phone		   = ndb.StringProperty(indexed=False)
	schedule_rank	= ndb.IntegerProperty(indexed=True)
	elo_score		= ndb.IntegerProperty(indexed=True)

class Scores(ndb.Model):
	"""
	A model for tracking game scores
	"""
	week			= ndb.IntegerProperty(indexed=True)
	tier			= ndb.IntegerProperty(indexed=True)
	slot			= ndb.IntegerProperty(indexed=True)
	game			= ndb.IntegerProperty(indexed=True)
	score1  		= ndb.IntegerProperty(indexed=False)
	score2  		= ndb.IntegerProperty(indexed=False)
	
	
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
			set_holidays(self)
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
		
		qry = Schedule.query(ancestor=db_key(now.year))
		active_schedule = qry.count() > 0
		
		template_values = {
			'year': get_year_string(),
			'page': 'signup',
			'user': user,
			'player_list': player_list,
			'is_signed_up': player is not None,
			'active_schedule': active_schedule,
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
#				self.response.out.write("Week: "+str(entry.week)+" Slot: "+str(entry.slot)+" = "+str(fto_week[(entry.week-1)][(entry.slot-1)]))
		
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
		player_list = qry_p.fetch()		 
		
		if self.request.get('action') == "Submit":
			for player in player_list:
				player.name = self.request.get('name-'+player.id)
				player.email = self.request.get('email-'+player.id)
				player.phone = str(self.request.get('phone-'+player.id)).translate(None, string.punctuation).translate(None, string.whitespace)
				player.schedule_rank = int(self.request.get('rank-'+player.id))
				player.elo_score = int(self.request.get('score-'+player.id))
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
									
					matchFound = False
					for fto_entry in fto_data:
						if fto_entry == fto:
							matchFound = True
					if matchFound == False:
						fto.put()			

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
			week = int(math.floor(int((today - startdate).days)/7)+1)
		if week < 1: week = 1
		player_data = get_player_data(week, self)
		old_player_list = player_data.keys()
		old_player_list = sorted(old_player_list, key=lambda k: player_data[k].rank)

		
		# If there is no existing schedule for this week, we know it is the first time the scheduler has run for this week
		# So we should first reorder the players based on the previous week's results. Unless this is week 1
		if week > 1:
			qry = Schedule.query(ancestor=db_key(year))
			qry = qry.filter(Schedule.week == week)
			if qry.count() == 0: #Check if there is already a schedule for this week, if there isn't, we must reorder the player list based on last week's scores
				qry = Schedule.query(ancestor=db_key(year))
				qry = qry.filter(Schedule.week == (week-1))
				qry = qry.order(-Schedule.tier, Schedule.position)
				schedule_results = qry.fetch()
				tiers = schedule_results[0].tier
				tier_position = []
				for x in range(tiers+1):
					tier_position.append([])
				on_bye = []
				for p in schedule_results:
					tier_position[p.tier].append([p.id, 0]) # Fill with the player IDs
					if p.tier == 0:
						on_bye.append(p.id)
				qry = Scores.query(ancestor=db_key(year))
				qry = qry.filter(Scores.week == (week-1))
				qry = qry.order(Scores.tier, Scores.game)
				results = qry.fetch()
				if results:
					for score in results:
						if score.game == 1:
							tier_position[score.tier][0][1] += (score.score1-score.score2)
							tier_position[score.tier][2][1] += (score.score1-score.score2)
							tier_position[score.tier][5][1] += (score.score1-score.score2)
							tier_position[score.tier][7][1] += (score.score1-score.score2)
							tier_position[score.tier][1][1] += (score.score2-score.score1)
							tier_position[score.tier][3][1] += (score.score2-score.score1)
							tier_position[score.tier][4][1] += (score.score2-score.score1)
							tier_position[score.tier][6][1] += (score.score2-score.score1)
						if score.game == 2:
							tier_position[score.tier][0][1] += (score.score1-score.score2)
							tier_position[score.tier][3][1] += (score.score1-score.score2)
							tier_position[score.tier][4][1] += (score.score1-score.score2)
							tier_position[score.tier][7][1] += (score.score1-score.score2)
							tier_position[score.tier][1][1] += (score.score2-score.score1)
							tier_position[score.tier][2][1] += (score.score2-score.score1)
							tier_position[score.tier][5][1] += (score.score2-score.score1)
							tier_position[score.tier][6][1] += (score.score2-score.score1)
						if score.game == 3:
							tier_position[score.tier][0][1] += (score.score1-score.score2)
							tier_position[score.tier][3][1] += (score.score1-score.score2)
							tier_position[score.tier][5][1] += (score.score1-score.score2)
							tier_position[score.tier][6][1] += (score.score1-score.score2)
							tier_position[score.tier][1][1] += (score.score2-score.score1)
							tier_position[score.tier][2][1] += (score.score2-score.score1)
							tier_position[score.tier][4][1] += (score.score2-score.score1)
							tier_position[score.tier][7][1] += (score.score2-score.score1)
					for t in range(1, tiers+1):
						tier_position[t] = sorted(tier_position[t], key=lambda k: k[1], reverse=True)
					temp_rank_list = []
					for x in range(1,tiers+1):
						if x == 1: # If the top tier, top performers move to the top
							temp_rank_list.append(tier_position[x][0][0])
							temp_rank_list.append(tier_position[x][1][0])
						temp_rank_list.append(tier_position[x][2][0])
						temp_rank_list.append(tier_position[x][3][0])
						if x > 1: # If not the top tier, bottom performers from tier above move down here
							temp_rank_list.append(tier_position[x-1][6][0])
							temp_rank_list.append(tier_position[x-1][7][0])						
						if x < tiers: # If not the bottom tier, top performers from tier below move up here
							temp_rank_list.append(tier_position[x+1][0][0])
							temp_rank_list.append(tier_position[x+1][1][0])
						temp_rank_list.append(tier_position[x][4][0])
						temp_rank_list.append(tier_position[x][5][0])
						if x == tiers: # If the bottom tier, bottom performers move to the bottom
							temp_rank_list.append(tier_position[x][6][0])
							temp_rank_list.append(tier_position[x][7][0])
					player_list = []
					for p in range(len(old_player_list)):
						if old_player_list[p] in on_bye:
							player_list.append(old_player_list[p])
						else:
							player_list.append(temp_rank_list.pop(0))
					# Store the new ranks in the database
					qry = Player_List.query(ancestor=db_key(year))
					pr = qry.fetch()
					for p in pr:
						for i,x in enumerate(player_list):
							if x == p.id:
								p.schedule_rank = i
								p.put()
					player_data = get_player_data(week,self) # Refresh the player_data with the new ranks
					
				else:
					player_list = old_player_list
			else:
				player_list = old_player_list
		else:
			player_list = old_player_list
		
		
		
		# Create a list of players ids on bye this week because of FTO
		bye_list = []
		if player_list:
			for p in player_list:
				if len(player_data[p].conflicts)>= 4:
					bye_list.append(p)
					self.response.out.write("Putting %s on a bye.<br>" % player_data[p].name)
		num_available_players = int(len(player_list) - len(bye_list)) #number of players not on bye
		slots_needed = math.floor(num_available_players / 8) # Since we are automatically reducing the slots required if we fail at finding a valid schedule, we can limit to 8 players per tier.
		if slots_needed > 5: slots_needed = 5  # Max of 5 matches per week. We only have 5 slots available.
	  
		valid_schedule = False
		while valid_schedule == False:
			if slots_needed == 0: break # Cannot create a schedule (too few players or an incredible number of conflicts)
			tier_list = list() # List of player ids per tier
			tier_slot_list = list() # List of available slots per tier after removing conflicts for each player in the tier
			tier_slot = list() # List of the slot each tier will play in
			
			players_per_slot = float(num_available_players) / float(slots_needed) #Put this many players into each tier
			counter = 0
			tier_list.append([]) #Add list for tier 0 (bye players)
			tier_list.append([]) #Add list for tier 1 (top players)
			for p in player_list:
				if p in bye_list: # player is on a bye and should be added to tier 0
					tier_list[0].append(p) #add a player to the bye tier
				else: #player is elligible to play and 
					# This code allocated player slots to the tiers when the players_per_slot number isn't an integer (like 9.5 players per tier)
					counter += 1
					if counter > players_per_slot and len(tier_list) < slots_needed+1:
						counter -= players_per_slot
						tier_list.append([]) #Add another tier
					tier_list[len(tier_list)-1].append(p) #Add a player to the current tier
			
			tier_slot_list.append([]) # empty set for tier 0 (byes)
			for x in range(1,len(tier_list)):
				self.response.out.write("Tier %s: Size %s<br>" % (x, len(tier_list[x])))
				random.shuffle(tier_list[x]) #randomly shuffle the list so ties in byes are ordered randomly
				tier_list[x] = sorted(tier_list[x], key=lambda k:player_data[k].byes, reverse=True) #order based on byes (decending order). Future orders will be random.
				tier_slot_list.append(remove_conflicts(tier_list[x], player_data, self))
			
			for i in range(25): # Try this up to X times.
				if not pick_slots(tier_slot, 1, tier_slot_list): #iterate through the slots per tier until a solution is found for every tier.
					# We couldn't find a schedule that works so go back and shuffle the most restrictive player list to get a new set of 8
#					stc = find_smallest_set(tier_slot_list) #stc = set to cycle ---- This could cause us to not find a solution. ----
					stc = random.randint(1,len(tier_slot_list)) #choose a random tier to shuffle. --- We don't know which tier is causing problems, so shuffle one at random ---
					while stc == len(tier_slot_list):
						stc = random.randint(1,len(tier_slot_list)) #choose a random tier to shuffle.
					self.response.out.write("Could not find a valid schedule. Shuffling tier %s and trying again. Count=%s/25<br>" % (stc,i+1))
					random.shuffle(tier_list[stc]) # Shuffle the players in the most restrictive tier. (This should probably be a random tier.)
					tier_slot_list[stc] = remove_conflicts(tier_list[stc], player_data, self)
				else:
					break
			
			for x in range(1, len(tier_list)):
				for p in range(len(tier_list[x])-1, 7, -1):
					tier_list[0].append(tier_list[x][p]) # Add alternate players to bye list
					tier_list[x].remove(tier_list[x][p]) # Remove alternate players from the tier list
				tier_list[x] = sorted(tier_list[x], key=lambda k:player_data[k].rank) # Sort the 8 players in each tier by rank
			
			# Check to see if we have a valid schedule
			valid_schedule = True
			for x in range(1, len(tier_slot)):
				if not tier_slot[x]: # No valid slots for this tier - bad news
					valid_schedule = False
			if valid_schedule == False: #clear the lists, reduce the number of matches, and try again
				self.response.out.write("We can't find a valid schedule so we're dropping from %s matches to %s and trying again.<br>" % (slots_needed, slots_needed-1))
				del tier_list[:]
				del tier_slot_list[:]
				del tier_slot[:]
				slots_needed -= 1

		# If we reach this point, we have a valid schedule! Save it to the database.
		# First delete any existing schedule for this week (in case the scheduler runs more than once)
		qry = Schedule.query(ancestor=db_key(year))
		qry = qry.filter(Schedule.week == week)
		results = qry.fetch()
		for r in results:
			r.key.delete()

		sg = sendgrid.SendGridAPIClient(apikey=keys.API_KEY) # Object for sending emails
		y=0
		for x in tier_list:
			z=0
			name_list = list()
			self.email_list = list()
			for p in x:
				z+=1
				s = Schedule(parent=db_key(year)) #database entry
				s.id = p
				s.name = player_data[p].name
				s.week = week
				s.slot = tier_slot[y]
				s.tier = y
				s.position = z #1-8
				s.put() #Stores the schedule data in the database
				
				# Add the player names and emails to some lists for creating and sending an iCalendar event
				name_list.append(player_data[p].name)
				self.email_list.append(player_data[p].email)
							
			if y > 0: # If this isn't tier 0 (players on bye)...
				# Calculate the date for this match
				self.match_date = startdate + datetime.timedelta(days=(7*(week-1)+(tier_slot[y]-1)))
				# Generate an iCalendar Event and email it to the players
            #RICH ADD START
            ## Required parameters for GenInvite
            #self.startHour  = 12
            #self.durationH  = 1
            #self.location   = 'N/S Sand Court'
            #self.match_time = datetime.datetime.combine(self.match_date, datetime.time(self.startHour,0,0))

            ##Organizer (Will recieve responses)
            #self.sendfrom   = 'brian.bartlow@hp.com'
            #
            ##Simple message to players
            ##Could add lineup for games
            #self.msg_to_plyrs = "Weekly match invite."

            ##MIME message generation
            #self.msg = MIMEMultipart("mixed")
            #self.msg['Subject'] = 'Sand Volleyball Match'
            #self.msg['From'] = self.sendfrom
            #self.msg['To']   = ", ".join(self.email_list)
            #
            ##Generate the invite (requires:
            ##                     self.match_date, self.email_list self.startHour,
            ##                     self.durationH, self.location, self.reminderMins,
            ##                     self.match_time, self.sendfrom self.msg_to_plyrs,
            ##                     self.msg
            #self.GenInvite()
            #
            ## Send the message via our own SMTP server.
            ## TODO: Needs to be modified for sendgrid
            #s = smtplib.SMTP('localhost')
            #s.sendmail(self.sendfrom, self.email_list,self.msg.as_string())
            #s.quit()
            #RICH ADD END
		
			y+=1
		
		sys.stdout.flush()
		template = JINJA_ENVIRONMENT.get_template('scheduler.html')
		self.response.write(template.render({}))
	#RICH ADD START
   #def GenInivte(self):
   #   #Create the calendar component
   #   cal = icalendar.Calendar()
   #   cal.add('method', 'REQUEST')
   #   cal.add('prodid', 'HP Sand VB ics')
   #   
   #   #This makes Outlook happy
   #   #Copied format based on working invite
   #   #We're all in Boise so I'm not too worried about TZ
   #   timz= icalendar.Timezone()
   #   timz.add('tzid', 'Mountain Standard Time')
   #   timzs= icalendar.TimezoneStandard()
   #   timzs.add('dtstart', datetime.datetime(1601, 1, 1, 2, 0 , 0))
   #   timzs['tzoffsetfrom'] = '-0600'
   #   timzs['tzoffsetto'] = '-0700' 
   #   timzd= icalendar.TimezoneDaylight()
   #   timzd.add('dtstart', datetime.datetime(1601, 1, 1, 2, 0 , 0))
   #   timzd['tzoffsetfrom'] = '-0700'
   #   timzd['tzoffsetto'] = '-0600' 
   #   timz.add_component(timzs)
   #   timz.add_component(timzd)
   #   cal.add_component(timz)
   #   
   #   #Create the Event component
   #   event = icalendar.Event()
   #   
   #   #Add attendees
   #   for a in range(len(self.email_list)):
   #      attendee = icalendar.vCalAddress('MAILTO:'+self.email_list[a])
   #      attendee.params['ROLE']= icalendar.vText('REQ-PARTICIPANT')
   #      attendee.params['PARTSTAT']= icalendar.vText('NEEDS-ACTION')
   #      attendee.params['RSVP']= icalendar.vText('TRUE')
   #      event.add('attendee', attendee)
   #   
   #   #Specify the organizer
   #   organizer = icalendar.vCalAddress('MAILTO:' + self.sendfrom)
   #   organizer.params['CN'] = icalendar.vText('Mr. Sandman')
   #   event.add('organizer', organizer)
   #   
   #   #Add more calendar invite information
   #   event.add('description', self.msg_to_plyrs)
   #   event.add('location', self.location)
   #   event.add('dtstart', self.match_time)
   #   event.add('dtend',   datetime.datetime.combine(self.match_date, datetime.time(self.startHour+self.durationH, 0, 0)))
   #   event.add('dtstamp', datetime.datetime.now())
   #   event['uid']  = uuid.uuid4().hex
   #   event.add('status', 'CONFIRMED')
   #   event.add('priority', 5)
   #   event.add('sequence', 0)
   #   event.add('created',   datetime.datetime.now())
   #   event.add('transp', "OPAQUE")
   #   
   #   alarm = icalendar.Alarm()
   #   alarm.add("action", "DISPLAY")
   #   alarm.add('description', "REMINDER")
   #   alarm.add("TRIGGER;RELATED=START", "-PT{0}M".format(self.reminderMins))
   #   event.add_component(alarm)
   #   
   #   cal.add_component(event)
   #   
   #   #Don't think we need to actually write a file out
   #   #We set the payload to the contents of cal.to_ical() below
   #   filename = "invite.ics"
   #   #f = open(filename, 'wb')
   #   #f.write(cal.to_ical())
   #   #f.close()
   #   
   #   attachment_part = email.MIMEBase.MIMEBase('text', 'calendar', method="REQUEST", name=filename)
   #   attachment_part.set_payload(cal.to_ical())
   #   attachment_part.set_type('text/calendar; charset=UTF-8;method=REQUEST;component =VEVENT')
   #   email.Encoders.encode_base64(attachment_part)
   #   attachment_part.add_header('Content-Description', filename)
   #   attachment_part.add_header('Content-class', 'urn:content-classes:calendarmessage')
   #   attachment_part.add_header('Content-ID', 'calendar_message')
   #   attachment_part.add_header('Filename', filename)
   #   attachment_part.add_header('Path', filename)
   #   self.msg.attach(attachment_part)
   #RICH ADD END

class Weekly_Schedule(webapp2.RequestHandler):
	def get(self):
		today = datetime.date.today()
		year = today.year
		# See if user is logged in and signed up
		login_info = get_login_info(self)
		user = users.get_current_user()
		player = get_player(self)	  
		
		# Calculate what week# next week will be
		if self.request.get('w'):
			week = int(self.request.get('w'))
		else:
			week = int(math.floor(int((today - startdate).days+3)/7))
		if week < 1:
			week = 1
		if week > numWeeks:
			week = numWeeks
		slots = []
		for d in range(5):
			slots.append(startdate + datetime.timedelta(days=(7*(week-1)+d)))
		
		qry = Schedule.query(ancestor=db_key(year))
		qry = qry.filter(Schedule.week == week)
		qry = qry.order(Schedule.slot, Schedule.position)
		schedule_data = qry.fetch()
		
		template_values = {
			'year': get_year_string(),
			'page': 'week',
			'week': week,
			'numWeeks': numWeeks,
			'slots': slots,
			'schedule_data': schedule_data,
			'is_signed_up': player is not None,
			'login': login_info,
		}

		template = JINJA_ENVIRONMENT.get_template('week.html')
		self.response.write(template.render(template_values))

class Daily_Schedule(webapp2.RequestHandler):
	def post(self):
		user = users.get_current_user()
		now = datetime.datetime.today()
		# See if user is logged in and signed up
		login_info = get_login_info(self)
		user = users.get_current_user()
		player = get_player(self)
		
		week = int(self.request.get('w'))
		day = int(self.request.get('d'))
		tier = int(self.request.get('t'))
		
		if self.request.get('action') == "Scores":
			qry = Scores.query(ancestor=db_key(now.year))
			qry = qry.filter(Scores.week == week, Scores.slot == day)
			sr = qry.fetch()
			for s in sr:
				s.key.delete() # Delete the old scores
			
			for g in range(1,4):
				score = Scores(parent=db_key(now.year))
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
					score.put() # Save the new scores
		
		self.redirect("day?w=%s&d=%s" % (week, day))
						
	def get(self):
		today = datetime.date.today()
		year = today.year		
		# See if user is logged in and signed up
		login_info = get_login_info(self)
		user = users.get_current_user()
		player = get_player(self)	  
		
		day = 0
		# Calculate what week and day it is
		if self.request.get('w'):
			week = int(self.request.get('w'))
		else:
			week = int(math.floor(int((today - startdate).days)/7)+1)
		if week < 1 :
			week = 1
			day = 1
		if week > numWeeks :
			week = numWeeks
		
		if self.request.get('d'):
			day = int(self.request.get('d'))
		else:
			if not day:
				day = today.weekday() + 1
		if day > 5 :
			day = 1
			week += 1
		
		schedule_day = startdate + datetime.timedelta(days=(7*(week-1)+(day-1)))
		
		qry = Schedule.query(ancestor=db_key(year))
		qry = qry.filter(Schedule.week == week, Schedule.slot == day)
		qry = qry.order(Schedule.position)
		schedule_data = qry.fetch()
		if len(schedule_data)>0:
			games = True
			tier = schedule_data[0].tier
		else:
			games = False
			tier = 0
		
		game_team = [[],[]],[[],[]],[[],[]]
		for p in schedule_data:
			for x in range(3):
				game_team[x][ms[x][p.position-1]].append(p.name)
		
		qry = Scores.query(ancestor=db_key(year))
		qry = qry.filter(Scores.week == week, Scores.slot == day)
		sr = qry.fetch()
		
		score = [['',''],['',''],['','']]
		if sr:
			for s in sr:
				score[s.game-1][0] = s.score1
				score[s.game-1][1] = s.score2
		
	
		template_values = {
			'year': get_year_string(),
			'page': 'day',
			'week': week,
			'day': day,
			'tier': tier,
			'games': games,
			'score': score,
			'numWeeks': numWeeks,
			'schedule_day': schedule_day,
			'game_team': game_team,
			'is_signed_up': player is not None,
			'login': login_info,
		}

		template = JINJA_ENVIRONMENT.get_template('day.html')
		self.response.write(template.render(template_values))


class Notify(webapp2.RequestHandler):
	def get(self):
		sg = sendgrid.SendGridAPIClient(apikey=keys.API_KEY)

		today = datetime.date.today()
		year = today.year
		
		# Calculate what week and day it is
		week = int(math.floor(int((today - startdate).days)/7))
		day = today.weekday() + 1

		from_email = Email("noreply@hpsandvolleyball.appspot.com")
#		to_email = Email("")
		to_email = Email("brian.bartlow@hp.com")
		subject = "Please Ignore"
		content = Content("text/html", "Please ignore this email, I am testing new functionality on the website.")

		player_data = get_player_data(0, self)
		to_list = []
		sendit = False
		
		if self.request.get('t') == "score":
			# Check to see if there is a match scheduled for today
			qry = Schedule.query(ancestor=db_key(year))
			qry = qry.filter(Schedule.week == week, Schedule.slot == day)
			qry = qry.order(Schedule.position)
			schedule_data = qry.fetch()
			if schedule_data: # If there is a match scheduled for today
				# Check to see if scores have been entered for today's match
				qry = Scores.query(ancestor=db_key(year))
				qry = qry.filter(Scores.week == week, Scores.slot == day)
				sr = qry.count()
				if sr == 0: # If no scores have been entered for today's match, email all of today's players to remind them to enter the score.
					subject = "Reminder to submit scores"
					content = Content("text/html", "Please go to the <a href=\"http://hpsandvolleyball.appspot.com/day\">Score Page</a> and enter the scores from today's games.")
					sendit = True
					for p in schedule_data:
#						pass
						to_list.append(p.email)

		elif self.request.get('t') == "fto":
			subject = "Reminder to check and update your FTO/Conflicts for next week"
			content = Content("text/html", """Next week's schedule will be generated at 2:00. Please go to the <a href=\"http://hpsandvolleyball.appspot.com/fto\">FTO Page</a> and check to make sure your schedule is up-to-date for next week.
			If that link doesn't work, you probably need to log in again. Be sure to log in with your correct Google account.""")
			sendit = True
			for p in player_data:
				if p.email:
#					pass
					to_list.append(p.email)
		
		elif self.request.get('t') == "test":
			subject = "Test"
			content = Content("text/html", "Test email. Please ignore.")
			to_list.append("brian.bartlow@hp.com")
			sendit = True
		
		if sendit:
			mail = Mail(from_email, subject, to_email, content)
			response = sg.client.mail.send.post(request_body=mail.get())
			print(response.status_code)
			print(response.body)
			print(response.headers)

		
app = webapp2.WSGIApplication([
	('/',					MainPage),
	('/signup',				Signup),
	('/unsignup',			Unsignup),
	('/info',				Info),
	('/ftolog',				Ftolog),
	('/fto',				FTO),
	('/week',				Weekly_Schedule),
	('/day',				Daily_Schedule),
	('/admin',				Admin),
	('/tasks/notify',		Notify),
	('/tasks/scheduler',	Scheduler),
], debug=True)
