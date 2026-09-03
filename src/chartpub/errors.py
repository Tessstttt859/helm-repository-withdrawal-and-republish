class ChartpubError(Exception):
    """Base class for concise operator-facing failures."""


class ContractError(ChartpubError):
    """The publication contract is invalid."""


class PublicationError(ChartpubError):
    """A publication transition could not be completed safely."""


class RemoteConflict(PublicationError):
    """Remote state changed after it was inspected."""
