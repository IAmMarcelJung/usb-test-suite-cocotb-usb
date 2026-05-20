import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge
from cocotb.utils import get_sim_time

from cocotb_usb.usb.pid import PID
from cocotb_usb.usb.endpoint import EndpointType, EndpointResponse
from cocotb_usb.usb.packet import crc16

from cocotb_usb.utils import grouper_tofit, parse_csr, assertEqual

from cocotb_usb.host import UsbTest
import inspect


class UsbTestValenty(UsbTest):
    """Class for testing ValentyUSB IP core.

    Args:
        dut : Object under test as passed by cocotb.
        csr_file (str): Path to a CSV file containing CSR register addresses.
        decouple_clocks (bool, optional): Indicates whether host and device
            share clock signal.
    """
    def __init__(self, dut, csr_file, cdc=False, **kwargs):
        from cocotb_usb.wishbone import WishboneMaster

        if cdc:
            dut._log.info("CDC is enabled")
            self.clk_sys = dut.clksys
            self.clk_factor = 9
            self.clock_100_period = 10000
            cocotb.start_soon(Clock(dut.clksys, self.clock_100_period, 'ps').start())
        else:
            self.clk_sys = dut.clk12
            self.clk_factor = 1
            dut._log.info("CDC is DISABLED")

        self.wb = WishboneMaster(dut, "wishbone", self.clk_sys, timeout=20)
        self.csrs = dict()
        self.csrs = parse_csr(csr_file)
        kwargs['test_name'] = inspect.stack()[2][3]
        super().__init__(dut, **kwargs)

    async def reset(self):
        await super().reset()

        await self.write(self.csrs['usb_setup_ev_enable'], 0xff)
        await self.write(self.csrs['usb_in_ev_enable'], 0xff)
        await self.write(self.csrs['usb_out_ev_enable'], 0xff)

        await self.write(self.csrs['usb_setup_ev_pending'], 0xff)
        await self.write(self.csrs['usb_in_ev_pending'], 0xff)
        await self.write(self.csrs['usb_out_ev_pending'], 0xff)
        await self.write(self.csrs['usb_address'], 0)

    async def write(self, addr, val):
        await self.wb.write(addr, val)

    async def read(self, addr):
        value = await self.wb.read(addr)
        return value

    async def connect(self):
        USB_PULLUP_OUT = self.csrs['usb_pullup_out']
        await self.write(USB_PULLUP_OUT, 1)

    async def clear_pending(self, epaddr):
        if EndpointType.epdir(epaddr) == EndpointType.IN:
            self.dut._log.info("Clearing IN_EV_PENDING")
            await self.write(self.csrs['usb_in_ctrl'], 0x20)
            await self.write(self.csrs['usb_in_ev_pending'], 0xff)
        else:
            self.dut._log.info("Clearing OUT_EV_PENDING")
            await self.write(self.csrs['usb_out_ev_pending'], 0xff)
            await self.write(self.csrs['usb_out_ctrl'], 0x20)

    async def disconnect(self):
        await super().disconnect()
        USB_PULLUP_OUT = self.csrs['usb_pullup_out']
        self.address = 0
        await self.write(USB_PULLUP_OUT, 0)

    async def pending(self, ep):
        if EndpointType.epdir(ep) == EndpointType.IN:
            val = await self.read(self.csrs['usb_in_status'])
            return val & (1 << 4)
        else:
            val = await self.read(self.csrs['usb_out_status'])
            return ((val & (1 << 5) | (1 << 4))
                    and (EndpointType.epnum(ep) == (val & 0x0f)))

    async def expect_setup(self, epaddr, expected_data):
        actual_data = []
        for i in range(300 * self.clk_factor):
            self.dut._log.debug("Interrupt loop {}".format(i))
            status = await self.read(self.csrs['usb_setup_ev_pending'])
            have = status & 0x1
            if have:
                break
            await RisingEdge(self.clk_sys)

        for i in range(128 * self.clk_factor):
            self.dut._log.debug("Prime loop {}".format(i))
            status = await self.read(self.csrs['usb_setup_status'])
            have = status & 0x10
            if have:
                break
            await RisingEdge(self.clk_sys)

        for i in range(48 * self.clk_factor):
            self.dut._log.debug("Read loop {}".format(i))
            status = await self.read(self.csrs['usb_setup_status'])
            have = status & 0x10
            if not have:
                break
            v = await self.read(self.csrs['usb_setup_data'])
            actual_data.append(v)
            await RisingEdge(self.clk_sys)

        if len(actual_data) < 2:
            raise AssertionError("data was short (got {}, expected {})".format(
                expected_data, actual_data))
        actual_data, actual_crc16 = actual_data[:-2], actual_data[-2:]

        self.print_ep(epaddr, "Got: %r (expected: %r)", actual_data,
                      expected_data)
        assertEqual(expected_data, actual_data,
                    "SETUP packet not received")
        assertEqual(crc16(expected_data), actual_crc16,
                    "CRC16 not valid")
        await self.write(self.csrs['usb_setup_ctrl'], 2)

    async def drain_setup(self):
        actual_data = []
        for i in range(48):
            status = await self.read(self.csrs['usb_setup_status'])
            have = status & 0x10
            if not have:
                break
            v = await self.read(self.csrs['usb_setup_data'])
            actual_data.append(v)
            await RisingEdge(self.dut.clk12)
        await self.write(self.csrs['usb_setup_ctrl'], 2)
        await self.write(self.csrs['usb_setup_ev_pending'], 0xff)
        return actual_data

    async def drain_out(self):
        actual_data = []
        for i in range(70):
            status = await self.read(self.csrs['usb_out_status'])
            have = status & (1 << 4)
            if not have:
                break
            v = await self.read(self.csrs['usb_out_data'])
            actual_data.append(v)
            await RisingEdge(self.dut.clk12)
        await self.write(self.csrs['usb_out_ev_pending'], 0xff)
        await self.write(self.csrs['usb_out_ctrl'], 0x10)
        return actual_data[:-2]  # Strip off CRC16

    async def expect_data(self, epaddr, expected_data, expected):
        actual_data = []
        for i in range(1500 * self.clk_factor):
            self.dut._log.debug("Interrupt loop {}".format(i))
            status = await self.read(self.csrs['usb_out_ev_pending'])
            have = status & 0x1
            if have:
                break
            await RisingEdge(self.clk_sys)

        for i in range(128 * self.clk_factor):
            self.dut._log.debug("Prime loop {}".format(i))
            status = await self.read(self.csrs['usb_out_status'])
            have = status & (1 << 4)
            if have:
                break
            await RisingEdge(self.clk_sys)

        for i in range(256 * self.clk_factor):
            self.dut._log.debug("Read loop {}".format(i))
            status = await self.read(self.csrs['usb_out_status'])
            have = status & (1 << 4)
            if not have:
                break
            v = await self.read(self.csrs['usb_out_data'])
            actual_data.append(v)
            await RisingEdge(self.clk_sys)

        if expected == PID.ACK:
            if len(actual_data) < 2:
                raise AssertionError("data {} was short".format(actual_data))
            actual_data, actual_crc16 = actual_data[:-2], actual_data[-2:]

            self.print_ep(epaddr, "Got: %r (expected: %r)", actual_data,
                          expected_data)
            assertEqual(expected_data, actual_data,
                        "DATA packet not correctly received")
            assertEqual(crc16(expected_data), actual_crc16,
                        "CRC16 not valid")
            pending = await self.read(self.csrs['usb_out_ev_pending'])
            if pending != 1:
                raise AssertionError('event not generated')
            await self.write(self.csrs['usb_out_ev_pending'], pending)

    async def set_response(self, ep, response):
        if (EndpointType.epdir(ep) == EndpointType.IN
                and response == EndpointResponse.ACK):
            await self.write(self.csrs['usb_in_ctrl'], EndpointType.epnum(ep))
        elif (EndpointType.epdir(ep) == EndpointType.OUT
                and response == EndpointResponse.ACK):
            await self.write(self.csrs['usb_out_ctrl'],
                             0x10 | EndpointType.epnum(ep))

    async def send_data(self, token, ep, data):
        for b in data:
            await self.write(self.csrs['usb_in_data'], b)
        await self.write(self.csrs['usb_in_ctrl'],
                         EndpointType.epnum(ep) & 0x0f)

    async def transaction_setup(self, addr, data, epnum=0):
        epaddr_out = EndpointType.epaddr(0, EndpointType.OUT)

        xmit = cocotb.start_soon(self.host_setup(addr, epnum, data))
        await self.expect_setup(epaddr_out, data)
        await xmit

    async def transaction_data_out(self,
                                   addr,
                                   ep,
                                   data,
                                   chunk_size=64,
                                   expected=PID.ACK,
                                   datax=PID.DATA1):
        epnum = EndpointType.epnum(ep)

        for _i, chunk in enumerate(grouper_tofit(chunk_size, data)):
            self.dut._log.warning("Sending {} bytes to host"
                                  .format(len(chunk)))
            self.packet_deadline = get_sim_time("us") + super().MAX_PACKET_TIME
            await self.set_response(ep, EndpointResponse.ACK)
            xmit = cocotb.start_soon(
                self.host_send(datax, addr, epnum, chunk, expected))
            await self.expect_data(epnum, list(chunk), expected)
            await xmit

            if datax == PID.DATA0:
                datax = PID.DATA1
            else:
                datax = PID.DATA0

    async def transaction_data_in(self,
                                  addr,
                                  ep,
                                  data,
                                  chunk_size=64,
                                  datax=PID.DATA1):
        epnum = EndpointType.epnum(ep)
        sent_data = 0
        for i, chunk in enumerate(grouper_tofit(chunk_size, data)):
            current = get_sim_time("us")
            if current > self.request_deadline:
                raise AssertionError("Failed to get all data in time")

            self.dut._log.debug("Expecting chunk {}".format(i))
            self.packet_deadline = current + 5e2

            sent_data = 1
            self.dut._log.debug(
                "Actual data we're expecting: {}".format(chunk))
            for b in chunk:
                await self.write(self.csrs['usb_in_data'], b)
            await self.write(self.csrs['usb_in_ctrl'], epnum)
            recv = cocotb.start_soon(self.host_recv(datax, addr, epnum, chunk))
            await recv

            if datax == PID.DATA0:
                datax = PID.DATA1
            else:
                datax = PID.DATA0
        if not sent_data:
            await self.write(self.csrs['usb_in_ctrl'], epnum)
            recv = cocotb.start_soon(self.host_recv(datax, addr, epnum, []))
            await self.send_data(datax, epnum, data)
            await recv

    async def set_data(self, ep, data):
        for b in data:
            await self.write(self.csrs['usb_in_data'], b)

    async def control_transfer_out(self, addr, setup_data, descriptor_data=None):
        epaddr_out = EndpointType.epaddr(0, EndpointType.OUT)
        epaddr_in = EndpointType.epaddr(0, EndpointType.IN)

        if (setup_data[0] & 0x80) == 0x80:
            raise Exception(
                "setup_data indicated an IN transfer, but you requested"
                "an OUT transfer"
            )

        setup_ev = await self.read(self.csrs['usb_setup_ev_pending'])

        self.dut._log.info("setup stage")
        await self.transaction_setup(addr, setup_data)
        self.request_deadline = get_sim_time("us") + super().MAX_REQUEST_TIME

        setup_ev = await self.read(self.csrs['usb_setup_ev_pending'])
        await self.write(self.csrs['usb_setup_ev_pending'], setup_ev)

        if (setup_data[7] != 0
                or setup_data[6] != 0) and descriptor_data is None:
            raise Exception(
                "setup_data indicates data, but no descriptor data"
                "was specified"
            )
        if (setup_data[7] == 0
                and setup_data[6] == 0) and descriptor_data is not None:
            raise Exception(
                "setup_data indicates no data, but descriptor data"
                "was specified"
            )
        if descriptor_data is not None:
            self.dut._log.info("data stage")
            await self.transaction_data_out(addr, epaddr_out, descriptor_data)

        self.dut._log.info("status stage")
        self.packet_deadline = get_sim_time("us") + super().MAX_PACKET_TIME
        await self.write(self.csrs['usb_in_ctrl'], 0)
        await self.transaction_status_in(addr, epaddr_in)
        await RisingEdge(self.dut.clk12)
        await RisingEdge(self.dut.clk12)
        in_ev = await self.read(self.csrs['usb_in_ev_pending'])
        await self.write(self.csrs['usb_in_ev_pending'], in_ev)
        await self.write(self.csrs['usb_in_ctrl'], 1 << 5)
        await RisingEdge(self.dut.clk12)
        await RisingEdge(self.dut.clk12)

        if get_sim_time("us") > self.request_deadline:
            raise AssertionError("Failed to process the OUT request in time")

    async def control_transfer_in(self, addr, setup_data, descriptor_data=None):
        epaddr_out = EndpointType.epaddr(0, EndpointType.OUT)
        epaddr_in = EndpointType.epaddr(0, EndpointType.IN)

        if (setup_data[0] & 0x80) == 0x00:
            raise Exception(
                "setup_data indicated an OUT transfer, but you requested"
                "an IN transfer"
            )

        setup_ev = await self.read(self.csrs['usb_setup_ev_pending'])

        self.dut._log.info("setup stage")
        await self.transaction_setup(addr, setup_data)
        self.request_deadline = get_sim_time("us") + super().MAX_REQUEST_TIME

        setup_ev = await self.read(self.csrs['usb_setup_ev_pending'])
        await self.write(self.csrs['usb_setup_ev_pending'], setup_ev)

        in_ev = await self.read(self.csrs['usb_in_ev_pending'])
        if (setup_data[7] != 0
                or setup_data[6] != 0) and descriptor_data is None:
            raise Exception(
                "setup_data indicates data, but no descriptor data"
                "was specified"
            )
        if (setup_data[7] == 0
                and setup_data[6] == 0) and descriptor_data is not None:
            raise Exception(
                "setup_data indicates no data, but descriptor data"
                "was specified"
            )
        if descriptor_data is not None:
            self.dut._log.info("data stage")
            await self.transaction_data_in(addr, epaddr_in, descriptor_data)

            await RisingEdge(self.dut.clk12)
            await RisingEdge(self.dut.clk12)
            in_ev = await self.read(self.csrs['usb_in_ev_pending'])
            await self.write(self.csrs['usb_in_ev_pending'], in_ev)

        self.packet_deadline = get_sim_time("us") + super().MAX_PACKET_TIME
        await self.write(self.csrs['usb_out_ctrl'], 0x10)
        self.dut._log.info("status stage")
        out_ev = await self.read(self.csrs['usb_out_ev_pending'])
        await self.transaction_status_out(addr, epaddr_out)
        await RisingEdge(self.dut.clk12)

        await RisingEdge(self.clk_sys)
        await RisingEdge(self.clk_sys)
        await RisingEdge(self.clk_sys)

        out_ev = await self.read(self.csrs['usb_out_ev_pending'])
        await self.write(self.csrs['usb_out_ctrl'], 0x20)
        await self.write(self.csrs['usb_out_ev_pending'], out_ev)

        if get_sim_time("us") > self.request_deadline:
            raise AssertionError("Failed to process the IN request in time")

    async def set_device_address(self, address):
        await super().set_device_address(address)
        await self.write(self.csrs['usb_address'], address)
