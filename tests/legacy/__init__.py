"""Frozen copies of the pre-streaming (windowed) strategy implementations.

Reference only: ``tests/test_strategy_stream.py`` asserts the streaming rewrites emit the
same signal on every bar as these did when given the full candle history. Do not edit.
"""

from legacy.bollinger import BollingerReversion
from legacy.breakout import Breakout
from legacy.dca import DCA
from legacy.funding_bias import FundingBias
from legacy.ma_crossover import MACrossover
from legacy.macd import MACDMomentum
from legacy.regime import RegimeSwitch
from legacy.rsi_reversion import RSIReversion
from legacy.supertrend import Supertrend
from legacy.trend_ls import TrendLongShort

LEGACY = {
    cls.name: cls
    for cls in (
        MACrossover, RSIReversion, Breakout, BollingerReversion, MACDMomentum,
        Supertrend, DCA, RegimeSwitch, TrendLongShort, FundingBias,
    )
}
