#!/observinglogs/anaconda3/bin/python

import sys, os

# Make sure to replace "/KeOLA/webInterface" with the actual working directory 
# of server.py
sys.path.insert(0,'/home/keola/obsMonitor/KeOLA/webInterface')

from server import app as application
