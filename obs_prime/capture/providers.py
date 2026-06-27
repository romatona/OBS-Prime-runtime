from __future__ import annotations

from abc import ABC, abstractmethod

from ..models import CaptureFrame


class CaptureProvider(ABC):
    @abstractmethod
    def capture(self) -> CaptureFrame:
        raise NotImplementedError
