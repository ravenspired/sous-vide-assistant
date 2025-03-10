# Anova Precision Nano BTLE Interface

A complete Bluetooth Low Energy (BTLE) interface for the Anova Nano Sous Vide device. This module provides automated device discovery, connection management, and an interactive command interface to control and monitor your Anova Nano.

## Features

- **Auto-Scanning & Connection:** Automatically discovers nearby Anova devices and establishes a connection
- **Real-Time Monitoring:** Retrieve sensor readings (water temperature) and device status
- **Full Device Control:** Start/stop cooking, set target temperature, cooking timer, and temperature unit
- **Firmware Management:** Query and display the device's firmware information
- **Robust Communication:** Uses Protocol Buffers for data serialization and COBS for message framing

## Requirements

- Python 3.9 or higher
- [Bleak](https://github.com/hbldh/bleak) library for Bluetooth LE communication

## Installation

1. **Install Python Dependencies:**
   ```bash
   pip install bleak
   ```

2. **Clone/Download the Repository:**
   ```bash
   git clone https://github.com/yourusername/anova-nano.git
   cd anova-nano
   ```

## Usage

### Interactive Mode

Run the module directly to start the interactive command loop:

```bash
python anova_nano.py
```

Available commands:
- `START` - Begin the cooking process
- `STOP` - End the cooking process
- `GET_SENSORS` - Retrieve current sensor values (water temperature)
- `READ_TARGET_TEMP` - Get the current target temperature
- `READ_UNIT` - Get the temperature unit (C/F)
- `READ_TIMER` - Get the remaining cooking time
- `GET_FIRMWARE_INFO` - Retrieve firmware details
- `SET_UNIT [C|F]` - Set the temperature unit
- `SET_TEMP [value]` - Set a new target temperature
- `SET_TIMER [minutes]` - Set the cooking timer

Type `exit` to quit the interactive session.

### Programmatic Usage

```python
from anova_nano import AnovaNanoDevice

# Discover and connect to a device
device = AnovaNanoDevice.discover_and_connect()

# Get current temperature
temp_unit = device.get_temp_unit()
target_temp = device.read_target_temp()
print(f"Target temperature: {target_temp}°{temp_unit}")

# Set new temperature (in the current unit)
device.set_target_temp(65.5)

# Start cooking
device.start_cooking()

# Get water temperature and device status
status = device.get_status()
print(f"Water temperature: {status['water_temp']}°{temp_unit}")

# Disconnect when done
device.disconnect()
```

## Architecture

The code is organized into several key components:

- **NanoConstants:** Defines BTLE-related constants (UUIDs, scaling factors, timeouts)
- **SensorValue Dataclass:** Wraps sensor readings with conversion to/from Protocol Buffer messages
- **COBS Functions:** Handles message framing for robust communication
- **AnovaNanoDevice Class:** Encapsulates device communication and control methods
- **Interactive Loop:** Provides a user-friendly command-line interface

## Communication Protocol

- **Message Framing:** All messages use COBS encoding with a trailing zero
- **Command Structure:** Two-byte header (domain and message type) plus optional payload
- **Response Handling:** Aggregates multiple chunks until the message is complete
- **Data Serialization:** Uses Protocol Buffers for encoding/decoding structured messages

## Debugging

Enable detailed logging for troubleshooting:

```python
import logging
logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(levelname)s - %(message)s')
```

## Contributing

Contributions, issue reports, and suggestions are welcome! To contribute:

1. Fork the repository
2. Create your feature branch (`git checkout -b feature/amazing-feature`)
3. Commit your changes (`git commit -m 'Add some amazing feature'`)
4. Push to the branch (`git push origin feature/amazing-feature`)
5. Open a Pull Request

## License

This project is licensed under the MIT License:

```
MIT License

Copyright (c) 2025 [Your Name or Organization]

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

For more information on this license, please see [https://opensource.org/licenses/MIT](https://opensource.org/licenses/MIT).
