from typing import List, Dict
import random
from copy import copy
import pandas as pd

from utils import STRAT_PARAMS, resample_timeframe, get_library
from database import Hdf5Client
from models import BacktestResult
from strategies import obv, ichimoku, support_resistance


class Nsga2:

    def __init__(self, exchange: str, symbol: str, strategy: str, tf: str, from_time: int, to_time: int,
                 population_size: int):
        self.exchange = exchange
        self.symbol = symbol
        self.strategy = strategy
        self.tf = tf
        self.from_time = from_time
        self.to_time = to_time
        self.population_size = population_size

        self.params_data = STRAT_PARAMS[strategy]
        self.population_params = []

        if self.strategy in ["obv", "ichimoku", "sup_res"]:
            h5_db = Hdf5Client(exchange)
            self.data = h5_db.get_data(symbol, from_time, to_time)
            self.data = resample_timeframe(self.data, tf)

        elif self.strategy in ["sma", "psar", "atr", "gpsar"]:
            self.lib = get_library()
            if self.strategy == "sma":
                self.obj = self.lib.Sma_new(exchange.encode(), symbol.encode(), tf.encode(), from_time, to_time)
            elif self.strategy == "psar":
                self.obj = self.lib.Psar_new(exchange.encode(), symbol.encode(), tf.encode(), from_time, to_time)
            elif self.strategy == "atr":
                self.obj = self.lib.Atr_new(exchange.encode(), symbol.encode(), tf.encode(), from_time, to_time)
            elif self.strategy == "gpsar":
                self.obj = self.lib.GradientPsar_new(exchange.encode(), symbol.encode(), tf.encode(), from_time, to_time)

    def _set_population_info(self, population: List[BacktestResult]):
        for individual in population:
            individual.symbol = self.symbol
            individual.strategy = self.strategy
            individual.tf = self.tf
            individual.from_time = pd.to_datetime(self.from_time, unit='ms')
            individual.to_time = pd.to_datetime(self.to_time, unit='ms')
        return population

    def create_initial_population(self) -> List[BacktestResult]:

        population = []

        while len(population) < self.population_size:
            backtest = BacktestResult()
            for p_code, p in self.params_data.items():
                if p["type"] == int:
                    backtest.parameters[p_code] = random.randint(p["min"], p["max"])
                elif p["type"] == float:
                    backtest.parameters[p_code] = round(random.uniform(p["min"], p["max"]), p["decimals"])

            if backtest not in population:
                population.append(backtest)
                self.population_params.append(backtest.parameters)

        population = self._set_population_info(population)

        return population

    def create_new_population(self, fronts: List[List[BacktestResult]]) -> List[BacktestResult]:

        new_pop = []

        for front in fronts:
            if len(new_pop) + len(front) > self.population_size:
                max_individuals = self.population_size - len(new_pop)
                if max_individuals > 0:
                    new_pop += sorted(front, key=lambda x: getattr(x, "crowding_distance"))[-max_individuals:]
            else:
                new_pop += front

        new_pop = self._set_population_info(new_pop)

        return new_pop

    def create_offspring_population(self, population: List[BacktestResult]) -> List[BacktestResult]:

        offspring_pop = []

        while len(offspring_pop) != self.population_size:

            parents: List[BacktestResult] = []

            for i in range(2):
                random_parents = random.sample(population, k=2)
                if random_parents[0].rank != random_parents[1].rank:
                    best_parent = min(random_parents, key=lambda x: getattr(x, "rank"))
                else:
                    best_parent = max(random_parents, key=lambda x: getattr(x, "crowding_distance"))

                parents.append(best_parent)

            new_child = BacktestResult()
            new_child.parameters = copy(parents[0].parameters)

            # Crossover

            number_of_crossovers = random.randint(1, len(self.params_data))
            params_to_cross = random.sample(list(self.params_data.keys()), k=number_of_crossovers)

            for p in params_to_cross:
                new_child.parameters[p] = copy(parents[1].parameters[p])

            # Mutation

            number_of_mutations = random.randint(0, len(self.params_data))
            params_to_change = random.sample(list(self.params_data.keys()), k=number_of_mutations)

            for p in params_to_change:
                mutations_strength = random.uniform(-2, 2)
                new_child.parameters[p] = self.params_data[p]["type"](new_child.parameters[p] * (1 + mutations_strength))
                new_child.parameters[p] = max(new_child.parameters[p], self.params_data[p]["min"])
                new_child.parameters[p] = min(new_child.parameters[p], self.params_data[p]["max"])

                if self.params_data[p]["type"] == float:
                    new_child.parameters[p] = round(new_child.parameters[p], self.params_data[p]["decimals"])

            new_child.parameters = self._params_constraints(new_child.parameters)

            if new_child.parameters not in self.population_params:
                offspring_pop.append(new_child)
                self.population_params.append(new_child.parameters)

        offspring_pop = self._set_population_info(offspring_pop)

        return offspring_pop

    def _params_constraints(self, params: Dict) -> Dict:
        if self.strategy == "obv":
            pass

        elif self.strategy == "sup_res":
            pass

        elif self.strategy == "ichimoku":
            params["kijun_period"] = max(params["kijun_period"], params["tenkan_period"])

        elif self.strategy == "sma":
            params["slow_ma"] = max(params["slow_ma"], params["fast_ma"])

        elif self.strategy == "psar":
            params["initial_acc"] = min(params["initial_acc"], params["max_acc"])
            params["acc_increment"] = min(params["acc_increment"], params["max_acc"] - params["initial_acc"])

        elif self.strategy == "atr":
            pass

        elif self.strategy == "gpsar":
            params["initial_acc"] = min(params["initial_acc"], params["max_acc"])
            params["acc_increment"] = min(params["acc_increment"], params["max_acc"] - params["initial_acc"])

        return params

    def crowding_distance(self, population: List[BacktestResult]) -> List[BacktestResult]:

        # TODO add num_trades
        for objective in ["pnl", "max_dd", "sharpe_ratio"]:

            population = sorted(population, key=lambda x: getattr(x, objective))
            min_value = getattr(min(population, key=lambda x: getattr(x, objective)), objective)
            max_value = getattr(max(population, key=lambda x: getattr(x, objective)), objective)

            population[0].crowding_distance = float("inf")
            population[-1].crowding_distance = float("inf")

            for i in range(1, len(population) - 1):
                distance = getattr(population[i + 1], objective) - getattr(population[i - 1], objective)
                diff = max_value - min_value
                if diff != 0:
                    distance = distance / (diff)
                population[i].crowding_distance += distance

        return population

    def non_dominated_sorting(self, population: Dict[int, BacktestResult]) -> List[List[BacktestResult]]:

        fronts = []

        # TODO add num_trades
        for id_1, indiv_1 in population.items():
            for id_2, indiv_2 in population.items():
                # WIP - Original Code
                # if (
                #         indiv_1.pnl >= indiv_2.pnl
                #         and indiv_1.max_dd <= indiv_2.max_dd
                #         and ( indiv_1.pnl > indiv_2.pnl or indiv_1.max_dd < indiv_2.max_dd )
                # ):
                #     indiv_1.dominates.append(id_2)
                # elif (
                #         indiv_2.pnl >= indiv_1.pnl
                #         and indiv_2.max_dd <= indiv_1.max_dd
                #         and ( indiv_2.pnl > indiv_1.pnl or indiv_2.max_dd < indiv_1.max_dd )
                # ):
                #     indiv_1.dominated_by += 1

                if (
                    indiv_1.cagr >= indiv_2.cagr
                    and indiv_1.sharpe_ratio >= indiv_2.sharpe_ratio
                    and ( indiv_1.cagr > indiv_2.cagr or indiv_1.sharpe_ratio > indiv_2.sharpe_ratio)
                ):
                    indiv_1.dominates.append(id_2)
                elif (
                        indiv_2.cagr >= indiv_1.cagr
                        and indiv_2.sharpe_ratio >= indiv_1.sharpe_ratio
                        and (indiv_2.cagr > indiv_1.cagr or indiv_2.sharpe_ratio > indiv_1.sharpe_ratio)
                ):
                    indiv_1.dominated_by += 1

            if indiv_1.dominated_by == 0:
                if len(fronts) == 0:
                    fronts.append([])
                fronts[0].append(indiv_1)
                indiv_1.rank = 0

        i = 0

        while True:
            fronts.append([])

            for indiv_1 in fronts[i]:
                for indiv_2_id in indiv_1.dominates:
                    population[indiv_2_id].dominated_by -= 1
                    if population[indiv_2_id].dominated_by == 0:
                        fronts[i + 1].append(population[indiv_2_id])
                        population[indiv_2_id].rank = i + 1

            if len(fronts[i + 1]) > 0:
                i += 1
            else:
                del fronts[-1]
                break

        return fronts

    def evaluate_population(self, population: List[BacktestResult]) -> List[BacktestResult]:

        if self.strategy == "obv":
            for bt in population:
                bt.pnl, bt.max_dd, bt.num_trades, bt.sharpe_ratio, bt.cagr = obv.backtest(self.data, ma_period=bt.parameters["ma_period"])

                if bt.pnl == 0:
                    bt.pnl = -float("inf")
                    bt.max_dd = float("inf")
                    bt.num_trades = 0
                    bt.sharpe_ratio = -float("inf")
                    bt.cagr = -float("inf")

            return population

        elif self.strategy == "ichimoku":
            for bt in population:
                bt.pnl, bt.max_dd, bt.num_trades, bt.sharpe_ratio, bt.cagr = ichimoku.backtest(self.data, tenkan_period=bt.parameters["tenkan_period"],
                                                      kijun_period=bt.parameters["kijun_period"])

                if bt.pnl == 0:
                    bt.pnl = -float("inf")
                    bt.max_dd = float("inf")
                    bt.num_trades = 0
                    bt.sharpe_ratio = -float("inf")
                    bt.cagr = -float("inf")

            return population

        elif self.strategy == "sup_res":

            for bt in population:
                bt.pnl, bt.max_dd, bt.num_trades, bt.sharpe_ratio, bt.cagr = support_resistance.backtest(self.data, min_points=bt.parameters["min_points"],
                                                                min_diff_points=bt.parameters["min_diff_points"],
                                                                rounding_nb=bt.parameters["rounding_nb"],
                                                                take_profit=bt.parameters["take_profit"],
                                                                stop_loss=bt.parameters["stop_loss"])

                if bt.pnl == 0:
                    bt.pnl = -float("inf")
                    bt.max_dd = float("inf")
                    bt.num_trades = 0
                    bt.sharpe_ratio = -float("inf")
                    bt.cagr = -float("inf")

            return population

        elif self.strategy == "sma":

            for bt in population:
                self.lib.Sma_execute_backtest(self.obj, bt.parameters["slow_ma"], bt.parameters["fast_ma"])
                bt.pnl = self.lib.Sma_get_pnl(self.obj)
                bt.max_dd = self.lib.Sma_get_max_dd(self.obj)
                bt.num_trades = self.lib.Sma_get_num_trades(self.obj)
                bt.sharpe_ratio = self.lib.Sma_get_sharpe_ratio(self.obj)
                bt.cagr = self.lib.Sma_get_cagr(self.obj)

                if bt.pnl == 0:
                    bt.pnl = -float("inf")
                    bt.max_dd = float("inf")
                    bt.num_trades = 0
                    bt.sharpe_ratio = -float("inf")
                    bt.cagr = -float("inf")

            return population

        elif self.strategy == "psar":

            for bt in population:
                self.lib.Psar_execute_backtest(self.obj, bt.parameters["initial_acc"], bt.parameters["acc_increment"],
                                               bt.parameters["max_acc"])
                bt.pnl = self.lib.Psar_get_pnl(self.obj)
                bt.max_dd = self.lib.Psar_get_max_dd(self.obj)
                bt.num_trades = self.lib.Psar_get_num_trades(self.obj)
                bt.sharpe_ratio = self.lib.Psar_get_sharpe_ratio(self.obj)
                bt.cagr = self.lib.Psar_get_cagr(self.obj)

                if bt.pnl == 0:
                    bt.pnl = -float("inf")
                    bt.max_dd = float("inf")
                    bt.num_trades = 0
                    bt.sharpe_ratio = -float("inf")
                    bt.cagr = -float("inf")

            return population

        elif self.strategy == "atr":
            for bt in population:
                self.lib.Atr_execute_backtest(self.obj, bt.parameters["period"], bt.parameters["atr_multiplier"])
                bt.pnl = self.lib.Atr_get_pnl(self.obj)
                bt.max_dd = self.lib.Atr_get_max_dd(self.obj)
                bt.num_trades = self.lib.Atr_get_num_trades(self.obj)
                bt.sharpe_ratio = self.lib.Atr_get_sharpe_ratio(self.obj)
                bt.cagr = self.lib.Atr_get_cagr(self.obj)

                if bt.pnl == 0:
                    bt.pnl = -float("inf")
                    bt.max_dd = float("inf")
                    bt.num_trades = 0
                    bt.sharpe_ratio = -float("inf")
                    bt.cagr = -float("inf")

            return population

        elif self.strategy == "gpsar":

            for bt in population:
                self.lib.GradientPsar_execute_backtest(self.obj, bt.parameters["initial_acc"], bt.parameters["acc_increment"],
                                               bt.parameters["max_acc"], bt.parameters["gradient_threshold"], bt.parameters["gradient_period"])
                bt.pnl = self.lib.GradientPsar_get_pnl(self.obj)
                bt.max_dd = self.lib.GradientPsar_get_max_dd(self.obj)
                bt.num_trades = self.lib.GradientPsar_get_num_trades(self.obj)
                bt.sharpe_ratio = self.lib.GradientPsar_get_sharpe_ratio(self.obj)
                bt.cagr = self.lib.GradientPsar_get_cagr(self.obj)

                if bt.pnl == 0:
                    bt.pnl = -float("inf")
                    bt.max_dd = float("inf")
                    bt.num_trades = 0
                    bt.sharpe_ratio = -float("inf")
                    bt.cagr = -float("inf")

            return population
