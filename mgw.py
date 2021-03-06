#!/usr/local/bin/python
import serial
import time
import json
import sqlite3
import threading
import random
import logging
import sys
import os
import socket
import re
import argparse
import requests
import smtplib

def log(data, action_config):
  LOG.info("Log action for '%s'", data)
  return True

def send_sms(data, action_config):
  if not action_config.get('enabled'):
    return False

  if 'message' in data:
    msg = data['message']
  else:
    msg = '{sensor_type} on board {board_desc} ({board_id}) reports value {sensor_data}'.format(**data)

  LOG.debug('Sending SMS')

  url = action_config['endpoint']
  params = {'username': action_config['user'],
          'password': action_config['password'],
          'msisdn': action_config['recipient'],
          'message': msg}

  logging.getLogger("urllib3").setLevel(logging.CRITICAL)
  try:
    r = requests.get(url, params=params, timeout=5)
    r.raise_for_status()
  except (requests.HTTPError, requests.ConnectionError, requests.exceptions.Timeout) as e:
    LOG.warning("Got exception '%s' in send_sms", e)
    return False

  result = r.text.split('|')
  if result[0] != '0':
    LOG.warning("Fail in send_sms '%s' '%s'", result[0], result[1])
    return False

  return True

def send_mail(data, action_config):
  if not action_config.get('enabled'):
    return False

  if 'message' in data:
    msg = data['message']
  else:
    msg = '{sensor_type} on board {board_desc} ({board_id}) reports value {sensor_data}'.format(**data)

  LOG.debug('Sending mail')

  message = "From: {sender}\nTo: {recipient}\nSubject: {subject}\n\n{msg}\n\n".format(msg=msg, **action_config)
  try:
    s = smtplib.SMTP(action_config['host'], action_config['port'], timeout=5)
    s.starttls()
    s.login(action_config['user'], action_config['password'])
    s.sendmail(action_config['sender'], action_config['recipient'], message)
    s.quit()
  except (socket.gaierror, socket.timeout, smtplib.SMTPAuthenticationError, smtplib.SMTPDataError) as e:
    LOG.warning("Got exception '%' in send_mail", e)
    return False
  else:
    return True

def action_execute(data, action, action_config):
  result = 0
  for a in action:
    LOG.debug("Action execute '%s'", a)
    if not eval(a['name'])(data, action_config.get(a['name'])):
      if 'failback' in a:
        result += action_execute(data, a['failback'], action_config)
    else:
      result += 1
  return result

def action_helper(data, action_details, action_config=None):
  action_details.setdefault('check_if_armed', {'default': True})
  action_details['check_if_armed'].setdefault('except', [])
  action_details.setdefault('action_interval', 0)
  action_details.setdefault('threshold', 'lambda x: True')
  action_details.setdefault('fail_count', 0)
  action_details.setdefault('fail_interval', 600)

  LOG.debug("Action helper '%s' '%s'", data, action_details)
  now = int(time.time())

  action_status.setdefault(data['board_id'], {})
  action_status[data['board_id']].setdefault(data['sensor_type'], {'last_action': 0, 'last_fail': []})

  action_status[data['board_id']][data['sensor_type']]['last_fail'] = \
    [i for i in action_status[data['board_id']][data['sensor_type']]['last_fail'] if now - i < action_details['fail_interval']]

  if (bool(action_details['check_if_armed']['default']) ^ bool(data['board_id'] in action_details['check_if_armed']['except'])):
    if (not STATUS['armed']):
      return

  if not eval(action_details['threshold'])(data['sensor_data']):
    return

  if len(action_status[data['board_id']][data['sensor_type']]['last_fail']) <= action_details['fail_count']-1:
    action_status[data['board_id']][data['sensor_type']]['last_fail'].append(now)
    return

  if (now - action_status[data['board_id']][data['sensor_type']]['last_action'] <= action_details['action_interval']):
    return

  if action_execute(data, action_details['action'], action_config):
    action_status[data['board_id']][data['sensor_type']]['last_action'] = now

def load_config(config_name):
  if not os.path.isfile(config_name):
    raise KeyError("Config '{}' is missing".format(config_name))

  with open(config_name) as json_config:
    config = json.load(json_config)

  return config

def connect_db(db_file):
  db = sqlite3.connect(db_file)
  return db

def create_db(db_file, appdir, create_sensor_table=False):
  board_map = load_config(appdir + '/boards.config.json')
  db = connect_db(db_file)
  db.execute("DROP TABLE IF EXISTS board_desc");
  db.execute('''CREATE TABLE board_desc(board_id TEXT PRIMARY KEY, board_desc TEXT)''');

  for key in board_map:
    db.execute("INSERT INTO board_desc(board_id, board_desc) VALUES(?, ?)",
      (key, board_map[key]))
  db.commit()

  if (create_sensor_table):
    db.execute("DROP TABLE IF EXISTS metrics")
    db.execute('''CREATE TABLE metrics (id INTEGER PRIMARY KEY AUTOINCREMENT, board_id TEXT, sensor_type TEXT,
      last_update TIMESTAMP DEFAULT (STRFTIME('%s', 'now')), data TEXT DEFAULT NULL)''')

    db.execute("DROP INDEX IF EXISTS idx_board_id")
    db.execute("CREATE INDEX idx_board_id ON metrics (board_id, sensor_type, last_update, data)")

  db.execute("DROP TABLE IF EXISTS last_metrics")
  db.execute("CREATE TABLE last_metrics (board_id TEXT, sensor_type TEXT, last_update TIMESTAMP, data TEXT)")

  db.execute("DROP TRIGGER IF EXISTS insert_metric")
  db.execute('''CREATE TRIGGER insert_metric INSERT ON metrics WHEN NOT EXISTS(SELECT 1 FROM last_metrics
    WHERE board_id=new.board_id and sensor_type=new.sensor_type) BEGIN INSERT into last_metrics
    values(new.board_id, new.sensor_type, new.last_update, new.data); END''')

  db.execute("DROP TRIGGER IF EXISTS update_metric")
  db.execute('''CREATE TRIGGER update_metric INSERT ON metrics WHEN EXISTS(SELECT 1 FROM last_metrics
    WHERE board_id=new.board_id and sensor_type=new.sensor_type) BEGIN UPDATE last_metrics
    SET data=new.data, last_update=new.last_update WHERE board_id==new.board_id
    and sensor_type==new.sensor_type; END''')

def create_logger(level, log_file=None):
  logger = logging.getLogger()
  logger.setLevel(level)
  formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(threadName)s - %(message)s')

  if log_file:
    handler = logging.FileHandler(log_file)
  else:
    handler = logging.StreamHandler(sys.stdout)

  handler.setFormatter(formatter)
  logger.addHandler(handler)

class mgmt_Thread(threading.Thread):
  def __init__(self, appdir):
    super(mgmt_Thread, self).__init__()
    self.name = 'mgmt'

    conf = load_config(appdir + '/global.config.json')
    board_map = load_config(appdir + '/boards.config.json')
    sensor_map = load_config(appdir + '/sensors.config.json')

    self.socket = conf['mgmt_socket']
    self.serial = serial.Serial(conf['serial']['device'],
            conf['serial']['speed'],
            timeout=conf['serial']['timeout'])

    self.msd = failure_Thread(name='msd',
            loop_sleep=conf['msd']['loop_sleep'],
            db_file=conf['db_file'],
            action_interval=conf['msd']['action_interval'],
            query=conf['msd']['query'],
            action=conf['msd']['action'],
            board_map=board_map,
            action_config=conf['action_config']) #Missing sensor detector
    self.mgw = mgw_Thread(ser=self.serial,
            loop_sleep=conf['loop_sleep'],
            gateway_ping_time=conf['gateway_ping_time'],
            db_file=conf['db_file'],
            board_map=board_map,
            sensor_map=sensor_map,
            action_config=conf['action_config'])

  def run(self):
    LOG.info('Starting')

    self.msd.start()
    self.mgw.start()

    if os.path.exists(self.socket):
      os.remove(self.socket)

    self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    self.server.bind(self.socket)
    self.server.listen(1)
    while True:
      conn, addr = self.server.accept()
      data = conn.recv(1024)
      if data:
        try:
          data = json.loads(data)
        except (ValueError) as e:
          LOG.warning("Got exception '%s' in mgmt thread", e)
          continue
        if 'action' in data:
          LOG.debug("Got '%s' on mgmt socket", data)
          if data['action'] == 'status':
            conn.send(json.dumps(STATUS))
          elif data['action'] == 'send' and 'data' in data:
            try:
              r_cmd = "{nodeid}:{cmd}".format(**data['data'])
              self.serial.write(r_cmd)
            except (IOError, ValueError, serial.serialutil.SerialException) as e:
              LOG.error("Got exception '%s' in mgmt thread", e)
          elif data['action'] == 'set' and 'data' in data:
            for key in data['data']:
              STATUS[key] = data['data'][key]
              if key == 'mgw':
                self.mgw.enabled.set() if data['data'][key] else self.mgw.enabled.clear()
              elif key == 'msd':
                self.msd.enabled.set() if data['data'][key] else self.msd.enabled.clear()

      conn.close()

class failure_Thread(threading.Thread):
  def __init__(self, name, loop_sleep, db_file, action_interval,
          query, action, board_map, action_config):
    super(failure_Thread, self).__init__()
    self.name = name
    self.daemon = True
    self.enabled = threading.Event()
    if STATUS[name]:
      self.enabled.set()
    self.loop_sleep = loop_sleep
    self.db_file = db_file
    self.action_interval = action_interval
    self.query = query
    self.action = action
    self.board_map = board_map
    self.action_config = action_config
    self.failed = {}

  def handle_failed(self, board_id, value):
    now = int(time.time())
    data = {'board_id': board_id, 'sensor_data': 1, 'sensor_type': self.name}
    action_details = {'check_if_armed': {'default': 0}, 'action_interval': self.action_interval, 'action': self.action}

    if self.name == 'msd':
      message = 'No update from {} ({}) since {} seconds'.format(self.board_map[board_id],
              board_id, now - value)

      data['message'] = message
      action_helper(data, action_details, self.action_config)

  def run(self):
    LOG.info('Starting')
    self.db = connect_db(self.db_file)
    while True:
      self.enabled.wait()
      now = int(time.time())

      for board_id, value in self.db.execute(self.query):
        self.handle_failed(board_id, value)

      time.sleep(self.loop_sleep)

class mgw_Thread(threading.Thread):
  def __init__(self, ser, loop_sleep, gateway_ping_time,
          db_file, board_map, sensor_map, action_config):
    super(mgw_Thread, self).__init__()
    self.name = 'mgw'
    self.daemon = True
    self.enabled = threading.Event()
    if STATUS["mgw"]:
      self.enabled.set()
    self.serial = ser
    self.loop_sleep = loop_sleep
    self.last_gw_ping = 0
    self.gateway_ping_time = gateway_ping_time
    self.db_file = db_file
    self.sensor_map = sensor_map
    self.board_map = board_map
    self.action_config = action_config

  def ping_gateway(self):
    try:
      self.serial.write('1:1')
      time.sleep(1)
    except (IOError, ValueError, serial.serialutil.SerialException) as e:
      LOG.error("Got exception '%' in ping_gateway", e)
    else:
      self.last_gw_ping = int(time.time())

  def run(self):
    LOG.info('Starting')
    self.db = connect_db(self.db_file)

    #[ID][metric:value] / [10][voltage:3.3]
    re_data = re.compile('\[(?P<board_id>\d+)\]\[(?P<sensor_type>.+):(?P<sensor_data>.+)\]')

    while True:
      self.enabled.wait()
      try:
        s_data = self.serial.readline().strip()
        m = re_data.match(s_data)
        #{"board_id": 0, "sensor_type": "temperature", "sensor_data": 2}
        data = m.groupdict()
      except (IOError, ValueError, serial.serialutil.SerialException) as e:
        LOG.error("Got exception '%' in mgw thread", e)
        self.serial.close()
        time.sleep(self.loop_sleep)
        try:
          self.serial.open()
        except (OSError) as e:
          LOG.warning('Failed to open serial')
        continue
      except (AttributeError) as e:
        if len(s_data) > 0:
          LOG.debug('> %s', s_data)
        continue
      finally:
        if (int(time.time()) - self.last_gw_ping >= self.gateway_ping_time):
          self.ping_gateway()

      LOG.debug("Got data from serial '%s'", data)

      try:
        self.db.execute("INSERT INTO metrics(board_id, sensor_type, data) VALUES(?, ?, ?)",
                (data['board_id'], data['sensor_type'], data['sensor_data']))
        self.db.commit()
      except (sqlite3.IntegrityError) as e:
        LOG.error("Got exception '%' in mgw thread", e)
      except (sqlite3.OperationalError) as e:
        time.sleep(1+random.random())
        try:
          self.db.commit()
        except (sqlite3.OperationalError) as e:
          LOG.error("Got exception '%' in mgw thread", e);

      try:
        action_details = self.sensor_map[data['sensor_type']]
        action_details['action']
      except (KeyError) as e:
        LOG.debug("Missing sensor_map/action for sensor_type '%s'", data['sensor_type'])
      else:
        data['board_desc'] = self.board_map[str(data['board_id'])]
        action_helper(data, action_details, self.action_config)

STATUS = {
  "armed": 1,
  "msd": 1,
  "mgw": 1,
  "fence": 1,
}
action_status = {}

LOG = logging.getLogger(__name__)

if __name__ == "__main__":
  parser = argparse.ArgumentParser(description='Moteino gateway')
  parser.add_argument('--dir', required=True, help='Root directory, should cotains *.config.json')
  args = parser.parse_args()

  conf = load_config(args.dir + '/global.config.json')
  create_logger(conf['logging']['level'])

  mgmt = mgmt_Thread(appdir=args.dir)
  mgmt.start()
