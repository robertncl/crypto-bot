"""Risk management: position sizing, protective exits, and a drawdown kill-switch."""

from crypto_bot.risk.manager import RiskDecision, RiskManager

__all__ = ["RiskManager", "RiskDecision"]
