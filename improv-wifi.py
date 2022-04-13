import asyncio
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

IMPROV_WIFI_SERVICE_UUID = BTUUID("00467768-6228-2272-4663-277478268000")
print("IMPROV_WIFI_SERVICE_UUID:", IMPROV_WIFI_SERVICE_UUID)

# Statuses:
IMPROV_STATE_STOPPED_BYTES = bytes([0x00])
IMPROV_STATE_AWAIT_AUTHORIZATION_BYTES = bytes([0x01])
IMPROV_STATE_AUTHORIZED_BYTES = bytes([0x02])
IMPROV_STATE_PROVISIONING_BYTES = bytes([0x03])
IMPROV_STATE_PROVISIONED_BYTES = bytes([0x04])

IMPROV_ERROR_NO_ERROR_BYTES = bytes([0x00])
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


def publish_changed_if_changed(key, notifier):
    global global_state
    global previous_state
    current_value = global_state[key]
    previous_value = previous_state[key]
    if current_value != previous_value:
        if key != "debugging":  # do not overspam with debugging info
            print("Publishing changed key '{}' to new value '{}' changed from previous '{}'".format(key, current_value,
                                                                                                    previous_value))
        previous_state[key] = current_value
        notifier.changed(current_value)


def do_identify():
    Popen(["/usr/bin/bash", "/usr/local/sbin/improv-identify.sh"], shell=False, close_fds=True,
          stdin=None, stdout=None, stderr=None)
    print("Done calling identify script")


async def get_wifi_status():
    print("Getting wifi status")
    proc = subprocess.Popen(["/usr/bin/bash", "/usr/local/sbin/improv-status.sh"], shell=False, close_fds=True)
    return_code = proc.wait(timeout=1)  # 2-second timeout to get status
    print("status return code:", return_code)
    if return_code == 0:
        return True
    return False


async def do_connect(ssid, password):
    print("Will connect to wifi SSID '{}' with password '{}'".format(ssid, password))
    proc = subprocess.Popen(["/usr/bin/bash", "/usr/local/sbin/improv-config.sh", ssid, password], shell=False,
                            close_fds=True)
    print("return code:", proc.wait())
    print("Done wifi provisioning with SSID '{}' and password '{}'".format(ssid, password))


def parse_command(value):
    if value == IMPROV_COMMAND_IDENTIFY_BYTES:
        print("Got a call for identify")
        return {"command": "identify"}

    # If not the identify command, then it's a Wi-Fi command, which we need to parse.
    command = value[0]
    data_length = value[1]
    data = value[2:2 + data_length]
    if command == 0x01:  # Send Wifi-Settings.
        print("Got a call for send_wifi_settings")
        ssid_length = data[0]
        ssid_start = 1
        ssid_end = ssid_start + ssid_length

        pass_length = data[ssid_end]
        pass_start = ssid_end + 1
        pass_end = pass_start + pass_length

        ssid = data[ssid_start:ssid_end].decode("utf-8")
        password = data[pass_start:pass_end].decode("utf-8")

        print("Decoding done, will connect to wifi SSID '{}' with password '{}'".format(ssid, password))
        return {"command": "connect", "ssid": ssid, "password": password}

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
        with open("/etc/machine.uuid") as f:  # @TODO: use systemd machine ID instead.
            uuid = f.read().rstrip("\n")
        return bytes(uuid, "utf-8")

    @machine_uuid.descriptor("BAB2", DescFlags.READ)
    def machine_uuid_descriptor(self, options):
        return bytes("BABE, read-only, returns the UUID.", "utf-8")

    # A debugging thing, always increasing counter, we can notify on..
    @characteristic("BEEF", CharFlags.READ | CharFlags.NOTIFY)
    def debugging(self, options):
        print("Got a call for debugging via READ...")
        global global_state
        print("Got a call for debugging via READ: {}".format(global_state["debugging"]))
        return global_state["debugging"]

    # Simple READ, no Notify, return directly.
    @characteristic("00467768-6228-2272-4663-277478268005", CharFlags.READ)
    def capabilities(self, options):
        print("Got a call for capabilities")
        return IMPROV_CAPABILITY_SUPPORTS_IDENTIFY

    # STATE
    @characteristic("00467768-6228-2272-4663-277478268001", CharFlags.READ | CharFlags.NOTIFY)
    def current_state(self, options):
        print("Got a call for current_state via READ...")
        global global_state
        print("Got a call for current_state via READ: {}".format(global_state["state"]))
        return global_state["state"]

    # ERROR
    @characteristic("00467768-6228-2272-4663-277478268002", CharFlags.READ | CharFlags.NOTIFY)
    def error_state(self, options):
        print("Got a call for error_state via READ...")
        global global_state
        print("Got a call for error_state via READ: {}".format(global_state["error"]))
        return global_state["error"]

    # RPC_RESULT
    @characteristic("00467768-6228-2272-4663-277478268004", CharFlags.READ | CharFlags.NOTIFY)
    def rpc_result(self, options):
        print("Got a call for rpc_result via READ...")
        global global_state
        print("Got a call for rpc_result via READ: {}".format(global_state["result"]))
        return global_state["result"]

    # RPC_COMMAND
    # Main driver, which is the WRITE-only command characteristic.
    @characteristic("00467768-6228-2272-4663-277478268003", CharFlags.WRITE).setter
    def rpc_command(self, value, options):
        print("Got a write call for rpc_command")
        print("rpc_command value:", value)
        global global_state
        global_state["command"] = parse_command(value)
        print("Command parsed: {}".format(global_state["command"]))


async def main():
    bus = await get_message_bus()

    improv_wifi_service = ImprovWifiService()
    await improv_wifi_service.register(bus, "/improv/wifi/ImprovWifiService")

    agent = NoIoAgent()  # All-allowed agent, with no IO, requires root.
    await agent.register(bus)

    adapter = await Adapter.get_first(bus)  # Find the first BT adapter on the bus.

    node_name = os.uname().nodename
    print("Advertisement will use node_name: ", node_name)

    # 0x0280 -> "Generic Media Player"
    #  -> see https://specificationrefs.bluetooth.com/assigned-values/Appearance%20Values.pdf
    advert = Advertisement(localName=node_name, serviceUUIDs=[IMPROV_WIFI_SERVICE_UUID], appearance=0x0280,
                           timeout=0)
    await advert.register(bus, adapter)

    print("Advertisement registered")

    global global_state

    improv_wifi_service.error_state.changed(global_state["error"])
    improv_wifi_service.current_state.changed(global_state["state"])
    improv_wifi_service.rpc_result.changed(global_state["result"])
    improv_wifi_service.debugging.changed(global_state["debugging"])

    while True:
        # First, increase the debugging counter and set the debugging characteristic.
        global_state["counter"] += 1
        global_state["debugging"] = bytes("debugging {}".format(global_state["counter"]), "utf-8")

        # Reset the status back to normal after a certain amount of loops.
        if global_state["reset_status_after_counter"] != 0:
            print("Should reset status after counter: {}".format(global_state["reset_status_after_counter"]))
            if global_state["counter"] > global_state["reset_status_after_counter"]:
                print("Resetting status after counter: {}".format(global_state["counter"]))
                global_state["reset_status_after_counter"] = 0
                global_state["command"] = IMPROV_NO_COMMAND
                global_state["state"] = IMPROV_STATE_AUTHORIZED_BYTES  # @TODO: timeout?
                global_state["error"] = IMPROV_ERROR_NO_ERROR_BYTES
                global_state["result"] = IMPROV_RESULT_NONE_BYTES

        if global_state["operation"] == "provisioning":
            # If we're provisioning, check the status.
            print("Checking provisioning status...")
            global_state["loops_after_provisioning_started"] += 1
            if await get_wifi_status():
                print("Provisioning successful!")
                global_state["state"] = IMPROV_STATE_PROVISIONED_BYTES
                global_state["result"] = IMPROV_RESULT_OK_EMPTY_BYTES
                global_state["operation"] = "none"
                global_state["reset_status_after_counter"] = global_state["counter"] + 30
            else:
                if global_state["loops_after_provisioning_started"] > 10:
                    print("Provisioning failed! (Not authorized)")
                    global_state["state"] = IMPROV_STATE_AUTHORIZED_BYTES
                    global_state["error"] = IMPROV_ERROR_NOT_AUTHORIZED_BYTES
                    global_state["operation"] = "none"
                    global_state["reset_status_after_counter"] = global_state["counter"] + 10

        if global_state["command"]["command"] != "none":
            print("Command received!!!!: {}".format(global_state["command"]))
            if global_state["command"]["command"] == "identify":
                do_identify()
            elif global_state["command"]["command"] == "connect":
                print("Will connect!")
                print("SSID: {}".format(global_state["command"]["ssid"]))
                print("Password: {}".format(global_state["command"]["password"]))

                # Do the actual provisioning...
                await do_connect(global_state["command"]["ssid"], global_state["command"]["password"])

                # Send the "provisioning" notification...
                global_state["state"] = IMPROV_STATE_PROVISIONING_BYTES

                # Mark operation as doing something, so we report status on the next loop.
                global_state["operation"] = "provisioning"
                global_state["loops_after_provisioning_started"] = 0

            global_state["command"] = IMPROV_NO_COMMAND

        # Publish everything as notifications.
        # Publish changed attributes as notifications, but only when they actually changed.
        publish_changed_if_changed("debugging", improv_wifi_service.debugging)
        publish_changed_if_changed("error", improv_wifi_service.error_state)
        publish_changed_if_changed("result", improv_wifi_service.rpc_result)
        publish_changed_if_changed("state", improv_wifi_service.current_state)

        await asyncio.sleep(1)

    # Handle any dbus requests.
    # noinspection PyUnreachableCode
    await bus.wait_for_disconnect()


if __name__ == "__main__":
    asyncio.run(main())
