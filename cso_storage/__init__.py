"""Screening tool: storage needed at a CSO to meet a spill-frequency target,
from rain gauge data, EDM spill records, sewer GIS and OS MasterMap."""
from .model import ModelParams, simulate

__all__ = ["ModelParams", "simulate"]
