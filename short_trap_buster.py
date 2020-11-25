import asyncio
from datetime import datetime, timedelta
from typing import Dict, List, Tuple

import alpaca_trade_api as tradeapi
from liualgotrader.common import config
from liualgotrader.common.tlog import tlog
from liualgotrader.common.trading_data import (buy_indicators,
                                               last_used_strategy,
                                               latest_cost_basis, open_orders,
                                               sell_indicators, stop_prices,
                                               target_prices)
from liualgotrader.fincalcs.trends import SeriesTrendType, get_series_trend
from liualgotrader.fincalcs.vwap import add_daily_vwap, anchored_vwap
from liualgotrader.strategies.base import Strategy, StrategyType
from pandas import DataFrame as df
from pandas import concat
from scipy.stats import linregress, norm
from tabulate import tabulate
from talib import MACD


class ShortTrapBuster(Strategy):
    name = "short_trap_buster"
    was_above_vwap: Dict = {}
    volume_test_time: Dict = {}
    potential_trap: Dict = {}
    trap_start_time: Dict = {}

    def __init__(
        self,
        batch_id: str,
        schedule: List[Dict],
        ref_run_id: int = None,
        check_patterns: bool = False,
    ):
        self.check_patterns = check_patterns
        super().__init__(
            name=self.name,
            type=StrategyType.DAY_TRADE,
            batch_id=batch_id,
            ref_run_id=ref_run_id,
            schedule=schedule,
        )

    async def buy_callback(self, symbol: str, price: float, qty: int) -> None:
        pass

    async def sell_callback(self, symbol: str, price: float, qty: int) -> None:
        latest_cost_basis[symbol] = price

    async def create(self) -> None:
        await super().create()
        tlog(f"strategy {self.name} created")

    async def run(
        self,
        symbol: str,
        shortable: bool,
        position: int,
        minute_history: df,
        now: datetime,
        portfolio_value: float = None,
        trading_api: tradeapi = None,
        debug: bool = False,
        backtesting: bool = False,
    ) -> Tuple[bool, Dict]:
        if not shortable:
            return False, {}

        data = minute_history.iloc[-1]
        if data.close > data.average:
            self.was_above_vwap[symbol] = True

        if (
            await super().is_buy_time(now)
            and not position
            and not open_orders.get(symbol, None)
            and self.was_above_vwap.get(symbol, False)
        ):
            lbound = config.market_open.replace(second=0, microsecond=0)
            close = (
                minute_history["close"][lbound:]
                .dropna()
                .between_time("9:30", "16:00")
                .resample("5min")
                .last()
            ).dropna()
            open = (
                minute_history["open"][lbound:]
                .dropna()
                .between_time("9:30", "16:00")
                .resample("5min")
                .first()
            ).dropna()
            high = (
                minute_history["high"][lbound:]
                .dropna()
                .between_time("9:30", "16:00")
                .resample("5min")
                .max()
            ).dropna()
            low = (
                minute_history["low"][lbound:]
                .dropna()
                .between_time("9:30", "16:00")
                .resample("5min")
                .min()
            ).dropna()
            volume = (
                minute_history["volume"][lbound:]
                .dropna()
                .between_time("9:30", "16:00")
                .resample("5min")
                .sum()
            ).dropna()
            volume = volume[volume != 0]

            df = concat(
                [
                    open.rename("open"),
                    high.rename("high"),
                    low.rename("low"),
                    close.rename("close"),
                    volume.rename("volume"),
                ],
                axis=1,
            )
            if not add_daily_vwap(df):
                tlog(f"[{now}]{symbol} failed in add_daily_vwap")
                return False, {}

            vwap_series = df["average"]

            # calc macd on 5 min
            close_5min = (
                minute_history["close"]
                .dropna()
                .between_time("9:30", "16:00")
                .resample("5min")
                .last()
            ).dropna()

            if debug:
                tlog(
                    f"\n{tabulate(df[-10:], headers='keys', tablefmt='psql')}"
                )
            macds = MACD(close_5min)
            macd = macds[0].round(3)
            macd_signal = macds[1].round(3)
            macd_hist = macds[2].round(3)
            vwap_series = vwap_series.round(3)
            close = close.round(3)
            to_buy = False
            if (
                not self.potential_trap.get(symbol, False)
                and close[-1] < vwap_series[-1]
                and close[-2] < vwap_series[-2]
                and close[-3] < vwap_series[-3]
                and close[-1] < open[-1]
                and close[-2] < open[-2]
                and close[-3] < open[-3]
                and macd[-1] < macd_signal[-1] < 0
                and macd[-1] < 0
                and macd_hist[-1] < macd_hist[-2] < macd_hist[-3] < 0
                and data.close < data.open
                and data.close
                < minute_history["close"][-2]
                < minute_history["close"][-3]
            ):
                self.potential_trap[symbol] = True
                self.trap_start_time[symbol] = now
                tlog(
                    f"[self.name]:{symbol}@{now} potential short-trap {data.close}"
                )
                return False, {}
            elif self.potential_trap.get(symbol, False):
                a_vwap = anchored_vwap(
                    minute_history, self.trap_start_time[symbol]
                )
                if (
                    len(a_vwap) > 10
                    and minute_history.close[-1] > a_vwap[-1]
                    and minute_history.close[-2] > a_vwap[-2]
                ):
                    tlog(
                        f"[self.name]:{symbol}@{now} crossed above anchored-vwap {data.close}"
                    )
                    slope_min, _ = get_series_trend(minute_history.close[-10:])
                    slope_a_vwap, _ = get_series_trend(a_vwap[-10:])

                    if slope_min > slope_a_vwap:
                        to_buy = True
                    else:
                        tlog(
                            f"[self.name]:{symbol}@{now} anchored-vwap slope {slope_a_vwap} below symbol {slope_min}"
                        )

            if to_buy:
                stop_price = vwap_series[-1] * 0.98
                target_price = stop_price * 1.1
                stop_prices[symbol] = round(stop_price, 2)
                target_prices[symbol] = round(target_price, 2)

                if portfolio_value is None:
                    if trading_api:
                        retry = 3
                        while retry > 0:
                            try:
                                portfolio_value = float(
                                    trading_api.get_account().portfolio_value
                                )
                                break
                            except ConnectionError as e:
                                tlog(
                                    f"[{symbol}][{now}[Error] get_account() failed w/ {e}, retrying {retry} more times"
                                )
                                await asyncio.sleep(0)
                                retry -= 1

                        if not portfolio_value:
                            tlog(
                                "f[{symbol}][{now}[Error] failed to get portfolio_value"
                            )
                            return False, {}
                    else:
                        raise Exception(
                            f"{self.name}: both portfolio_value and trading_api can't be None"
                        )

                shares_to_buy = int(portfolio_value * 0.02 / data.close)
                if not shares_to_buy:
                    shares_to_buy = 1

                buy_price = data.close
                tlog(
                    f"[{self.name}][{now}] Submitting buy for {shares_to_buy} shares of {symbol} at {buy_price} target {target_prices[symbol]} stop {stop_prices[symbol]}"
                )
                buy_indicators[symbol] = {
                    "vwap_series": vwap_series[-5:].tolist(),
                    "a_vwap_series": a_vwap[-5:].tolist(),
                    "5-min-close": close[-5:].tolist(),
                    "vwap": data.vwap,
                    "avg": data.average,
                    "volume": minute_history["volume"][-5:].tolist(),
                }
                return (
                    True,
                    {
                        "side": "buy",
                        "qty": str(shares_to_buy),
                        "type": "limit",
                        "limit_price": str(buy_price),
                    },
                )

        if (
            await super().is_sell_time(now)
            and position
            and last_used_strategy[symbol].name == self.name
            and not open_orders.get(symbol)
        ):
            sell_reasons = []
            to_sell = False
            if data.close <= stop_prices[symbol]:
                to_sell = True
                sell_reasons.append("stopped")
            elif data.close >= target_prices[symbol]:
                to_sell = True
                sell_reasons.append("above target")

            if to_sell:
                sell_indicators[symbol] = {
                    "vwap": data.vwap,
                    "avg": data.average,
                    "reasons": sell_reasons,
                }

                tlog(
                    f"[{self.name}][{now}] Submitting sell for {position} shares of {symbol} at market with reason:{sell_reasons}"
                )
                return (
                    True,
                    {
                        "side": "sell",
                        "qty": str(position),
                        "type": "market",
                    },
                )

        return False, {}
