"""The offline delay model (training, evaluation, prediction).

The only package allowed to import numpy, lightgbm, catboost, xgboost or
optuna -- they live in the optional `ml` extra, and nothing else in dbrel
may depend on them.
"""
