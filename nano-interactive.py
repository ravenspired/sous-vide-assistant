#!/usr/bin/env python3
"""
Anova Nano Sous Vide BTLE Communication Module (Interactive)

This script auto-scans for an Anova Nano device, connects, reads the temperature unit,
and then enters an interactive command loop. You can choose from commands to start/stop
cooking, read sensor values, target temperature, timer, firmware info, or send settings
commands (set unit, set target temp, set timer).

Commands supported (mirroring the JavaScript implementation):
  START              - Start cooking.
  STOP               - Stop cooking.
  GET_SENSORS        - Get sensor values.
  READ_TARGET_TEMP   - Read current target temperature.
  READ_UNIT          - Read current temperature unit.
  READ_TIMER         - Read current cooking timer.
  GET_FIRMWARE_INFO  - Get firmware info.
  SET_UNIT           - Set temperature unit (C or F).
  SET_TEMP           - Set target temperature (float).
  SET_TIMER          - Set cooking timer (in seconds).
Type "exit" to quit.
"""

import asyncio
import struct
import logging
from dataclasses import dataclass
from typing import Optional, Any

# Third party imports – install these via pip if needed
import bleak
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData

# Import the generated protobuf module (compiled from anova.proto)
import anova_pb2

# Setup logging
logger = logging.getLogger(__name__)

# Constants for BTLE communication
class NanoConstants:
    """Constants for Anova Nano device communication"""
    SERVICE_UUID = "0e140000-0af1-4582-a242-773e63054c68"
    TX_CHAR_UUID = "0e140001-0af1-4582-a242-773e63054c68"
    RX_CHAR_UUID = "0e140002-0af1-4582-a242-773e63054c68"
    ASYNC_CHAR_UUID = "0e140003-0af1-4582-a242-773e63054c68"
    
    # Scales used by the device (if not overridden by a device read)
    TARGET_TEMP_SCALE = 10
    TEMP_SCALE = 100
    SENSOR_TEMP_SCALE = 1
    
    # Scanning timeout in seconds
    SCAN_TIMEOUT = 10.0

@dataclass
class SensorValue:
    """
    Convenience wrapper for sensor readings.
    
    This class can convert to/from the anova_pb2.SensorValue protobuf message.
    (Its fields are: value, units, and sensor_type.)
    """
    value: int
    units: int  
    sensor_type: int

    def to_proto(self) -> bytes:
        proto = anova_pb2.SensorValue()
        proto.value = self.value
        proto.units = self.units
        proto.sensorType = self.sensor_type
        return proto.SerializeToString()

    @classmethod
    def from_proto(cls, data: bytes) -> 'SensorValue':
        proto = anova_pb2.SensorValue()
        proto.ParseFromString(data)
        return cls(
            value=proto.value,
            units=proto.units,
            sensor_type=proto.sensorType
        )

class DeviceNotFoundError(Exception):
    """Raised when no Anova device is found during scanning."""
    pass

class ConnectionFailedError(Exception):
    """Raised when connection to device fails."""
    pass

def cobs_encode(data: bytes) -> bytes:
    """
    Consistent Overhead Byte Stuffing (COBS) encoding.
    Returns the COBS‑encoded bytes with a trailing zero.
    """
    output = bytearray()
    code_ptr = 0
    code = 1
    
    output.append(0)  # Reserve space for code
    
    for byte in data:
        if byte == 0:
            output[code_ptr] = code
            code_ptr = len(output)
            output.append(0)
            code = 1
        else:
            output.append(byte)
            code += 1
            if code == 0xFF:
                output[code_ptr] = code
                code_ptr = len(output)
                output.append(0)
                code = 1
                
    output[code_ptr] = code
    output.append(0)
    return bytes(output)

def cobs_decode(data: bytes) -> bytes:
    """
    Decode COBS-encoded data.
    Returns the original bytes.
    """
    output = bytearray()
    i = 0
    while i < len(data):
        code = data[i]
        i += 1
        for j in range(code - 1):
            if i < len(data):
                output.append(data[i])
                i += 1
        if code < 0xFF and i < len(data):
            output.append(0)
    return bytes(output)

class AnovaNanoDevice:
    """
    Controller class for the Anova Nano sous vide device.
    Handles BTLE communication, command sending, and status monitoring.
    """
    def __init__(self, device: BLEDevice, client: bleak.BleakClient):
        self.device = device
        self.client = client
        self._is_cooking = False
        self._temp_unit = None  # Will be read from the device
        
        # Temperature limits
        self.max_f = 197.0
        self.min_f = 32.0
        self.max_c = 92.0
        self.min_c = 0.0
        
    @classmethod
    async def discover_and_connect(cls) -> 'AnovaNanoDevice':
        logger.info("Starting scan for Anova devices...")
        def detection_callback(device: BLEDevice, advertisement_data: AdvertisementData):
            logger.debug(f"Discovered device: {device.name} ({device.address})")
        scanner = bleak.BleakScanner(
            detection_callback,
            service_uuids=[NanoConstants.SERVICE_UUID]
        )
        devices = await scanner.discover(timeout=NanoConstants.SCAN_TIMEOUT)
        anova_devices = [
            d for d in devices
            if any(u.lower() == NanoConstants.SERVICE_UUID.lower() for u in d.metadata.get("uuids", []))
        ]
        if not anova_devices:
            raise DeviceNotFoundError("No Anova devices found")
        device = anova_devices[0]
        logger.info(f"Connecting to device at {device.address}")
        try:
            client = bleak.BleakClient(device)
            await client.connect()
            if not client.is_connected:
                raise ConnectionFailedError(f"Failed to connect to device at {device.address}")
            logger.info(f"Successfully connected to device at {device.address}")
            nano = cls(device, client)
            await nano.prepare_for_use()
            return nano
        except Exception as e:
            raise ConnectionFailedError(f"Error connecting to device at {device.address}: {str(e)}")
        
    async def prepare_for_use(self):
        """Setup notification handlers and read the device temperature unit."""
        self._response_queue = asyncio.Queue()
        await self.client.start_notify(NanoConstants.RX_CHAR_UUID, self._notification_handler)
        await self.client.start_notify(NanoConstants.ASYNC_CHAR_UUID, self._async_notification_handler)
        self._temp_unit = await self.get_temp_unit()
        logger.info(f"Device temperature unit set to: {self._temp_unit}")
        
    def _notification_handler(self, char_handle: int, data: bytes):
        logger.debug(f"Received notification: {data.hex()}")
        if hasattr(self, "_response_queue"):
            self._response_queue.put_nowait(data)
            
    def _async_notification_handler(self, char_handle: int, data: bytes):
        logger.debug(f"Received async notification: {data.hex()}")
        
    def _encode_command(self, domain: int, msg_type: int, data: Optional[bytes] = None) -> bytes:
        cmd = struct.pack('BB', domain, msg_type)
        if data:
            cmd += data
        return cobs_encode(cmd)
        
    def decode_integer_value(self, data: bytes) -> int:
        """
        Decode a COBS-encoded IntegerValue protobuf message.
        
        Returns:
            The integer value contained in the message.
        """
        try:
            decoded = cobs_decode(data)
            logger.debug(f"Decoded IntegerValue bytes (raw): {decoded.hex()}")
            
            # Remove trailing zero (COBS frame delimiter) if present.
            if decoded and decoded[-1] == 0:
                decoded = decoded[:-1]
                logger.debug(f"Trimmed trailing zero: {decoded.hex()}")
            
            # If the message has a 2-byte header (e.g. for timer: 00 12 or for unit: 00 07)
            # and if the third byte is the expected protobuf tag (0x08 for field 1),
            # then remove the header.
            if len(decoded) >= 4 and decoded[2] == 0x08:
                logger.debug("Detected header in IntegerValue message, stripping first 2 bytes")
                decoded = decoded[2:]
                logger.debug(f"Bytes after header removal: {decoded.hex()}")
            
            int_msg = anova_pb2.IntegerValue()
            int_msg.ParseFromString(decoded)
            return int_msg.value
        except Exception as e:
            logger.error("Error decoding IntegerValue: %s", e)
            raise


    async def get_temp_unit(self) -> int:
        """Read the device's temperature unit (GET_TEMP_UNITS)."""
        logger.info("Reading temperature unit from device")
        cmd = self._encode_command(
            anova_pb2.DomainType.ANOVA_DOMAIN_ID_CONFIG,
            anova_pb2.ConfigDomainMessageType.GET_TEMP_UNITS
        )
        result = await self.send_command(cmd, decoder=self.decode_integer_value)
        logger.info(f"Received temperature unit value: {result}")
        return result

    async def read_target_temp(self) -> float:
        """
        Read target temperature (GET_TEMP_SETPOINT) as a float, scaled to degrees.
        """
        cmd = self._encode_command(
            anova_pb2.DomainType.ANOVA_DOMAIN_ID_CONFIG,
            anova_pb2.ConfigDomainMessageType.GET_TEMP_SETPOINT
        )
        value = await self.send_command(cmd, decoder=self.decode_integer_value)
        # Divide by the scale factor (10) to convert back to degrees.
        return value / NanoConstants.TARGET_TEMP_SCALE


    async def read_timer(self) -> int:
        """Read the cooking timer (GET_COOKING_TIMER) as an integer."""
        cmd = self._encode_command(
            anova_pb2.DomainType.ANOVA_DOMAIN_ID_CONFIG,
            anova_pb2.ConfigDomainMessageType.GET_COOKING_TIMER
        )
        value = await self.send_command(cmd, decoder=self.decode_integer_value)
        return value

    def decode_firmware_info(self, data: bytes) -> dict:
        """
        Decode a COBS-encoded FirmwareInfo protobuf message into a dictionary.
        
        This function searches for the first occurrence of 0x0A (the commitId tag)
        and uses the bytes from that point onward for parsing. It converts the commitId
        into a hexadecimal string and formats the dateCode as a human-readable date.
        """
        try:
            decoded = cobs_decode(data)
            logger.debug(f"Decoded FirmwareInfo bytes (raw): {decoded.hex()}")
            # Remove trailing zero (COBS frame delimiter) if present.
            if decoded and decoded[-1] == 0:
                decoded = decoded[:-1]
                logger.debug(f"Trimmed trailing zero: {decoded.hex()}")
            # Find the first occurrence of 0x0A (commitId tag, field 1, wire type 2)
            idx = decoded.find(b'\x0A')
            if idx == -1:
                raise ValueError("0x0A (commitId tag) not found in FirmwareInfo message")
            trimmed = decoded[idx:]
            logger.debug(f"FirmwareInfo bytes after header removal: {trimmed.hex()}")
            
            info = anova_pb2.FirmwareInfo()
            info.ParseFromString(trimmed)
            
            # Convert commitId to a hex string if it is in bytes.
            commit_id = info.commitId
            if isinstance(commit_id, bytes):
                commit_id = commit_id.hex()
                
            # Convert the dateCode (assumed to be a UNIX timestamp) to a readable date.
            from datetime import datetime
            try:
                date_str = datetime.fromtimestamp(info.dateCode).strftime('%Y-%m-%d %H:%M:%S')
            except Exception:
                date_str = str(info.dateCode)
                
            return {"commitId": commit_id, "tagId": info.tagId, "dateCode": date_str}
        except Exception as e:
            logger.error("Error decoding FirmwareInfo: %s", e)
            raise Exception("Error parsing message with type 'FirmwareInfo'") from e



    async def get_firmware_info(self) -> dict:
        """Get firmware info (GET_FIRMWARE_INFO)."""
        cmd = self._encode_command(
            anova_pb2.DomainType.ANOVA_DOMAIN_ID_CONFIG,
            anova_pb2.ConfigDomainMessageType.GET_FIRMWARE_INFO
        )
        info = await self.send_command(cmd, decoder=self.decode_firmware_info)
        return info

    async def send_command(self, command: bytes, decoder: Optional[Any] = None) -> Any:
        """
        Send a command to the device and wait for a full response.
        The response is aggregated from notifications until a chunk is less than 20 bytes
        or its last byte is zero.
        """
        if not self.client.is_connected:
            raise ConnectionFailedError("Device disconnected")
            
        for i in range(0, len(command), 20):
            chunk = command[i:i + 20]
            await self.client.write_gatt_char(NanoConstants.TX_CHAR_UUID, chunk)
            
        response_chunks = []
        while True:
            chunk = await self._response_queue.get()
            response_chunks.append(chunk)
            if len(chunk) < 20 or chunk[-1] == 0:
                break
        
        full_response = b"".join(response_chunks)
        logger.debug(f"Aggregated full response: {full_response.hex()}")
        
        if decoder:
            return decoder(full_response)
        return full_response

    def decode_sensor_value_list(self, data: bytes) -> dict:
        """
        Decode a COBS-encoded SensorValueList protobuf message into a dict.
        Extracts the water temperature sensor value (and other sensor data)
        and applies proper scaling. The sensor values are converted into standard
        Python dictionaries for easier printing.
        """
        try:
            decoded = cobs_decode(data)
            logger.debug(f"Decoded SensorValueList bytes (raw): {decoded.hex()}")
            if decoded and decoded[-1] == 0:
                decoded = decoded[:-1]
                logger.debug(f"Trimmed trailing zero: {decoded.hex()}")
            if len(decoded) >= 2 and decoded[:2] == b'\x00\x05':
                logger.debug("Detected header in SensorValueList message, stripping first 2 bytes")
                decoded = decoded[2:]
                logger.debug(f"Bytes after header removal: {decoded.hex()}")

            sensor_list = anova_pb2.SensorValueList()
            sensor_list.ParseFromString(decoded)
            
            # Define mapping from UnitType to (scale factor, unit string)
            unit_mapping = {
                anova_pb2.UnitType.DEGREES_POINT_1C:  (10, 'C'),
                anova_pb2.UnitType.DEGREES_POINT_1F:  (10, 'F'),
                anova_pb2.UnitType.DEGREES_POINT_01C: (100, 'C'),
                anova_pb2.UnitType.DEGREES_POINT_01F: (100, 'F'),
                anova_pb2.UnitType.DEGREES_C:         (1, 'C'),
                anova_pb2.UnitType.DEGREES_F:         (1, 'F'),
            }

            status = {}
            # We'll convert each sensor value to a dictionary.
            sensor_dicts = []
            for sensor in sensor_list.values:
                # Try to map the enum values to their names (if available)
                try:
                    unit_name = anova_pb2.UnitType.Name(sensor.units)
                except Exception:
                    unit_name = sensor.units
                try:
                    sensor_type_name = anova_pb2.SensorValue.SensorType.Name(sensor.sensorType)
                except Exception:
                    sensor_type_name = sensor.sensorType

                sensor_dict = {
                    "value": sensor.value,
                    "units": unit_name,
                    "sensorType": sensor_type_name,
                }
                sensor_dicts.append(sensor_dict)

                # If this sensor is the water temperature sensor, record current_temp.
                if sensor.sensorType == anova_pb2.SensorValue.SensorType.WaterTemp:
                    scale, _ = unit_mapping.get(sensor.units, (10, 'F'))
                    status['current_temp'] = sensor.value / scale

            if 'current_temp' not in status:
                status['current_temp'] = None

            status['device_address'] = self.device.address
            status['device_name'] = self.device.name if self.device.name else "Unknown"
            status['is_cooking'] = self._is_cooking
            status['sensor_values'] = sensor_dicts
            
        except Exception as e:
            logger.error(f"Error parsing SensorValueList message: {e}")
            raise Exception("Error parsing message with type 'SensorValueList'") from e

        logger.debug("Full SensorValueList received: %s", status)
        return status

    async def start_cooking(self):
        logger.info("Starting cooking process")
        cmd = self._encode_command(
            anova_pb2.DomainType.ANOVA_DOMAIN_ID_CONFIG,
            anova_pb2.ConfigDomainMessageType.START_COOKING
        )
        await self.send_command(cmd)
        self._is_cooking = True
        
    async def stop_cooking(self):
        logger.info("Stopping cooking process")
        cmd = self._encode_command(
            anova_pb2.DomainType.ANOVA_DOMAIN_ID_CONFIG,
            anova_pb2.ConfigDomainMessageType.STOP_COOKING
        )
        await self.send_command(cmd)
        self._is_cooking = False
        
    async def get_status(self) -> dict:
        """
        Get the current device status by sending a GET_SENSORS command
        and decoding the SensorValueList protobuf message response.
        """
        logger.info("Requesting device status (SensorValueList)")
        cmd = self._encode_command(
            anova_pb2.DomainType.ANOVA_DOMAIN_ID_CONFIG,
            anova_pb2.ConfigDomainMessageType.GET_SENSORS
        )
        response = await self.send_command(cmd, decoder=self.decode_sensor_value_list)
        if response is None:
            logger.error("No response received for GET_SENSORS command")
            raise Exception("No sensor response received")
        return response

    async def set_target_temp(self, temp: float):
        logger.info(f"Setting target temperature to {temp}")
        temp_val = int(temp * NanoConstants.TARGET_TEMP_SCALE)
        int_msg = anova_pb2.IntegerValue(value=temp_val)
        data = int_msg.SerializeToString()
        cmd = self._encode_command(
            anova_pb2.DomainType.ANOVA_DOMAIN_ID_CONFIG,
            anova_pb2.ConfigDomainMessageType.SET_TEMP_SETPOINT,
            data
        )
        await self.send_command(cmd)
        
    async def set_timer(self, minutes: int):
        logger.info(f"Setting timer for {minutes} minutes")
        int_msg = anova_pb2.IntegerValue(value=minutes)
        data = int_msg.SerializeToString()
        cmd = self._encode_command(
            anova_pb2.DomainType.ANOVA_DOMAIN_ID_CONFIG,
            anova_pb2.ConfigDomainMessageType.SET_COOKING_TIMER,
            data
        )
        await self.send_command(cmd)
        
    async def set_temp_unit(self, unit: str):
        logger.info(f"Setting temperature unit to {unit}")
        unit_enum = (anova_pb2.UnitType.DEGREES_POINT_1C if unit.upper() == 'C'
                     else anova_pb2.UnitType.DEGREES_POINT_1F)
        int_msg = anova_pb2.IntegerValue(value=unit_enum)
        data = int_msg.SerializeToString()
        cmd = self._encode_command(
            anova_pb2.DomainType.ANOVA_DOMAIN_ID_CONFIG,
            anova_pb2.ConfigDomainMessageType.SET_TEMP_UNITS,
            data
        )
        await self.send_command(cmd)
        
    async def disconnect(self):
        if self.client.is_connected:
            logger.info(f"Disconnecting from device at {self.device.address}")
            await self.client.disconnect()

def map_unit_value(raw: int) -> str:
    # According to the proto, these values are defined:
    # 0: DEGREES_POINT_1C, 1: DEGREES_POINT_1F,
    # 4: DEGREES_POINT_01C, 5: DEGREES_POINT_01F,
    # 6: DEGREES_C, 7: DEGREES_F.
    if raw in [0, 4, 6]:
        return "C"
    elif raw in [1, 5, 7]:
        return "F"
    else:
        return "Unknown"

# Interactive loop function
async def interactive_loop(device: AnovaNanoDevice):
    def print_commands():
        print("\nAvailable commands:")
        print("  START              - Start cooking (no parameters)")
        print("  STOP               - Stop cooking (no parameters)")
        print("  GET_SENSORS        - Get sensor values (no parameters)")
        print("  READ_TARGET_TEMP   - Read current target temperature (no parameters)")
        print("  READ_UNIT          - Read current temperature unit (no parameters)")
        print("  READ_TIMER         - Read current cooking timer (no parameters)")
        print("  GET_FIRMWARE_INFO  - Get firmware info (no parameters)")
        print("  SET_UNIT           - Set temperature unit (parameter: C or F)")
        print("  SET_TEMP           - Set target temperature (parameter: float)")
        print("  SET_TIMER          - Set cooking timer (parameter: minutes as integer)")
        print("Type 'exit' to quit.\n")

    print_commands()
    
    while True:
        cmd = (await asyncio.to_thread(input, "Enter command: ")).strip().upper()
        if cmd == "EXIT":
            break
        elif cmd == "START":
            await device.start_cooking()
            print("Cooking started.")
        elif cmd == "STOP":
            await device.stop_cooking()
            print("Cooking stopped.")
        elif cmd == "GET_SENSORS":
            status = await device.get_status()
            print("Sensor Status:")
            import json
            print(json.dumps(status, indent=2))
        elif cmd == "READ_TARGET_TEMP":
            value = await device.read_target_temp()
            print(f"Target temperature: {value}")
        elif cmd == "READ_UNIT":
            raw_value = await device.get_temp_unit()
            unit_str = map_unit_value(raw_value)
            print(f"Temperature unit: {unit_str} (raw value: {raw_value})")
        elif cmd == "READ_TIMER":
            value = await device.read_timer()
            print(f"Timer: {value} minutes")
        elif cmd == "GET_FIRMWARE_INFO":
            info = await device.get_firmware_info()
            print("Firmware Info:")
            print(f"  Commit ID: {info['commitId']}")
            print(f"  Tag ID:    {info['tagId']}")
            print(f"  Date:      {info['dateCode']}")
        elif cmd == "SET_UNIT":
            new_unit = (await asyncio.to_thread(input, "Enter unit (C or F): ")).strip().upper()
            await device.set_temp_unit(new_unit)
            print(f"Temperature unit set to {new_unit}.")
        elif cmd == "SET_TEMP":
            temp_str = await asyncio.to_thread(input, "Enter target temperature (float): ")
            try:
                temp = float(temp_str)
            except Exception:
                print("Invalid temperature value.")
                continue
            await device.set_target_temp(temp)
            print(f"Target temperature set to {temp}.")
        elif cmd == "SET_TIMER":
            timer_str = await asyncio.to_thread(input, "Enter cooking timer in minutes (integer): ")
            try:
                minutes = int(timer_str)
            except Exception:
                print("Invalid timer value.")
                continue
            await device.set_timer(minutes)
            print(f"Cooking timer set to {minutes} minutes.")
        else:
            print("Unknown command. Valid commands are:")
            print_commands()


# Main function
async def main():
    try:
        device = await AnovaNanoDevice.discover_and_connect()
        await interactive_loop(device)
        await device.disconnect()
    except DeviceNotFoundError:
        print("No Anova devices found nearby")
    except ConnectionFailedError as e:
        print(f"Connection failed: {e}")
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
    asyncio.run(main())
