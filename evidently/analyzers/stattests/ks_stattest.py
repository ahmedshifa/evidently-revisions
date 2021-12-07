#!/usr/bin/env python
# coding: utf-8
import pandas as pd

from scipy.stats import ks_2samp


def ks_stat_test(reference_data: pd.DataFrame, current_data: pd.DataFrame):
    return ks_2samp(reference_data, current_data)[1]
