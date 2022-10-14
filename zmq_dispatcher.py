#!/usr/bin/env python3

import argparse
import configparser
import copy
import datetime
import json
import logging
import os
import random
import sys
import time

import redis
import zmq

import requests
import ipaddress
import re

import util
import updates
from helpers import (contributor_helper, geo_helper, live_helper,
                     trendings_helper, users_helper)

configfile = os.path.join(os.path.dirname(os.path.realpath(__file__)), 'config/config.cfg')
cfg = configparser.ConfigParser()
cfg.read(configfile)

logDir = cfg.get('Log', 'directory')
logfilename = cfg.get('Log', 'dispatcher_filename')
logPath = os.path.join(logDir, logfilename)

cidr_array = []
clientInfo = cfg.get('Log','clientInfo')
import_tag_all_attributes = cfg.get('Log', 'import_tag_all_attributes')
dashboard_user_auth_key = cfg.get('Log', 'dashboard_user_auth_key')
import_all_attributes = False


if not os.path.exists(logDir):
    os.makedirs(logDir)
try:
    logging.basicConfig(filename=logPath, filemode='a', level=logging.INFO)
except PermissionError as error:
    print(error)
    print("Please fix the above and try again.")
    sys.exit(126)
logger = logging.getLogger('zmq_dispatcher')

LISTNAME = cfg.get('RedisLIST', 'listName')

serv_log = redis.StrictRedis(
        host=cfg.get('RedisGlobal', 'host'),
        port=cfg.getint('RedisGlobal', 'port'),
        db=cfg.getint('RedisLog', 'db'),
        decode_responses=True)
serv_redis_db = redis.StrictRedis(
        host=cfg.get('RedisGlobal', 'host'),
        port=cfg.getint('RedisGlobal', 'port'),
        db=cfg.getint('RedisDB', 'db'),
        decode_responses=True)
serv_list = redis.StrictRedis(
        host=cfg.get('RedisGlobal', 'host'),
        port=cfg.getint('RedisGlobal', 'port'),
        db=cfg.getint('RedisLIST', 'db'),
        decode_responses=True)

live_helper = live_helper.Live_helper(serv_redis_db, cfg)
geo_helper = geo_helper.Geo_helper(serv_redis_db, cfg)
contributor_helper = contributor_helper.Contributor_helper(serv_redis_db, cfg)
users_helper = users_helper.Users_helper(serv_redis_db, cfg)
trendings_helper = trendings_helper.Trendings_helper(serv_redis_db, cfg)


##############
## HANDLERS ##
##############

def handler_skip(zmq_name, jsonevent):
    logger.info('Log not processed')
    return

def handler_audit(zmq_name, jsondata):
    action = jsondata.get('action', None)
    jsonlog = jsondata.get('Log', None)

    if action is None or jsonlog is None:
        return

    # consider login operations
    if action == 'log': # audit is related to log
        logAction = jsonlog.get('action', None)
        if logAction == 'login': # only consider user login
            timestamp = int(time.time())
            email = jsonlog.get('email', '')
            org = jsonlog.get('org', '')
            users_helper.add_user_login(timestamp, org, email)
    else:
        pass

def handler_dispatcher(zmq_name, jsonObj, tmp):
    if "Event" in jsonObj:
      if "Attribute" in jsonObj['Event']:
        handler_event(zmq_name, jsonObj, tmp)


def handler_keepalive(zmq_name, jsonevent, tmp):
    logger.info('Handling keepalive')
    to_push = [ jsonevent['uptime'] ]
    live_helper.publish_log(zmq_name, 'Keepalive', to_push)

# Login are no longer pushed by `misp_json_user`, but by `misp_json_audit`
def handler_user(zmq_name, jsondata, tmp):
    logger.info('Handling user')
    action = jsondata['action']
    json_user = jsondata['User']
    json_org = jsondata['Organisation']
    org = json_org['name']
    if action == 'edit': #only consider user login
        pass
    else:
        pass

def handler_conversation(zmq_name, jsonevent, tmp):
    logger.info('Handling conversation')
    try: #only consider POST, not THREAD
        jsonpost = jsonevent['Post']
    except KeyError as e:
        logger.error('Error in handler_conversation: {}'.format(e))
        return
    org = jsonpost['org_name']
    categ = None
    action = 'add'
    eventName = 'no name or id yet...'
    contributor_helper.handleContribution(zmq_name, org,
                    'Discussion',
                    None,
                    action,
                    isLabeled=False)
    # add Discussion
    nowSec = int(time.time())
    trendings_helper.addTrendingDisc(eventName, nowSec)

def handler_object(zmq_name, jsondata):
    logger.info('Handling object')
    # check if jsonattr is an mispObject object
    if 'Object' in jsondata:
        jsonobj = jsondata['Object']
        soleObject = copy.deepcopy(jsonobj)
        del soleObject['Attribute']
        for jsonattr in jsonobj['Attribute']:
            jsonattrcpy = copy.deepcopy(jsonobj)
            jsonattrcpy['Event'] = jsondata['Event']
            jsonattrcpy['Attribute'] = jsonattr
            handler_attribute(zmq_name, jsonattrcpy, False, parentObject=soleObject)

def handler_sighting(zmq_name, jsondata):
    logger.info('Handling sighting')
    jsonsight = jsondata['Sighting']
    org = jsonsight['Event']['Orgc']['name']
    categ = jsonsight['Attribute']['category']
    action = jsondata.get('action', None)
    contributor_helper.handleContribution(zmq_name, org, 'Sighting', categ, action, pntMultiplier=2)
    handler_attribute(zmq_name, jsonsight, hasAlreadyBeenContributed=True)

    timestamp = jsonsight.get('date_sighting', None)

    if jsonsight['type'] == "0": # sightings
        trendings_helper.addSightings(timestamp)
    elif jsonsight['type'] == "1": # false positive
        trendings_helper.addFalsePositive(timestamp)

def handler_event(zmq_name, jsonobj, tmp):
    logger.info('Handling event')
    #fields: threat_level_id, id, info
    jsonevent = jsonobj['Event']
    #Add trending
    eventName = jsonevent['info']
    timestamp = jsonevent['timestamp']
    trendings_helper.addTrendingEvent(eventName, timestamp)
    tags = []
    for tag in jsonevent.get('Tag', []):
        tags.append(tag)
    trendings_helper.addTrendingTags(tags, timestamp)

    import_all_attributes = False
    eventid = jsonevent['id']
    if "Attribute" in jsonevent:
      if check_event_tag(eventid):
        import_all_attributes = True


    #redirect to handler_attribute
    if 'Attribute' in jsonevent:
        attributes = jsonevent['Attribute']
        if type(attributes) is list:
            for attr in attributes:
                jsoncopy = copy.deepcopy(jsonobj)
                jsoncopy['Attribute'] = attr
                #handler_attribute(zmq_name, jsoncopy)
                handler_attribute(zmq_name, attr, import_all_attributes)
        else:
            handler_attribute(zmq_name, attributes, import_all_attributes)

    if 'Object' in jsonevent:
        objects = jsonevent['Object']
        if type(objects) is list:
            for obj in objects:
                jsoncopy = copy.deepcopy(jsonobj)
                jsoncopy['Object'] = obj
                handler_object(zmq_name, jsoncopy)
        else:
            handler_object(zmq_name, objects)

    action = jsonobj.get('action', None)
    eventLabeled = len(jsonobj.get('EventTag', [])) > 0
    org = jsonobj.get('Orgc', {}).get('name', None)

    if org is not None:
        contributor_helper.handleContribution(zmq_name, org,
                        'Event',
                        None,
                        action,
                        isLabeled=eventLabeled)

def handler_attribute(zmq_name, jsonobj, import_all_attributes, hasAlreadyBeenContributed=False, parentObject=False):
    logger.info('Handling attribute')
    # check if jsonattr is an attribute object
    if 'Attribute' in jsonobj:
        jsonattr = jsonobj['Attribute']
    else:
        jsonattr = jsonobj

    attributeType = 'Attribute' if jsonattr['object_id'] == '0' else 'ObjectAttribute'
    
    #Add trending
    categName = jsonattr['category']
    timestamp = jsonattr.get('timestamp', int(time.time()))
    trendings_helper.addTrendingCateg(categName, timestamp)
    tags = []
    for tag in jsonattr.get('Tag', []):
        tags.append(tag)
    trendings_helper.addTrendingTags(tags, timestamp)


    if not import_all_attributes:
      if 'Tag' in jsonobj:
        for tag in jsonobj['Tag']:
          if tag['name'] == import_tag_all_attributes:
            import_all_attributes = True
   
    if "value" in jsonattr:
     if check_ip(jsonattr['value'], cidr_array) or import_all_attributes:
        
      #try to get coord from ip
      if jsonattr['category'] == "Network activity":
        geo_helper.getCoordFromIpAndPublish(jsonattr['value'], jsonattr['category'])

      #try to get coord from ip
      if jsonattr['type'] == "phone-number":
        geo_helper.getCoordFromPhoneAndPublish(jsonattr['value'], jsonattr['category'])

      # Push to log
      if "event_id" in jsonobj:
        jsonobj_tmp = { 'Event':
               { 'id': jsonobj['event_id'] },
               'Attribute': {
                 'id': jsonobj['id'],
                 'type': jsonobj['type'],
                 'category': jsonobj['category'],
                 'event_id': jsonobj['event_id'],
                 'timestamp': jsonobj['timestamp'],
                 'value': jsonobj['value'],
                 'comment': jsonobj['comment'],
            }}
        live_helper.publish_log(zmq_name, attributeType, jsonobj_tmp)

    if "Attribute" in jsonobj:
      if "action" in jsonobj:
        if jsonobj['action'] == "add":
          eventid = jsonobj['Attribute']['event_id']
          if check_event_tag(eventid):
            import_all_attributes = True
          if check_ip(jsonobj['Attribute']['value'], cidr_array) or import_all_attributes:
            print("iterpiam")

          


      #live_helper.publish_log(zmq_name, attributeType, jsonobj)

def handler_diagnostic_tool(zmq_name, jsonobj):
    try:
        res = time.time() - float(jsonobj['content'])
    except Exception as e:
        logger.error(e)
    serv_list.set('diagnostic_tool_response', str(res))

###############
## MAIN LOOP ##
###############

def process_log(zmq_name, event):
    topic, eventdata = event.split(' ', maxsplit=1)
    jsonevent = json.loads(eventdata)
    try:
        dico_action[topic](zmq_name, jsonevent)
    except KeyError as e:
        logger.error(e)

def process_log(zmq_name, event):
    topic, eventdata = event.split(' ', maxsplit=1)
    jsonevent = json.loads(eventdata)
    try:
        dico_action[topic](zmq_name, jsonevent, False)
    except KeyError as e:
        logger.error(e)

def create_cidr_array(): # Creating CIDR array for further processing.
    list_file = open(clientInfo, 'r')
    cidr_list = list_file.read().splitlines()
    for cidr in cidr_list:
        match_text = re.search("[a-zA-Z]", cidr)
        match = re.search("[.]", cidr)
        if not match_text:
            if match:
              cidr_array.append(ipaddress.ip_network(cidr, strict=False))
    return cidr_array

def check_ip(ip_addr, cidr_array): #Checks if IP is under listed CIDRs
    match_text = re.search("[a-zA-Z]", ip_addr)
    match_asn = re.search("[0-9./]", ip_addr)
    if match_text:
        return False
    elif not match_asn:
        return False
    else:
      try:
        ipAddress = ipaddress.ip_address(ip_addr)
      except:
        print('Value {} does not contain IP address.'.format(ip_addr))
        return False
      for cidr in cidr_array:
        if ipAddress in cidr: return True
      return False

def check_event_tag(eventid):
    url = "https://misp-zeromq:8443/events/" + eventid
    re = requests.post(url,
                       verify=False,
                       headers = {"Authorization": dashboard_user_auth_key,
                                  "Accept": "application/json",
                                  "Content-Type": "application/json"
                                }
        )
    re_json = json.loads(re.text)
    import_all_attributes = False
    if 'Tag' in re_json['Event']:
      for tag in re_json['Event']['Tag']:
        if tag['name'] == import_tag_all_attributes:
          import_all_attributes = True

    return import_all_attributes

def main(sleeptime):
    updates.check_for_updates()

    cidr_array = create_cidr_array()

    numMsg = 0
    while True:
        content = serv_list.rpop(LISTNAME)
        if content is None:
            log_text = 'Processed {} message(s) since last sleep.'.format(numMsg)
            logger.info(log_text)
            numMsg = 0
            time.sleep(sleeptime)
            continue
        content = content
        the_json = json.loads(content)
        zmqName = the_json['zmq_name']
        content = the_json['content']
        process_log(zmqName, content)
        numMsg += 1


dico_action = {
        "misp_json":                handler_dispatcher,
        "misp_json_event":          handler_event,
        "misp_json_self":           handler_keepalive,
        "misp_json_attribute":      handler_attribute,
        "misp_json_object":         handler_object,
        "misp_json_sighting":       handler_sighting,
        "misp_json_organisation":   handler_skip,
        "misp_json_user":           handler_user,
        "misp_json_conversation":   handler_conversation,
        "misp_json_object_reference": handler_skip,
        "misp_json_audit": handler_audit,
        "diagnostic_channel":       handler_diagnostic_tool
        }


if __name__ == "__main__":

    parser = argparse.ArgumentParser(description='The ZMQ dispatcher. It pops from the redis buffer then redispatch it to the correct handlers')
    parser.add_argument('-s', '--sleep', required=False, dest='sleeptime', type=int, help='The number of second to wait before checking redis list size', default=1)
    args = parser.parse_args()

    try:
        main(args.sleeptime)
    except (redis.exceptions.ResponseError, KeyboardInterrupt) as error:
        print(error)


