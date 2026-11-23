"""The only file of the indicators module that imports quantitative or data libraries (SPEC-019, rule 3).

Every other file reaches those libraries through this one. A scan test fails if any other file imports them.
"""

import numpy as np  # noqa: E402,F401
from financetoolkit.technicals import breadth_model as ta_breadth  # noqa: E402,F401
from financetoolkit.technicals import momentum_model as ta_momentum  # noqa: E402,F401
from financetoolkit.technicals import overlap_model as ta_overlap  # noqa: E402,F401
from financetoolkit.technicals import volatility_model as ta_volatility  # noqa: E402,F401
from scipy import linalg as sp_linalg  # noqa: E402,F401
from scipy import signal as sp_signal  # noqa: E402,F401
