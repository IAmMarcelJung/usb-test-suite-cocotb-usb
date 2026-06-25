# cocotb_usb
Python package for USB test suite based on cocotb.

> [!NOTE]
>[Claude](https://www.anthropic.com/claude) was used to convert the setup to a Nix-based one and to add tests for USB CDC.

The package also provides experimental support for the [USB_CDC](https://github.com/ulixxe/usb_cdc) IP core via the `UsbTestCDC` harness in `cocotb_usb/host_cdc.py`.

## Installation

When using the [parent repository](https://github.com/antmicro/usb-test-suite-build), this package and its dependencies are installed automatically by the Nix development environment (`nix develop`) — no manual installation is needed.

The steps below are only required to use this package standalone.

### Dependencies
* python3
* [cocotb](https://github.com/cocotb/cocotb) >= 2.0
### Setup
```
pip install cocotb
git clone https://github.com/antmicro/usb-test-suite-cocotb-usb
pip install ./usb-test-suite-cocotb-usb/
```
## Usage
See [usb-test-suite-testbenches](https://github.com/antmicro/usb-test-suite-testbenches) or its [parent repository](https://github.com/antmicro/usb-test-suite-build) for examples.
