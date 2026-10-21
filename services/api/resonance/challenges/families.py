"""Circuit families for hidden-fault cases (public).

The family of a case is public: it is the template of the frozen intended revision, shown in
the schematic. A learner may choose it (``CaseCreateRequest.template_id``); otherwise
``draw_family`` draws it with the published ``policy.CASE_FAMILY_PRIOR`` from its own
``secrets`` draw, independent of the (private) case seed, so the family carries no information
about the hidden condition and a published seed reproduces a case together with its public
family.
"""

from __future__ import annotations

import secrets
from typing import cast

from ..contracts.circuits import TemplateId
from ..contracts.policy import CASE_FAMILY_PRIOR

CASE_FAMILIES: tuple[TemplateId, ...] = cast(tuple[TemplateId, ...], tuple(CASE_FAMILY_PRIOR))

_SYSTEM_RANDOM = secrets.SystemRandom()


def draw_family() -> TemplateId:
    """A circuit family drawn with ``policy.CASE_FAMILY_PRIOR`` (OS randomness)."""
    weights = [CASE_FAMILY_PRIOR[f] for f in CASE_FAMILIES]
    return _SYSTEM_RANDOM.choices(CASE_FAMILIES, weights=weights, k=1)[0]
