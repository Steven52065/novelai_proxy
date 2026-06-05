from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.config import AppConfig


def test_reserved_upstream_id_is_rejected():
    with pytest.raises(ValidationError, match="reserved"):
        AppConfig.model_validate(
            {
                "novelai": {
                    "upstreams": [
                        {"id": "__all__", "api_key": ""},
                    ],
                },
            }
        )
