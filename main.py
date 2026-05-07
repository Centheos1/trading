import logging
from datetime import datetime, timezone
import pandas as pd

import backtester
import optimiser
from data_service import DataCollector, TickDataCollector
from database import Hdf5Client
from exchanges.binance import BinanceClient
from exchanges.oanda import OandaClient
from utils import TF_EQUIV
from models import OandaSymbolDetails

logger = logging.getLogger()
logger.setLevel(logging.DEBUG)

stream_formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(filename)s %(lineno)d :: %(message)s")

stream_handler = logging.StreamHandler()
stream_handler.setFormatter(stream_formatter)
stream_handler.setLevel(logging.INFO)

file_formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(filename)s %(lineno)d :: %(message)s")

file_handler = logging.FileHandler("./logs/info.log")
file_handler.setFormatter(file_formatter)
file_handler.setLevel(logging.DEBUG)

logger.addHandler(stream_handler)
logger.addHandler(file_handler)


if __name__ == "__main__":

    client = None
    exchange = None

    while True:
        mode = input("Choose the program mode (data / backtest / optimise / tide / wave / ui / execute): ").lower()
        if mode in ["data", "backtest", "optimise", "tide", "wave", "ui", "execute"]:
            break

    if mode == "ui":
        from ui.app import main as ui_main
        ui_main()
        exit(0)

    if mode == "tide":
        from tide.tide_cli import run_interactive
        exit(run_interactive())

    if mode == "wave":
        from wave.wave_cli import run_interactive as wave_interactive
        exit(wave_interactive())  # noqa: direct import avoids __init__ runpy conflict

    if mode == "execute":
        import sys, os, asyncio, time, signal as signal_mod, json
        sys.path.insert(0, os.path.join(os.path.dirname(__file__),
                                         'backtestingCpp', 'orderflow', 'build'))
        try:
            import orderflow_engine as ofe
        except ImportError:
            print("orderflow_engine not built. Run: cd backtestingCpp/orderflow && ./build.sh")
            exit(1)

        try:
            import websockets
        except ImportError:
            print("pip install websockets")
            exit(1)

        from execution.models import SizingConfig, SizingMode
        from execution.binance_broker import BinanceBroker
        from execution.execution_manager import ExecutionManager

        symbol = input("Symbol [BTCUSDT]: ").strip().upper() or "BTCUSDT"

        sizing_modes = {"1": SizingMode.FIXED_QTY, "2": SizingMode.FIXED_NOTIONAL, "3": SizingMode.PCT_BALANCE}
        sizing_choice = input("Sizing mode (1=Fixed Qty, 2=Fixed $, 3=% Balance) [1]: ").strip() or "1"
        sizing_mode = sizing_modes.get(sizing_choice, SizingMode.FIXED_QTY)

        default_val = "0.001" if sizing_mode == SizingMode.FIXED_QTY else "100"
        sizing_value = float(input(f"Sizing value [{default_val}]: ").strip() or default_val)

        sizing = SizingConfig(mode=sizing_mode, value=sizing_value, max_position=sizing_value * 10)

        def on_order(order):
            logger.info("ORDER %s %s %s qty=%.6f price=%.2f [%s] %s",
                        order.id, order.side.value, order.symbol,
                        order.fill_quantity, order.fill_price,
                        order.status.value, order.signal_type)

        broker = BinanceBroker()
        exec_mgr = ExecutionManager(
            broker=broker, symbol=symbol, sizing=sizing,
            cooldown_s=5.0, order_callback=on_order,
        )

        if not exec_mgr.start():
            print("Failed to connect to Binance. Check .env API keys.")
            exit(1)

        acct = exec_mgr.account
        testnet_label = "(TESTNET)" if os.getenv("BINANCE_TESTNET", "true").lower() == "true" else "(LIVE)"
        print(f"\n{'='*60}")
        print(f"  Binance Futures {testnet_label}")
        print(f"  Symbol:    {symbol}")
        print(f"  Balance:   {acct.balance:.2f} USDT")
        print(f"  Sizing:    {sizing_mode.value} = {sizing_value}")
        print(f"{'='*60}")

        confirm = input("\nArm execution? (yes/no) [no]: ").strip().lower()
        if confirm != "yes":
            print("Aborted.")
            exec_mgr.stop()
            exit(0)

        config = ofe.EngineConfig()
        config.tick_size = 0.01
        engine = ofe.OrderFlowEngine(config)
        engine.set_signal_callback(lambda sig: exec_mgr.on_signal(sig))

        exec_mgr.arm()
        print("Execution ARMED. Press Ctrl+C to stop.\n")

        import requests as req
        try:
            resp = req.get("https://fapi.binance.com/fapi/v1/depth",
                           params={"symbol": symbol, "limit": 1000}, timeout=10)
            resp.raise_for_status()
            j = resp.json()
            snap = ofe.DepthUpdate()
            snap.timestamp = int(time.time() * 1000)
            snap.first_update_id = j.get("lastUpdateId", 0)
            snap.final_update_id = snap.first_update_id
            snap.is_snapshot = True
            bids, asks = [], []
            for b in j.get("bids", []):
                lv = ofe.DepthLevel()
                lv.price, lv.quantity = float(b[0]), float(b[1])
                bids.append(lv)
            for a in j.get("asks", []):
                lv = ofe.DepthLevel()
                lv.price, lv.quantity = float(a[0]), float(a[1])
                asks.append(lv)
            snap.bids, snap.asks = bids, asks
            engine.process_depth(snap)
            logger.info("Depth snapshot: %d bids, %d asks", len(bids), len(asks))
        except Exception as e:
            logger.error("Depth snapshot failed: %s", e)

        async def run_feed():
            base = "wss://fstream.binance.com/ws/"
            sym_lower = symbol.lower()
            trade_ws = await websockets.connect(f"{base}{sym_lower}@trade")
            depth_ws = await websockets.connect(f"{base}{sym_lower}@depth@100ms")

            async def read_trades():
                try:
                    async for msg in trade_ws:
                        j = json.loads(msg)
                        t = ofe.Trade()
                        t.timestamp = j["T"]
                        t.price = float(j["p"])
                        t.quantity = float(j["q"])
                        t.is_buyer_maker = j["m"]
                        engine.process_trade(t)
                except (websockets.ConnectionClosed, asyncio.CancelledError):
                    pass

            async def read_depth():
                try:
                    async for msg in depth_ws:
                        j = json.loads(msg)
                        u = ofe.DepthUpdate()
                        u.timestamp = j.get("E", 0)
                        u.first_update_id = j.get("U", 0)
                        u.final_update_id = j.get("u", 0)
                        u.is_snapshot = False
                        b_list, a_list = [], []
                        for b in j.get("b", []):
                            lv = ofe.DepthLevel()
                            lv.price, lv.quantity = float(b[0]), float(b[1])
                            b_list.append(lv)
                        for a in j.get("a", []):
                            lv = ofe.DepthLevel()
                            lv.price, lv.quantity = float(a[0]), float(a[1])
                            a_list.append(lv)
                        u.bids, u.asks = b_list, a_list
                        engine.process_depth(u)
                except (websockets.ConnectionClosed, asyncio.CancelledError):
                    pass

            async def status_printer():
                while True:
                    await asyncio.sleep(30)
                    side = exec_mgr.current_side
                    pos_str = f"{side.value} {exec_mgr.current_qty:.6f}" if side else "Flat"
                    logger.info("Position: %s | Orders: %d", pos_str, len(exec_mgr.orders))
                    exec_mgr.refresh_account()

            tasks = [
                asyncio.create_task(read_trades()),
                asyncio.create_task(read_depth()),
                asyncio.create_task(status_printer()),
            ]
            stop = asyncio.Event()
            try:
                await stop.wait()
            except asyncio.CancelledError:
                pass
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await trade_ws.close()
            await depth_ws.close()

        try:
            asyncio.run(run_feed())
        except KeyboardInterrupt:
            print("\nShutting down...")
        finally:
            exec_mgr.disarm(close_position=True)
            time.sleep(2)
            exec_mgr.stop()
            print("Execution stopped.")
        exit(0)

    # Exchange
    while True:
        exchange = input("Choose an exchange: ").lower()

        if exchange == "":
            exchange = "oanda"

        if exchange in ["binance", "oanda"]:
            break

    if exchange == 'binance':
        client = BinanceClient(futures=True)
    elif exchange == 'oanda':
        client = OandaClient()

    # Symbol
    while True:
        symbol = input("Choose a symbol: ").upper()

        if exchange == "binance" and symbol == "":
            symbol = "BTCUSDT"

        if exchange == "oanda" and symbol == "":
            symbol = "BTC_USD"

        if exchange == 'oanda' and symbol == 'ALL':
            break

        if symbol in client.symbols:
            break

    # Data
    if mode == "data":

        data_collector = DataCollector(exchange=exchange)
        pull_type = "TICKER"

        if exchange == 'binance':
            while True:
                pull_type = input("Choose a pull type (ticker / account / ticks): ").upper()
                if pull_type in ["TICKER", "ACCOUNT", "TICKS"]:
                    break
        elif exchange == 'oanda':
            while True:
                pull_type = input("Choose a pull type (ticker or account): ").upper()
                if pull_type in ["TICKER", "ACCOUNT"]:
                    break

        if pull_type == "TICKS":
            while True:
                try:
                    duration = int(input("Collection duration in seconds (0 = until Ctrl+C): "))
                    break
                except ValueError:
                    continue

            tick_collector = TickDataCollector(exchange=exchange, futures=True)
            tick_collector.collect(symbol, duration)
            logger.info("Tick data collected")

        elif pull_type == "TICKER":

            # Select from time
            while True:
                from_time = input("Data from (yyyy-mm-dd or Press Enter): ")
                if from_time == "":
                    from_time = 0
                    break

                try:
                    from_time = int(datetime.strptime(from_time, "%Y-%m-%d").timestamp() * 1000)
                    break
                except ValueError:
                    continue

            if symbol == 'ALL':
                data_collector.sync_all(from_time)
            else:
                data_collector.collect_all(symbol, from_time)
            logger.info("Data collected")

        elif pull_type == "ACCOUNT":
            details = data_collector.client.symbol_details
            for name, detail in details.items():
                detail = OandaSymbolDetails(**detail)
                # TODO - save to db
                print(detail)


    # Strategy
    elif mode in ['backtest', "optimise"]:

        available_strategies = ["obv", "ichimoku", "sup_res", "sma", "psar", "atr", "gpsar", "orderflow"]

        # Select strategy
        while True:
            strategy = input(f"Choose a strategy ({', '.join(available_strategies)}): ")
            if strategy in available_strategies:
                break

        # Select timeframe
        while True:
            tf = input(f"Choose a timeframe ({', '.join((TF_EQUIV.keys()))}): ").lower()
            if tf in TF_EQUIV.keys():
                break

        # Select from time
        while True:
            from_time = input("Backtest from (yyyy-mm-dd or Press Enter): ")
            if from_time == "":
                from_time = 0
                break

            try:
                from_time = int(datetime.strptime(from_time, "%Y-%m-%d").timestamp() * 1000)
                break
            except ValueError:
                continue

        # Select to time
        while True:
            to_time = input("Backtest to (yyyy-mm-dd or Press Enter): ")
            if to_time == "":
                to_time = int(datetime.now(tz=timezone.utc).timestamp() * 1000)
                break

            try:
                to_time = int(datetime.strptime(to_time, "%Y-%m-%d").timestamp() * 1000)
                break
            except ValueError:
                continue

        if mode == "backtest":
            print(backtester.run(exchange, symbol, strategy, tf, from_time, to_time))
        elif mode == "optimise":

            while True:
                is_save = input("Save results (t or f): ")
                if is_save == "t":
                    is_save = True
                    break
                elif is_save == "f" or is_save == "":
                    is_save = False
                    break
                else:
                    continue

            results = list()
            db = Hdf5Client(exchange="optimised_parameters")

            pop_size = 0
            generations = 0

            # Population Size
            while True:
                try:
                    pop_size = int(input("Choose a population size: "))
                    break
                except ValueError:
                    continue

            # Iterations
            while True:
                try:
                    generations = int(input("Choose the number of generations: "))
                    break
                except ValueError:
                    continue

            if symbol == 'ALL':
                symbols = client.symbols

                for i, symbol in enumerate(symbols):
                    print(f"symbol: {symbol}")
                    nsga2 = optimiser.Nsga2(exchange, symbol, strategy, tf, from_time, to_time, pop_size)

                    p_population = nsga2.create_initial_population()
                    p_population = nsga2.evaluate_population(p_population)
                    p_population = nsga2.crowding_distance(p_population)

                    g = 0
                    while g < generations:

                        q_population = nsga2.create_offspring_population(p_population)
                        q_population = nsga2.evaluate_population(q_population)

                        r_population = p_population + q_population

                        nsga2.population_params.clear()

                        i = 0
                        population = dict()
                        for bt in r_population:
                            bt.reset_results()
                            nsga2.population_params.append(bt.parameters)
                            population[i] = bt
                            i += 1

                        fronts = nsga2.non_dominated_sorting(population)
                        for j in range(len(fronts)):
                            fronts[j] = nsga2.crowding_distance(fronts[j])

                        p_population = nsga2.create_new_population(fronts)

                        for i, individual in enumerate(list(p_population)):
                            individual.order = i
                            results.append(individual)
                            print(f"{individual}")

                        print(f"\rgenerations: {int((g + 1) / generations * 100)}%", end='')

                        g += 1

                    print("\n")
                    print(f"\rsymbols: {int(i + 1) / len(symbols) * 100}%",end='')

                print(
                    f"\nexchange: {exchange} | symbol: {symbol} | strategy: {strategy} | timeframe {tf}\n{pd.to_datetime(from_time, unit='ms')} -> {pd.to_datetime(to_time, unit='ms')}\n")

            else:
                nsga2 = optimiser.Nsga2(exchange, symbol, strategy, tf, from_time, to_time, pop_size)

                p_population = nsga2.create_initial_population()
                p_population = nsga2.evaluate_population(p_population)
                p_population = nsga2.crowding_distance(p_population)

                g = 0
                while g < generations:

                    q_population = nsga2.create_offspring_population(p_population)
                    q_population = nsga2.evaluate_population(q_population)

                    r_population = p_population + q_population

                    nsga2.population_params.clear()

                    i = 0
                    population = dict()
                    for bt in r_population:
                        bt.reset_results()
                        nsga2.population_params.append(bt.parameters)
                        population[i] = bt
                        i += 1

                    fronts = nsga2.non_dominated_sorting(population)
                    for j in range(len(fronts)):
                        fronts[j] = nsga2.crowding_distance(fronts[j])

                    p_population = nsga2.create_new_population(fronts)

                    print(f"\r{int((g + 1) / generations * 100)}%", end='')

                    g += 1

                print(f"\nexchange: {exchange} | symbol: {symbol} | strategy: {strategy} | timeframe {tf}\n{pd.to_datetime(from_time, unit='ms')} -> {pd.to_datetime(to_time, unit='ms')}\n")

                for i, individual in enumerate(list(p_population)):
                    # print(f"{i + 1} {individual}")
                    individual.order = i
                    print(f"{individual}")
                    results.append(individual)

            if is_save:
                pass
                # best_results = {}

                # for result in results:
                #     if (result.symbol not in best_results) or (result.pnl > best_results[result.symbol].pnl):
                #         best_results[result.symbol] = result
                #
                # best_results = sorted(best_results.values(), key=lambda x: x.pnl, reverse=True)
                #
                # for i, individual in enumerate(best_results[:20]):
                #     print(f"{i + 1} {individual}")
                # .h5_serialise()
                # db.write_optimised_parameters(symbol=f"{symbol}_{strategy}_{tf}", results=individual.h5_serialise())
