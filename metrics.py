from typing import List
import numpy as np
import pandas as pd


def compute_sharpe_ratio(returns: List[float], risk_free_rate: float = 0.0) -> float:

    """
    Compute the annualized Sharpe ratio of a portfolio.

    Args:
        returns (List): A list of returns for the portfolio.
        risk_free_rate (float): The risk-free rate.

    Returns:
        float: The annualized Sharpe ratio of the portfolio.
    """

    if len(returns) == 0:
        return -1

    # Calculate the mean and standard deviation of the returns
    mean_return = np.mean(returns)
    std_return = np.std(returns)

    if std_return == 0:
        return 0

    # Calculate the annualized Sharpe ratio
    sharpe_ratio = (mean_return - risk_free_rate) / std_return # * np.sqrt(len(returns))

    # if sharpe_ratio > 6:
    #     print(f"returns: {returns} \n mean_return: {mean_return} | std_return: {std_return} | sharpe_ratio: {sharpe_ratio}")

    return sharpe_ratio


def compute_cagr_from_returns(returns: List[float], from_time: pd.Timestamp, to_time: pd.Timestamp, initial_value: float = 1.0) -> float:

    years = (to_time - from_time).days / 365.25
    if years <= 0:
        return 0

    # final_value = np.nanprod([1 + r for r in returns])
    final_value = initial_value
    for r in returns:
        if not np.isnan(r):
            final_value *= (1 + r / 100)
    if final_value <= 0:
        return -1

    cagr = (final_value / initial_value) ** (1 / years) - 1

    # print(f"returns: {returns}")
    # print(f"years: {years} | initial_value:  {round(initial_value, 2)} | final_value:  {round(final_value, 2)}")

    return cagr

