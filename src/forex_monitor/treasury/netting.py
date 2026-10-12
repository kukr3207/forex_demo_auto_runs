"""Bilateral payment netting and settlement instruction generation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Dict, Iterable, Mapping, Sequence, Tuple

from forex_monitor.errors import ValidationError
from forex_monitor.models import as_decimal
from forex_monitor.treasury.cash import _currency


@dataclass(frozen=True)
class PaymentObligation:
    obligation_id: str
    payer: str
    receiver: str
    currency: str
    amount: Decimal
    value_date: date
    netting_set: str

    def __post_init__(self) -> None:
        if not all((self.obligation_id.strip(), self.payer.strip(), self.receiver.strip(), self.netting_set.strip())):
            raise ValidationError("payment obligation fields cannot be blank")
        if self.payer == self.receiver:
            raise ValidationError("payment payer and receiver must differ")
        object.__setattr__(self, "currency", _currency(self.currency))
        object.__setattr__(self, "amount", as_decimal(self.amount, "payment amount", positive=True))


@dataclass(frozen=True)
class NettingInstruction:
    netting_set: str
    payer: str
    receiver: str
    currency: str
    amount: Decimal
    value_date: date
    obligation_ids: Tuple[str, ...]


@dataclass(frozen=True)
class NettingResult:
    gross_amount: Decimal
    net_amount: Decimal
    reduction: Decimal
    instructions: Tuple[NettingInstruction, ...]

    @property
    def efficiency(self) -> Decimal:
        if self.gross_amount == 0:
            return Decimal("0")
        return self.reduction / self.gross_amount


class NettingEngine:
    def net(self, obligations: Sequence[PaymentObligation]) -> NettingResult:
        if len({item.obligation_id for item in obligations}) != len(obligations):
            raise ValidationError("payment obligations contain repeated ids")
        groups: Dict[Tuple[str, str, str, date], list[PaymentObligation]] = {}
        for item in obligations:
            parties = tuple(sorted((item.payer, item.receiver)))
            key = (item.netting_set, parties[0] + "\x00" + parties[1], item.currency, item.value_date)
            groups.setdefault(key, []).append(item)
        instructions = []
        gross = sum((item.amount for item in obligations), Decimal("0"))
        for (netting_set, joined, currency, value_date), values in groups.items():
            first_party, second_party = joined.split("\x00", 1)
            net = Decimal("0")
            for value in values:
                net += value.amount if value.payer == first_party else -value.amount
            if net == 0:
                continue
            payer = first_party if net > 0 else second_party
            receiver = second_party if net > 0 else first_party
            instructions.append(NettingInstruction(
                netting_set,
                payer,
                receiver,
                currency,
                abs(net),
                value_date,
                tuple(sorted(item.obligation_id for item in values)),
            ))
        instructions.sort(key=lambda item: (item.value_date, item.currency, item.payer, item.receiver))
        net_amount = sum((item.amount for item in instructions), Decimal("0"))
        return NettingResult(gross, net_amount, gross - net_amount, tuple(instructions))

    def multilateral(self, obligations: Sequence[PaymentObligation]) -> NettingResult:
        if len({item.netting_set for item in obligations}) > 1:
            raise ValidationError("multilateral netting requires one netting set")
        grouped: Dict[Tuple[str, date], list[PaymentObligation]] = {}
        for item in obligations:
            grouped.setdefault((item.currency, item.value_date), []).append(item)
        instructions = []
        gross = sum((item.amount for item in obligations), Decimal("0"))
        net_amount = Decimal("0")
        for (currency, value_date), values in grouped.items():
            balances: Dict[str, Decimal] = {}
            for value in values:
                balances[value.payer] = balances.get(value.payer, Decimal("0")) - value.amount
                balances[value.receiver] = balances.get(value.receiver, Decimal("0")) + value.amount
            debtors = [[party, -amount] for party, amount in balances.items() if amount < 0]
            creditors = [[party, amount] for party, amount in balances.items() if amount > 0]
            debtors.sort()
            creditors.sort()
            debit_index = 0
            credit_index = 0
            while debit_index < len(debtors) and credit_index < len(creditors):
                payer, due = debtors[debit_index]
                receiver, receivable = creditors[credit_index]
                amount = min(due, receivable)
                instructions.append(NettingInstruction(
                    values[0].netting_set,
                    str(payer),
                    str(receiver),
                    currency,
                    amount,
                    value_date,
                    tuple(sorted(item.obligation_id for item in values)),
                ))
                net_amount += amount
                debtors[debit_index][1] = due - amount
                creditors[credit_index][1] = receivable - amount
                if debtors[debit_index][1] == 0:
                    debit_index += 1
                if creditors[credit_index][1] == 0:
                    credit_index += 1
        return NettingResult(gross, net_amount, gross - net_amount, tuple(instructions))
