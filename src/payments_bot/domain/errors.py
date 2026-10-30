"""Domain errors. Every rule violation raises a subclass of `DomainError`."""


class DomainError(Exception):
    """Base class for ledger rule violations."""


class InvalidAmountError(DomainError):
    """An amount is not canonical, is negative where it cannot be, or exceeds the maximum."""


class InvalidCurrencyError(DomainError):
    """A currency is not an ISO 4217 code (three uppercase letters)."""


class InvalidEntryError(DomainError):
    """An entry breaks a structural invariant of its kind."""


class MissingReasonError(DomainError):
    """An adjustment was requested without a non-blank reason."""


class NotAPaymentError(DomainError):
    """Adjustments may only target payments, never other adjustments."""


class CurrencyMismatchError(DomainError):
    """An adjustment must use the currency of the payment it corrects."""


class NoChangeError(DomainError):
    """A correction would not change the effective amount."""


class AlreadyVoidedError(DomainError):
    """The payment's effective amount is already zero."""
