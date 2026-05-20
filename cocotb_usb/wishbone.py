import cocotb
from cocotb_bus.drivers import BusDriver
from cocotb.triggers import RisingEdge, Event


def is_sequence(arg):
    return (not hasattr(arg, "strip") and hasattr(arg, "__getitem__")
            or hasattr(arg, "__iter__"))


class WBAux():
    """Wishbone Auxiliary Wrapper Class"""
    def __init__(self,
                 sel=0xf,
                 adr=0,
                 datwr=None,
                 waitStall=0,
                 waitIdle=0,
                 tsStb=0):
        self.sel = sel
        self.adr = adr
        self.datwr = datwr
        self.waitIdle = waitIdle
        self.waitStall = waitStall
        self.ts = tsStb


class WBOp():
    """Wishbone Operations Wrapper Class"""
    def __init__(self, adr=0, dat=None, idle=0, sel=0xf):
        self.adr = adr
        self.dat = dat
        self.sel = sel
        self.idle = idle


class WBRes():
    """Wishbone Result Wrapper Class"""
    def __init__(self,
                 ack=0,
                 sel=0xf,
                 adr=0,
                 datrd=None,
                 datwr=None,
                 waitIdle=0,
                 waitStall=0,
                 waitAck=0):
        self.ack = ack
        self.sel = sel
        self.adr = adr
        self.datrd = datrd
        self.datwr = datwr
        self.waitStall = waitStall
        self.waitAck = waitAck
        self.waitIdle = waitIdle


class Wishbone(BusDriver):
    """Wishbone"""
    _signals = ["cyc", "stb", "we", "sel", "adr", "datwr", "datrd", "ack"]
    _optional_signals = ["err", "stall", "rty"]

    def __init__(self, entity, name, clock, width=32):
        BusDriver.__init__(self, entity, name, clock)
        self._width = width
        self.bus.cyc.setimmediatevalue(0)
        self.bus.stb.setimmediatevalue(0)
        self.bus.we.setimmediatevalue(0)
        self.bus.adr.setimmediatevalue(0)
        self.bus.datwr.setimmediatevalue(0)
        self.bus.sel.setimmediatevalue((1 << len(self.bus.sel)) - 1)


class WishboneMaster(Wishbone):
    """Wishbone master"""
    def __init__(self, entity, name, clock, timeout=None, width=32):
        sTo = ", no cycle timeout"
        if timeout is not None:
            sTo = ", cycle timeout is %u clock cycles" % timeout
        self.busy_event = Event("%s_busy" % name)
        self._timeout = timeout
        self.busy = False
        self._acked_ops = 0
        self._res_buf = []
        self._aux_buf = []
        self._op_cnt = 0
        self._clk_cycle_count = 0
        Wishbone.__init__(self, entity, name, clock, width)
        self.log.info("Wishbone Master created%s" % sTo)

    async def _clk_cycle_counter(self):
        clkedge = RisingEdge(self.clock)
        self._clk_cycle_count = 0
        while self.busy:
            await clkedge
            self._clk_cycle_count += 1

    async def _open_cycle(self):
        if self.busy:
            self.log.error("Opening Cycle, but WB Driver is already busy.")
            await self.busy_event.wait()
        self.busy_event.clear()
        self.busy = True
        cocotb.start_soon(self._read())
        cocotb.start_soon(self._clk_cycle_counter())
        self.bus.cyc.value = 1
        self._acked_ops = 0
        self._res_buf = []
        self._aux_buf = []
        self.log.debug("Opening cycle, %u Ops" % self._op_cnt)

    async def _close_cycle(self):
        clkedge = RisingEdge(self.clock)
        count = 0
        last_acked_ops = 0
        while self._acked_ops < self._op_cnt:
            if last_acked_ops != self._acked_ops:
                self.log.debug("Waiting for missing acks: %u/%u" %
                               (self._acked_ops, self._op_cnt))
            last_acked_ops = self._acked_ops
            count += 1
            if (not (self._timeout is None)):
                if (count > self._timeout):
                    raise AssertionError(
                        "Timeout of %u clock cycles reached when waiting for"
                        "reply from slave"
                        % self._timeout)
            await clkedge

        self.busy = False
        self.busy_event.set()
        self.bus.cyc.value = 0
        await clkedge

    async def _wait_stall(self):
        """Wait for stall to be low before continuing (Pipelined Wishbone)"""
        clkedge = RisingEdge(self.clock)
        count = 0
        if hasattr(self.bus, "stall"):
            while self.bus.stall.value:
                await clkedge
                count += 1
                if (not (self._timeout is None)):
                    if (count > self._timeout):
                        raise AssertionError(
                            "Timeout of %u clock cycles reached when on stall"
                            "from slave"
                            % self._timeout)
            self.log.debug("Stalled for %u cycles" % count)
        return count

    async def _wait_ack(self):
        """Wait for ACK on the bus before continuing (Non pipelined Wishbone)"""
        clkedge = RisingEdge(self.clock)
        count = 0
        if not hasattr(self.bus, "stall"):
            while not self._get_reply():
                await clkedge
                count += 1
            self.log.debug("Waited %u cycles for acknowledge" % count)
        return count

    def _get_reply(self):
        tmpAck = int(self.bus.ack.value)
        tmpErr = 0
        tmpRty = 0
        if hasattr(self.bus, "err"):
            tmpErr = int(self.bus.err.value)
        if hasattr(self.bus, "rty"):
            tmpRty = int(self.bus.rty.value)
        if ((tmpAck + tmpErr + tmpRty) > 1):
            raise AssertionError(
                "Slave raised more than one reply line at once! ACK: %u ERR:"
                "%u RTY: %u"
                % (tmpAck, tmpErr, tmpRty))
        return (tmpAck + 2 * tmpErr + 3 * tmpRty)

    async def _read(self):
        count = 0
        clkedge = RisingEdge(self.clock)
        while self.busy:
            reply = self._get_reply()
            if (bool(reply)):
                datrd = int(self.bus.datrd.value)
                tmpRes = WBRes(ack=reply,
                               sel=None,
                               adr=None,
                               datrd=datrd,
                               datwr=None,
                               waitIdle=None,
                               waitStall=None,
                               waitAck=self._clk_cycle_count)
                self._res_buf.append(tmpRes)
                self._acked_ops += 1
            await clkedge
            count += 1

    async def _drive(self, we, adr, datwr, sel, idle):
        clkedge = RisingEdge(self.clock)
        if self.busy:
            if idle is not None:
                idlecnt = idle
                while idlecnt > 0:
                    idlecnt -= 1
                    await clkedge
            self.bus.stb.value = 1
            self.bus.adr.value = adr
            self.bus.sel.value = sel
            self.bus.datwr.value = datwr
            self.bus.we.value = we
            await clkedge
            stalled = await self._wait_stall()
            self._aux_buf.append(
                WBAux(sel, adr, datwr, stalled, idle, self._clk_cycle_count))
            await self._wait_ack()
            self.bus.stb.value = 0
            self.bus.we.value = 0
        else:
            self.log.error("Cannot drive the Wishbone bus outside a cycle!")

    async def send_cycle(self, arg):
        """Send a list of WBOp operations in one Wishbone cycle."""
        cnt = 0
        clkedge = RisingEdge(self.clock)
        await clkedge
        if is_sequence(arg):
            if len(arg) < 1:
                self.log.error("List contains no operations to carry out")
                return None
            else:
                self._op_cnt = len(arg)
                firstword = True
                result = []
                for op in arg:
                    if not isinstance(op, WBOp):
                        raise AssertionError(
                            "Sorry, argument must be a list of WBOp (Wishbone"
                            "Operation) objects!"
                        )
                    if firstword:
                        firstword = False
                        await self._open_cycle()

                    if op.dat is not None:
                        we = 1
                        dat = op.dat
                    else:
                        we = 0
                        dat = 0
                    await self._drive(we, op.adr, dat, op.sel, op.idle)
                    self.log.debug(
                        "#%3u WE: %s ADR: 0x%08x DAT: 0x%08x SEL: 0x%1x IDLE:"
                        "%3u"
                        % (cnt, we, op.adr << 2, dat, op.sel, op.idle))
                    cnt += 1
                await self._close_cycle()

                for res, aux in zip(self._res_buf, self._aux_buf):
                    res.datwr = aux.datwr
                    res.sel = aux.sel
                    res.adr = aux.adr
                    res.waitIdle = aux.waitIdle
                    res.waitStall = aux.waitStall
                    res.waitAck -= aux.ts
                    result.append(res)

                return result
        else:
            raise AssertionError(
                "Sorry, argument must be a list of WBOp (Wishbone Operation)"
                " objects!"
            )

    async def read(self, adr):
        result = await self.send_cycle([WBOp(adr >> 2)])
        for rec in result:
            self.log.debug("Result: {}".format(rec))
        return result[-1].datrd

    async def write(self, adr, data):
        result = await self.send_cycle([WBOp(adr >> 2, data)])
        for rec in result:
            self.log.debug("Result: {}".format(rec))
        return 0
