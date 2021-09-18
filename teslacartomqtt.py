#!/usr/bin/env python3
"""
Bridge betwen a Tesla Car connection and MQTT

Configuration can be given on the command line, or via the OS environment, by
prefixing the long option with "TESLA_" and converting to uppercase, for example, the
"--email" option would be:

TESLA_EMAIL=elon@tesla.com


"""
import os
import sys
import argparse
import queue
import time
import json
import math
import traceback
import logging

import paho.mqtt.client
import teslapy


TESLA_QUEUE_TIMEOUT = 11 * 60
TESLA_QUEUE_ACTIVE = 15
TESLA_MAX_SLEEP_TIME = 3600

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s: %(levelname)s:%(name)s: %(message)s"
)
log = logging.getLogger(__name__)


class TeslaToMqtt:
    def __init__(self) -> None:
        self.config = self._initconfig()
        self.carq = queue.Queue()
        self.carq.put(None)  # Start the queue right away
        self._pubstate = {}
        self._vin = None
        if self.config.gpshome:
            parts = self.config.gpshome.split(",")
            lat, lng = forcefloat(parts[0].strip()), forcefloat(parts[1].strip())
            self.home = (lat, lng)
        else:
            self.home = None
        self.error_sleep_time = TESLA_QUEUE_ACTIVE

    def start(self):
        self.client = paho.mqtt.client.Client()
        self.client.on_connect = self.onmqttconnect
        self.client.on_message = self.onmqttmessage
        self.client.connect(self.config.mqtthost)
        self.client.loop_start()

        # Run the tesla thread, resume on error
        while 1:
            try:
                self.teslathread()
            except Exception as e:
                # Clear the command queue
                while self.carq.qsize() > 1:
                    try:
                        self.carq.get_nowait()
                    except queue.Empty:
                        pass

                log.exception("Error in tesla thread")
                # log.debug("Error in tesla thread: %s", e)
                # traceback.print_exc()
                log.info("Sleeping %0.1f seconds from error", self.error_sleep_time)
                time.sleep(self.error_sleep_time)
                self.error_sleep_time *= 1.5
                if self.error_sleep_time > TESLA_MAX_SLEEP_TIME:
                    self.error_sleep_time = TESLA_MAX_SLEEP_TIME

    def onmqttconnect(self, client, userdata, flags, rc):
        self.client.subscribe(f"{self.config.basetopic}/+/set")

    def onmqttmessage(self, client, userdata, msg):
        parts = msg.topic.split("/")
        payload = msg.payload.decode()
        setting = parts[-2]
        log.debug("Incomming MQTT Message: %s : %s", setting, payload)
        if setting == "charge_limit":
            self.carq.put({"name": "CHANGE_CHARGE_LIMIT", "percent": forceint(payload)})
        elif setting == "charging":
            if payload == "true":
                self.carq.put({"name": "START_CHARGE"})
            elif payload == "false":
                self.carq.put({"name": "STOP_CHARGE"})
        else:
            log.error("Unknown MQTT setting: %s", setting)

    def _initconfig(self):
        parser = argparse.ArgumentParser(
            description="Bridge a Tesla Car connection and MQTT."
        )
        parser.add_argument("--email", help="login email", required=True)
        parser.add_argument("--passcode", help="two factor passcode")
        parser.add_argument(
            "--vin",
            help="the VIN of the desired vehicle, "
            "only required if more than one vehicle on the account",
        )
        parser.add_argument("--mqtthost", help="MQTT server host name", required=True)
        parser.add_argument(
            "--basetopic",
            help="base MQTT topic for pub/sub messages, no trailing /",
            default="tesla/car",
        )
        parser.add_argument(
            "--gpshome",
            help='the lat,long of the "home", if not set, vehicle will always be home. '
            "Home is assumed to be within about 100m for lat,long",
        )
        parser.add_argument(
            "--debug",
            help="If set with any value, will include debug level logging",
        )

        # Get the OS environnement arguments
        cmdlineargs = sys.argv.copy()
        for arg, val in os.environ.items():
            if arg.startswith("TESLA_"):
                arg = arg[len("TESLA_") :].lower()
                if val:
                    arg = f"--{arg}={val}"
                else:
                    arg = f"--{arg}"
                cmdlineargs.append(arg)

        args = parser.parse_args(cmdlineargs[1:])
        if args.debug:
            logging.getLogger().setLevel(logging.DEBUG)
        log.debug("Processed command line arguments: %s", cmdlineargs)
        return args

    def teslathread(self):
        with teslapy.Tesla(self.config.email) as tesla:
            tesla.fetch_token()
            cars = tesla.vehicle_list()
            if self.config.vin:
                for car in cars:
                    if car["vin"].upper() == self.config.vin.upper():
                        break
            else:
                car = cars[0]

            cardata = car.get_vehicle_data()
            self._vin = car["vin"]
            self.homeassistantsetup(cardata)

            sleeptime = TESLA_QUEUE_TIMEOUT

            while 1:
                active = False
                if sleeptime > TESLA_QUEUE_TIMEOUT:
                    sleeptime = TESLA_QUEUE_TIMEOUT
                log.debug("Sleeping %.1fs", sleeptime)
                try:
                    cmd = self.carq.get(timeout=sleeptime)
                    active = True
                except queue.Empty:
                    cmd = None

                if cmd:
                    log.debug("Sending Tesla command: %s", cmd)
                    try:
                        car.command(**cmd)
                    except teslapy.VehicleError as e:
                        if e.args[0] != "already_set":
                            raise e
                        else:
                            log.debug("Command already set, ignored")

                # print(vehicles)
                # print(vehicles[0].get_vehicle_data())
                summary = car.get_vehicle_summary()
                log.debug("Tesla car state: %s", summary.get("state"))
                if summary.get("state") == "online":
                    data = car.get_vehicle_data()
                    chargedata = data["charge_state"]
                    drivedata = data["drive_state"]
                    self.pubifchanged("charging", chargedata["charging_state"])
                    self.pubifchanged("time_to_full", chargedata["time_to_full_charge"])
                    self.pubifchanged("battery_level", chargedata["battery_level"])
                    self.pubifchanged("charge_limit", chargedata["charge_limit_soc"])

                    homestate = "home"
                    if self.home:
                        dist = haversine(
                            self.home,
                            (
                                forcefloat(drivedata["latitude"]),
                                forcefloat(drivedata["longitude"]),
                            ),
                        )
                        if dist > 100:
                            homestate = "not_home"
                    self.pubifchanged(
                        "gps",
                        json.dumps(
                            {
                                "latitude": forcefloat(drivedata["latitude"]),
                                "longitude": forcefloat(drivedata["longitude"]),
                                "heading": forcefloat(drivedata["heading"]),
                                "speed": forcefloat(drivedata["speed"]),
                                "state": homestate,
                                "gps_accuracy": 1,
                            }
                        ),
                    )
                    shiftstate = "P"
                    if drivedata["shift_state"]:
                        shiftstate = drivedata["shift_state"]
                        if shiftstate != "P":
                            active = True
                    self.pubifchanged("shift_state", shiftstate)

                    if chargedata["charging_state"] == "Charging":
                        active = True

                    if active:
                        sleeptime = TESLA_QUEUE_ACTIVE
                        if shiftstate == "P":
                            sleeptime *= 4  # Sleep for longer if parked
                    else:
                        sleeptime *= 1.2
                    self.error_sleep_time = TESLA_QUEUE_ACTIVE

    def pubifchanged(self, item, value):
        "Publish to MQTT item (basetopic will be applied), with value, if it has changed."
        if self._pubstate.get(item) != value:
            self.client.publish(f"{self.config.basetopic}/{item}", value)
            self._pubstate[item] = value

    def homeassistantsetup(self, cardata):
        "Publish config for Home Assistant"
        carname = cardata["vehicle_state"]["vehicle_name"]
        carmodel = cardata["vehicle_config"]["car_type"]
        carmodel = carmodel[:-1] + " " + carmodel[-1:]
        carmodel = (
            carmodel.title() + " " + cardata["vehicle_config"]["trim_badging"].upper()
        )
        self.client.publish(
            f"homeassistant/sensor/{self._vin}/charging/config",
            json.dumps(
                {
                    "name": f"{carname} Charging State",
                    "state_topic": f"{self.config.basetopic}/charging",
                    "unique_id": f"{self._vin}_charging",
                    "device": {"identifiers": [f"{self._vin}_device"]},
                    "icon": "mdi:ev-station",
                }
            ),
        )
        self.client.publish(
            f"homeassistant/sensor/{self._vin}/battery/config",
            json.dumps(
                {
                    "name": f"{carname} Battery Level",
                    "state_topic": f"{self.config.basetopic}/battery_level",
                    "unique_id": f"{self._vin}_battery_level",
                    "unit_of_measurement": "%",
                    "device_class": "battery",
                    "device": {
                        "identifiers": [f"{self._vin}_device"],
                        "name": f"{carname} Vehicle",
                        "manufacturer": "Tesla",
                        "model": carmodel,
                    },
                }
            ),
        )
        self.client.publish(
            f"homeassistant/sensor/{self._vin}/timetofull/config",
            json.dumps(
                {
                    "name": f"{carname} Time to Full",
                    "state_topic": f"{self.config.basetopic}/time_to_full",
                    "unique_id": f"{self._vin}_time_to_full",
                    "unit_of_measurement": "h",
                    "device": {"identifiers": [f"{self._vin}_device"]},
                    "icon": "hass:clock-fast",
                }
            ),
        )
        self.client.publish(
            f"homeassistant/number/{self._vin}/chargelimit/config",
            json.dumps(
                {
                    "name": f"{carname} Charge Limit",
                    "state_topic": f"{self.config.basetopic}/charge_limit",
                    "command_topic": f"{self.config.basetopic}/charge_limit/set",
                    "unique_id": f"{self._vin}_charge_limit",
                    "min": 50,
                    "max": 100,
                    "device": {"identifiers": [f"{self._vin}_device"]},
                    "icon": "hass:battery-alert",
                }
            ),
        )

        self.client.publish(
            f"homeassistant/device_tracker/{self._vin}/gps/config",
            json.dumps(
                {
                    "name": f"{carname} Location",
                    "json_attributes_topic": f"{self.config.basetopic}/gps",
                    "state_topic": f"{self.config.basetopic}/gps",
                    "value_template": "{{value_json.state}}",
                    "unique_id": f"{self._vin}_gps",
                    "device": {"identifiers": [f"{self._vin}_device"]},
                    "source_type": "gps",
                    "icon": "mdi:crosshairs-gps",
                }
            ),
        )


def forcefloat(v):
    try:
        return float(v)
    except:
        return 0


def forceint(v):
    return int(forcefloat(v))


# from: https://janakiev.com/blog/gps-points-distance-python/
def haversine(coord1, coord2):
    """Distance between 2 GPS points"""
    R = 6372800  # Earth radius in meters
    lat1, lon1 = coord1
    lat2, lon2 = coord2

    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)

    a = (
        math.sin(dphi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    )

    return 2 * R * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def main():
    t = TeslaToMqtt()
    t.start()


if __name__ == "__main__":
    main()
