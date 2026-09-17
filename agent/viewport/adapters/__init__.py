"""Source adapters. Add new vendors here."""

from __future__ import annotations

from ..config import SourceConfig
from .base import SourceAdapter
from .onvif import OnvifAdapter
from .reolink import ReolinkAdapter
from .rtsp import RtspAdapter

ADAPTERS = {"reolink": ReolinkAdapter, "onvif": OnvifAdapter, "rtsp": RtspAdapter}


def build_adapter(config: SourceConfig) -> SourceAdapter:
    try:
        return ADAPTERS[config.type](config)
    except KeyError:
        raise ValueError(f"unsupported source type: {config.type}; "
                         f"choose from {', '.join(ADAPTERS)}") from None


__all__ = ["ADAPTERS", "SourceAdapter", "OnvifAdapter", "ReolinkAdapter", "RtspAdapter",
           "build_adapter"]
