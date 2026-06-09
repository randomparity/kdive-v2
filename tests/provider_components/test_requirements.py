from __future__ import annotations

import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.provider_components.requirements import (
    CmdlineRequirements,
    ConfigRequirements,
    validate_cmdline_requirements,
    validate_config_requirements,
)


def test_config_requirements_accept_matching_values() -> None:
    validate_config_requirements(
        "CONFIG_VIRTIO_BLK=y\nCONFIG_DEBUG_INFO=y\n",
        ConfigRequirements(required={"CONFIG_VIRTIO_BLK": "y"}),
    )


def test_config_requirements_reject_missing_value() -> None:
    with pytest.raises(CategorizedError) as caught:
        validate_config_requirements("", ConfigRequirements(required={"CONFIG_VIRTIO_BLK": "y"}))

    assert caught.value.category is ErrorCategory.CONFIGURATION_ERROR


def test_cmdline_requirements_accept_required_tokens() -> None:
    validate_cmdline_requirements(
        "console=ttyS0 root=/dev/vda dhash_entries=1",
        CmdlineRequirements(required_tokens=["console=ttyS0", "root=/dev/vda"]),
        platform_cmdline="console=ttyS0 root=/dev/vda",
    )


def test_cmdline_requirements_rejects_protected_override() -> None:
    with pytest.raises(CategorizedError) as caught:
        validate_cmdline_requirements(
            "console=tty0 root=/dev/vda",
            CmdlineRequirements(required_tokens=["root=/dev/vda"], protected_prefixes=["console="]),
            platform_cmdline="console=ttyS0 root=/dev/vda",
        )

    assert caught.value.category is ErrorCategory.CONFIGURATION_ERROR
