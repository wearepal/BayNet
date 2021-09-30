"""Python Bayesian Network library."""
from typing import Any
import warnings

from baynet.interventions import *
from baynet.structure import DAG
from baynet.structure_generation import *


class Graph(DAG):
    """Temporary thin wrapper to preserve old API."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """Initialise Graph object with warning."""
        warnings.warn(
            DeprecationWarning(
                "baynet.Graph has been renamed baynet.DAG, "
                "Graph will be removed in a future release."
            )
        )
        super().__init__(*args, **kwargs)
