from cocotb_usb.host import UsbTest

# TSIGATT (USB 2.0 Table 7-14): device must assert DP pullup within 100ms of
# power-on.  The usb_cdc phy_rx state machine uses a hardware counter that
# fires at 16ms (within spec).  We wait 20ms after releasing hardware reset
# to ensure that timer has expired before sending any USB traffic.
TSIGATT_WAIT_US = 20e3


class UsbTestCDC(UsbTest):
    """Harness for the usb_cdc IP core.

    Identical to UsbTest except reset() waits for the core's 16ms TSIGATT
    power-on timer before returning, so subsequent test code can start
    enumeration immediately.
    """

    async def reset(self):
        await super().reset()
        await self.wait(TSIGATT_WAIT_US, "us")
