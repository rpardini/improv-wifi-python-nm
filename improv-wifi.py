import asyncio
import logging
import os
import subprocess
from subprocess import Popen

from bluez_peripheral.advert import Advertisement
from bluez_peripheral.agent import NoIoAgent
from bluez_peripheral.gatt.characteristic import characteristic, CharacteristicFlags as CharFlags
from bluez_peripheral.gatt.descriptor import DescriptorFlags as DescFlags
from bluez_peripheral.gatt.service import Service
from bluez_peripheral.util import *
from bluez_peripheral.uuid import BTUUID
from cysystemd import journal
from cysystemd.daemon import notify, Notification

# Setup logging;
if os.environ.get("NOTIFY_SOCKET") is not None:
    # under systemd; default to DEBUG, and log ONLY to systemd journal (NOT STDOUT)
    logging.basicConfig(level=logging.DEBUG, handlers=[journal.JournaldLogHandler()])
else:
    # not under systemd; default to DEBUG, and log to STDOUT
    logging.basicConfig(level=logging.DEBUG)

logger = logging.getLogger("improv-wifi")

# Notify systemd we're going up...
if os.environ.get("NOTIFY_SOCKET") is not None:
    notify(Notification.STATUS, "Starting up...")

IMPROV_WIFI_SERVICE_UUID = BTUUID("00467768-6228-2272-4663-277478268000")
logger.debug("IMPROV_WIFI_SERVICE_UUID: %s", IMPROV_WIFI_SERVICE_UUID,
             extra={"IMPROV_WIFI_SERVICE_UUID": IMPROV_WIFI_SERVICE_UUID})

# Statuses:
IMPROV_STATE_STOPPED_BYTES = bytes([0x00])
IMPROV_STATE_AWAIT_AUTHORIZATION_BYTES = bytes([0x01])
IMPROV_STATE_AUTHORIZED_BYTES = bytes([0x02])
IMPROV_STATE_PROVISIONING_BYTES = bytes([0x03])
IMPROV_STATE_PROVISIONED_BYTES = bytes([0x04])

IMPROV_ERROR_NO_ERROR_BYTES = bytes([0x00])
IMPROV_ERROR_UNABLE_TO_CONNECT_BYTES = bytes([0x03])
IMPROV_ERROR_NOT_AUTHORIZED_BYTES = bytes([0x04])

IMPROV_CAPABILITY_SUPPORTS_IDENTIFY = bytes([0x01])

IMPROV_COMMAND_IDENTIFY_BYTES = bytes([0x02, 0x00, 0x02])

IMPROV_RESULT_NONE_BYTES = bytes([0x00, 0x00, 0x00])
IMPROV_RESULT_OK_EMPTY_BYTES = bytes([0x01, 0x00, 0x01])

IMPROV_NO_COMMAND = {"command": "none"}

global_state = {
    "command": IMPROV_NO_COMMAND,
    "state": IMPROV_STATE_AUTHORIZED_BYTES,
    "error": IMPROV_ERROR_NO_ERROR_BYTES,
    "result": IMPROV_RESULT_NONE_BYTES,
    "connect_hotspot": False,
    "debugging": bytes("no debugging yet", "utf-8"),
    "counter": 0,
    "operation": "none",
    "loops_after_provisioning_started": 0,
    "reset_status_after_counter": 0
}

previous_state = {
    "state": None,
    "error": None,
    "result": None,
    "debugging": None
}

timeout_logger_modulo = 60
lock_after_counter = (5 * 60)  # 5 Minutes...


def publish_changed_if_changed(key, notifier):
    global global_state
    global previous_state
    current_value = global_state[key]
    previous_value = previous_state[key]
    if current_value != previous_value:
        if key != "debugging":  # do not overspam with debugging info
            logger.info(
                "Publishing changed key '%s' to new value '%s' changed from previous '%s'", key, current_value,
                previous_value, extra={"KEY": key, "NEW_VALUE": current_value, "PREVIOUS_VALUE": previous_value})
        if key == "state":
            logger.warning("State changed from '%s' to '%s'", previous_value, current_value)
        previous_state[key] = current_value
        notifier.changed(current_value)


def do_identify():
    Popen(["/usr/bin/bash", "/usr/local/sbin/improv-identify.sh"], shell=False, close_fds=True,
          stdin=None, stdout=None, stderr=None)
    logger.info("Done calling identify script")


def get_wifi_status(hotspot):
    return_code = get_wifi_status_raw()
    if hotspot:
        if return_code == 10:
            return True
    else:
        if return_code == 0:
            return True
    return False


def get_wifi_status_raw():
    logger.debug("Getting wifi status")
    proc = subprocess.Popen(["/usr/bin/bash", "/usr/local/sbin/improv-status.sh"], shell=False, close_fds=True)
    return_code = proc.wait(timeout=1)  # 2-second timeout to get status
    logger.info("wifi status return code: %s", return_code, extra={"WIFI_STATUS_RETURN_CODE": return_code})
    return return_code


def get_wifi_ap_list():
    proc = subprocess.Popen(["/usr/bin/bash", "/usr/local/sbin/improv-listaps.sh"], shell=False, close_fds=True,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    output = proc.communicate()[0].decode("utf-8")  # not absolutely clear this is utf-8
    logger.info("wifi list output: '%s'", output.replace("\n", ","))

    wifi_ap_power_dict = {}  # Dict to store AP -> power pairs
    lines = output.split("\n")  # Split the lines into array

    for line in lines:  # Loop over the lines
        line = line.strip()  # trim the line
        if line == "": continue  # skip empty lines
        name, power = line.replace("\\:", "--escapedcolon--").split(":")  # Split the line into name and power
        name = name.replace("--escapedcolon--", ":")
        power = int(power)  # convert power to int
        if power > wifi_ap_power_dict.get(name, 0):
            wifi_ap_power_dict[name] = power  # Update the dict with the new power

    # get an array with the dict keys for the first 25 items
    to_return_keys = [x[0] for x in (sorted(wifi_ap_power_dict.items(), key=lambda x: x[1], reverse=True)[:25])]
    logger.debug("to_return_keys: '%s'", to_return_keys)

    # join the keys with a null byte, plus a null byte at the end
    return "\0".join(to_return_keys) + "\0"


async def do_connect(ssid, password):
    logger.info("Will connect to wifi SSID '%s' with password length '%s'", ssid, len(password), extra={"SSID": ssid})
    proc = subprocess.Popen(["/usr/bin/bash", "/usr/local/sbin/improv-config.sh", ssid, password], shell=False,
                            close_fds=True)
    logger.info("return code: %s", proc.wait())
    logger.warning("Done wifi provisioning with SSID '%s' and password length '%s'", ssid, len(password),
                   extra={"SSID": ssid})


def parse_command(value):
    if value == IMPROV_COMMAND_IDENTIFY_BYTES:
        logger.info("Got a call for identify")
        return {"command": "identify"}

    # If not the identify command, then it's a Wi-Fi command, which we need to parse.
    command = value[0]
    data_length = value[1]
    data = value[2:2 + data_length]
    if command == 0x01:  # Send Wifi-Settings.
        logger.info("Got a call for send_wifi_settings")
        ssid_length = data[0]
        ssid_start = 1
        ssid_end = ssid_start + ssid_length

        pass_length = data[ssid_end]
        pass_start = ssid_end + 1
        pass_end = pass_start + pass_length

        ssid = data[ssid_start:ssid_end].decode("utf-8")
        password = data[pass_start:pass_end].decode("utf-8")

        logger.info(
            "Decoding done, will connect to wifi SSID '%s' with password length '%s'", ssid, len(password))
        return {"command": "connect", "ssid": ssid, "password": password, "hotspot": (ssid == "" and password == "")}

    return {"command": "unknown"}


# due to options parameter being required, but unused.
# noinspection PyUnusedLocal
class ImprovWifiService(Service):
    def __init__(self):
        super().__init__(uuid=IMPROV_WIFI_SERVICE_UUID, primary=True)

    # ######## Some extra characteristics, with hostname and UUID. #############
    @characteristic("CAFE", CharFlags.READ)
    def machine_name(self, options):
        return bytes(os.uname().nodename, "utf-8")

    @machine_name.descriptor("CAF2", DescFlags.READ)
    def machine_name_descriptor(self, options):
        return bytes("CAFE, read-only, returns the hostname.", "utf-8")

    @characteristic("BABE", CharFlags.READ)
    def machine_uuid(self, options):
        with open(os.environ.get('UUID_FILE')) as f:
            uuid = f.read().rstrip("\n")
        return bytes(uuid, "utf-8")

    @machine_uuid.descriptor("BAB2", DescFlags.READ)
    def machine_uuid_descriptor(self, options):
        return bytes("BABE, read-only, returns the UUID.", "utf-8")

    # ### Extra characteristics for network state ####
    @characteristic("DEAD", CharFlags.READ)
    def network_state(self, options):
        logger.debug("Got a call for network_state")
        status_bytes = bytes([get_wifi_status_raw()])
        logger.info("Got a call for network_state, result '%s'", status_bytes)
        return status_bytes

    @network_state.descriptor("DEA2", DescFlags.READ)
    def network_state_descriptor(self, options):
        return bytes(
            "Networking state; 0=WifiClient configured+connected; 1=Wifi configured, but not connected. 10/11 same "
            "for Hotspot. 55=unknown.",
            "utf-8"
        )

    # Extra characteristic, READ-only, returns a list of available SSIDs, sorted and null-separated and terminated
    @characteristic("B00B", CharFlags.READ)
    def ap_list(self, options):
        logger.debug("Got a call for ap_list, starting...")
        ap_list_bytes = bytes(get_wifi_ap_list(), "utf-8")
        logger.info("Got a call for ap_list, result '%s'", ap_list_bytes)
        return ap_list_bytes

    @ap_list.descriptor("B002", DescFlags.READ)
    def ap_list_descriptor(self, options):
        return bytes("Null separated/terminated list of available SSIDs in UTF-8. If empty, then no SSIDs available.",
                     "utf-8")

    # A debugging thing, always increasing counter, we can notify on..
    @characteristic("BEEF", CharFlags.READ | CharFlags.NOTIFY)
    def debugging(self, options):
        logger.debug("Got a call for debugging via READ...")
        global global_state
        logger.info("Got a call for debugging via READ: %s", global_state["debugging"])
        return global_state["debugging"]

    # Simple READ, no Notify, return directly.
    @characteristic("00467768-6228-2272-4663-277478268005", CharFlags.READ)
    def capabilities(self, options):
        logger.info("Got a call for capabilities")
        return IMPROV_CAPABILITY_SUPPORTS_IDENTIFY

    # STATE
    @characteristic("00467768-6228-2272-4663-277478268001", CharFlags.READ | CharFlags.NOTIFY)
    def current_state(self, options):
        logger.debug("Got a call for current_state via READ...")
        global global_state
        logger.info("Got a call for current_state via READ: %s", global_state["state"])
        return global_state["state"]

    # ERROR
    @characteristic("00467768-6228-2272-4663-277478268002", CharFlags.READ | CharFlags.NOTIFY)
    def error_state(self, options):
        logger.debug("Got a call for error_state via READ...")
        global global_state
        logger.info("Got a call for error_state via READ: %s", global_state["error"])
        return global_state["error"]

    # RPC_RESULT
    @characteristic("00467768-6228-2272-4663-277478268004", CharFlags.READ | CharFlags.NOTIFY)
    def rpc_result(self, options):
        logger.debug("Got a call for rpc_result via READ...")
        global global_state
        logger.info("Got a call for rpc_result via READ: %s", global_state["result"])
        return global_state["result"]

    # RPC_COMMAND
    # Main driver, which is the WRITE-only command characteristic.
    @characteristic("00467768-6228-2272-4663-277478268003", CharFlags.WRITE).setter
    def rpc_command(self, value, options):
        logger.debug("Got a write call for rpc_command")
        global global_state
        global_state["command"] = parse_command(value)
        logger.info("Command parsed: '%s'", global_state["command"]["command"])


async def main():
    bus = await get_message_bus()

    improv_wifi_service = ImprovWifiService()
    await improv_wifi_service.register(bus, "/improv/wifi/ImprovWifiService")

    agent = NoIoAgent()  # All-allowed agent, with no IO, requires root.
    await agent.register(bus)

    adapter = await Adapter.get_first(bus)  # Find the first BT adapter on the bus.

    node_name = os.uname().nodename
    logger.info("Advertisement will use node_name: %s", node_name, extra={"NODE_NAME": node_name})

    # 0x0280 -> "Generic Media Player"
    #  -> see https://specificationrefs.bluetooth.com/assigned-values/Appearance%20Values.pdf
    advert = Advertisement(localName=node_name, serviceUUIDs=[IMPROV_WIFI_SERVICE_UUID], appearance=0x0280,
                           timeout=0)
    await advert.register(bus, adapter)

    logger.info("Advertisement registered")

    global global_state
    global lock_after_counter

    improv_wifi_service.error_state.changed(global_state["error"])
    improv_wifi_service.current_state.changed(global_state["state"])
    improv_wifi_service.rpc_result.changed(global_state["result"])
    improv_wifi_service.debugging.changed(global_state["debugging"])

    # Notify systemd all is well.
    if os.environ.get("NOTIFY_SOCKET") is not None:
        notify(Notification.READY)
        notify(Notification.STATUS, "Started!")

    while True:
        # First, increase the debugging counter and set the debugging characteristic.
        global_state["counter"] += 1
        global_state["debugging"] = bytes("debugging {}".format(global_state["counter"]), "utf-8")
        set_state_via_timeout = True

        # Reset the status back to normal after a certain amount of loops.
        if global_state["reset_status_after_counter"] != 0:
            logger.debug("Should reset status after counter: %s, current counter %s",
                         global_state["reset_status_after_counter"], global_state["counter"],
                         extra={"COUNTER": global_state["counter"]})
            if global_state["counter"] > global_state["reset_status_after_counter"]:
                logger.warning("Resetting status after counter: %s", global_state["counter"],
                               extra={"COUNTER": global_state["counter"]})
                global_state["reset_status_after_counter"] = 0
                global_state["command"] = IMPROV_NO_COMMAND
                global_state["result"] = IMPROV_RESULT_NONE_BYTES
                global_state["error"] = IMPROV_ERROR_NO_ERROR_BYTES
                set_state_via_timeout = True  # Yes, reset the state based on the timer...

        if global_state["operation"] == "provisioning":
            # If we're provisioning, check the status.
            set_state_via_timeout = False  # don't change the state later, we're provisioning.
            logger.info("Checking provisioning status...")
            global_state["loops_after_provisioning_started"] += 1
            if get_wifi_status(global_state["connect_hotspot"]):
                logger.warning("Provisioning successful!")
                global_state["state"] = IMPROV_STATE_PROVISIONED_BYTES
                global_state["result"] = IMPROV_RESULT_OK_EMPTY_BYTES
                global_state["error"] = IMPROV_ERROR_NO_ERROR_BYTES
                global_state["operation"] = "none"
                # Will stay in this state. To reprovision, user will have to reboot.
            else:
                if global_state["loops_after_provisioning_started"] > 10:
                    logger.error("Provisioning failed! (Unable to connect to WiFi)")
                    global_state["error"] = IMPROV_ERROR_UNABLE_TO_CONNECT_BYTES
                    global_state["operation"] = "none"
                    global_state["reset_status_after_counter"] = global_state["counter"] + 15

        if global_state["command"]["command"] != "none":
            logger.info("Command received!!!!: %s", global_state["command"]["command"])
            if global_state["command"]["command"] == "identify":
                do_identify()
            elif global_state["command"]["command"] == "connect":
                # Make sure we're authorized, do nothing if we're not.
                if global_state["state"] == IMPROV_STATE_AUTHORIZED_BYTES:
                    set_state_via_timeout = False  # don't change the state later, we're handling a command.
                    # but, do give it an extra minute before locking, since we might be over it by a large amount.
                    if global_state["counter"] >= lock_after_counter:
                        logger.warning("Counter is over limit, giving it an extra minute...")
                        lock_after_counter = global_state["counter"] + 62

                    logger.info("Will connect!",
                                extra={"COUNTER": global_state["counter"], "SSID": global_state["command"]["ssid"]})

                    # Mark global state as hotspot if such is the case.
                    global_state["connect_hotspot"] = global_state["command"]["hotspot"]

                    # Do the actual provisioning...
                    await do_connect(global_state["command"]["ssid"], global_state["command"]["password"])

                    # Send the "provisioning" notification...
                    global_state["state"] = IMPROV_STATE_PROVISIONING_BYTES

                    # Mark operation as doing something, so we report status on the next loop.
                    global_state["operation"] = "provisioning"
                    global_state["loops_after_provisioning_started"] = 0
                else:
                    logger.error("Got command to connect, but not authorized!")
                    global_state["error"] = IMPROV_ERROR_NOT_AUTHORIZED_BYTES
                    global_state["reset_status_after_counter"] = global_state["counter"] + 3  # Change back after 3s

            global_state["command"] = IMPROV_NO_COMMAND

        # Default set state to lock provisioning after the timeout.
        if set_state_via_timeout:
            new_state = IMPROV_STATE_AUTHORIZED_BYTES  # by default unlocked.

            # Get file name from PROVISION_CONFIG_FILE - If it does NOT exist, we've never been provisioned, so no
            if os.path.exists(os.environ.get('PROVISION_CONFIG_FILE')):

                if global_state["counter"] % timeout_logger_modulo == 0:
                    logger.debug("PROVISION_CONFIG_FILE exists. counter: %s, lock_after_counter: %s",
                                 global_state["counter"], lock_after_counter)

                if global_state["counter"] > lock_after_counter:
                    new_state = IMPROV_STATE_AWAIT_AUTHORIZATION_BYTES  # locked!

                    # log it only once, otherwise, we'll flood the logs.
                    if global_state["state"] != new_state:
                        logger.warning("Counter is over limit, LOCKED...")
                        if os.environ.get("NOTIFY_SOCKET") is not None:
                            notify(Notification.STATUS, "Working, but LOCKED.")
            else:
                if global_state["counter"] % timeout_logger_modulo == 0:
                    logger.debug("PROVISION_CONFIG_FILE does not exist, won't lock. counter: %s",
                                 global_state["counter"])

            global_state["state"] = new_state

        else:
            logger.debug("Not setting state via timeout (%s): %s", global_state["counter"], global_state["state"])

        # Publish changed attributes as notifications, but only when they actually changed.
        publish_changed_if_changed("debugging", improv_wifi_service.debugging)
        publish_changed_if_changed("error", improv_wifi_service.error_state)
        publish_changed_if_changed("result", improv_wifi_service.rpc_result)
        publish_changed_if_changed("state", improv_wifi_service.current_state)

        await asyncio.sleep(1)

    # Handle any dbus requests.
    # noinspection PyUnreachableCode
    await bus.wait_for_disconnect()


try:
    if (os.environ.get('PROVISION_CONFIG_FILE')) is None:
        raise Exception("PROVISION_CONFIG_FILE environment variable not set")
    if (os.environ.get('UUID_FILE')) is None:
        raise Exception("UUID_FILE environment variable not set")
    asyncio.run(main())
except Exception as e:
    logger.exception("main exception", exc_info=e)
