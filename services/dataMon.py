##############################################################
# KeOLA data directory monitoring tool - W.M. Keck Observatory
# - - - - - - - - - - - - - - - - - - - - - - - - - - - - - -  
# Author: Ian Cunnyngham
# Mentor: Dr. Luca Rizzi
#
#   This service watches the collection of active logs, tracking 
# their addition and removal from active status, monitoring all 
# their associated data directories for FITS data, and ingesting
# the header metadata of these files as they are added.  
# Additionally, helper modules have been written to obtain 
# additional information from the Keck's information relavant 
# to each log including weather data and twilight information.
# Weather data is polled at weatherPollInterval min intervals,
# and can also be requested directly from the log interface.
# Twilight info is polled on initial ObsLog tracking, only 
# adding it to the log if it is not present already.  
#
#   All of this functionality runs through one cycle and then
# sleeps for sleepTime seconds
#
#   The intention of this script is that it be fully "stateless".
# That is to say, the script can crash and be restarted with no
# loss of continuity.  Nothing is stored internally that is not
# simply a convenient local copy of what is in the database.
#
################################################################

import os, time

from datetime import datetime, timedelta, date
import pyfits
import pymongo, bson
import warnings

# Custom mododules for pulling in data from outside sources
import urllib2
import getWeather
import getSchedules


### Configuration ###

# How long (in seconds) should the script sleep in between monitoring cycles
sleepTime = 2

# How long (in minutes) between weather updates
weatherPollInterval = 60


### Observation Log monitoring Class ###

class ObsLog:
    def __init__(self, workingDB, logID):
        """ Initializes and begins tracking of a log that has been marked as active """

        self.db = workingDB
        self.id = logID


        # Query the database for this log's info and store it
        self.log = self.db.logs.find_one({"_id": logID})

        # Store the instrument name for conevenience 
        self.instrument = self.log["instrument"]


        # Query the database for this log's instrument information and store info
        self.instr = self.db.instruments.find_one({"name": self.instrument})

        # Determine if the "ignore_end_card" flag should be used in reading
        # this instruments FITS data
        if "missing_end_card" in self.instr and self.instr["missing_end_card"]:
            self.missing_end_card = True
        else:
            self.missing_end_card = False

        if "instrAttr" in self.instr:
            self.instrAttr = self.instr["instrAttr"]
        else:
            self.instrAttr = [0, "INSTRUME"]

        # Catch if twilight information is missing, and if so, add it
        if "twilight" not in self.log:
            print "Appending twilight information to log ", self.id

            # Catch potential errors trying to fetch scehdule data from the web
            try:
                # Get the schedules for this log's date, select Keck I's schedule
                schedule = getSchedules.fromWeb( self.log["utcDate"].date() )[1]
            except urllib2.URLError:
                print "Error getting twilight info from schedule, appending empty entry"
                twilight = {}
            else:
                # Parse out a nice dictionary for the twilight attributes
                twilight = getSchedules.parseTwilight( schedule )

            # Append and save it to the log
            self.log["twilight"] = twilight
            self.db.logs.save( self.log )

        # If twilight is properly attached to this log, update the sunset and sunrise times
        if ("twilight" in self.log) and ("sunset" and "sunrise" in self.log["twilight"]):
            global startPoll, endPoll
            sunrise = datetime.strptime(self.log["twilight"]["sunrise"], "%H:%M:%S")
            sunset = datetime.strptime(self.log["twilight"]["sunset"], "%H:%M:%S")
            startPoll = (sunset - timedelta( minutes=60 )).time()
            endPoll = (sunrise + timedelta( minutes=60 )).time()

        # Try to find the date of the last weather entry 
        lastWeather = self.db.entries.find({"logID":self.id, "type":"weather"}).sort([("utcDate",-1)]).limit(1)

        if lastWeather.count() > 0:
            # If it exists, store the date. 
            self.weatherUpdated = lastWeather[0]["utcDate"]
        else:
            # Else, make sure the weather will be updated on next polling 
            # by setting a phantom last update time 
            self.weatherUpdated = (datetime.utcnow() - timedelta( minutes=weatherPollInterval+1 ))

    def alertEntry(self, alert):
        aEntry = {"type": "alert",
            "title": alert,
            "logID": self.id,
            "utcDate": now }
        self.db.entries.save( aEntry )

    def weatherEntry(self, weather):
        now = datetime.utcnow()

        try:
            w = weather[ self.instr["telescope"] ].current()
        except IOError:
            print "Error opening weather log, appending empty entry"
            w = {}

        wEntry = {"type": "weather",
            "weather": w,
            "logID": self.id,
            "utcDate": now }
        self.db.entries.save( wEntry )
        
        self.weatherUpdated = now
        
    def excludeEntry(self, f, dir, reason=""):
        exEntry = {"type": "exclude",
            "filename": f,
            "directory": dir,
            "reason": reason,
            "logID": self.id,
            "utcDate": datetime.utcnow()}
        self.db.entries.save( exEntry )


    def startDir(self, dir):
        """ Start tracking a directory now that FITS files have been found in it """
        # append the directory to the activeDirs list 
        self.log["activeDirs"].append( dir )

        # Save the change to the database 
        db.logs.save( self.log )


    def stopDir(self, dir):
        """ Stop actively watching a directory """
        # Remove the directory from the activeDirs list
        self.log["activeDirs"].remove( dir )

        # Save the change to the log
        db.logs.save( self.log )

    def trackDirs(self):
        # Find the set of all directories not currently marked active and iterate through them
        for d in ( set(self.log["dataDirs"]) - set(self.log["activeDirs"]) ):
            # Try to switch to the directory, but don't worry if this fails
            try:
                os.chdir( d )
            except OSError:
                pass
            else:
                # When in the directory, see if the number of .fits files in this
                # directory exceeds 0.  If it does, start the log with this as
                # the dataDir
                if len( [f for f in os.listdir(".") if f[-5:] == ".fits"] ) > 0: 
                    self.startDir(d)

                    # Print output and append a log entry 
                    print "Now watching", d, "for log", logID, "at", datetime.utcnow(), "UTC"
                    self.alertEntry( "Data found in "+ d +".  Now tracking." )

        # Now iterate through all the active directories
        if len(self.log["activeDirs"]) > 0:
            # Find all the tracked fits files for this log and store it
            self.trackedFITS = self.findTrackedFITS()

            # Monitor each active directory
            for d in self.log["activeDirs"]:
                self.monitorDir( d )


    def findTrackedFITS(self):
        """ Find files for the current log that are tracked by the database already """
        
        # Start an empty dictionary whose keys will store which directories files
        # are from, and whose values store a list of the filenames
        tFiles = {} 

        for d in self.log["dataDirs"]:
            tFiles[ d ] = []
        
        #   Search within the working database, in the collection "fits"
        # for any documents whose "logID" key has a value matching the 
        # this log's logID.  Only return value of the "filename" and 
        # "directory" fields
        for f in self.db.fits.find({"logID": self.id}, {"filename": 1, "directory": 1}):
            tFiles[ f["directory"] ].append( f["filename"] )

        # Do the same thing for excluded files
        for f in self.db.entries.find({"logID": self.id, "type": "exclude"}):
            tFiles[ f["directory"] ].append( f["filename"] )

        # Return all files tracked and excluded
        return tFiles


    def monitorDir(self, dir):
        """ Find any new fits files in this log's data directory and track them """

        # Try to change to the data directory for this log
        try:
            os.chdir(dir)
        except OSError:
            # Stop trying to monitor this directory for now
            self.stopDir( dir )
            
            # Print output and append log entry
            print "Can no longer access", dir, "for log", str(self.id), ".  Removed from active monitoring."
            self.alertEntry( "Can no longer access "+ dir +". Removed it from active monitoring." )

            return 
        
        # Create a list of files in the current directory which end in ".fits"
        curFiles = [f for f in os.listdir(".") if f[-5:] == ".fits"]
         
        if self.trackedFITS[dir] != curFiles:
            # Find the set of new files
            newFiles = set( curFiles )-set( self.trackedFITS[dir] )

            for f in newFiles:
                # Find the time of most recent modification
                modOn = datetime.fromtimestamp( os.stat(f).st_mtime )

                # Try to open the fits file in pyfits
                for t in range(3):
                    try:
                        if self.missing_end_card:
                            fitsHdrs = pyfits.open(f, ignore_missing_end=True)
                        else:
                            fitsHdrs = pyfits.open(f)
                    except UserWarning:
                        if t != 2:
                            print "Truncation caught, retrying in 5 seconds"
                        else:
                            print "Giving up on reading " + f + ".  Adding to exclusion list"
                            self.excludeEntry(f, dir, "Repeated truncate error")
                    except IOError:
                        if t != 2:
                            print "IOError caught, retrying in 5 seconds"
                        else:
                            print "Giving up on reading " + f + ".  Adding to exclusion list"
                            self.excludeEntry(f, dir, "Repeated IOError")
                    else:
                        # Successful, don't retry
                        break
                    time.sleep(5)

                # If fitsHdrs is now defined, go ahead            
                if 'fitsHdrs' in locals(): 
                    #   Create a list representing all the headers in the fits file, 
                    # each element of which is a dictionary containing all the header information
                    # as key, value pairs.
                    #
                    #   For instance, to access the tag "AIRMASS" in the 0th header of a fits file:
                    # headers[0]["airmass"]

                    headers = [] 
                    for h in fitsHdrs:
                        headDict = {}
                        for k, v in h.header.iteritems():
                            # Dictionary keys must not have "." for mongoDB, make sure none do
                            key = str.replace(k, ".", "-") 
                            if type(v) is str:
                                v = v.strip()
                            headDict[key] = v
                        headers.append( headDict )

                    # Make sure this fits file belongs to the correct instrument
                    try:
                        fitsInstr = reduce(lambda memo, i: memo[i], self.instrAttr, headers)
                    except KeyError:
                        self.alertEntry( "Warning: " + str( self.instrAttr ) + " attribute not found for " + f)
                    else: 
                        if self.instrument not in str(fitsInstr):
                            self.alertEntry( "Warning: " + self.instrument+" not in instrument keyword of " + f)

                    # Create a record to be stored in the database 
                    fRecord = {"filename": f,
                        "directory": dir,
                        "logID": self.id,
                        "last_modified": modOn,
                        "headers": headers }

                    # Insert record into the database
                    self.db.fits.insert( fRecord )

                    # Insert new entry for frontend showing a new file was found
                    #   Inserting the fits ID 
                    fEntry = {"logID": self.id,
                        "type": "file",
                        "name": f,
                        "directory": dir,
                        "utcDate": datetime.utcnow(),
                        "fitsID": fRecord["_id"] }
                    self.db.entries.insert( fEntry )

                    del fitsHdrs

### Main Program ###

# Convert pyfits file truncation warning into an error so we can catch it
warnings.filterwarnings('error', '.*truncated.*');

# Connect to MongoDB database (default connection options work for now)
conn = pymongo.Connection()
db = conn.obsLog

# Instatiate two getWeather.Weather()s to pull data from meteorolgical logs
# (One for each telescope) 
weather = {1: getWeather.Weather(1), 2: getWeather.Weather(2) }

# Create global startPoll and endPoll times, based on the
# sunset and sunrise times +- and hour.  They will be 
# updated every time a log with twilight information 
# is added.  This will be used for determining
# when weather polling should start
# (Default to False to make sure they are only
#  ued when defined properly)
startPoll = False
endPoll = False


# Empty dictionary whose keys will be logIDs 
# and whose values will be the corresponding instance of ObsLog 
curLogs = {}

# ... same as above, but for proto logs
curPLogs = {}

# Continuously monitor this directory until forced to exit
while True:

    ### Handle active logs ###

    #####
    # Respond to changes in what logs are marked as active

    # Get a list of active logIDs from the database
    activeLogIDs = [ l["logID"] for l in db.activeLogs.find() ]

    # Get a list of currently tracked logIDs
    curLogIDs = [ id for id in curLogs.keys() ]

    # Instantiate a new ObsLog for each new logID found
    for logID in set(activeLogIDs) - set(curLogIDs):
        curLogs[ logID ] = ObsLog(db, logID)
        print "Tracking new log ", logID, " at ", datetime.utcnow(), "UTC"

    # Remove any logs that have become inactive (removed from the activeLogs db) 
    for logID in set(curLogIDs) - set(activeLogIDs):
        del curLogs[ logID ]
        print "Ended tracking of log ", logID, " at ", datetime.utcnow(), "UTC"

    #####
    # Weather requests
    
    # Get weather for every log that has a weatherRequest outstanding
    # ( Store a list of logs that we've responded too so that we don't 
    #   respond to them more than once. )
    responded = []
    for req in db.entries.find({"type": "weatherRequest"}):
        logID = req["logID"]
        if logID not in responded:
            curLogs[ logID ].weatherEntry( weather )
            responded.append( logID )
        db.entries.remove( req )

    # Get the current time for use with further weather polling 
    now = datetime.utcnow()

    # Determine if we are within sundown if possible, and set
    # whether we should be polling weather for the logs or not
    if startPoll and endPoll:
        if (now.time() > startPoll) and (now.time() < endPoll):
            pollWeather = True
        else:
            pollWeather = False
    else:
        pollWeather = True

    #####
    # Cycle through all active logs and monitor

    for obslog in curLogs.values():
        # Monitor the directories of each active log
        obslog.trackDirs() 

        # Check if this log shoudld poll for new weather
        if pollWeather and (now - obslog.weatherUpdated) > timedelta( minutes=weatherPollInterval ):
            obslog.weatherEntry( weather )


    ### Sleep ### 

    # sleep for sleepTime seconds until next check
    time.sleep( sleepTime )
    
    # DEBUG OUTPUT
    # print datetime.utcnow(), "cycle complete"
