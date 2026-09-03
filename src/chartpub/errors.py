"""Operator-facing failures and their stable process exit codes."""

from __future__ import annotations


class ChartpubError(Exception):
    """Base class for concise operator-facing failures."""

    exit_code = 1


class UsageError(ChartpubError):
    """The command was invoked in a way that cannot be satisfied."""

    exit_code = 2


class ContractError(UsageError):
    """The publication contract is invalid."""

    exit_code = 2


class ValidationError(ChartpubError):
    """A candidate failed validation and must not become discoverable."""

    exit_code = 3


class PublicationError(ChartpubError):
    """A publication transition could not be completed safely."""

    exit_code = 5


class RemoteConflict(PublicationError):
    """Remote state changed after it was inspected."""

    exit_code = 4


class RollbackError(PublicationError):
    """A partial attempt could not be rolled back and needs an operator."""

    exit_code = 6


EXIT_OK = 0
EXIT_INTERNAL = 1
EXIT_USAGE = 2
EXIT_VALIDATION = 3
EXIT_CONFLICT = 4
EXIT_PUBLICATION = 5
EXIT_ROLLBACK = 6
