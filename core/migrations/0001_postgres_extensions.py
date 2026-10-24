"""Установка расширений PostgreSQL."""

from __future__ import annotations

from django.contrib.postgres.operations import (
    BtreeGinExtension,
    TrigramExtension,
    UnaccentExtension,
)
from django.db import migrations


class Migration(migrations.Migration):
    """Расширения, используемые поиском по сотрудникам и справочникам."""

    initial = True

    dependencies: list[tuple[str, str]] = []

    operations = [
        # pg_trgm — поиск по неточному совпадению.
        TrigramExtension(),
        # unaccent — снятие диакритики в именах сотрудников иностранных
        # представительств: поиск «Muller» должен находить «Müller».
        UnaccentExtension(),
        # btree_gin — совместные индексы по полнотекстовому вектору и
        # обычным полям: поиск по фамилии в пределах одного подразделения.
        BtreeGinExtension(),
    ]
