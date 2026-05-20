import itertools
from random import randint

from cocotb.triggers import Timer


class UnstableClock:
    """A 50:50 duty cycle clock driver with added jitter.

    Args:
        signal: The clock pin/signal to be driven.
        period (int): The clock period. Must convert to an even number of
            timesteps.
        jitter_neg (int): Maximum negative jitter applied to each half period.
        jitter_pos (int): Maximum positive jitter applied to each half period.
        units (str, optional): One of
            ``None``, ``'fs'``, ``'ps'``, ``'ns'``, ``'us'``, ``'ms'``,
            ``'sec'``.
            When no *units* is given (``None``) the timestep is determined by
            the simulator.
    """
    def __init__(self, signal, period, jitter_neg, jitter_pos, units=None):
        self.signal = signal
        self.period = period
        self.half_period = period / 2
        self.jitter_neg = jitter_neg
        self.jitter_pos = jitter_pos
        self.units = units

    @property
    def frequency(self):
        scale = {'fs': 1e15, 'ps': 1e12, 'ns': 1e9, 'us': 1e6,
                 'ms': 1e3, 'sec': 1}
        s = scale.get(self.units, 1) if self.units else 1
        return s / self.period / 1e6  # MHz

    async def start(self, cycles=None, start_high=True):
        """Drive the clock with per-half-period jitter.

        Args:
            cycles (int, optional): Number of full cycles, or ``None`` forever.
            start_high (bool, optional): Start high. Default True.
        """
        it = itertools.count() if cycles is None else range(cycles)

        if start_high:
            self.signal.value = 1
            for _ in it:
                await Timer(self.half_period + randint(-self.jitter_neg, self.jitter_pos), self.units)
                self.signal.value = 0
                await Timer(self.half_period + randint(-self.jitter_neg, self.jitter_pos), self.units)
                self.signal.value = 1
        else:
            self.signal.value = 0
            for _ in it:
                await Timer(self.half_period + randint(-self.jitter_neg, self.jitter_pos), self.units)
                self.signal.value = 1
                await Timer(self.half_period + randint(-self.jitter_neg, self.jitter_pos), self.units)
                self.signal.value = 0

    def __str__(self):
        return self.__class__.__name__ + "(%3.1f MHz)" % self.frequency
