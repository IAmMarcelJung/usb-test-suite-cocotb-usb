import inspect
import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge, Timer, ClockCycles
from cocotb.utils import get_sim_time

from cocotb_usb.descriptors import (Descriptor, getDescriptorRequest,
                                    setAddressRequest, setConfigurationRequest)
from cocotb_usb.usb.pid import PID
from cocotb_usb.usb.endpoint import EndpointType
from cocotb_usb.usb.packet import (wrap_packet, token_packet, data_packet,
                                   sof_packet, handshake_packet)
from cocotb_usb.usb.pp_packet import pp_packet

from cocotb_usb.utils import grouper_tofit, assertEqual
from cocotb_usb.monitor import UsbMonitor


class UsbTest:
    """
    Base class for communicating with a USB test bench.

    Args:
        dut : Object under test as passed by cocotb.
        decouple_clocks (bool, optional): Indicates whether host and device
            share clock signal. If set to False (default), you must provide
            clk48_device clock in test.
    """
    RETRY_INTERVAL = 50  # us
    MAX_REQUEST_TIME = 5e6      # 5 seconds
    MAX_PACKET_TIME = 5e4       # 50 ms
    MAX_DATA_PACKET_TIME = 5e5  # 500 ms

    def __init__(self, dut, **kwargs):
        decouple_clocks = kwargs.get('decouple_clocks', False)
        self.max_packet_size = kwargs.get('max_packet_size', 32)
        self.dut = dut
        self.clock_period = 20830
        cocotb.start_soon(Clock(dut.clk48_host, self.clock_period, 'ps').start())
        if not decouple_clocks:
            cocotb.start_soon(
                Clock(dut.clk48_device, self.clock_period, 'ps').start())

        self.dut.usb_d_p.value = 0
        self.dut.usb_d_n.value = 0
        self.packet_deadline = float('inf')
        self.request_deadline = float('inf')

        self.monitor = UsbMonitor(self.dut,
                                  "usb",
                                  self.dut.clk48_host,
                                  clk_period=self.clock_period)

        test_name = kwargs.get('test_name', inspect.stack()[2][3])
        name_bytes = test_name.encode()[:512].ljust(512, b'\x00')
        self.dut.test_name.value = int.from_bytes(name_bytes, 'little')

    async def reset(self):
        """Reset DUT."""
        self.dut.reset.value = 1
        self.dut.usb_d_p.value = 1
        self.dut.usb_d_n.value = 0
        self.address = 0

        await ClockCycles(self.dut.clk48_host, 50, rising=True)
        self.dut.reset.value = 0
        await ClockCycles(self.dut.clk48_host, 50, rising=True)

    async def wait(self, time, units="us"):
        """Simple wait with heartbeat logging."""
        beat = True

        async def heartbeat():
            while beat:
                await Timer(1, unit="ms")
                ct = get_sim_time("us")
                self.dut._log.info("Waiting, current time {:.0f}".format(ct))

        cocotb.start_soon(heartbeat())
        await Timer(time, unit="us")
        beat = False

    async def port_reset(self, time=10e3, recover=False):
        """Send USB port reset - SE0 condition."""
        self.dut._log.info("[Resetting port for {} us]".format(time))
        self.dut.usb_d_p.value = 0
        self.dut.usb_d_n.value = 0

        await self.wait(time, "us")
        await self.connect()
        if recover:
            await self.wait(1e4, "us")

    async def connect(self):
        """Simulate FS connect to DUT - DP pulled high."""
        self.dut.usb_d_p.value = 1
        self.dut.usb_d_n.value = 0
        await ClockCycles(self.dut.clk48_host, 10)

    async def disconnect(self):
        """Simulate device disconnect, both lines pulled low."""
        self.dut.usb_d_p.value = 0
        self.dut.usb_d_n.value = 0
        await ClockCycles(self.dut.clk48_host, 10)
        self.address = 0

    def print_ep(self, epaddr, msg, *args):
        self.dut._log.info("ep(%i, %s): %s" %
                           (EndpointType.epnum(epaddr),
                            EndpointType.epdir(epaddr).name, msg) % args)

    async def _host_send_packet(self, packet):
        """Send a USB packet."""
        packet = 'JJJJJJJJ' + wrap_packet(packet)
        assertEqual('J', packet[-1], "Packet didn't end in J: " + packet)

        for v in packet:
            if v == '0' or v == '_':
                self.dut.usb_d_p.value = 0
                self.dut.usb_d_n.value = 0
            elif v == '1':
                self.dut.usb_d_p.value = 1
                self.dut.usb_d_n.value = 1
            elif v == '-' or v == 'I':
                self.dut.usb_d_p.value = 1
                self.dut.usb_d_n.value = 0
            elif v == 'J':
                self.dut.usb_d_p.value = 1
                self.dut.usb_d_n.value = 0
            elif v == 'K':
                self.dut.usb_d_p.value = 0
                self.dut.usb_d_n.value = 1
            else:
                raise AssertionError("Unknown value: %s" % v)
            await RisingEdge(self.dut.clk48_host)

    async def host_send_token_packet(self, pid, addr, ep):
        await self._host_send_packet(token_packet(pid, addr, ep))

    async def host_send_data_packet(self, pid, data):
        assert pid in (PID.DATA0, PID.DATA1), pid
        await self._host_send_packet(data_packet(pid, data))

    async def host_send_sof(self, time):
        await self._host_send_packet(sof_packet(time))

    async def host_send_ack(self):
        await self._host_send_packet(handshake_packet(PID.ACK))

    async def host_send(self, data01, addr, epnum, data, expected=PID.ACK):
        """Send data out the virtual USB connection, including an OUT token."""
        self.retry = True
        while self.retry:
            current = get_sim_time("us")
            self.dut._log.info("Sending data at {:.0f}, deadline {:.0f}"
                               .format(current, self.packet_deadline))
            if current > self.packet_deadline:
                raise AssertionError("Did not finish data transfer in time")

            await self.host_send_token_packet(PID.OUT, addr, epnum)
            await self.host_send_data_packet(data01, data)
            await self.host_expect_packet(handshake_packet(expected),
                                          "Expected {} packet."
                                          .format(expected))

    async def host_setup(self, addr, epnum, data):
        """Send data out the virtual USB connection, including a SETUP token."""
        setup_deadline = get_sim_time("us") + 5e3
        self.retry = True
        while self.retry:
            current = get_sim_time("us")
            self.dut._log.info("Sending setup packet at {:.0f}, "
                               "deadline {:.0f}".format(current,
                                                        setup_deadline))
            if current > setup_deadline:
                raise AssertionError("Failed to send setup packet")

            await self.host_send_token_packet(PID.SETUP, addr, epnum)
            await self.host_send_data_packet(PID.DATA0, data)
            await self.host_expect_ack()

    async def host_recv(self, data01, addr, epnum, data):
        """Send data out the virtual USB connection, including an IN token."""
        self.retry = True
        while self.retry:
            await Timer(5, "us")
            current = get_sim_time("us")
            self.dut._log.info("Getting data at {:.0f}, deadline {:.0f}"
                               .format(current, self.packet_deadline))
            if current > self.packet_deadline:
                raise AssertionError("Did not receive data in time")

            await self.host_send_token_packet(PID.IN, addr, epnum)
            await self.host_expect_data_packet(data01, data)
        await self.host_send_ack()

    async def host_expect_packet(self, packet, msg=None):
        self.monitor.prime()
        result = await self.monitor.wait_for_recv(1e9)  # 1 ms max
        if result is None:
            current = get_sim_time("us")
            raise AssertionError(f"No full packet received @{current}")

        await RisingEdge(self.dut.clk48_host)
        self.dut.usb_d_p.value = 1
        self.dut.usb_d_n.value = 0

        expected = pp_packet(wrap_packet(packet))
        actual = pp_packet(result)
        nak = pp_packet(wrap_packet(handshake_packet(PID.NAK)))
        if (actual == nak) and (expected != nak):
            self.dut._log.warning("Got NAK, retry")
            await Timer(self.RETRY_INTERVAL, 'us')
            return
        else:
            self.retry = False
            assertEqual(expected, actual, msg)

    async def host_expect_ack(self):
        """Expect an ACK packet."""
        await self.host_expect_packet(handshake_packet(PID.ACK),
                                      "Expected ACK packet.")

    async def host_expect_nak(self):
        """Expect a NAK packet."""
        await self.host_expect_packet(handshake_packet(PID.NAK),
                                      "Expected NAK packet.")

    async def host_expect_stall(self):
        """Expect a STALL packet."""
        await self.host_expect_packet(handshake_packet(PID.STALL),
                                      "Expected STALL packet.")

    async def host_expect_data_packet(self, pid, data):
        """Expect to receive a data packet."""
        assert pid in (PID.DATA0, PID.DATA1), pid
        await self.host_expect_packet(
            data_packet(pid, data),
            "Expected %s packet with %r" % (pid.name, data))

    async def transaction_setup(self, addr, data, epnum=0):
        xmit = cocotb.start_soon(self.host_setup(addr, epnum, data))
        await xmit

    async def transaction_data_out(self,
                                   addr,
                                   ep,
                                   data,
                                   chunk_size=64,
                                   datax=PID.DATA0,
                                   expected=PID.ACK):

        for _i, chunk in enumerate(grouper_tofit(chunk_size, data)):
            self.dut._log.warning("Sending {} bytes to device".format(
                len(chunk)))
            self.packet_deadline = (get_sim_time("us") +
                                    self.MAX_DATA_PACKET_TIME)
            xmit = cocotb.start_soon(
                self.host_send(datax, addr, ep, chunk, expected))
            await xmit

    async def transaction_data_in(self, addr, ep, data, chunk_size=None):
        epnum = EndpointType.epnum(ep)
        datax = PID.DATA1
        sent_data = 0
        if chunk_size is None:
            chunk_size = self.max_packet_size
        for i, chunk in enumerate(grouper_tofit(chunk_size, data)):
            current = get_sim_time("us")
            if current > self.request_deadline:
                raise AssertionError("Failed to get all data in time")

            self.dut._log.debug("Expecting chunk {}".format(i))
            self.packet_deadline = current + 5e2

            sent_data = 1
            self.dut._log.debug(
                "Actual data we're expecting: {}".format(chunk))

            recv = cocotb.start_soon(self.host_recv(datax, addr, epnum, chunk))
            await recv

            if datax == PID.DATA0:
                datax = PID.DATA1
            else:
                datax = PID.DATA0

        if not sent_data:
            recv = cocotb.start_soon(self.host_recv(datax, addr, epnum, []))
            await recv

    async def transaction_status_in(self, addr, ep):
        epnum = EndpointType.epnum(ep)
        assert EndpointType.epdir(ep) == EndpointType.IN
        xmit = cocotb.start_soon(self.host_recv(PID.DATA1, addr, epnum, []))
        await xmit

    async def transaction_status_out(self, addr, ep):
        epnum = EndpointType.epnum(ep)
        assert EndpointType.epdir(ep) == EndpointType.OUT
        xmit = cocotb.start_soon(self.host_send(PID.DATA1, addr, epnum, []))
        await xmit

    async def control_transfer_out(self, addr, setup_data, descriptor_data=None):
        """Perform an OUT control transfer."""
        epaddr_out = EndpointType.epaddr(0, EndpointType.OUT)
        epaddr_in = EndpointType.epaddr(0, EndpointType.IN)

        if (setup_data[0] & 0x80) == 0x80:
            raise Exception(
                "setup_data indicated an IN transfer, but you requested"
                "an OUT transfer"
            )
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

        self.dut._log.info("setup stage")
        await self.transaction_setup(addr, setup_data)
        self.request_deadline = get_sim_time("us") + self.MAX_REQUEST_TIME

        if descriptor_data is not None:
            self.dut._log.info("data stage")
            await self.transaction_data_out(
                    addr,
                    epaddr_out,
                    descriptor_data,
                    datax=PID.DATA1)
            await RisingEdge(self.dut.clk48_host)

        self.dut._log.info("status stage")
        self.packet_deadline = get_sim_time("us") + self.MAX_PACKET_TIME
        await self.transaction_status_in(addr, epaddr_in)

        if get_sim_time("us") > self.request_deadline:
            raise AssertionError("Failed to process the OUT request in time")

        await RisingEdge(self.dut.clk48_host)

    async def control_transfer_in(self, addr, setup_data, descriptor_data=None):
        """Perform an IN control transfer."""
        epaddr_out = EndpointType.epaddr(0, EndpointType.OUT)
        epaddr_in = EndpointType.epaddr(0, EndpointType.IN)

        if (setup_data[0] & 0x80) == 0x00:
            raise Exception(
                "setup_data indicated an OUT transfer, but you requested"
                "an IN transfer"
            )
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

        self.dut._log.info("setup stage")
        self.packet_deadline = get_sim_time("us") + self.MAX_PACKET_TIME
        await self.transaction_setup(addr, setup_data)
        self.request_deadline = get_sim_time("us") + self.MAX_REQUEST_TIME

        if descriptor_data is not None:
            self.dut._log.info("data stage")
            await self.transaction_data_in(addr, epaddr_in, descriptor_data)

        await RisingEdge(self.dut.clk48_host)

        self.dut._log.info("status stage")
        self.packet_deadline = get_sim_time("us") + self.MAX_PACKET_TIME
        await self.transaction_status_out(addr, epaddr_out)

        if get_sim_time("us") > self.request_deadline:
            raise AssertionError("Failed to process the IN request in time")

        await RisingEdge(self.dut.clk48_host)

    async def set_device_address(self, address, skip_recovery=False):
        """Set USB device address."""
        self.dut._log.info("[Setting device address to {}]".format(address))
        await self.control_transfer_out(
            self.address,
            setAddressRequest(address),
            None,
        )
        if not skip_recovery:
            await self.wait(2e3, "us")
        self.address = address

    async def get_device_descriptor(self, response, length=18):
        """Read the device descriptor from DUT."""
        self.dut._log.info("[Getting device descriptor]")
        request = getDescriptorRequest(descriptor_type=Descriptor.Types.DEVICE,
                                       descriptor_index=0,
                                       lang_id=Descriptor.LangId.UNSPECIFIED,
                                       length=length)
        await self.control_transfer_in(self.address, request, response)

    async def get_configuration_descriptor(self, length, response):
        """Read a configuration descriptor from DUT."""
        self.dut._log.info("[Getting config descriptor]")
        request = getDescriptorRequest(
            descriptor_type=Descriptor.Types.CONFIGURATION,
            descriptor_index=0,
            lang_id=Descriptor.LangId.UNSPECIFIED,
            length=length)

        await self.control_transfer_in(self.address, request, response)

    async def get_string_descriptor(self, lang_id, idx, response, length=255):
        """Read a string descriptor from DUT."""
        self.dut._log.info("[Getting string descriptor {} of langId {:#x}]"
                           .format(idx, lang_id))
        request = getDescriptorRequest(descriptor_type=Descriptor.Types.STRING,
                                       descriptor_index=idx,
                                       lang_id=lang_id,
                                       length=length)

        await self.control_transfer_in(self.address, request, response)

    async def get_device_qualifier(self, length, response):
        """Read a device qualifier descriptor from DUT."""
        self.dut._log.info("[Getting device qualifier descriptor]")
        request = getDescriptorRequest(
            descriptor_type=Descriptor.Types.DEVICE_QUALIFIER,
            descriptor_index=0,
            lang_id=Descriptor.LangId.UNSPECIFIED,
            length=length)

        await self.control_transfer_in(self.address, request, response)

    async def set_configuration(self, idx):
        """Send a SET_CONFIGURATION standard device request to DUT."""
        request = setConfigurationRequest(idx)

        self.dut._log.info("[Setting device configuration {}]".format(idx))
        await self.control_transfer_out(
            self.address,
            request,
            None,
        )
