import asyncio
from datetime import datetime
from functools import lru_cache

import numpy as np
from beanie.odm.operators.update.general import Set
from loguru import logger
from numba import jit

from app.db.models import Contest, ContestRecordPredict, User
from app.utils import exception_logger_reraise


@lru_cache
def pre_sum_of_sigma(k: int) -> float:
    """
    Series cache
    :param k:
    :return:
    """
    if k < 0:
        raise ValueError(f"{k=}, pre_sum's index less than zero!")
    return (5 / 7) ** k + pre_sum_of_sigma(k - 1) if k >= 1 else 1


@lru_cache
def adjustment_for_delta_coefficient(k: int) -> float:
    """
    This function could also be `return 1 / (1 + sum((5 / 7) ** i for i in range(k + 1)))`
    but use a `pre_sum_of_sigma` function(which is also cached) is faster.
    When k is big enough, result approximately equals to 2/9.
    :param k:
    :return:
    """
    return 1 / (1 + pre_sum_of_sigma(k))


@jit(nopython=True, fastmath=True, parallel=True)
def expected_win_rate(vector: np.ndarray, scalar: float) -> np.ndarray:
    """
    Test result had shown this function has a quite decent performance.
    TODO: write a benchmark note.
    :param vector:
    :param scalar:
    :return:
    """
    return 1 / (1 + np.power(10, (scalar - vector) / 400))


@exception_logger_reraise
async def predict_contest(
    contest_name: str,
) -> None:
    """
    Core predict function using official contest rating algorithm
    :param contest_name:
    :return:
    """
    # update_user_using_prediction is True for biweekly contests because next day's weekly contest needs the latest info
    update_user_using_prediction = contest_name.lower().startswith("bi")
    logger.info(f"start run predict_contest, {update_user_using_prediction=}")
    records = (
        await ContestRecordPredict.find(
            ContestRecordPredict.contest_name == contest_name,
            ContestRecordPredict.score != 0,
        )
        .sort(ContestRecordPredict.rank)
        .to_list()
    )

    rank_array = np.array([record.rank for record in records])
    rating_array = np.array([record.old_rating for record in records])
    k_array = np.array([record.attendedContestsCount for record in records])

    expected_rating_list = list()
    coefficient_of_delta_list = list()

    logger.info("start loop for calculating expected_rating")
    for i in range(len(rank_array)):
        # no need to filter itself, add all then minus 0.5 is the same.
        # + 1 - 0.5 = + 0.5 works because including i=j is more convenient, can reuse expected_win_rate function below.
        expected_rank = np.sum(expected_win_rate(rating_array, rating_array[i])) + 0.5
        mean_rank = np.sqrt(expected_rank * rank_array[i])
        # Use binary search to find the expected rating
        lo, hi = 0, 4000  # 4000 could be big enough, max rating now is 3686.
        max_iteration = 25
        target = mean_rank - 1
        while hi - lo > 0.1 and max_iteration >= 0:
            mid = lo + (hi - lo) / 2
            approximation = np.sum(expected_win_rate(rating_array, mid))
            if approximation < target:
                hi = mid
            else:
                lo = mid
            max_iteration -= 1
        expected_rating = mid
        coefficient_of_delta = adjustment_for_delta_coefficient(k_array[i])
        expected_rating_list.append(expected_rating)
        coefficient_of_delta_list.append(coefficient_of_delta)
    logger.info("end loop for calculating expected_rating")
    expected_rating_array = np.array(expected_rating_list)
    coefficient_of_delta_array = np.array(coefficient_of_delta_list)
    delta_rating_array = coefficient_of_delta_array * (
        expected_rating_array - rating_array
    )
    new_rating_array = rating_array + delta_rating_array

    # update ContestRecordPredict collection
    predict_time = datetime.utcnow()
    for i, record in enumerate(records):
        record.delta_rating = delta_rating_array[i]
        record.new_rating = new_rating_array[i]
        record.predict_time = predict_time
    tasks = (record.save() for record in records)
    await asyncio.gather(*tasks)
    logger.success("predict_contest finished updating ContestRecordPredict")
    if update_user_using_prediction:
        logger.info("immediately write predicted result back into User collection")
        tasks = (
            User.find_one(
                User.username == record.username,
                User.data_region == record.data_region,
            ).update(
                Set(
                    {
                        User.rating: record.new_rating,
                        User.attendedContestsCount: record.attendedContestsCount + 1,
                        User.update_time: datetime.utcnow(),
                    }
                )
            )
            for record in records
        )
        await asyncio.gather(*tasks)
        logger.success("predict_contest finished updating User using predicted result")
    await Contest.find_one(Contest.titleSlug == contest_name).update(
        Set(
            {
                Contest.predict_time: datetime.utcnow(),
            }
        )
    )
    logger.info("finished updating predict_time in Contest database")
